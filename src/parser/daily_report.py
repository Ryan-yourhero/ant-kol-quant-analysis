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
你是理财社区"理财盘友圈"每日复盘分析员。所有事实（基金方向、人数、金额、百分比、置信度、信号类型）都已由 Python 预处理完毕并随输入给出；你只负责自然语言解释与组织。

二、报告禁止出现的内容（强制）
1. **不要出现任何代码风格的字段名、键值对或 JSON 表达式**（如 `sell_kol_count=0`、`buy_amount_trend=-80%`、`conversion_out` 等）。所有数据用自然语言描述，零值用"无 / 为零 / 为 0"表述。
2. **不要出现"数据完整性""采集完整""采集正常到底""crawl_status""stop_type""bottom""expand_remaining""数据完整"等任何与采集状态相关的字眼**。系统对正常采集情况不会提供采集状态元数据；只有异常时才可能给出 `data_warning`，那时才在「风险提示」用自然语言描述。
3. **不要把"其他/待分类"作为正式投资方向出现在报告任何位置**（方向汇总、推荐、趋势表中均不允许）。Python 已过滤；LLM 不得脑补。
4. **不要新增、删除、合并、拆分方向**。「方向汇总」「近7日趋势」「买入推荐」「卖出推荐」中出现的 direction 必须完全等于输入 `allowed_directions` 列表中的元素。
5. **禁止根据基金名称、动态正文、大V观点、历史数据自行推断方向**。每条交易的 direction 已在输入表格的「投资方向」列直接给出，原样使用。

三、输入结构
- 原始数据表：每行一笔操作，列含「投资方向」。
- 推荐候选方向 buy_candidates / sell_candidates：已剔除「待确认」/「其他/待分类」，每条带 confidence 与 signal_type。
- 近7日历史对比 JSON：含 directions 与 kols。
- allowed_directions：本次报告允许出现的全部 direction 字符串列表。
- unmapped_funds：未命中 fund_direction_master 主库的基金名+大V+操作清单（用于「待确认基金」提示，不参与方向汇总）。

四、转换操作语义
- 主动卖出：按真实减仓信号处理
- 转换转出（如"由电网方向转换至港股方向"）：描述为"从 X 方向转换至 Y 方向"，**禁止**拆成"卖出 X + 买入 Y"；不计入"卖出强度/减仓"

五、状态标签与共识口径
- 状态标签（"加仓增强 / 买卖并存 / 由买转卖"等）由 Python 给出，原样引用，不要推翻重判。
- 至少 2 位大V参与才可用"共识/共识形成/共识扩散"；只有 1 位大V必须用"个体行为/个体重仓/单点信号"。

六、近7日对比的两层口径（禁止混用）
- 大V整体口径：kols[].last_7d.avg_daily_buy_amount（用于「核心大V操作详解」）
- 大V×方向口径：kols[].directions[].direction_avg_daily_buy_amount（用于「近7日趋势变化」表）
- 两者是不同层级，数值通常不同。百分比变化率必须严格对应当前引用的基准口径。

七、买入推荐
只能从 buy_candidates 挑选，最多 3 个。推荐理由用自然语言写"今日买入人数 / 今日买入金额 / 近7日变化 / 参考大V / 置信度"，不要带字段名。

八、卖出推荐
只能从 sell_candidates 挑选，最多 3 个。转换转出不与主动卖出等权处理。如果只有卖出份额没有金额，不要估算金额，只用"卖出份额""卖出操作数""连续卖出天数"。

九、输出结构（严格按此顺序与标题）
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

    parts += [
        "",
        "## allowed_directions（方向汇总/趋势/推荐中允许出现的 direction 白名单）",
        "```json",
        json.dumps(sorted(allowed_directions), ensure_ascii=False, indent=2),
        "```",
        "以上列表来自 fund_direction_master 主库，是本次报告允许出现的全部 direction。",
        "**严禁**新增、删除、合并、拆分方向；**严禁**使用列表之外的方向名（包括但不限于「其他/待分类」「待确认」）。",
        "",
        "## unmapped_funds（未命中主库的基金 — 仅在「待确认基金」节展示，不参与方向汇总）",
        "```json",
        json.dumps(unmapped_funds, ensure_ascii=False, indent=2),
        "```",
    ]

    # 把推荐候选也单独传给 LLM（强制它只能从中选）
    if historical_context:
        import json as _json
        buy_cands = historical_context.get("recommend_buy_candidates", [])
        sell_cands = historical_context.get("recommend_sell_candidates", [])
        if buy_cands or sell_cands:
            parts += [
                "",
                "## 推荐候选方向（Python 预筛选，已剔除「其他/待分类」）",
                "买入推荐 / 卖出推荐 **只能**从下列候选方向中挑选（最多 3 个）：",
                "```json",
                _json.dumps(
                    {
                        "buy_candidates": buy_cands,
                        "sell_candidates": sell_cands,
                    },
                    ensure_ascii=False, indent=2, default=str,
                ),
                "```",
            ]

    if historical_context:
        import json as _json

        parts += [
            "",
            "## 近7日历史对比数据",
            "以下是前置 Python 聚合计算出的结构化历史对比数据（JSON），",
            "请结合它判断「今日 vs 近7日」的相对强弱，不要只凭今日绝对金额下结论。",
            "**禁止自行重算百分比 / 金额 / 方向。** 数字一致性已通过 `consistency_check` 校验。",
            "```json",
            _json.dumps(historical_context, ensure_ascii=False, indent=2, default=str),
            "```",
        ]
    else:
        parts += [
            "",
            "## 近7日历史对比数据",
            "（本次未提供历史对比数据，若涉及历史强弱判断请标注「历史样本不足」。）",
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

    return _sanitize_report(result["content"], allowed_directions, unmapped_funds)


# ============================================================
#  后置过滤：剔除 LLM 仍写出的禁用内容（确定性检查）
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
]


