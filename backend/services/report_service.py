"""
每日 AI 分析报告服务
====================
从「成表」的 Excel（output/YYYYMMDD.xlsx）重建 TradeRecord 列表，
调用 src.parser.daily_report.analyze_daily 生成每日复盘报告。

支持：
- 列出所有有数据的日期 + 报告生成状态（历史）
- 异步批量生成报告（补跑历史日期）
- 读取某日期的报告内容
"""

from __future__ import annotations

import glob
import logging
import os
import re
import sys
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from src.parser.direction_classifier import is_other_direction  # noqa: E402
from src.parser.models import TradeRecord  # noqa: E402

logger = logging.getLogger("backend.report_service")

OUTPUT_DIR = os.path.join(BASE_DIR, "output")

# ---- 线程安全状态（持久化到磁盘） ----
# 用 RLock：_run() 在 with _lock 块内会调用 _mark_date_status()，
# 后者内部再次加锁；普通 Lock 不可重入，会在此死锁导致 /api/reports 永久阻塞。
_lock = threading.RLock()
_STATE_FILE = os.path.join(OUTPUT_DIR, "_report_state.json")


def _load_state() -> dict:
    """从磁盘读取持久化的状态，启动后立即恢复。"""
    if os.path.exists(_STATE_FILE):
        try:
            import json as _json
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                return _json.load(f)
        except Exception:
            pass
    return {
        "status": "idle",
        "message": "",
        "total": 0,
        "done": 0,
        "current_date": None,
        "failed_dates": [],
        "started_at": None,
        "finished_at": None,
        "date_status": {},  # per-date 实时状态：{ "20260924": "generating" / "success" / "failed" }
    }


_state = _load_state()


def _save_state() -> None:
    """持久化状态。"""
    try:
        import json as _json
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            _json.dump(_state, f, ensure_ascii=False)
    except Exception as e:
        logger.warning("保存报告状态失败: %s", e)


def _mark_date_status(date_str: str, status: str) -> None:
    """更新单日期状态。status: generating / success / failed"""
    with _lock:
        _state["date_status"][date_str] = status
        _save_state()


def _date_status_for(date_str: str) -> str:
    """查询某日期的状态：generating / success / failed / not_started"""
    with _lock:
        s = _state["date_status"].get(date_str)
        if s:
            return s
    if os.path.exists(_report_path(date_str)):
        return "success"
    return "not_started"


# ============================================================
#  从 Excel 重建 TradeRecord
# ============================================================

# 与 excel_exporter.EXCEL_COLUMNS 的表头顺序对齐
_EXCEL_HEADERS = [
    "大V昵称",
    "收益率",
    "发布时间",
    "动态正文",
    "操作类型",
    "操作状态",
    "基金名称",
    "买入金额（元）",
    "卖出份额（份）",
    "采集时间",
    "备注",
]


def _clean_amount(v) -> Optional[str]:
    """清洗金额：去掉千分位逗号，把 '--'/'-'/'None' 等占位符归为 None。"""
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "--", "-", "None", "nan", "N/A"):
        return None
    return s.replace(",", "")


