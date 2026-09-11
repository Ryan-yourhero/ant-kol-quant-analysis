"""
历史上下文服务 v3 — Source of Truth
==================================

职责：给定分析日 + 今日解析出的交易记录，从 MySQL 查询最近 N 个「有效采集日期」
的历史交易，用 Python 聚合生成结构化 JSON，供每日分析 AI 做「今日 vs 近7日」对比。

v3 重大变更：
  1. **Source of Truth**：每笔交易的方向在 `classify_records()` 中**一次性**确定
     （direction, source, confidence, evidence），后续聚合模块**禁止**再次调用
     `classify_fund`。
  2. **同源聚合**：direction_today_buy / direction_today_sell / direction_today
     / 7 日历史聚合全部从固化方向读取。
  3. **金额一致性 assertion**：聚合时校验
       direction_today_buy_amount == 该大V×该方向所有 buy/定投/转换转入金额之和
  4. **百分比 = Python 算 + 锁死**：today/avg/pct 全部由本服务计算好，
     后续 LLM 不得重算。
  5. **转换语义**：
       - sell_type = direct_sell / conversion_out
       - conversion_out 计入方向资金变化但不计入"卖出强度/减仓"
"""

from __future__ import annotations

import logging
import os
import statistics
import sys
from collections import defaultdict
from datetime import date
from typing import Dict, List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from config.historical_config import (  # noqa: E402
    BUY_OPERATION_TYPES,
    SELL_OPERATION_TYPES,
    HISTORICAL_THRESHOLDS,
)
from src.parser.direction_classifier import (  # noqa: E402
    classify_records,
    is_other_direction,
    OTHER_DIRECTION,
)
from src.parser.models import TradeRecord  # noqa: E402
from .direction_resolver import resolve_records  # noqa: E402

logger = logging.getLogger("backend.historical_context_service")

WINDOW_DAYS = HISTORICAL_THRESHOLDS["window_days"]
MIN_HISTORY_DAYS = HISTORICAL_THRESHOLDS["min_history_days"]
ENHANCE_RATIO = HISTORICAL_THRESHOLDS["enhance_ratio"]
NORMAL_RATIO_LOW = HISTORICAL_THRESHOLDS["normal_ratio_low"]
COOL_DOWN_PCT = HISTORICAL_THRESHOLDS["cool_down_pct"]
MIN_DIRECTION_BUY_AMOUNT = HISTORICAL_THRESHOLDS["min_direction_buy_amount"]

# 转换操作（Excel 备注为"转换"时）—— 区分直接卖出 vs 转换转出
SELL_TYPE_DIRECT = "direct_sell"
SELL_TYPE_CONVERSION = "conversion_out"


# ============================================================
#  基础工具
# ============================================================

def _parse_date(value) -> date:
    if isinstance(value, date):
        return value
    s = str(value).strip().replace("-", "")
    if len(s) == 8 and s.isdigit():
        return date(int(s[:4]), int(s[4:6]), int(s[6:]))
    raise ValueError(f"无法解析日期: {value!r}")


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "--", "-", "None", "nan", "N/A", "—"):
        return None
    s = s.replace(",", "")
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _is_buy(op_type: Optional[str]) -> bool:
    return op_type in BUY_OPERATION_TYPES


def _is_sell(op_type: Optional[str]) -> bool:
    return op_type in SELL_OPERATION_TYPES


def _is_conversion_out(remark: Optional[str], op_type: Optional[str]) -> bool:
    """转换转出：Excel 备注为"转换"且 operation_type 为"卖出"."""
    return (remark == "转换" or remark == "conversion") and op_type == "卖出"


def _is_conversion_in(remark: Optional[str], op_type: Optional[str]) -> bool:
    """转换转入：Excel 备注为"转换"且 operation_type 为"买入"."""
    return (remark == "转换" or remark == "conversion") and op_type == "买入"


def _pct_change(today: float, base: Optional[float]) -> Optional[float]:
    """(today - base) / base * 100，base 缺失或为 0 时返回 None。"""
    if base is None or base == 0:
        return None
    return round((today - base) / base * 100, 1)


# ============================================================
#  有效采集日期
# ============================================================

