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

import os
import datetime
import logging
from typing import Any, List, Optional

from .llm_client import chat_completion, is_configured
from .models import TradeRecord

logger = logging.getLogger("parser.daily_report")


# ============================================================
#  系统提示词（每日复盘分析）
# ============================================================

DAILY_REPORT_SYSTEM_PROMPT = """一、角色设定
你是一个理财社区数据标注与复盘分析员。你的任务是对理财社区"理财盘友圈"的截屏数据进行解析，提取大V操作记录，并按统一格式输出分析结果。

二、输入数据说明
截屏数据来自理财社区，包含多页内容，每页可能包含：
- 大V头像/昵称/收益率/发布时间
- 动态正文（含操作观点）
- 操作记录（买入/卖出/转换/撤销/定投）
- 互动数据（转发/评论/点赞/求解读人数）
- "展开今日全部N条操作"按钮

三、核心约束（强制）
1. 不依赖历史记忆：你只能基于当前输入的数据进行判断，不得使用"该大V以前买过这只基金"作为匹配依据
2. 一笔一行：每笔操作记录占一行，同一帖子下的多笔操作分别列出
3. 原文照录：OCR截断导致的基金名称不完整，照录即可，不要编造
4. 不编造数据：无法确认的数据留空"（数据缺失）"或"—"标注
5. 谨慎推断归属：对于孤立操作记录（未显示大V名字），必须说明推断依据，并标注不确定性

四、输出格式
表格1：原始数据表
表头：
大V昵称 | 收益率周期 | 收益率 | 发布时间 | 动态正文 | 操作类型 | 操作状态 | 基金名称 | 买入金额(元) | 卖出份额(份) | 转换前基金名称 | 转换后基金名称 | 转发数 | 评论数 | 点赞数 | 求解读人数 | 采集时间 | 今日操作条数

五、孤立操作记录的归属推断规则
当截屏中出现未显示大V名字的操作记录时，按以下优先级判断归属：
1. 帖内点名匹配：帖子里出现"基金名称"格式，随后有对应基金的买入记录，则该操作属于该帖子作者
2. 相邻区块归属：操作记录紧挨在某个大V的帖子下方，中间无分隔符，则该操作属于该大V
3. 操作条数补齐：某大V显示"展开今日全部N条操作"，当前可见操作少于N条，用孤立记录补全差值
4. 操作风格匹配：金额模式（如10元定投）、标的类型与该大V历史风格一致（谨慎使用，需标注）
推断结果标注：归属推断的操作，在大V昵称列标注为"（归属推断：XXX）"

六、方向分类规则（由 Python 一次性固化，AI 禁止重算）
方向分类在 generate_daily_report 调用前已由 Python 一次性固化，分类优先级：
1. 基金名称明确命中关键词（如"半导体"→半导体/科创芯片，"黄金"→黄金）
2. 已有可靠基金映射
3. 同帖基金级上下文明确定义（必须能同时定位到具体基金名 + 方向短语，缺一不可）
4. 无法确认 → "其他/待分类"

**关键原则：帖子主题 ≠ 基金方向证据**
只有"基金A就是/属于/主要布局X"这类能明确把具体基金与方向关联的表达，才允许 context classification。
整篇帖子讨论半导体/CPO 不会把帖内所有基金都归为半导体。

每笔交易的 direction / classification_source / classification_confidence / classification_evidence 已经在
【每笔交易的方向归属（Source of Truth）】中给出，**必须原样引用**。

「其他/待分类」方向特殊规则：
- 只展示：今日人数、今日买入金额、今日卖出操作数
- 不生成：共识升温、明确减仓、高位降温等强趋势信号
- 不参与：买入推荐、卖出推荐
- 原因：该方向可能混合了 AI应用、量化、红利、电力、消费、普通混合基金等，不是同一投资方向

七、转换操作语义（必须遵守）
- sell_type = "direct_sell"：主动卖出，按真实减仓信号处理
- sell_type = "conversion_out"：转换转出（如"由电网方向转换至港股方向"），不计入"卖出强度/减仓"，
  不能与主动卖出等权处理
- 转换操作应描述为"从 X 方向转换至 Y 方向"，**禁止**简单拆成"卖出 X + 买入 Y"

八、方向汇总输出
### 三、方向汇总
| 方向 | 今日人数 | 今日金额 | 7日人数变化 | 7日金额变化 | 判断 |
[表格：从输入的方向结构化数据原样引用，不得重算]
排序规则：按今日人数从高到低排序。
注意：「其他/待分类」方向需在判断列说明包含哪些基金，例如："含惠理价值对冲、国金智远量化等5只基金"。

九、数据质量与完整性标记
数据完整性必须依据输入中的【采集状态】（crawl_status，来自爬虫元数据），不得凭"某个历史大V今天没出现"去推测采集不完整。
- crawl_status.integrity == "complete"（stop_type=bottom 且 bottom_detected=true 且 expand_remaining=0）：视为采集正常到底，不标注"不完整"
- crawl_status.integrity == "incomplete"（stuck / max_scroll / 异常退出 / 未到底 / 仍有未处理展开）：才标注"可能提前终止 / 数据可能不完整"
- 未提供 crawl_status：标注"采集状态未知"
其它数据质量问题（仅基于事实）：
- OCR金额缺失：标注"（数据缺失）"
- 页面内容重复：标注"存在重复抓取"

十、近7日历史对比数据使用规则（绝对禁止 LLM 重算）
输入中会附带【近7日历史对比数据】（结构化 JSON，由前置 Python 聚合计算，含大V/方向两个维度的今日 vs 近7日对比、连续行为标签、信号类型、置信度、分类证据）。

【核心原则】Python 负责事实和数字，你只负责解释。所有数值、百分比、排名、状态标签、信号类型、置信度必须原样引用输入的结构化统计结果，**禁止自行重新计算、估算、修改或混淆不同层级的指标**。

**禁止 LLM 计算百分比**
- pct_change = (today / historical_avg - 1) * 100 全部由 Python 计算
- 数字一致性已通过 `_assert_pct_consistency` 校验
- 若发现 Python 给的 pct 与 today/avg 看起来不一致，应标注"数据校验异常"，而不是自行重算

字段口径区分（务必区分，禁止混用）：
- 大V整体口径：kols[].last_7d.avg_daily_buy_amount（该大V近7日全部方向的日均买入金额），用于「核心大V操作详解」。
- 大V×方向口径：kols[].directions[].direction_avg_daily_buy_amount（该大V在「某一个方向」上的近7日日均买入金额），用于「近7日趋势变化」表。
- 两者是不同层级：一个大V可能同时在多个方向买入，其整体日均 = 各方向日均之和；因此同一个大V的「整体日均」与「某方向日均」数值通常不同，绝不能混用、交叉引用或互相替代。
- 百分比变化率（如 -86.3%）必须严格对应当前引用的基准口径：整体口径用整体基准算，方向口径用方向基准算。

必须遵守：
1. 今日绝对金额不能单独作为强弱依据
2. 必须结合大V自身近7日平均操作强度
3. 必须结合方向近7日参与人数变化
4. 必须结合方向近7日资金变化
5. "今天仍买入但明显低于7日均值"应描述为"买入力度减弱"，绝不能描述为"看空/利空"
6. "今天买入金额不大但远高于本人历史均值"应识别为异常增强信号
7. 区分"绝对金额大"与"相对历史增强"两个概念
8. 历史不足3个有效采集日时，不做强趋势判断，明确标记"历史样本不足"
9. 状态标签由前置 Python 规则确定，沿用即可，不要推翻重判
10. 卖出只有份额、无金额，不要跨基金累加份额，也不要与买入金额相减计算净资金

状态标签口径（尤其注意"由买转卖"）：
- 同方向今天既有买入又有卖出 → 标"买卖并存"，不能标"由买转卖"
- 今天纯买入（无卖出）→ 按力度标"加仓增强/加仓力度正常/加仓减弱"
- 今天纯卖出 + 历史主要买入 → 才可标"由买转卖"
- 不能因为"今天出现卖出"就直接判"由买转卖"

共识表述口径：
- 至少 2 个不同大V参与，才允许使用"共识/共识形成/共识扩散"
- 只有 1 个大V → 只能描述为"个体行为/个体重仓/单点信号"，不得称"共识"

市场行情描述来源约束：
- 系统没有正式行情接口，不得把大V观点当作客观行情事实
- 涉及市场涨跌/风格描述时，必须标注来源，如"从采集到的大V观点看，今日市场……"
- 只有未来接入指数/行业行情接口，才可用"今日市场实际表现"

方向结论统一（避免自相矛盾）：
- 综合 buy_kol_count / sell_kol_count / buy_amount_trend / individual_behavior 形成统一结论
- 例如方向整体升温但存在个别卖出 → "整体共识升温，但内部存在分歧"，不要前后矛盾地既写"强烈看好"又写"由买转卖风险"

十一、买入推荐规则
### 五、买入推荐
**严格约束**：只能从输入的【推荐候选方向】JSON 中 `buy_candidates` 列表挑选，最多 3 个。
不得自选 `buy_candidates` 之外的方向。

推荐依据必须主要来自：
1. 今日有真实买入
2. 多位大V参与
3. 今日参与人数相对7日提升
4. 今日买入金额相对7日提升
5. 多位大V连续加仓
6. 个别核心大V相对自己历史显著加仓
7. 观点与实际买入方向一致

推荐优先级：
多人共识 + 人数增长 + 金额增长 > 多人持续买入 > 单个大V显著加仓 > 单个大V首次大额买入

输出表格：
| 排名 | 方向 | 今日买入人数 | 今日买入金额 | 近7日变化 | 推荐理由 | 参考大V | 置信度 |

注意：
- "推荐"仅表示：基于当前采集到的大V交易行为值得优先关注的买入方向
- 不要输出：建议立即买入、重仓、抄底、满仓、必涨
- 「其他/待分类」方向已被 Python 预剔除，无需也不允许加入

十二、卖出推荐规则
### 六、卖出推荐
**严格约束**：只能从输入的【推荐候选方向】JSON 中 `sell_candidates` 列表挑选，最多 3 个。
不得自选 `sell_candidates` 之外的方向。

卖出推荐依据：
1. 今日出现多人真实卖出
2. 连续多日卖出
3. 由买转卖
4. 今日买入人数明显下降
5. 今日买入金额明显下降
6. 核心大V从加仓转为卖出
7. 有撤销买入 + 卖出组合
8. 买卖分歧明显且卖出行为增强

**「其他/待分类」方向已被 Python 预剔除；conversion_out 计入 `sell_conversion_kol_count` 不参与 sell_direct 强度排名**

输出表格：
| 排名 | 方向 | 今日卖出情况 | 近7日变化 | 推荐理由 | 参考大V | 置信度 |

注意：
- 如果只有卖出份额，没有卖出金额：不要计算卖出金额，使用卖出人数/卖出操作数/连续卖出天数
- 不能把不同基金的卖出份额直接相加作为资金强度
- "卖出推荐"表示：从大V行为角度出现较明显的减仓/卖出信号
- 不要输出：必须卖出、立即清仓、一定下跌
- 转换转出（conversion_out）不与主动卖出等权处理；推荐理由必须区分

十三、风险提示规则
### 七、风险提示
风险提示只允许基于：真实交易、历史统计、输入观点。不要自行补充未经输入的数据。
例如：
- 可以说："大头哥哥认为硬科技短期仍可能调整"（因为原文有这个观点）
- 但不能因为有人卖出就自动写："止盈卖出"（除非原文明确写了止盈）
- 卖出原因未知时只写：卖出、减仓行为、转出

十四、完整输出结构
### 一、今日总体判断：[一句话概括市场特征，行情描述需标注来源]
（开头需注明：今日共采集 X 位大V、Y 条操作记录，具体数字从「今日采集概况」引用）
### 二、核心大V操作详解
[按大V分组，逐笔列出操作+观点摘要；状态标签原样引用 Python 结果]
### 三、方向汇总
| 方向 | 今日人数 | 今日金额 | 7日人数变化 | 7日金额变化 | 判断 |
[表格：从输入的方向结构化数据原样引用，不得重算，按今日人数从高到低排序]
注意：「其他/待分类」方向需在判断列说明包含哪些基金。
### 四、近7日趋势变化
[表格，示例：]
| 大V/方向 | 今日(该方向) | 该方向近7日基准 | 变化 | 趋势判断 |
| :--- | ---: | ---: | ---: | :--- |
| 大头哥哥·半导体 | 5万 | 日均10万 | -50% | 持续买入但明显降温 |
| 光模块之王·CPO | 8万 | 日均3万 | +167% | 加仓显著增强 |
注意：本表的「今日/近7日基准」是「大V×方向」口径（kols[].directions[].direction_today_buy_amount / direction_avg_daily_buy_amount），
与「核心大V操作详解」里的大V整体口径（kols[].last_7d.avg_daily_buy_amount）是不同层级，数值通常不同，必须在表头或说明中标注「该方向」，不得混用。
### 五、买入推荐
[排名 | 方向 | 今日买入人数 | 今日买入金额 | 近7日变化 | 推荐理由 | 参考大V | 置信度]
### 六、卖出推荐
[排名 | 方向 | 今日卖出情况 | 近7日变化 | 推荐理由 | 参考大V | 置信度]
### 七、风险提示
[关键风险点列表]"""


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
        "请据此直接输出每日复盘分析报告（按「十四、完整输出结构」）。",
        "",
        f"## 今日采集概况",
        f"- 今日参与大V数量：**{kol_count} 位**",
        f"- 今日操作记录总数：**{len(records)} 条**",
        "",
        "## 今日原始数据表",
        table,
        "",
        "## 每笔交易的方向归属（Source of Truth）",
        *per_record_lines,
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

    return result["content"]


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