def load_records_from_excel(date_str: str) -> List[TradeRecord]:
    """从 output/YYYYMMDD.xlsx 重建 TradeRecord 列表（成表数据）。

    每条记录附带 final direction（来源：fund_direction_master）：
    - DB 命中 manual/verified=True → direction = DB.direction, source=manual, verified=True
    - DB 命中但 verified=False    → direction = DB.direction, source=db, verified=False
    - DB 未命中                  → direction = "待确认", source=unmapped, verified=False

    真实交易不会因为未命中而被过滤；所有原始记录完整保留。
    """
    import openpyxl

    cleaned = date_str.replace("-", "")
    excel_path = os.path.join(OUTPUT_DIR, f"{cleaned}.xlsx")
    if not os.path.exists(excel_path):
        logger.warning("Excel 不存在，无法重建记录: %s", excel_path)
        return []

    wb = openpyxl.load_workbook(excel_path, read_only=True)
    ws = wb.active
    records: List[TradeRecord] = []
    header: Optional[list] = None

    try:
        for row in ws.iter_rows(values_only=True):
            if header is None:
                header = list(row)
                continue
            if not row or all(c is None or str(c).strip() == "" for c in row):
                continue

            data = dict(zip(header, row))
            operation_type = data.get("操作类型")
            remark = data.get("备注")
            fund_name = data.get("基金名称")

            # 转换操作：把 remark="转换" 映射到 转换前/后 基金名称，供方向汇总区分转入/转出
            convert_from = None
            convert_to = None
            if remark == "转换":
                if operation_type == "卖出":
                    convert_from = fund_name
                elif operation_type == "买入":
                    convert_to = fund_name

            records.append(
                TradeRecord(
                    kol_name=data.get("大V昵称"),
                    yield_rate=data.get("收益率"),
                    publish_time=data.get("发布时间"),
                    opinion_text=data.get("动态正文"),
                    operation_type=operation_type,
                    operation_status=data.get("操作状态"),
                    fund_name=fund_name,
                    buy_amount=_clean_amount(data.get("买入金额（元）")),
                    sell_shares=_clean_amount(data.get("卖出份额（份）")),
                    collect_time=data.get("采集时间"),
                    remark=remark,
                    convert_from_fund=convert_from,
                    convert_to_fund=convert_to,
                )
            )
    finally:
        wb.close()

    logger.info("从 Excel 重建 %d 条记录（日期 %s）", len(records), cleaned)

    # ---- 注入最终 direction（唯一 Source of Truth：fund_direction_master） ----
    inject_final_directions(records)

    return records


UNMAPPED_DIRECTION = "待确认"


