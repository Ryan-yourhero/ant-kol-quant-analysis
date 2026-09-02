"""
历史上下文服务 — HistoricalContextService
=========================================

职责：给定分析日 + 今日解析出的交易记录，从 MySQL 查询最近 N 个「有效采集日期」
的历史交易，用 Python 聚合生成结构化 JSON，供每日分析 AI 做「今日 vs 近7日」对比。

核心口径（与需求一致）：
- 历史窗口 = 分析日之前的最近 N 个「有效采集日期」，而非连续自然日。
- 有效采集日期：结合 crawl_runs，同一 collect_date 优先 success/completed 的 run，
  否则取最新（id 最大）的 run，避免开发期间同一天多次运行导致历史交易重复计算。
- 买入类 = 买入 + 定投（buy_amount 计入资金流入，原始 operation_type 保留）。
- 卖出类只统计 sell_shares（份额），且不跨基金求和；跨基金只比较
  sell_operation_count / sell_days / sell_kol_count 等可比指标。
- 7 日平均金额区分两种口径：avg_daily_buy_amount（含 0 买入日）、
  avg_buy_amount_on_buy_days（仅买入日）。
- 方向分类：无法明确命中关键词时归「其他」，不强行归类。

不直接暴露 LLM 访问 MySQL；本服务负责查询 + 聚合，输出 JSON 交给上层分析。
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
from src.parser.direction_classifier import classify_fund  # noqa: E402
from src.parser.models import TradeRecord  # noqa: E402

logger = logging.getLogger("backend.historical_context_service")

WINDOW_DAYS = HISTORICAL_THRESHOLDS["window_days"]
MIN_HISTORY_DAYS = HISTORICAL_THRESHOLDS["min_history_days"]
ENHANCE_RATIO = HISTORICAL_THRESHOLDS["enhance_ratio"]
NORMAL_RATIO_LOW = HISTORICAL_THRESHOLDS["normal_ratio_low"]
COOL_DOWN_PCT = HISTORICAL_THRESHOLDS["cool_down_pct"]
MIN_DIRECTION_BUY_AMOUNT = HISTORICAL_THRESHOLDS["min_direction_buy_amount"]


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
    """清洗金额/份额：去千分位逗号，占位符归 None。"""
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


def _is_buy(operation_type: Optional[str]) -> bool:
    return operation_type in BUY_OPERATION_TYPES


def _is_sell(operation_type: Optional[str]) -> bool:
    return operation_type in SELL_OPERATION_TYPES


def _pct_change(today: float, base: Optional[float]) -> Optional[float]:
    """(today - base) / base * 100，base 缺失或为 0 时返回 None。"""
    if base is None or base == 0:
        return None
    return round((today - base) / base * 100, 1)


# ============================================================
#  有效采集日期
# ============================================================

def _valid_history_runs(session, analysis_date: date) -> List[Tuple[int, date]]:
    """返回分析日之前的最近 N 个有效采集 run（按日期升序）。

    同一 collect_date 若有多个 crawl_run：
      - 优先 status in ('success', 'completed') 的 run
      - 否则取 id 最大的 run（最新）
    通过返回「有效 run_id」而非日期，从根本上避免同一天多次运行导致历史交易重复计算。
    """
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
#  历史聚合
# ============================================================

def _load_history_operations(session, run_ids: List[int]):
    """查询这些有效 run 的 operations。

    返回 [(kol_name, collect_date, operation_type, fund_name, buy_amount, sell_shares), ...]
    """
    from src.storage.models import Operation

    ops = (
        session.query(Operation)
        .filter(Operation.crawl_run_id.in_(run_ids))
        .all()
    )
    rows = []
    for o in ops:
        kol_name = o.kol.name if o.kol else None
        if not kol_name:
            continue
        rows.append(
            (
                kol_name,
                o.collect_date,
                o.operation_type,
                o.fund_name,
                o.buy_amount,
                o.sell_shares,
            )
        )
    return rows


def _aggregate_history(rows, dates: List[date]):
    """把历史原始行聚合为按大V/方向的结构。"""
    kol_daily_buy: Dict[str, Dict[date, float]] = defaultdict(lambda: defaultdict(float))
    kol_daily_sell: Dict[str, Dict[date, List[Tuple[str, Optional[float]]]]] = defaultdict(lambda: defaultdict(list))
    # 大V × 方向 × 日期 → 买入金额 / 买卖标记
    kol_dir_day: Dict[str, Dict[str, Dict[date, Dict[str, object]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(lambda: {"buy": False, "sell": False, "buy_amount": 0.0}))
    )
    direction_daily: Dict[str, Dict[date, Dict]] = defaultdict(
        lambda: defaultdict(lambda: {"kols": set(), "buy_amount": 0.0})
    )
    kol_sell_ops: Dict[str, List[dict]] = defaultdict(list)
    kol_buy_op_count: Dict[str, int] = defaultdict(int)
    kol_sell_op_count: Dict[str, int] = defaultdict(int)

    date_set = set(dates)

    for kol_name, d, op_type, fund_name, buy_amount, sell_shares in rows:
        if d not in date_set:
            continue
        direction = classify_fund(fund_name)[0]

        if _is_buy(op_type):
            amt = _to_float(buy_amount) or 0.0
            kol_daily_buy[kol_name][d] += amt
            kol_buy_op_count[kol_name] += 1
            cell = kol_dir_day[kol_name][direction][d]
            cell["buy"] = True
            cell["buy_amount"] = float(cell["buy_amount"]) + amt
            direction_daily[direction][d]["kols"].add(kol_name)
            direction_daily[direction][d]["buy_amount"] += amt
        elif _is_sell(op_type):
            shares = _to_float(sell_shares)
            kol_sell_op_count[kol_name] += 1
            kol_daily_sell[kol_name][d].append((fund_name or "", shares))
            kol_dir_day[kol_name][direction][d]["sell"] = True
            if shares is not None:
                kol_sell_ops[kol_name].append(
                    {"fund_name": fund_name or "", "sell_shares": shares, "collect_date": d.isoformat()}
                )
        # 撤销等其它类型：不参与金额/份额统计

    return kol_daily_buy, kol_daily_sell, kol_dir_day, direction_daily, kol_sell_ops, kol_buy_op_count, kol_sell_op_count


# ============================================================
#  今日聚合
# ============================================================

def _aggregate_today(records: List[TradeRecord]):
    kol_today: Dict[str, dict] = defaultdict(
        lambda: {"buy_amount": 0.0, "buy_ops": 0, "sell_ops": 0, "op_count": 0, "directions": set(), "has_sell": False}
    )
    direction_today: Dict[str, dict] = defaultdict(
        lambda: {"kols": set(), "sell_kols": set(), "buy_amount": 0.0}
    )

    for r in records:
        kol = (r.kol_name or "").strip()
        if not kol:
            continue
        direction = classify_fund(r.fund_name)[0]

        kol_today[kol]["op_count"] += 1
        kol_today[kol]["directions"].add(direction)

        if _is_buy(r.operation_type):
            amt = _to_float(r.buy_amount) or 0.0
            kol_today[kol]["buy_amount"] += amt
            kol_today[kol]["buy_ops"] += 1
            direction_today[direction]["kols"].add(kol)
            direction_today[direction]["buy_amount"] += amt
        elif _is_sell(r.operation_type):
            kol_today[kol]["sell_ops"] += 1
            kol_today[kol]["has_sell"] = True
            direction_today[direction]["sell_kols"].add(kol)

    return kol_today, direction_today


# ============================================================
#  连续行为 / 状态标签
# ============================================================

def _compute_continuity(kol_name, direction, dates, analysis_date, kol_dir_day, today_buy, today_sell):
    """计算某大V×方向的连续行为指标。

    连续按「有效采集日期序列」（历史 dates + 今天）判断，不是按大V有操作的日期。
    days_since_last_buy/sell 按自然日差。
    """
    hist_buy = []
    hist_sell = []
    for d in dates:
        cell = kol_dir_day.get(kol_name, {}).get(direction, {}).get(d)
        hist_buy.append(bool(cell.get("buy")) if cell else False)
        hist_sell.append(bool(cell.get("sell")) if cell else False)

    # 含今天的完整序列
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
    """基于确定规则生成状态标签（不交给 AI 自由判断）。"""
    if history_days < MIN_HISTORY_DAYS:
        return "历史样本不足"

    # 同方向今天既有买入又有卖出 → 买卖并存，不能误判为「由买转卖」
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


def _direction_signal(today_kol_count: int, today_sell_kol_count: int, buy_change_pct: Optional[float]) -> str:
    """方向信号类型（Python 计算，AI 引用，不重算）。"""
    # 实际卖出是最明确的信号，优先判断
    if today_sell_kol_count >= 2:
        return "明确减仓"
    if today_sell_kol_count >= 1:
        return "分歧" if today_kol_count >= 1 else "明确减仓"

    if today_kol_count == 0:
        return "无信号"
    if today_kol_count == 1:
        return "个体行为"

    # >= 2 个大V买入
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
    """方向置信度（Python 计算）。"""
    if direction == "其他" or today_kol_count <= 1 or today_buy < MIN_DIRECTION_BUY_AMOUNT:
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
#  主入口
# ============================================================

def build(analysis_date, today_records: List[TradeRecord], today_complete: bool = True) -> dict:
    """生成历史上下文 JSON。"""
    from src.storage.db_storage import _get_session, is_configured as mysql_configured

    ad = _parse_date(analysis_date)
    kol_today, direction_today = _aggregate_today(today_records)

    result: Dict = {
        "analysis_date": ad.isoformat(),
        "history_days_available": 0,
        "history_dates": [],
        "history_insufficient": False,
        "kols": [],
        "directions": [],
    }

    if not mysql_configured():
        logger.warning("MySQL 未配置，历史上下文为空")
        return result

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
        rows = _load_history_operations(session, run_ids)
    finally:
        session.close()

    (
        kol_daily_buy,
        kol_daily_sell,
        kol_dir_day,
        direction_daily,
        kol_sell_ops,
        kol_buy_op_count,
        kol_sell_op_count,
    ) = _aggregate_history(rows, dates)

    # ---- 大V维度 ----
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
        active_days = sum(1 for d in dates if buy_by_day.get(d, 0.0) > 0 or sell_by_day.get(d))

        avg_daily = round(total_buy / history_days, 2) if history_days else 0.0
        avg_on_buy_days = round(total_buy / buy_days, 2) if buy_days else None
        median_daily = round(statistics.median(daily_buy_values), 2) if history_days else 0.0

        # ---- 方向维度（大V × 方向） ----
        directions_out = []
        all_dirs = set(t.get("directions", set()))
        for d in dates:
            all_dirs |= set(kol_dir_day.get(kol, {}).keys())

        for direction in sorted(all_dirs):
            dir_today_buy = _direction_today_buy(today_records, kol, direction)
            dir_today_sell = _direction_today_sell(today_records, kol, direction)

            cont = _compute_continuity(
                kol, direction, dates, ad, kol_dir_day, dir_today_buy > 0, dir_today_sell
            )

            dir_cells = kol_dir_day.get(kol, {}).get(direction, {})
            dir_daily_buy = [float(dir_cells.get(d, {}).get("buy_amount", 0.0)) for d in dates]
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

            directions_out.append(
                {
                    "direction": direction,
                    "consecutive_buy_days": cont["consecutive_buy_days"],
                    "consecutive_sell_days": cont["consecutive_sell_days"],
                    "last_operation_direction": cont["last_operation_direction"],
                    "days_since_last_buy": cont["days_since_last_buy"],
                    "days_since_last_sell": cont["days_since_last_sell"],
                    "direction_today_buy_amount": dir_today_buy,
                    "direction_avg_daily_buy_amount": dir_avg_daily,
                    "status_label": label,
                }
            )

        cmp_avg = _pct_change(today_buy, avg_daily)
        cmp_median = _pct_change(today_buy, median_daily)

        kols_out.append(
            {
                "kol_name": kol,
                "history_days_available": history_days,
                "today": {
                    "buy_amount": round(today_buy, 2),
                    "buy_operation_count": t.get("buy_ops", 0),
                    "sell_operation_count": t.get("sell_ops", 0),
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
            }
        )

    result["kols"] = kols_out

    # ---- 方向维度 ----
    directions_out = []
    all_dirs = set(direction_today.keys()) | set(direction_daily.keys())
    for direction in sorted(all_dirs):
        dt = direction_today.get(direction, {"kols": set(), "sell_kols": set(), "buy_amount": 0.0})
        today_kol_count = len(dt.get("kols", set()))
        today_sell_kol_count = len(dt.get("sell_kols", set()))
        today_buy = dt.get("buy_amount", 0.0)

        daily_kol_counts = []
        daily_buy_amounts = []
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
        avg_kol_count = round(sum(daily_kol_counts) / history_days, 2) if history_days else 0.0

        kol_change_pct = _pct_change(today_kol_count, avg_kol_count)
        buy_change_pct = _pct_change(today_buy, avg_daily_buy)

        signal_type = _direction_signal(today_kol_count, today_sell_kol_count, buy_change_pct)
        confidence = _direction_confidence(direction, today_kol_count, today_buy, kol_change_pct, buy_change_pct)

        directions_out.append(
            {
                "direction": direction,
                "today": {
                    "buy_kol_count": today_kol_count,
                    "sell_kol_count": today_sell_kol_count,
                    "buy_amount": round(today_buy, 2),
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
            }
        )

    result["directions"] = directions_out
    return result


def _direction_today_buy(records: List[TradeRecord], kol: str, direction: str) -> float:
    total = 0.0
    for r in records:
        if (r.kol_name or "").strip() != kol:
            continue
        if classify_fund(r.fund_name)[0] != direction:
            continue
        if _is_buy(r.operation_type):
            total += _to_float(r.buy_amount) or 0.0
    return round(total, 2)


def _direction_today_sell(records: List[TradeRecord], kol: str, direction: str) -> bool:
    for r in records:
        if (r.kol_name or "").strip() != kol:
            continue
        if classify_fund(r.fund_name)[0] != direction:
            continue
        if _is_sell(r.operation_type):
            return True
    return False