def _sanitize_report(text: str, allowed_directions: List[str], unmapped_funds: List[Dict[str, Any]]) -> str:
    """对 LLM 生成的报告做后置过滤：

    1. 删除包含禁用关键字（数据完整性/采集完整/crawl_status 等）的整段；
    2. 删除包含「其他/待分类」作为方向行的整段（用作方向名时）；
    3. 兜底：若仍残留禁用字符串，整段直接删除；
    4. 校验 allowed_directions 白名单：方向汇总/趋势表中出现非白名单方向的整段删除。
    """
    if not text:
        return text

    lines = text.split("\n")
    out: List[str] = []
    in_forbidden_block = False
    block_buf: List[str] = []
    allowed_set = set(allowed_directions or [])

    def _flush_block(buf: List[str]) -> None:
        """决定是否保留 buf 块；保留则加入 out。"""
        if not buf:
            return
        joined = "\n".join(buf)
        # 1) 含禁用关键字 → 删除
        if any(kw in joined for kw in _FORBIDDEN_KEYWORDS):
            logger.warning("[sanitize] 删除含禁用关键字段落: %s", joined[:80].replace("\n", " "))
            return
        # 2) 方向行白名单校验（仅识别「### 二、方向汇总」后的方向单元格）
        # 简易规则：若 buf 内出现以 | 包裹的、且不是 allowed_set 中的方向名，整段删
        cleaned_buf: List[str] = []
        for ln in buf:
            if "|" in ln and ln.lstrip().startswith("|"):
                # 拆出第二列（方向列）
                parts = [p.strip() for p in ln.strip().strip("|").split("|")]
                if len(parts) >= 1:
                    dir_name = parts[0]
                    if dir_name and dir_name not in allowed_set and dir_name not in (
                        "方向", "大V/方向", "排名", "—", "", "合计",
                    ):
                        logger.warning(
                            "[sanitize] 删除非白名单方向行: %s",
                            dir_name,
                        )
                        continue
                cleaned_buf.append(ln)
            else:
                cleaned_buf.append(ln)
        out.extend(cleaned_buf)

    for line in lines:
        if line.startswith("### ") or line.startswith("---"):
            # 段落分隔：flush 上一段
            if block_buf:
                _flush_block(block_buf)
                block_buf = []
            in_forbidden_block = False
            out.append(line)
            continue
        # 段落级禁用标记：「> 数据完整性」等 blockquote 标记
        if line.lstrip().startswith(">") and any(kw in line for kw in _FORBIDDEN_KEYWORDS):
            in_forbidden_block = True
            continue
        if in_forbidden_block:
            # 跳过直到下一个段落分隔
            continue
        block_buf.append(line)

    _flush_block(block_buf)

    cleaned = "\n".join(out)

    # 兜底：最后再做一次"禁用关键字整行删除"
    final_lines = []
    for ln in cleaned.split("\n"):
        if any(kw in ln for kw in _FORBIDDEN_KEYWORDS):
            logger.warning("[sanitize-final] 删除残留禁用行: %s", ln[:80])
            continue
        final_lines.append(ln)
    return "\n".join(final_lines)


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

    logger.info("每日分析报告已保存: %s", report_path)
    return report_path


def analyze_daily(
    records: List[TradeRecord],
    output_dir: Optional[str] = None,
    date_str: Optional[str] = None,
    historical_context: Optional[dict] = None,
) -> Optional[str]:
    """一站式：records → AI 分析报告 → 保存 .md，返回报告路径（失败返回 None）。"""
    report_text = generate_daily_report(records, historical_context=historical_context)
    if not report_text:
        return None
    return export_daily_report(report_text, output_dir=output_dir, date_str=date_str)
