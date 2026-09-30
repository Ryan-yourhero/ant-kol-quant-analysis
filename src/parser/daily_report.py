"""
每日 AI 分析报告生成器（v3 — 严格 Source of Truth）
==================================================

在「采集 → screen_dump MD → AI 解析 → 成表(Excel)」之后，
基于已生成的 TradeRecord 列表，调用 LLM 生成一份每日复盘分析报告（Markdown）。

v3 关键约束：
  - 每笔交易 direction 已由 Python 在调用 generate_daily_report 之前固化
  - 报告输入必须包含"每笔交易的 direction"（per_record_assertion）
  - LLM 禁止重算 direction、禁止重算百分比、禁止重算金额
  - 所有数值/状态标签/置信度必须原样引用输入
"""

from __future__ import annotations

import json
import os
import re
import datetime
import logging
from typing import Any, List, Optional

from .llm_client import chat_completion, is_configured
from .models import TradeRecord

logger = logging.getLogger("parser.daily_report")


# ============================================================
#  系统提示词（每日复盘分析）
# ============================================================

DAILY_REPORT_SYSTEM_PROMPT = """一、角色
你是理财社区"理财盘友圈"每日复盘分析员。所有事实（基金方向、人数、金额、百分比）都已由 Python 预处理完毕并随输入给出；你负责把数据组织成一份完整、可读的每日复盘报告（Markdown）。

二、报告禁止出现的内容（强制）
1. **不要出现任何代码风格的字段名、键值对或 JSON 表达式**（如 `sell_kol_count=0`、`buy_amount_trend=-80%`、`conversion_out` 等）。所有数据用自然语言描述，零值用"无 / 为零 / 为 0"表述。
2. **不要出现"数据完整性""采集完整""采集正常到底""crawl_status""stop_type""bottom""expand_remaining""数据完整"等任何与采集状态相关的字眼**。系统对正常采集情况不会提供采集状态元数据；只有异常时才可能给出 `data_warning`，那时才在「风险提示」用自然语言描述。
3. **不要把"其他/待分类"作为正式投资方向出现在报告任何位置**（方向汇总、推荐、趋势表中均不允许）。Python 已过滤；LLM 不得脑补。
4. **不要新增、删除、合并、拆分方向**。「方向汇总」「近7日趋势」「买入推荐」「卖出推荐」中出现的 direction 必须完全等于输入 `allowed_directions` 列表中的元素。
5. **禁止根据基金名称、动态正文、大V观点、历史数据自行推断方向**。每条交易的 direction 已在输入表格的「投资方向」列直接给出，原样使用。

三、输入结构
- 原始数据表：每行一笔操作，列含「投资方向」。
- 推荐候选方向 buy_candidates / sell_candidates：已剔除「待确认」/「其他/待分类」。
- 近7日历史对比 JSON：含 directions 与 kols。
- allowed_directions：本次报告允许出现的全部 direction 字符串列表。
- unmapped_funds：未命中 fund_direction_master 主库的基金名+大V+操作清单（用于「待确认基金」提示，不参与方向汇总）。

四、转换操作语义
- 主动卖出：按真实减仓信号处理
- 转换转出（如"由电网方向转换至港股方向"）：描述为"从 X 方向转换至 Y 方向"，**禁止**拆成"卖出 X + 买入 Y"；不计入"卖出强度/减仓"

五、状态标签与共识口径（按大V人数判断）
- 每个方向的状态标签按以下规则判断：
  - 仅 1 位大V参与（无论买入或卖出）→ "个体行为"
  - 同一方向同时存在买入和卖出的大V → "分歧"
  - 2 位及以上大V仅买入 → "弱共识"（3 位及以上可写"共识"）
- 主动卖出与转换转出区分：转换转出不计入"卖出强度/减仓"。

六、近7日对比的两层口径（禁止混用）
- 大V整体口径：kols[].last_7d.avg_daily_buy_amount（用于「核心大V操作详解」）
- 大V×方向口径：kols[].directions[].direction_avg_daily_buy_amount（用于「近7日趋势变化」表）
- 两者是不同层级，数值通常不同。百分比变化率必须严格对应当前引用的基准口径。

七、买入推荐
只能从 buy_candidates 挑选，最多 3 个。每个推荐项第一行写 `N. **方向**（状态标签，置信度低/中/高）`，随后用自然语言段落说明：今日有几位大V买入、合计买入金额、近7日该方向日均买入及环比变化、参考大V及其观点。**禁止**使用"今日买入人数：""今日买入金额：""置信度：""信号类型："等带冒号的字段名或键值对。

八、卖出推荐
只能从 sell_candidates 挑选，最多 3 个。每个推荐项格式同买入推荐：`N. **方向**（状态标签，置信度低/中/高）` + 自然语言段落。转换转出不与主动卖出等权处理；只有卖出份额没有金额时，用"卖出份额""连续卖出天数"等自然语言描述，**不要估算金额**。**禁止**出现"信号类型""置信度"等字段名。

九、输出结构（严格按此顺序与标题，输出完整 Markdown）
### 一、今日总体判断
（开头注明：今日共采集 X 位大V、Y 条操作记录，具体数字从「今日采集概况」引用。行情描述必须标注来源，如"从采集到的大V观点看……"。）
### 二、方向汇总
（从输入 directions 引用，按今日人数从高到低排序；direction 必须在 allowed_directions 内。）
### 三、近7日趋势变化
（大V×方向口径；direction 必须在 allowed_directions 内。）
### 四、买入推荐
（最多 3 个；方向必须来自 buy_candidates。）
### 五、卖出推荐
（最多 3 个；方向必须来自 sell_candidates。）
### 六、风险提示
（只基于：真实交易、历史统计、输入观点；如有 data_warning，用自然语言标注"采集可能提前终止，数据可能不完整"。）
### 七、待确认基金
（如有 unmapped_funds，列出大V+基金名+操作类型，并明确"这些交易保留在原始记录中，不参与方向汇总与推荐"。如无 unmapped_funds，本节写"无"。）
### 八、核心大V操作详解
（按大V分组，逐笔列出操作+观点摘要。所有 direction 必须原样引用输入表格里的值。）
"""