def inject_final_directions(records: List[TradeRecord]) -> Dict[str, int]:
    """为每条 record 写入 final direction / source / verified / confidence。

    这是基金方向的唯一解析入口，一次性解析并固化；后续 historical_context /
    daily_report 只消费结果，禁止重新判断。

    解析优先级：
      1. fund_direction_master 精确匹配（normalized_name）
      2. 截断名称安全前缀匹配（仅当原始名带 "..." / "…"）
      3. direction_resolver.resolve_direction（rule → context → Tavily → LLM → 落库）
      4. 仍未确定 → direction="待确认"

    同一基金（normalized_name）只解析一次，结果缓存后回填所有 records，
    避免同一天 20 条交易调用 20 次 Tavily。

    Returns:
        统计字典，供调用方记录/验证。
    """
    from backend.services import fund_direction_repo as repo
    from backend.services import direction_resolver

    # 1) 收集唯一基金（按 normalized_name 去重）
    unique: Dict[str, Dict[str, Optional[str]]] = {}
    for r in records:
        fn = (r.fund_name or "").strip()
        if not fn:
            continue
        norm = repo.normalize_fund_name(fn)
        if not norm:
            continue
        if norm not in unique:
            unique[norm] = {"fund_name": fn, "opinion_text": r.opinion_text}
        elif not unique[norm]["opinion_text"] and r.opinion_text:
            unique[norm]["opinion_text"] = r.opinion_text

    # 2) 批量精确查 DB
    db_map: Dict[str, Any] = {}
    if unique:
        db_map = repo.lookup_many(list(unique.keys()))

    # 3) 逐基金解析 + 缓存
    cache: Dict[str, tuple] = {}
    stats = {
        "db_hit": 0,
        "prefix_hit": 0,
        "resolved": 0,
        "web_search": 0,
        "pending": 0,
    }

    for norm, meta in unique.items():
        fn = meta["fund_name"]
        snap = db_map.get(norm)

        # ---- 1. DB 精确匹配（有效方向才直接采用）----
        if snap and snap.direction and not is_other_direction(snap.direction):
            cache[norm] = _finalize(
                snap.direction,
                snap.classification_source or "db",
                bool(snap.verified),
                snap.confidence or ("high" if snap.verified else "low"),
            )
            stats["db_hit"] += 1
            continue

        # ---- 2. 截断名称安全前缀匹配 ----
        prefix_snap = _safe_prefix_match(repo, fn, norm)
        if prefix_snap is not None:
            cache[norm] = _finalize(
                prefix_snap.direction,
                prefix_snap.classification_source or "db",
                bool(prefix_snap.verified),
                prefix_snap.confidence or ("high" if prefix_snap.verified else "low"),
            )
            stats["prefix_hit"] += 1
            continue

        # ---- 3. resolve_direction（rule → context → Tavily → LLM → 落库）----
        result = direction_resolver.resolve_direction(
            fn, meta["opinion_text"], fund_code=None, fund_type_hint=None
        )
        stats["resolved"] += 1
        if result.source == "web_search":
            stats["web_search"] += 1

        if result.direction and not is_other_direction(result.direction):
            verified = result.confidence in ("high", "medium")
            cache[norm] = _finalize(
                result.direction, result.source, verified, result.confidence
            )
        else:
            cache[norm] = _finalize(UNMAPPED_DIRECTION, "unmapped", False, "low")
            stats["pending"] += 1

    # 4) 回填 records
    for r in records:
        fn = (r.fund_name or "").strip()
        norm = repo.normalize_fund_name(fn) if fn else ""
        if norm and norm in cache:
            direction, source, verified, confidence = cache[norm]
        else:
            direction, source, verified, confidence = (
                UNMAPPED_DIRECTION, "unmapped", False, "low"
            )
        r.direction = direction
        r.direction_source = source
        r.direction_verified = verified
        r.direction_confidence = confidence

    logger.info(
        "方向注入完成: DB命中 %d / 截断命中 %d / 自动补全 %d(其中web_search %d) / 待确认 %d",
        stats["db_hit"], stats["prefix_hit"], stats["resolved"],
        stats["web_search"], stats["pending"],
    )
    return stats


def _finalize(direction: str, source: str, verified: bool, confidence: str) -> tuple:
    """规范化方向四元组，供回填。"""
    return (direction, source, verified, confidence)


def _safe_prefix_match(repo, fn: str, norm: str):
    """截断基金名的安全前缀匹配。

    规则：
      1. 仅当原始 fund_name 明显带 "..." / "…" 时才允许 prefix matching
      2. 只有 1 个候选 → 直接命中
      3. 多个候选但 direction 完全一致 → 用共同 direction
      4. 多个候选且 direction 不一致 → 不自动匹配（返回 None）
    """
    if "..." not in fn and "…" not in fn:
        return None
    candidates = repo.lookup_by_prefix(norm)
    if not candidates:
        return None
    valid = [c for c in candidates if c.direction and not is_other_direction(c.direction)]
    if not valid:
        return None
    if len(valid) == 1:
        return valid[0]
    dirs = {c.direction for c in valid}
    if len(dirs) == 1:
        return valid[0]
    return None


