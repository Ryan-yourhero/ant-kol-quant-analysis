"""
历史基金方向批量补全脚本
============================

目标：
  把当前 MySQL operations 中出现过的全部基金统一进行方向补全，
  建立完整的 fund_direction_master 初始库。

绝不重复实现方向判断逻辑 —— 全部复用
  - backend.services.direction_resolver.resolve_direction
  - backend.services.fund_direction_repo（CRUD）
  - backend.services.web_search（联网）

参数：
  --only-unresolved   只处理未确认 / 未到 verified 的基金
  --limit N           本次处理基金数上限
  --dry-run           只查看待处理列表，不真正联网、不写库
  --retry-failed      重新处理上次失败（next_reverify_at 已过 / needs_reverify=true）
  --fund-type-hint TYPE  传给 resolver 的 fund_type_hint（默认根据名字启发）
  --batch-size N      写库时一次处理几只（默认 20，只影响日志节奏）
  --output PATH       人工确认清单输出路径（默认 output/backfill_unconfirmed_YYYYMMDD_HHMMSS.md）
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ---- 让脚本可直接执行 ----
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_fund_directions")

# ---- 复用现有服务（禁止重复实现）----
from src.storage.db_storage import init_db, _get_session
from src.storage.models import Operation
from src.parser.direction_classifier import OTHER_DIRECTION, is_other_direction
from backend.services import fund_direction_repo as repo
from backend.services import direction_resolver as resolver
from backend.services import web_search


# ============================================================
#  基金去重：从 operations 提取全部基金
# ============================================================

# 6 位基金代码（出现在基金名里时，如 "招商中证半导体产... 161725"）
# 但 operation.fund_name 通常不带代码 → 启发式从原帖 opinion_text 抽取
_CODE_RE = re.compile(r"\b(\d{6})\b")


def _guess_fund_type(name: str) -> str:
    """简单的基金类型启发（仅给 LLM 当 hint，LLM 可拒绝）。"""
    if "量化" in name:
        return "quant"
    if "指数" in name or "ETF" in name or "联接" in name:
        return "index"
    if "主题" in name or "行业" in name:
        return "industry_theme"
    if "灵活配置" in name or "混合" in name:
        return "mixed"
    return "active_equity"


def collect_unique_funds() -> Dict[str, Dict[str, Any]]:
    """从 operations 表收集全部唯一基金。

    Returns:
        { normalized_name: {"fund_name": 原名, "fund_code": 6位代码或None,
                           "seen_count": int, "kol_names": set, "last_seen": date} }
    """
    session = _get_session()
    try:
        rows = (
            session.query(Operation)
            .order_by(Operation.collect_date.desc())
            .all()
        )
        result: Dict[str, Dict[str, Any]] = {}
        for o in rows:
            fn = (o.fund_name or "").strip()
            if not fn:
                continue
            norm = repo.normalize_fund_name(fn)
            if not norm:
                continue
            # 尝试从 fund_name 本身抽 6 位代码（极少出现，但兜底）
            code = _CODE_RE.search(fn.replace("...", ""))
            code = code.group(1) if code else None
            # 也可以从 opinion_text 里抽
            if not code and o.post and o.post.opinion_text:
                m = _CODE_RE.search(o.post.opinion_text)
                if m:
                    code = m.group(1)
            entry = result.setdefault(norm, {
                "fund_name": fn,
                "fund_code": code,
                "seen_count": 0,
                "kol_names": set(),
                "last_seen": None,
            })
            entry["seen_count"] += 1
            if o.kol and o.kol.name:
                entry["kol_names"].add(o.kol.name)
            if entry["last_seen"] is None or (o.collect_date and o.collect_date > entry["last_seen"]):
                entry["last_seen"] = o.collect_date
            # fund_code 取最早一条非空（让原始权威代码不被覆盖）
            if not entry["fund_code"] and code:
                entry["fund_code"] = code
        return result
    finally:
        session.close()


# ============================================================
#  筛选：决定要处理哪些
# ============================================================

def filter_funds_to_process(
    funds: Dict[str, Dict[str, Any]],
    only_unresolved: bool = False,
    retry_failed: bool = False,
) -> List[Tuple[str, Dict[str, Any]]]:
    """根据 DB 状态筛选待处理基金。

    规则：
      - 默认：处理所有未命中 fund_direction_master 的
      - --only-unresolved：处理 verified=False（low/other/待人工）
      - --retry-failed：处理 needs_reverify=True 或 next_reverify_at 已过
    """
    norms = list(funds.keys())
    db_hits = repo.lookup_many(norms)
    now = datetime.now()

    out: List[Tuple[str, Dict[str, Any]]] = []
    for norm, info in funds.items():
        snap = db_hits.get(norm)
        if only_unresolved:
            # 命中 + verified=True → 跳过
            if snap and snap.verified:
                continue
            out.append((norm, info))
        elif retry_failed:
            # 命中 + next_reverify_at 已过 OR needs_reverify=True
            if snap and snap.next_reverify_at and snap.next_reverify_at > now and not snap.needs_reverify:
                continue
            if snap and snap.verified and not snap.needs_reverify:
                continue
            out.append((norm, info))
        else:
            # 默认：跳过已有 verified 记录；未命中或 verified=False 走补全
            if snap and snap.verified:
                continue
            out.append((norm, info))
    return out


# ============================================================
#  处理：复用 direction_resolver
# ============================================================

# 是否启用联网（与 resolver 保持一致；可通过环境变量关掉）
ENABLE_WEB = os.environ.get("ENABLE_WEB_FALLBACK", "1") == "1"

# daily limit 由 web_search 自身保证；如需跳过可在 .env 设 WEB_SEARCH_PROVIDER=none


def process_one(norm: str, info: Dict[str, Any], *, dry_run: bool) -> Dict[str, Any]:
    """处理一只基金；返回处理结果（含是否触发联网）。"""
    fund_name = info["fund_name"]
    fund_code = info.get("fund_code")

    pre = repo.lookup_fund(fund_name, fund_code=fund_code)
    pre_state = _summarize_snap(pre)

    if dry_run:
        return {
            "norm": norm,
            "fund_name": fund_name,
            "fund_code": fund_code,
            "pre": pre_state,
            "action": "dry_run_skip",
            "result": None,
            "post": None,
            "tavily_called": False,
            "duration_s": 0.0,
        }

    fund_type_hint = _guess_fund_type(fund_name)
    t0 = time.time()
    # 关键：复用现有 resolver；它会自动走 DB → 规则 → 联网，并自动落库
    result = resolver.resolve_direction(
        fund_name, fund_code=fund_code, fund_type_hint=fund_type_hint
    )
    elapsed = time.time() - t0

    post = repo.lookup_fund(fund_name, fund_code=fund_code)
    post_state = _summarize_snap(post)

    # 判定是否实际触发了联网（通过 post 是否有 last_search_at）
    tavily_called = bool(post and post.last_search_at and (
        not pre or (pre.last_search_at or datetime.min) < post.last_search_at
    ))

    return {
        "norm": norm,
        "fund_name": fund_name,
        "fund_code": fund_code,
        "pre": pre_state,
        "action": "resolved",
        "result": {
            "direction": result.direction,
            "confidence": result.confidence,
            "source": result.source,
        },
        "post": post_state,
        "tavily_called": tavily_called,
        "duration_s": round(elapsed, 2),
    }


def _summarize_snap(snap) -> Dict[str, Any]:
    if not snap:
        return {"state": "absent"}
    return {
        "state": "verified" if snap.verified else "provisional",
        "direction": snap.direction,
        "confidence": snap.confidence,
        "fund_type_detail": snap.fund_type_detail,
        "evidence_period": snap.evidence_period,
    }


# ============================================================
#  重新验证候选（用于未来定期补全）
# ============================================================

def build_reverify_candidates() -> List[Dict[str, Any]]:
    """收集需要重新验证的基金。

    触发条件（任一）：
      - direction = 其他/待分类
      - verified = False（provisional）
      - fund_type_detail ∈ (active_equity, mixed) 且 evidence_period 超过 2 个季度
      - needs_reverify = True
    """
    from sqlalchemy import or_, and_
    from src.storage.models import FundDirectionMaster
    from datetime import date

    session = _get_session()
    try:
        today = date.today()
        rows = session.query(FundDirectionMaster).all()
        out = []
        for r in rows:
            reasons = []
            if r.direction == OTHER_DIRECTION:
                reasons.append("其他/待分类")
            if not r.verified:
                reasons.append("provisional")
            if r.needs_reverify:
                reasons.append("needs_reverify=true")
            if r.fund_type_detail in ("active_equity", "mixed") and r.evidence_period:
                # evidence_period 形如 2026Q1
                m = re.match(r"(\d{4})Q([1-4])", r.evidence_period)
                if m:
                    yr, q = int(m.group(1)), int(m.group(2))
                    # 估算该季度末：yr, q*3 月
                    period_end = date(yr, q * 3, 1) if q < 4 else date(yr + 1, 1, 1)
                    months_diff = (today.year - period_end.year) * 12 + (today.month - period_end.month)
                    if months_diff >= 6:  # 超过 2 个季度（~6 个月）
                        reasons.append(f"主动/混合证据超过 2 个季度({r.evidence_period})")
            if reasons:
                out.append({
                    "id": r.id,
                    "fund_name": r.fund_name,
                    "direction": r.direction,
                    "confidence": r.confidence,
                    "verified": r.verified,
                    "fund_type_detail": r.fund_type_detail,
                    "evidence_period": r.evidence_period,
                    "reasons": reasons,
                    "next_reverify_at": r.next_reverify_at.isoformat() if r.next_reverify_at else None,
                })
        return out
    finally:
        session.close()


# ============================================================
#  输出
# ============================================================

def _status_marker(item: Dict[str, Any]) -> str:
    """根据处理结果给出单行状态 marker。"""
    if item.get("action") == "dry_run_skip":
        return "skip"
    res = item.get("result") or {}
    direction = res.get("direction", OTHER_DIRECTION)
    confidence = res.get("confidence", "low")
    if is_other_direction(direction):
        return "其他/待分类"
    if confidence == "high":
        return "confirmed"
    if confidence == "medium":
        return "provisional"
    return "provisional_low"


def print_progress(idx: int, total: int, item: Dict[str, Any]) -> None:
    fn = item["fund_name"]
    res = item.get("result")
    if not res:
        print(f"[{idx:>3}/{total}] {fn}  (dry-run)")
        return
    direction = res["direction"]
    confidence = res["confidence"]
    source = res["source"]
    if item.get("tavily_called"):
        web = "→ web_search"
    elif source == "web_search":
        web = "(DB 命中)"
    else:
        web = "→ rule/ctx"
    marker = _status_marker(item)
    print(
        f"[{idx:>3}/{total}] {fn}\n"
        f"   {web}\n"
        f"   → {direction}\n"
        f"   → {confidence}\n"
        f"   → {marker}\n"
        f"   → {'saved' if item.get('post', {}).get('state') != 'absent' else 'no-save'}"
    )


def print_summary(
    total_input: int,
    total_processed: int,
    by_status: Dict[str, int],
    tavily_calls: int,
    failed: int,
    elapsed_s: float,
) -> None:
    print("\n" + "=" * 60)
    print("📊 汇总")
    print("=" * 60)
    print(f"  总基金数:           {total_input}")
    print(f"  本次处理:           {total_processed}")
    print(f"  confirmed (high):   {by_status.get('confirmed', 0)}")
    print(f"  provisional (med):  {by_status.get('provisional', 0)}")
    print(f"  provisional (low):  {by_status.get('provisional_low', 0)}")
    print(f"  其他/待分类:        {by_status.get('其他/待分类', 0)}")
    print(f"  跳过:               {by_status.get('skip', 0)}")
    print(f"  失败:               {failed}")
    print(f"  Tavily 调用次数:    {tavily_calls}")
    print(f"  总耗时:             {elapsed_s:.1f}s")


# ============================================================
#  主流程
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="历史基金方向批量补全（复用 direction_resolver）"
    )
    parser.add_argument("--only-unresolved", action="store_true",
                        help="只处理 verified=False 的基金")
    parser.add_argument("--limit", type=int, default=0,
                        help="本次处理上限（0 = 全部）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只查看待处理列表")
    parser.add_argument("--retry-failed", action="store_true",
                        help="处理 needs_reverify=True / next_reverify_at 已过 的基金")
    parser.add_argument("--fund-type-hint", type=str, default=None,
                        help="强制设置 fund_type_hint（默认自动启发）")
    parser.add_argument("--output", type=str, default=None,
                        help="人工确认清单输出路径")
    args = parser.parse_args()

    init_db()

    # 1) 收集全部基金
    print("→ 收集历史 operations 中的全部基金...")
    all_funds = collect_unique_funds()
    print(f"  共 {len(all_funds)} 只唯一基金")

    # 2) 筛选
    if args.only_unresolved:
        print("→ 过滤：仅未确认基金（--only-unresolved）")
    elif args.retry_failed:
        print("→ 过滤：重处理失败基金（--retry-failed）")
    else:
        print("→ 过滤：跳过已有 verified=True")
    to_process = filter_funds_to_process(
        all_funds,
        only_unresolved=args.only_unresolved,
        retry_failed=args.retry_failed,
    )

    # 按 seen_count 降序（高曝光优先）+ 名称
    to_process.sort(key=lambda x: (-x[1].get("seen_count", 0), x[0]))

    if args.limit > 0:
        to_process = to_process[: args.limit]
    print(f"  待处理 {len(to_process)} 只")

    if args.dry_run:
        print("\n[DRY-RUN] 待处理基金列表：")
        for i, (norm, info) in enumerate(to_process, 1):
            seen = info.get("seen_count", 0)
            code = info.get("fund_code") or "-"
            print(f"  [{i:>3}] {info['fund_name']:30s}  code={code}  seen={seen}")
        return 0

    # 3) 处理
    print(f"\n→ 开始处理（{'DRY-RUN' if args.dry_run else '实际处理'}）...")
    t0 = time.time()
    by_status: Dict[str, int] = defaultdict(int)
    tavily_calls = 0
    failed = 0
    unconfirmed_for_manual: List[Dict[str, Any]] = []
    confirmed_set: List[Dict[str, Any]] = []
    pre_already: List[Dict[str, Any]] = []

    for i, (norm, info) in enumerate(to_process, 1):
        try:
            item = process_one(norm, info, dry_run=False)
        except Exception as e:  # noqa: BLE001
            logger.error("处理 %s 失败: %s", info["fund_name"], e)
            failed += 1
            continue

        print_progress(i, len(to_process), item)
        marker = _status_marker(item)
        by_status[marker] += 1
        if item["tavily_called"]:
            tavily_calls += 1
        # 收集人工确认清单
        if marker in ("其他/待分类", "provisional (low)", "provisional"):
            unconfirmed_for_manual.append({
                "fund_name": item["fund_name"],
                "fund_code": item["fund_code"],
                "direction": (item.get("result") or {}).get("direction"),
                "confidence": (item.get("result") or {}).get("confidence"),
                "source": (item.get("result") or {}).get("source"),
                "post": item.get("post"),
            })
        elif marker == "confirmed":
            confirmed_set.append({
                "fund_name": item["fund_name"],
                "fund_code": item["fund_code"],
                "direction": (item.get("result") or {}).get("direction"),
            })
        elif marker in ("provisional", "provisional_low"):
            unconfirmed_for_manual.append({
                "fund_name": item["fund_name"],
                "fund_code": item["fund_code"],
                "direction": (item.get("result") or {}).get("direction"),
                "confidence": (item.get("result") or {}).get("confidence"),
                "source": (item.get("result") or {}).get("source"),
                "post": item.get("post"),
            })
        elif marker == "skip":
            pre_already.append({
                "fund_name": item["fund_name"],
                "pre_state": item.get("pre"),
            })

    elapsed = time.time() - t0

    # 4) 汇总
    print_summary(
        total_input=len(all_funds),
        total_processed=len(to_process) - by_status.get("skip", 0),
        by_status=by_status,
        tavily_calls=tavily_calls,
        failed=failed,
        elapsed_s=elapsed,
    )

    # 5) 输出待人工确认清单
    output_path = args.output or os.path.join(
        BASE_DIR, "output",
        f"backfill_unconfirmed_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md",
    )
    _write_unconfirmed_list(
        output_path,
        unconfirmed_for_manual=unconfirmed_for_manual,
        confirmed_set=confirmed_set,
        all_funds=all_funds,
    )
    print(f"\n→ 待人工确认清单: {output_path}")

    # 6) 输出重新验证候选（仅显示，不自动处理）
    reverify = build_reverify_candidates()
    if reverify:
        print(f"\n→ 未来需要重新验证的基金: {len(reverify)} 只")
        print("  触发条件：其他/待分类、provisional、主动/混合证据超过 2 个季度、needs_reverify=true")
        for it in reverify[:5]:
            print(f"  - {it['fund_name']:30s}  reasons={','.join(it['reasons'])}")
        if len(reverify) > 5:
            print(f"  ... 还有 {len(reverify)-5} 只")

    return 0


def _write_unconfirmed_list(
    path: str,
    unconfirmed_for_manual: List[Dict[str, Any]],
    confirmed_set: List[Dict[str, Any]],
    all_funds: Dict[str, Dict[str, Any]],
) -> None:
    """输出 markdown 格式的待人工确认清单。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Backfill 待人工确认清单\n\n")
        f.write(f"生成时间：{datetime.now().isoformat(timespec='seconds')}\n\n")
        f.write(f"## 统计\n\n")
        f.write(f"- 总基金数：{len(all_funds)}\n")
        f.write(f"- 本次自动确认（high）：{len(confirmed_set)}\n")
        f.write(f"- 本次待人工确认：{len(unconfirmed_for_manual)}\n\n")

        f.write("## 已自动确认（high confidence）\n\n")
        if confirmed_set:
            f.write("| 基金 | 基金代码 | 方向 |\n|---|---|---|\n")
            for it in confirmed_set:
                f.write(f"| {it['fund_name']} | {it.get('fund_code') or '-'} | {it.get('direction')} |\n")
        else:
            f.write("（无）\n")
        f.write("\n")

        f.write("## 待人工确认（low / 其他/待分类）\n\n")
        if unconfirmed_for_manual:
            f.write("| # | 基金 | 基金代码 | 当前方向 | confidence | 来源 | 备注 |\n")
            f.write("|---|---|---|---|---|---|---|\n")
            for i, it in enumerate(unconfirmed_for_manual, 1):
                post = it.get("post") or {}
                f.write(
                    f"| {i} | {it['fund_name']} | {it.get('fund_code') or '-'} | "
                    f"{it.get('direction') or '-'} | {it.get('confidence') or '-'} | "
                    f"{it.get('source') or '-'} | "
                    f"fund_type={post.get('fund_type_detail') or '-'} | "
                    f"period={post.get('evidence_period') or '-'} |\n"
                )
        else:
            f.write("（无）\n")

        f.write("\n## 建议操作\n\n")
        f.write("1. 人工核对 `fund_name` 与基金实际投资方向\n")
        f.write("2. 调用 `POST /api/funds/confirm` 写回 verified=true\n")
        f.write("3. 主动/混合基金可等待下一季度报告后再次 backfill\n")


if __name__ == "__main__":
    sys.exit(main())