# ============================================================
#  记录 → Markdown 表格
# ============================================================

_REPORT_COLUMNS = [
    ("大V昵称", "kol_name"),
    ("收益率周期", "yield_period"),
    ("收益率", "yield_rate"),
    ("发布时间", "publish_time"),
    ("动态正文", "opinion_text"),
    ("操作类型", "operation_type"),
    ("操作状态", "operation_status"),
    ("基金名称", "fund_name"),
    ("方向", "direction"),
    ("买入金额(元)", "buy_amount"),
    ("卖出份额(份)", "sell_shares"),
    ("转换前基金名称", "convert_from_fund"),
    ("转换后基金名称", "convert_to_fund"),
    ("转发数", "repost_count"),
    ("评论数", "comment_count"),
    ("点赞数", "like_count"),
    ("求解读人数", "seek_interpret_count"),
    ("采集时间", "collect_time"),
    ("今日操作条数", "today_operation_count"),
]


def _cell(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    s = s.replace("|", "\\|").replace("\n", " ")
    return s


def records_to_markdown(records: List[TradeRecord]) -> str:
    """把 TradeRecord 列表序列化为 Markdown 表格文本，作为 LLM 输入。"""
    headers = [c[0] for c in _REPORT_COLUMNS]
    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("|" + "---|" * len(headers))

    for r in records:
        d = r.model_dump()
        cells = [_cell(d.get(key)) for _, key in _REPORT_COLUMNS]
        lines.append("| " + " | ".join(cells) + " |")

    return "\n".join(lines)


# ============================================================
#  主流程
# ============================================================

def generate_daily_report(
    records: List[TradeRecord],
    historical_context: Optional[dict] = None,
) -> Optional[str]:
    """调用 LLM 生成每日复盘分析报告，返回 Markdown 文本。

    Args:
        historical_context: 由 HistoricalContextService.build() 生成的结构化 JSON，
                            包含 kols / directions / consistency_check 等。
    """
    if not records:
        logger.warning("无记录，跳过每日分析报告")
        return None

    if not is_configured():
        logger.warning("LLM 未配置，跳过每日分析报告")
        return None

    table = records_to_markdown(records)

    kol_names = {r.kol_name for r in records if r.kol_name}
    kol_count = len(kol_names)

    # 抽取每笔 direction 来源（来自 historical_context.consistency_check 中
    # 隐含的 per_record；若未传则需要上游 build 时已固化）
    per_record_lines = []
    if historical_context:
        # historical_context 不直接暴露 per_record 列表（控制体积）；
        # 但每笔 direction 在一致性校验 items 里只到 kol/direction 级别。
        # 详情方向分类由上游调用方在调用前完成，本生成器不重新分类。
        per_record_lines = [
            "（每笔交易的 direction / source / confidence / evidence 已由前置 Python 固化，",
            "  详见 `historical_context.consistency_check.items` 中涉及的大V×方向汇总，",
            "  以及各 kol.directions[].direction_today_buy_amount 的归属。）",
        ]
    else:
        per_record_lines = [
            "（未提供历史上下文；无法给出每笔 direction 的归属证据，",
            "  报告生成前请确保调用 HistoricalContextService.build() 完成方向分类固化。）",
        ]

    parts = [
        "以下数据已由前置流程解析成表（每行一笔操作，字段见表头），",
        "请据此直接输出每日复盘分析报告（按「九、输出结构」）。",
        "",
        f"## 今日采集概况",
        f"- 今日参与大V数量：**{kol_count} 位**",
        f"- 今日操作记录总数：**{len(records)} 条**",
        "",
        "## 今日原始数据表（含「投资方向」列 — 每笔交易的最终方向已固化）",
        table,
        "",
        "## 每笔交易的方向归属（Source of Truth）",
        *per_record_lines,
    ]

    # allowed_directions + unmapped_funds —— LLM 方向汇总的唯一白名单 + 待确认基金清单
    allowed_directions: List[str] = []
    unmapped_funds: List[Dict[str, Any]] = []
    for r in records:
        d = getattr(r, "direction", None) or "待确认"
        if d in ("待确认", "其他/待分类"):
            unmapped_funds.append({
                "kol": r.kol_name or "-",
                "fund": r.fund_name or "-",
                "operation": r.operation_type or "-",
            })
        elif d not in allowed_directions:
            allowed_directions.append(d)

    # LLM 只需要总览 + 方向白名单 + 推荐候选 + 历史方向 Top10；不再传 records 表
    parts += [
        "",
        "## 今日数据概览（来自 Python 聚合）",
        f"- 参与大V数量：**{kol_count} 位**",
        f"- 操作总笔数：**{len(records)} 条**",
        f"- 主库命中笔数：**{sum(1 for r in records if getattr(r, 'direction', None) not in (None, '待确认', '其他/待分类'))} 条**",
        f"- 待确认基金笔数：**{sum(1 for r in records if getattr(r, 'direction', None) in ('待确认', '其他/待分类'))} 条**",
        "",
        "## allowed_directions（白名单）",
        "```json",
        json.dumps(sorted(allowed_directions), ensure_ascii=False, indent=2),
        "```",
        "**严禁**新增、删除、合并、拆分方向；**严禁**使用列表之外的方向名。",
        "",
        "## unmapped_funds（仅用于「待确认基金」附录；不参与方向汇总）",
        "```json",
        json.dumps(unmapped_funds, ensure_ascii=False, indent=2),
        "```",
    ]

    # 推荐候选 + 完整历史上下文（用于方向汇总 / 近7日趋势 / 买入卖出推荐 / 核心大V操作详解）
    if historical_context:
        import json as _json

        buy_cands = historical_context.get("recommend_buy_candidates", [])
        sell_cands = historical_context.get("recommend_sell_candidates", [])
        parts += [
            "",
            "## 推荐候选方向（Python 预筛选，已剔除「其他/待分类」）",
            "买入推荐 / 卖出推荐 **只能**从下列候选方向中挑选（最多 3 个）：",
            "```json",
            _json.dumps(
                {"buy_candidates": buy_cands, "sell_candidates": sell_cands},
                ensure_ascii=False, indent=2, default=str,
            ),
            "```",
        ]

        parts += [
            "",
            "## 近7日历史对比数据",
            "以下是前置 Python 聚合计算出的结构化历史对比数据（JSON），",
            "请结合它判断「今日 vs 近7日」的相对强弱，不要只凭今日绝对金额下结论。",
            "**禁止自行重算百分比 / 金额 / 方向。**",
            "```json",
            _json.dumps(historical_context, ensure_ascii=False, indent=2, default=str),
            "```",
        ]

    # data_warning
    if historical_context and historical_context.get("data_warning"):
        parts += [
            "",
            "## data_warning",
            historical_context["data_warning"],
        ]

    user_content = "\n".join(parts)

    logger.info(
        "生成每日复盘分析报告: %d 条记录, %d 字符, 历史上下文=%s",
        len(records),
        len(table),
        "有" if historical_context else "无",
    )

    result = chat_completion(
        [
            {"role": "system", "content": DAILY_REPORT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0.3,
        max_tokens=8192,
        timeout=300,
    )

    if not result["ok"]:
        logger.error("每日分析报告生成失败: %s", result["error"])
        return None

    return _sanitize_report(result["content"])


# ============================================================
#  后置过滤：禁用关键字兜底删除
# ============================================================

# 报告正文（面向用户）禁止出现的关键字
_FORBIDDEN_KEYWORDS = [
    "数据完整性",
    "采集完整",
    "采集正常到底",
    "数据完整",
    "crawl_status",
    "stop_type",
    "bottom_detected",
    "expand_remaining",
    # 「其他/待分类」作方向名时禁止出现
    "其他/待分类",
    # 推荐/趋势中禁止出现字段名标签（用户已要求删除）
    "信号类型",
    "置信度：",
    "置信度:",
]


def _sanitize_report(text: str) -> str:
    """LLM 输出兜底：删除包含禁用关键字的整行。"""
    if not text:
        return text
    out: List[str] = []
    for ln in text.split("\n"):
        if any(kw in ln for kw in _FORBIDDEN_KEYWORDS):
            logger.warning("[sanitize] 删除禁用行: %s", ln[:80])
            continue
        out.append(ln)
    return "\n".join(out)


def _resolve_output_dir() -> str:
    """返回 output 目录绝对路径。"""
    parser_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(parser_dir))
    return os.path.join(project_root, "output")


def export_daily_report(
    report_text: str,
    output_dir: Optional[str] = None,
    date_str: Optional[str] = None,
) -> str:
    """把报告文本保存到 output/daily_report_YYYYMMDD.md，返回文件路径。"""
    if output_dir is None:
        output_dir = _resolve_output_dir()
    os.makedirs(output_dir, exist_ok=True)

    if date_str:
        cleaned = date_str.replace("-", "")
        if len(cleaned) == 8 and cleaned.isdigit():
            date_str = cleaned
        else:
            date_str = None

    if not date_str:
        date_str = datetime.date.today().strftime("%Y%m%d")

    report_path = os.path.join(output_dir, f"daily_report_{date_str}.md")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)

    logger.info("每日分析报告（Markdown）已保存: %s", report_path)
    return report_path


def build_structured_report(
    records: List[TradeRecord],
    historical_context: Optional[dict],
    report_text: Optional[str],
) -> Dict[str, Any]:
    """Python 端直接产出结构化报告：summary_cards / tables / chart_data / appendix。

    完全不依赖 LLM，结构稳定。
    """
    kols: Dict[str, Dict[str, Any]] = {}
    op_dist: Dict[str, int] = {
        "买入": 0, "卖出": 0, "定投": 0, "转换转入": 0, "转换转出": 0, "撤销": 0, "其他": 0,
    }
    pending_rows: List[Dict[str, Any]] = []
    mapped_count = 0
    pending_count = 0

    # kol/direction 聚合
    by_direction_people: Dict[str, set] = {}
    by_direction_amount: Dict[str, float] = {}
    kol_buy_sell: Dict[str, Dict[str, float]] = {}

    for r in records:
        kol = (r.kol_name or "未知KOL").strip() or "未知KOL"
        ot = (r.operation_type or "").strip()
        remark = (r.remark or "").strip()
        direction = getattr(r, "direction", None) or "待确认"
        verified = bool(getattr(r, "direction_verified", False))

        if ot in ("买入", "定投"):
            op_dist["买入" if ot == "买入" else "定投"] += 1
            if remark == "转换":
                op_dist["转换转入"] += 1
        elif ot == "卖出":
            op_dist["卖出"] += 1
            if remark == "转换":
                op_dist["转换转出"] += 1
        elif ot == "撤销":
            op_dist["撤销"] += 1
        else:
            op_dist["其他"] += 1

        if direction in ("待确认", "其他/待分类"):
            pending_count += 1
            pending_rows.append({
                "kol": kol,
                "fund": r.fund_name or "-",
                "operation": ot or "-",
                "reason": _guess_pending_reason(r.fund_name or ""),
                "status": "待人工处理",
            })
        else:
            mapped_count += 1
            by_direction_people.setdefault(direction, set()).add(kol)
            amt = 0.0
            try:
                amt = float(r.buy_amount or 0)
            except (TypeError, ValueError):
                amt = 0.0
            if ot in ("买入", "定投"):
                by_direction_amount[direction] = by_direction_amount.get(direction, 0.0) + amt
                kol_buy_sell.setdefault(kol, {"buy": 0.0, "sell": 0.0})
                if remark == "转换":
                    kol_buy_sell[kol]["buy"] += 0.0
                else:
                    kol_buy_sell[kol]["buy"] += amt

        kol_set = kols.setdefault(kol, {"ops": 0, "buy": 0, "sell": 0})
        kol_set["ops"] += 1

    # 方向汇总表
    direction_people_table = sorted(
        [
            {"rank": i + 1, "direction": d, "kol_count": len(by_direction_people[d])}
            for i, d in enumerate(
                sorted(by_direction_people.keys(),
                       key=lambda x: (-len(by_direction_people[x]), -by_direction_amount.get(x, 0.0)))
            )
        ],
        key=lambda d: d["rank"],
    )

    direction_amount_table = sorted(
        [
            {"rank": i + 1, "direction": d, "buy_amount": round(by_direction_amount.get(d, 0.0), 2),
             "main_kols": ", ".join(sorted(by_direction_people[d]))}
            for i, d in enumerate(
                sorted(by_direction_amount.keys(), key=lambda x: -by_direction_amount[x])
            )
        ],
        key=lambda d: d["rank"],
    )

    # 趋势表
    trend_table: List[Dict[str, Any]] = []
    if historical_context and historical_context.get("directions"):
        for d in historical_context["directions"]:
            trend_table.append({
                "direction": d["direction"],
                "today_kol_count": d["today"]["buy_kol_count"],
                "today_buy_amount": d["today"]["buy_amount"],
                "avg_7d": d["last_7d"]["avg_daily_buy_amount"],
                "change_pct": d["comparison"]["buy_amount_change_pct"],
            })

    # 推荐表
    buy_recommend_table: List[Dict[str, Any]] = []
    sell_recommend_table: List[Dict[str, Any]] = []
    if historical_context:
        for c in historical_context.get("recommend_buy_candidates", []):
            buy_recommend_table.append({
                "direction": c["direction"],
                "today_buy_kol_count": c.get("today_buy_kol_count", 0),
                "today_buy_amount": c.get("today_buy_amount", 0),
                "buy_change_pct": c.get("buy_amount_change_pct"),
            })
        for c in historical_context.get("recommend_sell_candidates", []):
            sell_recommend_table.append({
                "direction": c["direction"],
                "today_sell_kol_count": c.get("today_sell_kol_count", 0),
            })

    # 核心大V操作
    kol_ops_table: List[Dict[str, Any]] = []
    for kol in sorted({(r.kol_name or "未知KOL") for r in records}):
        rows = [
            {
                "time": r.publish_time or "",
                "operation": r.operation_type or "-",
                "fund": r.fund_name or "-",
                "amount_or_shares": (r.buy_amount or r.sell_shares or "-"),
                "is_conversion": "是" if (r.remark or "") == "转换" else "否",
                "direction": getattr(r, "direction", None) or "待确认",
                "remark": r.remark or "",
            }
            for r in records if (r.kol_name or "未知KOL") == kol
        ]
        kol_ops_table.append({"kol": kol, "rows": rows})

    overview_table = [
        {"label": "参与大V数", "value": len(kols)},
        {"label": "操作总笔数", "value": len(records)},
        {"label": "买入笔数", "value": op_dist["买入"]},
        {"label": "卖出笔数", "value": op_dist["卖出"]},
        {"label": "定投笔数", "value": op_dist["定投"]},
        {"label": "转换转入", "value": op_dist["转换转入"]},
        {"label": "转换转出", "value": op_dist["转换转出"]},
        {"label": "撤销笔数", "value": op_dist["撤销"]},
        {"label": "主库命中笔数", "value": mapped_count},
        {"label": "待确认基金笔数", "value": pending_count},
    ]
    summary_cards = [
        {"title": "参与大V", "value": len(kols), "unit": "位"},
        {"title": "操作总笔数", "value": len(records), "unit": "笔"},
        {"title": "买入笔数", "value": op_dist["买入"], "unit": "笔"},
        {"title": "卖出笔数", "value": op_dist["卖出"], "unit": "笔"},
        {"title": "定投笔数", "value": op_dist["定投"], "unit": "笔"},
        {"title": "转换转入", "value": op_dist["转换转入"], "unit": "笔"},
        {"title": "转换转出", "value": op_dist["转换转出"], "unit": "笔"},
        {"title": "主库命中", "value": mapped_count, "unit": "笔"},
        {"title": "待确认基金", "value": pending_count, "unit": "笔"},
    ]
    chart_data = {
        "direction_buy_amount": [
            {"direction": r["direction"], "value": r["buy_amount"]}
            for r in direction_amount_table[:10]
        ],
        "direction_buy_people": [
            {"direction": r["direction"], "value": r["kol_count"]}
            for r in direction_people_table[:10]
        ],
        "operation_type_distribution": [
            {"name": k, "value": v}
            for k, v in op_dist.items() if v > 0
        ],
        "kol_buy_sell_compare": [
            {"name": k, "buy": round(v["buy"], 2), "sell": round(v["sell"], 2)}
            for k, v in kol_buy_sell.items() if (v["buy"] > 0 or v["sell"] > 0)
        ],
    }

    tables = {
        "overview_table": overview_table,
        "direction_people_table": direction_people_table,
        "direction_amount_table": direction_amount_table,
        "trend_table": trend_table,
        "buy_recommend_table": buy_recommend_table,
        "sell_recommend_table": sell_recommend_table,
        "kol_ops_table": kol_ops_table,
        "pending_funds_table": pending_rows,
    }

    return {
        "summary": report_text or "",
        "summary_cards": summary_cards,
        "tables": tables,
        "chart_data": chart_data,
        "appendix_pending_funds": pending_rows,
        "meta": {
            "kol_count": len(kols),
            "record_count": len(records),
            "mapped_count": mapped_count,
            "pending_count": pending_count,
            "has_direction_data": len(by_direction_people) > 0,
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        },
    }


def _guess_pending_reason(fund_name: str) -> str:
    """根据基金名称特征推断未命中主库的原因。"""
    if not fund_name:
        return "采集名称为空"
    if fund_name.endswith("...") or fund_name.endswith("…") or "..." in fund_name:
        return "名称截断无法准确匹配"
    if len(fund_name) < 6:
        return "OCR/采集名称不完整"
    return "主库暂无该基金"


def analyze_daily(
    records: List[TradeRecord],
    output_dir: Optional[str] = None,
    date_str: Optional[str] = None,
    historical_context: Optional[dict] = None,
) -> Optional[Dict[str, Any]]:
    """一站式：records → AI 分析报告 + 结构化报告（summary_cards / tables / chart_data / appendix）。

    Returns: dict with keys: report_path, summary, summary_cards, tables, chart_data, appendix_pending_funds, meta
    """
    report_text = generate_daily_report(records, historical_context=historical_context)
    if not report_text:
        return None

    # 1) 写 Markdown 报告（兼容原文件）
    if output_dir is None:
        output_dir = _resolve_output_dir()
    os.makedirs(output_dir, exist_ok=True)
    cleaned_date = (date_str or datetime.date.today().strftime("%Y%m%d")).replace("-", "")
    if len(cleaned_date) != 8 or not cleaned_date.isdigit():
        cleaned_date = datetime.date.today().strftime("%Y%m%d")
    md_path = os.path.join(output_dir, f"daily_report_{cleaned_date}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    logger.info("每日分析报告（Markdown）已保存: %s", md_path)

    # 2) 拼装结构化报告
    structured = build_structured_report(records, historical_context, report_text)

    # 3) 写 JSON 报告，前端可直接读取
    json_path = os.path.join(output_dir, f"daily_report_{cleaned_date}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(structured, f, ensure_ascii=False, indent=2)
    logger.info("每日分析报告（结构化 JSON）已保存: %s", json_path)

    structured["report_path"] = md_path
    structured["json_path"] = json_path
    return structured