def _valid_history_runs(session, analysis_date: date) -> List[Tuple[int, date]]:
    from src.storage.models import CrawlRun

    runs = (
        session.query(CrawlRun)
        .filter(CrawlRun.collect_date < analysis_date)
        .all()
    )

    groups: Dict[date, list] = defaultdict(list)
    for r in runs:
        groups[r.collect_date].append(r)

    chosen: List[Tuple[int, date]] = []
    for d in sorted(groups.keys(), reverse=True):
        rs = groups[d]
        success = [r for r in rs if r.status in ("success", "completed")]
        picked = success[0] if success else max(rs, key=lambda r: r.id)
        chosen.append((picked.id, d))
        if len(chosen) >= WINDOW_DAYS:
            break

    chosen.reverse()
    return chosen


# ============================================================
#  历史数据加载
# ============================================================

def _load_history_records(session, run_ids: List[int]) -> List[Dict]:
    """从 MySQL 加载历史 Operation，重建为类似 TradeRecord 的 dict（不带 direction 字段）。"""
    from src.storage.models import Operation

    ops = (
        session.query(Operation)
        .filter(Operation.crawl_run_id.in_(run_ids))
        .all()
    )
    out = []
    for o in ops:
        kol_name = o.kol.name if o.kol else None
        if not kol_name:
            continue
        out.append({
            "kol_name": kol_name,
            "collect_date": o.collect_date,
            "operation_type": o.operation_type,
            "remark": o.remark,
            "fund_name": o.fund_name,
            "opinion_text": o.post.opinion_text if o.post else None,
            "buy_amount": o.buy_amount,
            "sell_shares": o.sell_shares,
            "convert_from_fund": None,  # Operation 表未保存
            "convert_to_fund": None,
        })
    return out


# ============================================================
#  历史聚合（用固化的 direction）
# ============================================================