def _load_crawl_status(date_str: str) -> dict:
    """从 output/raw_pages_YYYYMMDD_*.json 读取最新一次的爬虫元数据，判断数据完整性。

    数据完整性依据爬虫元数据（stop_type / bottom_marker_detected / expand_remaining），
    而不是凭"某个历史大V今天没出现"去推测。
    """
    import json

    cleaned = date_str.replace("-", "")
    pattern = os.path.join(OUTPUT_DIR, f"raw_pages_{cleaned}_*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        return {"available": False, "integrity": "unknown"}

    latest = files[-1]
    try:
        with open(latest, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning("读取爬虫元数据失败: %s", e)
        return {"available": False, "integrity": "unknown"}

    stop_type = data.get("stop_type", "unknown")
    bottom_detected = bool(data.get("bottom_marker_detected", False))
    expand_remaining = data.get("expand_remaining_visible", 0)
    expand_failed = data.get("expand_permanently_failed", 0)

    complete = stop_type == "bottom" and bottom_detected and expand_remaining == 0
    integrity = "complete" if complete else "incomplete"

    return {
        "available": True,
        "integrity": integrity,
        "stop_type": stop_type,
        "bottom_detected": bottom_detected,
        "expand_remaining": expand_remaining,
        "expand_failed": expand_failed,
        "source_file": os.path.basename(latest),
    }


# ============================================================
#  历史列表
# ============================================================

def _dates_with_data() -> List[str]:
    """扫描 output/????????.xlsx 得到所有有数据的日期（YYYYMMDD 降序，最近日期在前）。"""
    dates = set()
    for p in glob.glob(os.path.join(OUTPUT_DIR, "????????.xlsx")):
        m = re.match(r"^(\d{8})\.xlsx$", os.path.basename(p))
        if m:
            dates.add(m.group(1))
    return sorted(dates, reverse=True)


def _report_path(date_str: str) -> str:
    cleaned = date_str.replace("-", "")
    return os.path.join(OUTPUT_DIR, f"daily_report_{cleaned}.md")


def _report_json_path(date_str: str) -> str:
    cleaned = date_str.replace("-", "")
    return os.path.join(OUTPUT_DIR, f"daily_report_{cleaned}.json")


def _excel_record_count(date_str: str) -> int:
    import openpyxl

    excel_path = os.path.join(OUTPUT_DIR, f"{date_str.replace('-', '')}.xlsx")
    try:
        wb = openpyxl.load_workbook(excel_path, read_only=True)
        n = wb.active.max_row - 1
        wb.close()
        return max(0, n)
    except Exception as e:
        logger.warning("读取 Excel 行数失败: %s", e)
        return 0


def list_report_history() -> dict:
    """列出所有有数据的日期及其报告生成状态。"""
    items = []
    for d in _dates_with_data():
        rp = _report_path(d)
        jp = _report_json_path(d)
        has_report = os.path.exists(rp)
        has_structured = os.path.exists(jp)
        # 单日期实时状态：generating / success / failed / not_started
        date_iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        report_status = _date_status_for(d)
        items.append(
            {
                "date": date_iso,
                "record_count": _excel_record_count(d),
                "has_report": has_report,
                "has_structured": has_structured,
                "report_status": report_status,
                "report_path": rp if has_report else None,
                "json_path": jp if has_structured else None,
            }
        )
    with _lock:
        status = dict(_state)
        # 把 date_status 也透出
        status["date_status"] = dict(_state.get("date_status", {}))
    return {"items": items, "status": status}


def get_report_content(date_str: str) -> Optional[str]:
    """读取某日期的报告 Markdown 原文（兼容旧版）。"""
    rp = _report_path(date_str)
    if not os.path.exists(rp):
        return None
    with open(rp, "r", encoding="utf-8") as f:
        return f.read()


def get_report_structured(date_str: str) -> Optional[dict]:
    """读取某日期的结构化报告（summary_cards / tables / chart_data / appendix）。"""
    import json as _json

    jp = _report_json_path(date_str)
    if not os.path.exists(jp):
        return None
    with open(jp, "r", encoding="utf-8") as f:
        return _json.load(f)


# ============================================================
#  异步生成
# ============================================================

def _generate_one(date_str: str):
    """生成单个日期的报告，返回 (ok, info)。"""
    records = load_records_from_excel(date_str)
    if not records:
        return False, "该日期无记录"

    from src.parser import analyze_daily
    from .historical_context_service import build as build_historical_context

    historical_context = None
    try:
        historical_context = build_historical_context(date_str, records)
        logger.info("[REPORT] %s 历史上下文构建完成: %d 大V, %d 方向",
                    date_str, len(historical_context.get("kols", [])), len(historical_context.get("directions", [])))
    except Exception as e:
        logger.warning("[REPORT] %s 历史上下文构建失败（降级为无历史对比）: %s", date_str, e)

    # 数据完整性（爬虫元数据）：正常采集下完全不传给 LLM；只有异常才注入 data_warning。
    crawl_status = _load_crawl_status(date_str)
    if historical_context is None:
        historical_context = {}
    if crawl_status.get("integrity") == "complete":
        # 正常采集：LLM 完全不知道 crawl_status；也不会出现任何"数据完整性"相关术语
        historical_context.pop("crawl_status", None)
        historical_context.pop("data_warning", None)
    else:
        # 异常采集：注入面向 LLM 的中文警告
        historical_context.pop("crawl_status", None)
        stop_type = crawl_status.get("stop_type", "unknown")
        expand_remaining = crawl_status.get("expand_remaining", 0)
        msg = "本次采集可能不完整"
        if stop_type == "max_scroll":
            msg += "（达到最大滚动次数）"
        elif stop_type == "stuck":
            msg += "（页面卡住）"
        elif expand_remaining and expand_remaining > 0:
            msg += f"（仍有 {expand_remaining} 个未处理的展开按钮）"
        historical_context["data_warning"] = msg

    path = analyze_daily(
        records,
        output_dir=OUTPUT_DIR,
        date_str=date_str,
        historical_context=historical_context,
    )
    if path:
        return True, path
    return False, "LLM 未配置或调用失败"


def _run(dates: List[str]):
    now = datetime.now().isoformat()
    with _lock:
        _state.update(
            status="generating",
            message=f"正在生成 {len(dates)} 个日期...",
            total=len(dates),
            done=0,
            current_date=None,
            failed_dates=[],
            started_at=now,
            finished_at=None,
        )
        _save_state()

    for d in dates:
        with _lock:
            _state["current_date"] = d
            _save_state()
        _mark_date_status(d, "generating")
        logger.info("[REPORT] 开始生成 %s", d)
        try:
            ok, info = _generate_one(d)
        except Exception as e:
            logger.error("[REPORT] %s 生成异常: %s", d, e, exc_info=True)
            ok, info = False, str(e)
        with _lock:
            if ok:
                _state["done"] += 1
                _mark_date_status(d, "success")
            else:
                _state["failed_dates"].append({"date": d, "error": info})
                _mark_date_status(d, "failed")
            _state["current_date"] = None
            _save_state()

    with _lock:
        failed = len(_state["failed_dates"])
        _state["status"] = "success" if failed == 0 else "failed"
        _state["message"] = f"完成 {_state['done']}/{_state['total']}，失败 {failed}"
        _state["finished_at"] = datetime.now().isoformat()
        _save_state()


def generate_reports_async(dates: Optional[List[str]] = None) -> dict:
    """启动后台批量生成。dates=None 表示全部有数据的日期。

    后台线程异常会被捕获并写入状态文件，**绝不**让 uvicorn worker 崩溃。
    """
    with _lock:
        if _state["status"] == "generating":
            return {"ok": False, "message": "正在生成中，请稍候"}

    if not dates:
        target = _dates_with_data()
    else:
        target = [d.replace("-", "") for d in dates if d]

    if not target:
        return {"ok": False, "message": "没有可生成的日期"}

    def _safe_run():
        try:
            _run(target)
        except Exception as e:
            logger.exception("[REPORT] 后台线程崩溃: %s", e)
            with _lock:
                _state["status"] = "failed"
                _state["message"] = f"后台线程异常: {e}"
                _state["finished_at"] = datetime.now().isoformat()
                _save_state()

    thread = threading.Thread(target=_safe_run, daemon=True)
    thread.start()
    return {"ok": True, "message": f"已启动 {len(target)} 个日期的报告生成"}