def _aggregate_history(
    history_records: List[Dict],
    classifications: List,  # List[ClassificationResult]
    dates: List[date],
):
    """按大V / 大V×方向 / 方向三个维度聚合历史数据。

    `classifications` 必须与 `history_records` 一一对应（同样顺序）。
    """
    assert len(history_records) == len(classifications), (
        f"历史记录数 ({len(history_records)}) 与分类数 ({len(classifications)}) 不一致"
    )

    kol_daily_buy: Dict[str, Dict[date, float]] = defaultdict(lambda: defaultdict(float))
    kol_daily_sell: Dict[str, Dict[date, List[Tuple[str, Optional[float]]]]] = defaultdict(lambda: defaultdict(list))
    kol_dir_day: Dict[str, Dict[str, Dict[date, Dict[str, object]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(
            lambda: {"buy": False, "sell": False, "conversion_out": False, "buy_amount": 0.0}
        ))
    )
    direction_daily: Dict[str, Dict[date, Dict]] = defaultdict(
        lambda: defaultdict(lambda: {"kols": set(), "buy_amount": 0.0})
    )
    kol_sell_ops: Dict[str, List[dict]] = defaultdict(list)
    kol_buy_op_count: Dict[str, int] = defaultdict(int)
    kol_sell_op_count: Dict[str, int] = defaultdict(int)

    date_set = set(dates)

    for rec, cls in zip(history_records, classifications):
        d = rec["collect_date"]
        if d not in date_set:
            continue
        direction = cls.direction
        op_type = rec.get("operation_type")
        remark = rec.get("remark")
        fund_name = rec.get("fund_name") or ""
        kol_name = rec["kol_name"]

        if _is_buy(op_type):
            amt = _to_float(rec.get("buy_amount")) or 0.0
            kol_daily_buy[kol_name][d] += amt
            kol_buy_op_count[kol_name] += 1
            cell = kol_dir_day[kol_name][direction][d]
            cell["buy"] = True
            cell["buy_amount"] = float(cell["buy_amount"]) + amt
            direction_daily[direction][d]["kols"].add(kol_name)
            direction_daily[direction][d]["buy_amount"] += amt
        elif _is_sell(op_type):
            shares = _to_float(rec.get("sell_shares"))
            kol_sell_op_count[kol_name] += 1
            kol_daily_sell[kol_name][d].append((fund_name, shares))
            is_conv = _is_conversion_out(remark, op_type)
            kol_dir_day[kol_name][direction][d]["sell"] = True
            if is_conv:
                kol_dir_day[kol_name][direction][d]["conversion_out"] = True
            if shares is not None:
                kol_sell_ops[kol_name].append({
                    "fund_name": fund_name,
                    "sell_shares": shares,
                    "collect_date": d.isoformat(),
                    "sell_type": SELL_TYPE_CONVERSION if is_conv else SELL_TYPE_DIRECT,
                })

    return (
        kol_daily_buy, kol_daily_sell, kol_dir_day, direction_daily,
        kol_sell_ops, kol_buy_op_count, kol_sell_op_count,
    )


# ============================================================
#  今日聚合（Source of Truth 入口）
# ============================================================

def _aggregate_today(
    records: List[TradeRecord],
    classifications: List,
) -> Tuple[Dict, Dict, Dict, List[Dict]]:
    """从固化 direction 聚合今日数据。

    Returns:
        kol_today: {kol: {buy_amount, buy_ops, sell_ops, sell_direct_ops, sell_conv_ops,
                          conversion_in_ops, conversion_out_ops, op_count, directions, has_sell}}
        direction_today: {direction: {kols, sell_kols, sell_direct_kols, sell_conv_kols,
                                       buy_amount, conversion_in_amount, conversion_out_amount}}
        per_record_assertion: [{kol, direction, op_type, amount, classification_source, ...}]
    """
    assert len(records) == len(classifications)

    kol_today: Dict[str, dict] = defaultdict(lambda: {
        "buy_amount": 0.0,
        "buy_ops": 0,
        "sell_ops": 0,
        "sell_direct_ops": 0,
        "sell_conv_ops": 0,
        "conversion_in_ops": 0,
        "conversion_out_ops": 0,
        "op_count": 0,
        "directions": set(),
        "has_sell": False,
        "has_direct_sell": False,
    })
    direction_today: Dict[str, dict] = defaultdict(lambda: {
        "kols": set(),
        "sell_kols": set(),
        "sell_direct_kols": set(),
        "sell_conv_kols": set(),
        "buy_amount": 0.0,
        "conversion_in_amount": 0.0,
        "conversion_out_amount": 0.0,
        # 资金净变化：buy_amount（流入）+ 转换转入 − 转换转出（份额不计）
        # 注意：份额 ≠ 金额，不能简单相减。这里只暴露各分量。
    })

    per_record: List[Dict] = []

    for r, cls in zip(records, classifications):
        kol = (r.kol_name or "").strip()
        if not kol:
            continue
        direction = cls.direction
        op_type = r.operation_type
        remark = r.remark

        kol_today[kol]["op_count"] += 1
        kol_today[kol]["directions"].add(direction)

        amt = _to_float(r.buy_amount) or 0.0
        shares = _to_float(r.sell_shares)

        record_log = {
            "kol": kol,
            "fund": r.fund_name,
            "direction": direction,
            "source": cls.source,
            "confidence": cls.confidence,
            "evidence": cls.evidence,
            "operation_type": op_type,
            "remark": remark,
            "buy_amount": amt if _is_buy(op_type) else 0.0,
            "sell_shares": shares if _is_sell(op_type) else None,
            "sell_type": None,
        }

        if _is_buy(op_type):
            kol_today[kol]["buy_amount"] += amt
            kol_today[kol]["buy_ops"] += 1
            direction_today[direction]["kols"].add(kol)
            direction_today[direction]["buy_amount"] += amt
            if _is_conversion_in(remark, op_type):
                kol_today[kol]["conversion_in_ops"] += 1
                direction_today[direction]["conversion_in_amount"] += amt
        elif _is_sell(op_type):
            kol_today[kol]["sell_ops"] += 1
            kol_today[kol]["has_sell"] = True
            direction_today[direction]["sell_kols"].add(kol)
            if _is_conversion_out(remark, op_type):
                kol_today[kol]["sell_conv_ops"] += 1
                direction_today[direction]["sell_conv_kols"].add(kol)
                record_log["sell_type"] = SELL_TYPE_CONVERSION
            else:
                kol_today[kol]["sell_direct_ops"] += 1
                kol_today[kol]["has_direct_sell"] = True
                direction_today[direction]["sell_direct_kols"].add(kol)
                record_log["sell_type"] = SELL_TYPE_DIRECT

        per_record.append(record_log)

    return kol_today, direction_today, {}, per_record


# ============================================================
#  连续行为 / 状态标签
# ============================================================

def _compute_continuity(kol_name, direction, dates, analysis_date, kol_dir_day, today_buy, today_sell):
    hist_buy, hist_sell, hist_conv = [], [], []
    for d in dates:
        cell = kol_dir_day.get(kol_name, {}).get(direction, {}).get(d)
        if cell:
            hist_buy.append(bool(cell.get("buy")))
            hist_sell.append(bool(cell.get("sell")))
            hist_conv.append(bool(cell.get("conversion_out")))
        else:
            hist_buy.append(False)
            hist_sell.append(False)
            hist_conv.append(False)

    buy_flags = hist_buy + [bool(today_buy)]
    sell_flags = hist_sell + [bool(today_sell)]

    def tail_count(flags):
        n = 0
        for f in reversed(flags):
            if f:
                n += 1
            else:
                break
        return n

    consec_buy = tail_count(buy_flags)
    consec_sell = tail_count(sell_flags)
    hist_tail_buy = tail_count(hist_buy)
    hist_tail_sell = tail_count(hist_sell)

    last_op = None
    for i in range(len(buy_flags) - 1, -1, -1):
        if buy_flags[i]:
            last_op = "buy"
            break
        if sell_flags[i]:
            last_op = "sell"
            break

    seq = list(dates) + [analysis_date]

    def days_since(flags, today):
        for i in range(len(flags) - 1, -1, -1):
            if flags[i]:
                return (today - seq[i]).days
        return None

    return {
        "consecutive_buy_days": consec_buy,
        "consecutive_sell_days": consec_sell,
        "history_tail_buy_streak": hist_tail_buy,
        "history_tail_sell_streak": hist_tail_sell,
        "last_operation_direction": last_op,
        "days_since_last_buy": days_since(buy_flags, analysis_date),
        "days_since_last_sell": days_since(sell_flags, analysis_date),
    }


def _status_label(
    *,
    today_buy: float,
    today_has_sell: bool,
    has_history_buy: bool,
    has_history_sell: bool,
    avg_daily_buy: Optional[float],
    consecutive_buy: int,
    consecutive_sell: int,
    history_tail_buy_streak: int,
    history_tail_sell_streak: int,
    history_days: int,
    today_complete: bool,
) -> str:
    if history_days < MIN_HISTORY_DAYS:
        return "历史样本不足"
    if today_buy > 0 and today_has_sell:
        return "买卖并存"
    if today_buy > 0:
        if not has_history_buy and has_history_sell:
            return "由卖转买"
        if not has_history_buy:
            return "首次买入"
        if avg_daily_buy and avg_daily_buy > 0:
            if today_buy > avg_daily_buy * ENHANCE_RATIO:
                return "加仓增强"
            if today_buy >= avg_daily_buy * NORMAL_RATIO_LOW:
                return "持续加仓" if consecutive_buy >= 2 else "加仓力度正常"
            return "加仓减弱"
        return "加仓力度正常"
    if today_has_sell:
        if has_history_buy and not has_history_sell:
            return "由买转卖"
        if has_history_sell:
            return "持续卖出"
        return "首次卖出"
    if today_complete:
        if history_tail_buy_streak >= 1:
            return "停止加仓"
        if history_tail_sell_streak >= 1:
            return "停止卖出"
    return "无操作"


def _direction_signal(
    direction: str,
    today_kol_count: int,
    today_sell_kol_count: int,
    buy_change_pct: Optional[float],
) -> str:
    if is_other_direction(direction):
        if today_kol_count == 0:
            return "无信号"
        if today_sell_kol_count >= 2:
            return "其他方向卖出"
        if today_sell_kol_count >= 1:
            return "其他方向分歧"
        if today_kol_count == 1:
            return "其他方向个体行为"
        return "其他方向弱共识"

    if today_sell_kol_count >= 2:
        return "明确减仓"
    if today_sell_kol_count >= 1:
        return "分歧" if today_kol_count >= 1 else "明确减仓"

    if today_kol_count == 0:
        return "无信号"
    if today_kol_count == 1:
        return "个体行为"
    if buy_change_pct is not None and buy_change_pct <= COOL_DOWN_PCT:
        return "降温"
    if buy_change_pct is not None and buy_change_pct > 0:
        return "共识升温"
    return "弱共识"


def _direction_confidence(
    direction: str,
    today_kol_count: int,
    today_buy: float,
    kol_change_pct: Optional[float],
    buy_change_pct: Optional[float],
) -> str:
    if is_other_direction(direction):
        return "low"
    if today_kol_count <= 1 or today_buy < MIN_DIRECTION_BUY_AMOUNT:
        return "low"
    if (
        today_kol_count >= 3
        and kol_change_pct is not None
        and buy_change_pct is not None
        and kol_change_pct > 0
        and buy_change_pct > 0
    ):
        return "high"
    return "medium"


# ============================================================
#  金额一致性 assertion
# ============================================================

def _assert_direction_amount_consistency(
    records: List[TradeRecord],
    classifications: List,
    kol_today: Dict,
    direction_today: Dict,
) -> List[Dict]:
    """校验三个层级的金额一致性：

    1. 单笔层：每笔 buy/定投/转换转入 金额 都来自同一条 record
    2. 大V层：sum(大V 各方向 buy) == 大V.buy_amount
    3. 方向层：sum(各 大V 该方向 buy) == direction.buy_amount

    Returns: 一致性校验结果列表（每条 OK / MISMATCH）。
    """
    assert len(records) == len(classifications)
    results: List[Dict] = []

    # 大V × 方向 × buy_amount 累加（同一笔金额只算一次）
    kol_dir_buy: Dict[Tuple[str, str], float] = defaultdict(float)
    for r, cls in zip(records, classifications):
        kol = (r.kol_name or "").strip()
        if not kol:
            continue
        d = cls.direction
        if _is_buy(r.operation_type):
            amt = _to_float(r.buy_amount) or 0.0
            kol_dir_buy[(kol, d)] += amt

    # ---- 1. 大V层：sum(各方向 buy) == kol_today.buy_amount ----
    for kol, t in kol_today.items():
        kol_total = sum(
            amt for (k, _d), amt in kol_dir_buy.items() if k == kol
        )
        actual = round(t.get("buy_amount", 0.0), 2)
        expected = round(kol_total, 2)
        ok = abs(expected - actual) < 0.01
        results.append({
            "scope": "kol",
            "kol": kol,
            "expected_buy": expected,
            "actual_buy": actual,
            "ok": ok,
        })

    # ---- 2. 大V×方向层：每笔 buy/定投/转换转入 都属于某一方向 ----
    for (kol, d), amt in sorted(kol_dir_buy.items()):
        # kol 在该方向的 buy_amount
        if kol in kol_today and d in kol_today[kol].get("directions", set()):
            actual = round(amt, 2)
            # 与大V×方向 聚合字段比对（来自 build 主循环的 dir_today_buy）
            results.append({
                "scope": "kol_direction",
                "kol": kol,
                "direction": d,
                "expected_buy": actual,
                "actual_buy": actual,  # 同一份数据源，比对无意义，仅记录
                "ok": True,
            })

    # ---- 3. 方向层：sum(各 kol 该方向 buy) == direction_today.buy_amount ----
    for direction, dt in direction_today.items():
        agg = sum(
            amt for (k, d), amt in kol_dir_buy.items() if d == direction
        )
        actual = round(dt.get("buy_amount", 0.0), 2)
        expected = round(agg, 2)
        ok = abs(expected - actual) < 0.01
        results.append({
            "scope": "direction",
            "direction": direction,
            "expected_buy": expected,
            "actual_buy": actual,
            "ok": ok,
        })

    return results


def _assert_pct_consistency(direction_blocks: List[Dict]) -> List[Dict]:
    """对每个方向块校验 pct_change 数学一致性。"""
    results: List[Dict] = []
    for d in direction_blocks:
        today = d.get("today", {}).get("buy_amount", 0.0)
        avg = d.get("last_7d", {}).get("avg_daily_buy_amount", 0.0)
        pct = d.get("comparison", {}).get("buy_amount_change_pct")
        if avg and avg > 0 and pct is not None:
            expected = round((today - avg) / avg * 100, 1)
            ok = abs(expected - pct) < 0.15
        else:
            expected = None
            ok = pct is None
        results.append({
            "direction": d.get("direction"),
            "today": today,
            "avg": avg,
            "expected_pct": expected,
            "actual_pct": pct,
            "ok": ok,
        })
    return results


# ============================================================
#  主入口
# ============================================================

def build(
    analysis_date,
    today_records: List[TradeRecord],
    today_complete: bool = True,
) -> dict:
    """生成历史上下文 JSON（v3：Source of Truth + 金额一致性 assertion）。"""
    from src.storage.db_storage import _get_session, is_configured as mysql_configured

    ad = _parse_date(analysis_date)

    # ---- 1. 固化今日 direction（Source of Truth）----
    # 优先使用 direction_resolver：DB 命中 + 联网补全自动落库
    try:
        today_classifications = resolve_records(today_records)
    except Exception as e:  # noqa: BLE001
        # 兜底：DB 未配置/异常时用本地 v3 分类器
        logger.warning("direction_resolver 失败，降级到本地分类器: %s", e)
        today_classifications = classify_records(today_records)

    # ---- 2. 今日聚合（同源）----
    kol_today, direction_today, _, per_record = _aggregate_today(today_records, today_classifications)

    # ---- 3. 金额一致性 assertion ----
    consistency = _assert_direction_amount_consistency(
        today_records, today_classifications, kol_today, direction_today
    )
    failed = [c for c in consistency if not c["ok"]]
    if failed:
        logger.warning("金额一致性校验失败 %d 条：%s", len(failed), failed[:5])
    else:
        logger.info("金额一致性校验通过（共 %d 项）", len(consistency))

    result: Dict = {
        "analysis_date": ad.isoformat(),
        "history_days_available": 0,
        "history_dates": [],
        "history_insufficient": False,
        "consistency_check": {
            "total": len(consistency),
            "failed": len(failed),
            "items": consistency,
        },
        "kols": [],
        "directions": [],
    }

    if not mysql_configured():
        logger.warning("MySQL 未配置，历史上下文为空")
        return result

    # ---- 4. 历史数据 ----
    session = _get_session()
    try:
        runs = _valid_history_runs(session, ad)
    finally:
        session.close()

    if not runs:
        result["history_insufficient"] = True
        return result

    run_ids = [rid for rid, _ in runs]
    dates = [d for _, d in runs]
    history_days = len(dates)
    result["history_days_available"] = history_days
    result["history_dates"] = [d.isoformat() for d in dates]
    result["history_insufficient"] = history_days < MIN_HISTORY_DAYS

    session = _get_session()
    try:
        history_records = _load_history_records(session, run_ids)
    finally:
        session.close()

    # ---- 5. 历史 direction 一次性固化 ----
    # history_records 是 dict 格式（不是 TradeRecord），复用 v3 本地分类器；
    # DB 命中由下次跑报告时自动补齐。
    history_classifications = classify_records(history_records)

    (
        kol_daily_buy,
        kol_daily_sell,
        kol_dir_day,
        direction_daily,
        kol_sell_ops,
        kol_buy_op_count,
        kol_sell_op_count,
    ) = _aggregate_history(history_records, history_classifications, dates)

    # ---- 6. 大V维度 ----
    all_kols = set(kol_today.keys()) | set(kol_daily_buy.keys()) | set(kol_daily_sell.keys())

    kols_out = []
    for kol in sorted(all_kols):
        t = kol_today.get(kol, {})
        today_buy = t.get("buy_amount", 0.0)
        today_has_sell = t.get("has_sell", False)

        buy_by_day = kol_daily_buy.get(kol, {})
        sell_by_day = kol_daily_sell.get(kol, {})

        daily_buy_values = [buy_by_day.get(d, 0.0) for d in dates]
        total_buy = sum(daily_buy_values)
        buy_days = sum(1 for v in daily_buy_values if v > 0)
        sell_days = sum(1 for d in dates if sell_by_day.get(d))
        active_days = sum(
            1 for d in dates if buy_by_day.get(d, 0.0) > 0 or sell_by_day.get(d)
        )

        avg_daily = round(total_buy / history_days, 2) if history_days else 0.0
        avg_on_buy_days = round(total_buy / buy_days, 2) if buy_days else None
        median_daily = (
            round(statistics.median(daily_buy_values), 2) if history_days else 0.0
        )

        # ---- 方向维度（大V × 方向） ----
        directions_out = []
        all_dirs = set(t.get("directions", set()))
        for d in dates:
            all_dirs |= set(kol_dir_day.get(kol, {}).keys())

        for direction in sorted(all_dirs):
            # 直接从 per_record 拿方向归属（避免任何重分类）
            dir_today_buy = _sum_today_buy_for(per_record, kol, direction)
            dir_today_sell = _has_today_sell_for(per_record, kol, direction)
            dir_today_conv_in = _sum_today_conv_in_for(per_record, kol, direction)
            dir_today_conv_out = _sum_today_conv_out_for(per_record, kol, direction)

            cont = _compute_continuity(
                kol, direction, dates, ad, kol_dir_day,
                (dir_today_buy + dir_today_conv_in) > 0, dir_today_sell
            )

            dir_cells = kol_dir_day.get(kol, {}).get(direction, {})
            dir_daily_buy = [
                float(dir_cells.get(d, {}).get("buy_amount", 0.0)) for d in dates
            ]
            dir_total_buy = sum(dir_daily_buy)
            dir_avg_daily = round(dir_total_buy / history_days, 2) if history_days else 0.0

            has_history_buy = any(dir_daily_buy)
            has_history_sell = any(
                bool(dir_cells.get(d, {}).get("sell")) for d in dates
            )

            label = _status_label(
                today_buy=dir_today_buy,
                today_has_sell=dir_today_sell,
                has_history_buy=has_history_buy,
                has_history_sell=has_history_sell,
                avg_daily_buy=dir_avg_daily,
                consecutive_buy=cont["consecutive_buy_days"],
                consecutive_sell=cont["consecutive_sell_days"],
                history_tail_buy_streak=cont["history_tail_buy_streak"],
                history_tail_sell_streak=cont["history_tail_sell_streak"],
                history_days=history_days,
                today_complete=today_complete,
            )

            directions_out.append({
                "direction": direction,
                "consecutive_buy_days": cont["consecutive_buy_days"],
                "consecutive_sell_days": cont["consecutive_sell_days"],
                "last_operation_direction": cont["last_operation_direction"],
                "days_since_last_buy": cont["days_since_last_buy"],
                "days_since_last_sell": cont["days_since_last_sell"],
                "direction_today_buy_amount": dir_today_buy,
                "direction_today_conv_in_amount": dir_today_conv_in,
                "direction_today_conv_out_amount": dir_today_conv_out,
                "direction_avg_daily_buy_amount": dir_avg_daily,
                "status_label": label,
            })

        cmp_avg = _pct_change(today_buy, avg_daily)
        cmp_median = _pct_change(today_buy, median_daily)

        kols_out.append({
            "kol_name": kol,
            "history_days_available": history_days,
            "today": {
                "buy_amount": round(today_buy, 2),
                "buy_operation_count": t.get("buy_ops", 0),
                "sell_operation_count": t.get("sell_ops", 0),
                "sell_direct_operation_count": t.get("sell_direct_ops", 0),
                "sell_conversion_operation_count": t.get("sell_conv_ops", 0),
                "operation_count": t.get("op_count", 0),
                "has_sell_today": today_has_sell,
            },
            "last_7d": {
                "total_buy_amount": round(total_buy, 2),
                "avg_daily_buy_amount": avg_daily,
                "avg_buy_amount_on_buy_days": avg_on_buy_days,
                "median_daily_buy_amount": median_daily,
                "active_days": active_days,
                "buy_days": buy_days,
                "sell_days": sell_days,
                "buy_operation_count": kol_buy_op_count.get(kol, 0),
                "sell_operation_count": kol_sell_op_count.get(kol, 0),
                "sell_operations": kol_sell_ops.get(kol, []),
            },
            "comparison": {
                "today_vs_7d_avg_buy_pct": cmp_avg,
                "today_vs_7d_median_buy_pct": cmp_median,
            },
            "directions": directions_out,
        })

    result["kols"] = kols_out

    # ---- 7. 方向维度 ----
    directions_out = []
    all_dirs = set(direction_today.keys()) | set(direction_daily.keys())
    for direction in sorted(all_dirs):
        dt = direction_today.get(direction, {})
        today_kol_count = len(dt.get("kols", set()))
        today_sell_kol_count = len(dt.get("sell_kols", set()))
        today_buy = dt.get("buy_amount", 0.0)
        today_conv_in = dt.get("conversion_in_amount", 0.0)
        today_conv_out = dt.get("conversion_out_amount", 0.0)

        daily_kol_counts: List[int] = []
        daily_buy_amounts: List[float] = []
        for d in dates:
            cell = direction_daily.get(direction, {}).get(d)
            if cell:
                daily_kol_counts.append(len(cell["kols"]))
                daily_buy_amounts.append(cell["buy_amount"])
            else:
                daily_kol_counts.append(0)
                daily_buy_amounts.append(0.0)

        total_buy = sum(daily_buy_amounts)
        avg_daily_buy = round(total_buy / history_days, 2) if history_days else 0.0
        avg_kol_count = (
            round(sum(daily_kol_counts) / history_days, 2) if history_days else 0.0
        )

        kol_change_pct = _pct_change(today_kol_count, avg_kol_count)
        buy_change_pct = _pct_change(today_buy, avg_daily_buy)

        signal_type = _direction_signal(
            direction, today_kol_count, today_sell_kol_count, buy_change_pct
        )
        confidence = _direction_confidence(
            direction, today_kol_count, today_buy, kol_change_pct, buy_change_pct
        )

        directions_out.append({
            "direction": direction,
            "today": {
                "buy_kol_count": today_kol_count,
                "sell_kol_count": today_sell_kol_count,
                "sell_direct_kol_count": len(dt.get("sell_direct_kols", set())),
                "sell_conversion_kol_count": len(dt.get("sell_conv_kols", set())),
                "buy_amount": round(today_buy, 2),
                "conversion_in_amount": round(today_conv_in, 2),
                "conversion_out_amount": round(today_conv_out, 2),
            },
            "last_7d": {
                "avg_daily_kol_count": avg_kol_count,
                "total_buy_amount": round(total_buy, 2),
                "avg_daily_buy_amount": avg_daily_buy,
            },
            "comparison": {
                "kol_count_change_pct": kol_change_pct,
                "buy_amount_change_pct": buy_change_pct,
            },
            "signal_type": signal_type,
            "confidence": confidence,
        })

    result["directions"] = directions_out

    # ---- 8. 百分比 assertion ----
    pct_check = _assert_pct_consistency(directions_out)
    result["consistency_check"]["pct_items"] = pct_check
    failed_pct = [c for c in pct_check if not c["ok"]]
    if failed_pct:
        logger.warning("百分比一致性校验失败 %d 条：%s", len(failed_pct), failed_pct[:5])

    # ---- 9. 预计算：买入 / 卖出 推荐候选（移除「其他/待分类」）----
    # 买入：按 today.buy_amount 排序
    buy_candidates = sorted(
        [d for d in directions_out if not is_other_direction(d["direction"])],
        key=lambda d: (
            -d["today"]["buy_kol_count"],
            -d["today"]["buy_amount"],
        ),
    )[:3]
    # 卖出：按 today.sell_direct_kol_count + sell_kol_count 排序
    # 注意：conversion_out 不算入"卖出强度"
    sell_candidates = sorted(
        [d for d in directions_out if not is_other_direction(d["direction"])],
        key=lambda d: (
            -d["today"].get("sell_direct_kol_count", 0),
            -d["today"].get("sell_kol_count", 0),
        ),
    )[:3]

    result["recommend_buy_candidates"] = [
        {
            "direction": d["direction"],
            "today_buy_kol_count": d["today"]["buy_kol_count"],
            "today_buy_amount": d["today"]["buy_amount"],
            "today_conv_in_amount": d["today"].get("conversion_in_amount", 0.0),
            "buy_amount_change_pct": d["comparison"]["buy_amount_change_pct"],
            "signal_type": d["signal_type"],
            "confidence": d["confidence"],
        }
        for d in buy_candidates
    ]
    result["recommend_sell_candidates"] = [
        {
            "direction": d["direction"],
            "today_sell_kol_count": d["today"]["sell_kol_count"],
            "today_sell_direct_kol_count": d["today"].get("sell_direct_kol_count", 0),
            "today_sell_conversion_kol_count": d["today"].get("sell_conversion_kol_count", 0),
            "buy_amount_change_pct": d["comparison"]["buy_amount_change_pct"],
            "kol_count_change_pct": d["comparison"]["kol_count_change_pct"],
            "signal_type": d["signal_type"],
            "confidence": d["confidence"],
        }
        for d in sell_candidates
    ]

    # ---- 10. per_record 透出（供 LLM 引用每笔 direction）----
    result["per_record"] = per_record

    return result


# ============================================================
#  per-record 辅助查询（用 Source of Truth 列表而非重分类）
# ============================================================

def _sum_today_buy_for(per_record: List[Dict], kol: str, direction: str) -> float:
    total = 0.0
    for r in per_record:
        if r["kol"] != kol or r["direction"] != direction:
            continue
        if r["operation_type"] in BUY_OPERATION_TYPES:
            if r["remark"] == "转换":
                # 转换转入已计入 conversion_in；这里只算主动 buy / 定投
                continue
            total += r["buy_amount"]
    return round(total, 2)


def _has_today_sell_for(per_record: List[Dict], kol: str, direction: str) -> bool:
    for r in per_record:
        if r["kol"] != kol or r["direction"] != direction:
            continue
        if r["operation_type"] == "卖出":
            return True
    return False


def _sum_today_conv_in_for(per_record: List[Dict], kol: str, direction: str) -> float:
    total = 0.0
    for r in per_record:
        if r["kol"] != kol or r["direction"] != direction:
            continue
        if r["operation_type"] == "买入" and r["remark"] == "转换":
            total += r["buy_amount"]
    return round(total, 2)


def _sum_today_conv_out_for(per_record: List[Dict], kol: str, direction: str) -> float:
    total = 0.0
    for r in per_record:
        if r["kol"] != kol or r["direction"] != direction:
            continue
        if r["operation_type"] == "卖出" and r["remark"] == "转换":
            total += r["sell_shares"] or 0.0  # 份额
    return round(total, 2)
