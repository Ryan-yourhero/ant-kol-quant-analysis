"""
AI 解析器 — 将完整 screen_dump MD 直接交给 LLM 做语义解析

架构：
  完整 MD → AI 语义解析 → 嵌套 JSON（帖子+操作数组）
  → Python 展平 → Pydantic 校验 → List[TradeRecord]

优化：AI 输出嵌套结构（帖子级字段只输出一次，操作在子数组），
      减少输出 Token 量，加速响应。
"""

from __future__ import annotations

import json
import os
import re
import datetime
import logging
from typing import Any, Dict, List, Optional

from .llm_client import chat_completion, is_configured
from .models import TradeRecord

logger = logging.getLogger("parser.ai_parser")

# ============================================================
#  System Prompt — 完整 MD 解析（嵌套 JSON 输出）
# ============================================================

SYSTEM_PROMPT = """你是蚂蚁财富大V操作数据的结构化解析助手。

输入：一份 screen_dump（屏幕文字镜像）Markdown 文件。
      文件以 "# 页面N" 分隔，每个页面包含连续滚屏抓取到的 UI 文本，每行一个元素。

你需要从整个 MD 中识别所有大V的帖子及其操作，返回嵌套 JSON。

输出格式（严格 JSON，不要 markdown 代码块，不要解释）：
{
  "posts": [
    {
      "kol_name": "光模块之王",
      "yield_rate": "26.49%",
      "publish_time": "16:18",
      "opinion_text": "指数回落了一点，继续加仓...",
      "operations": [
        {
          "operation_type": "买入",
          "operation_status": "确认中",
          "fund_name": "广发全球精选股票...",
          "buy_amount": "2500.00",
          "sell_shares": null,
          "remark": null
        }
      ]
    },
    {
      "kol_name": "Bells",
      "yield_rate": "15.30%",
      "publish_time": "15:55",
      "operations": [
        {"operation_type": "买入", "operation_status": "确认中", "fund_name": "建信上海金ETF联接C...", "buy_amount": "1000.00", "sell_shares": null, "remark": null},
        {"operation_type": "买入", "operation_status": "确认中", "fund_name": "国富全球科技互联...", "buy_amount": "1000.00", "sell_shares": null, "remark": null}
      ]
    }
  ]
}

注意：post 层面的字段（kol_name、yield_rate、publish_time、opinion_text）只出现一次。
同一个大V的每篇帖子是独立的 post 对象。跨页属于同一篇帖子则合并为一个 post。
不确定归属的匿名操作单独成 post，kol_name 填 "未知KOL" 或 "大V名(待定)"。

=== MD 格式说明 ===

大V帖子结构（按时间倒序排列）：
  大V昵称（如"光模块之王"）
  收益率行（如"近一年收益率26.49%"）
  时间 HH:MM
  （可选）观点文本（长句，可能跨多行）
  操作1：操作锚点 → 基金名 → 金额/份额
  操作2：...
  ...
  互动统计（忽略，不需输出）
  "展开今日全部N条操作"  ← 用做归属判断，但不需要输出到 JSON

操作锚点识别：
  "买入确认中" → 买入
  "定投确认中" → 定投
  "卖出确认中" → 卖出
  "转换确认中" → 拆成两笔：一笔卖出（源基金）+ 一笔买入（目标基金）
  "撤销" → 撤销

金额标签：
  "买入金额(元)" 下一行 → buy_amount
  "卖出份额(份)" 下一行 → sell_shares

=== 跨页归属推断 ===

页面滑动时，大V的名字可能被截断（只有操作没有KOL名）。你需要用以下方法推断归属：

决策树（按优先级）：

1. 帖内签名法（最可靠）
   若无KOL名的操作块上方有观点文本，且观点中出现了 "$基金名称$" 模式：
   → 观点中 $基金名$ == 操作基金名 → 归属该观点的大V。
   示例：观点"光模块三兄弟...$鑫元创业板人工智能指数$"，操作"鑫元创业板人工智能指..."→ 光模块之王。

2. 条数补齐法（强辅助证据，不可单独使用）
   全文中搜索"展开今日全部N条操作"，统计每个大V：
   - 声明N条
   - 该大V在有KOL名的区域中可见的操作条数
   - 缺口 = N - 可见条数
   条数补齐只能作为强辅助证据，必须同时结合以下上下文才能归属：
   - 时间顺序：匿名操作的时间是否落在该大V的帖子时间范围内
   - 页面位置：匿名区块与该大V在 MD 中的前后相邻关系
   - 基金/观点对应：匿名操作的基金或观点文本是否与该大V匹配
   若多个大V都存在相同缺口，不得只凭条数强行归属，应标记为"未知KOL"或"大V名(待定)"。
   注意：一条缺口只能分配给一个大V，不要重复使用。
   示例：Bells声明4条、可见2条(建信)、缺口2；匿名中有2条国富1000元，且匿名区块紧接Bells帖子之后、基金类型吻合→Bells。

3. 风格匹配法
   匿名区块的观点文本若含大V特征词，归属该大V。
   关键词："光模块三兄弟"→光模块之王、"慢慢变富"→慢慢变富在路上啊。

4. 相邻区块法
   匿名操作出现在某大V的操作之后、无分隔符（如"暂无更多内容"）→ 大概率同大V。

5. 无法确认 → kol_name: "未知KOL"

=== 归属可信度 ===
  kol_name 中直接写大V名 → 高度确信
  kol_name 中写"大V名(待定)" → 有证据但不100%肯定
  kol_name 中写"未知KOL" → 无法确定

=== 铁规 ===
1. 每篇帖子一个 post 对象。多个操作放在 operations 数组中。
2. operation_type 只能是以下四个值之一：买入 / 卖出 / 撤销 / 定投。
   "定投确认中"→定投，"买入确认中"→买入，"卖出确认中"→卖出。
3. 转换操作拆成两笔，remark 字段填 "转换"：
   卖出操作：operation_type=卖出，fund_name=源基金，sell_shares=卖出份额，remark="转换"
   买入操作：operation_type=买入，fund_name=目标基金，buy_amount=买入金额，remark="转换"
4. 原文基金名带"..."原样保留，禁止猜完整名称。
5. 原文没有的字段填 null，禁止编造。
6. 观点里的"买入/卖出"不是真实交易。真实交易看操作锚点。
7. fund_name 只填被操作基金，不填观点中提到的基金。
8. 优先保证准确率，不要强行匹配。不确定就写"未知KOL"。
9. 同一个大V的不同帖子用不同 post 对象（按时间区分）。
10. collect_time（采集时间）不要输出，由程序自动填入。

只返回 JSON。"""


# ============================================================
#  解析入口
# ============================================================

# 完整 MD 上下文长度限制（字符数）
# 当前 MD 仅数千字符，优先整份一次发给 AI。
# 分块逻辑仅为未来超长 MD 预留。
MAX_MD_CHARS = 50000


def parse_full_md(md_text: str) -> List[TradeRecord]:
    """
    将完整 MD 文本发送给 AI，直接获得结构化记录。

    如果 MD 超过长度限制，按页面分块，每块携带全局条数统计。
    """

    logger.info("AI 端到端解析: %d 字符", len(md_text))

    if len(md_text) <= MAX_MD_CHARS:
        return _parse_single(md_text)

    # 超长：分块
    return _parse_chunked(md_text)


def _parse_single(md_text: str) -> List[TradeRecord]:
    """单次 AI 调用解析完整 MD（嵌套 JSON 输出）"""
    result = chat_completion(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"请从以下 screen_dump MD 文件中提取所有大V的基金操作记录：\n\n{md_text}"},
        ],
        max_tokens=8192,
        timeout=300,
        response_format={"type": "json_object"},
    )

    if not result["ok"]:
        logger.error("AI 解析失败: %s", result["error"])
        return []

    content = result["content"]

    # 保存本次 AI 输入输出到日志目录
    _save_ai_log(md_text, content)

    records = _parse_ai_response(content)
    logger.info("AI 解析完成: %d 条记录", len(records))
    return records


def _save_ai_log(md_input: str, ai_output: str) -> None:
    """保存每次 AI 调用的输入 MD 和输出 JSON"""
    now = datetime.datetime.now()
    task_dir_name = now.strftime("%Y%m%d_%H%M%S")
    _parser_dir = os.path.dirname(os.path.abspath(__file__))
    _project_root = os.path.dirname(os.path.dirname(_parser_dir))
    log_dir = os.path.join(_project_root, "logs", "ai_tasks", task_dir_name)
    os.makedirs(log_dir, exist_ok=True)

    input_path = os.path.join(log_dir, "input.md")
    with open(input_path, "w", encoding="utf-8") as f:
        f.write(md_input)

    output_path = os.path.join(log_dir, "output.json")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(ai_output)

    logger.info("AI 日志已保存: %s", log_dir)


def _parse_chunked(md_text: str) -> List[TradeRecord]:
    """
    按页面分块发送，每块携带全局统计。

    全局统计 = 全文中提取的"展开今日全部N条操作" 和各KOL可见操作条数。
    """
    # 提取全局统计
    global_stats = _extract_global_stats(md_text)

    # 按页面分块
    pages = re.split(r"\n(?=# 页面\d+)", md_text)
    header = pages[0] if pages else ""

    all_records: List[TradeRecord] = []
    chunks = [header]  # 第一块 = 文件头

    total_chunks = 0
    for pg in pages[1:]:
        chunks[-1] += "\n" + pg
        if len(chunks[-1]) > MAX_MD_CHARS:
            chunks.append(header)
            total_chunks += 1

    total_chunks += 1
    logger.info("AI 分块解析: %d 个页面 → %d 块", len(pages) - 1, total_chunks)

    for ci, chunk in enumerate(chunks, 1):
        logger.info("  AI 块 %d/%d (%d 字符)...", ci, total_chunks, len(chunk))

        # 注入全局统计
        stats_block = _format_global_stats(global_stats)
        full_prompt = f"{stats_block}\n\n请从以下 screen_dump 片段提取操作记录：\n\n{chunk}"

        result = chat_completion(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": full_prompt},
            ],
            max_tokens=8192,
        )

        if result["ok"]:
            records = _parse_ai_response(result["content"])
            all_records.extend(records)
        else:
            logger.warning("  块 %d AI 失败，跳过", ci)

    logger.info("AI 分块解析完成: %d 条记录", len(all_records))
    return all_records


def _extract_global_stats(md_text: str) -> Dict[str, Dict[str, int]]:
    """
    从完整 MD 中提取全局统计：
    - 每个KOL的"展开今日全部N条操作"
    - 每个KOL在有KOL名区域中的可见操作条数
    """
    stats: Dict[str, Dict[str, int]] = {}

    # 找所有 KOL 块（KOL名 → 收益率 → ... → "展开今日全部N条操作"）
    # 模式：先找 KOL名行，然后在该区域内找 today_operation_count
    pages = re.split(r"\n(?=# 页面\d+)", md_text)

    for page in pages:
        lines = page.split("\n")
        current_kol = None

        for i, line in enumerate(lines):
            # 检测 KOL 名（2-12字的中英文数字，无标点）
            stripped = line.strip()
            if (
                len(stripped) >= 2
                and len(stripped) <= 12
                and re.match(r"^[\u4e00-\u9fa5A-Za-z0-9_]+$", stripped)
            ):
                # 确认是 KOL：下一行应该是收益率
                if i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    if "%" in next_line:
                        current_kol = stripped
                        if current_kol not in stats:
                            stats[current_kol] = {"total_claimed": 0, "visible_ops": 0}

            # 在 KOL 区域内查找 today_operation_count
            if current_kol:
                if "展开今日全部" in line:
                    m = re.search(r"展开今日全部(\d+)条操作", line)
                    if m:
                        stats[current_kol]["total_claimed"] = int(m.group(1))

                # 统计可见操作数
                if any(kw in line for kw in ("买入确认中", "卖出确认中", "转换确认中", "定投确认中", "撤销", "撤单")):
                    stats[current_kol]["visible_ops"] += 1

    # 计算缺口
    for k in stats:
        s = stats[k]
        s["missing"] = max(0, s["total_claimed"] - s["visible_ops"])

    return stats


def _format_global_stats(stats: Dict[str, Dict[str, int]]) -> str:
    """格式化全局统计为文本块"""
    if not stats:
        return ""

    lines = ["=== 全局 KOL 统计（全文中提取） ==="]
    for k in sorted(stats.keys()):
        s = stats[k]
        lines.append(
            f"  {k}: 声明{s['total_claimed']}条, 可见{s['visible_ops']}条, 缺口{s['missing']}条"
        )
    lines.append("（缺口条数 = 声明条数 - 已在有KOL名区域中找到的操作条数）")
    lines.append("（匿名区块中的操作数应与缺口条数匹配）")
    lines.append("")
    return "\n".join(lines)


# ============================================================
#  JSON 解析 & 展平
# ============================================================

def _try_fix_truncated_json(text: str) -> Optional[str]:
    """尝试修复被截断的 JSON：找到最后一个完整的元素并补全括号"""
    text = text.rstrip()

    # 策略1：找到最后一个完整的 operation 对象，补全 ]}]}
    last_op = text.rfind('"}')
    if last_op > 0:
        # 看后面是否已有闭合
        after = text[last_op + 2:]
        if "]" not in after and "}" not in after:
            candidate = text + "\n    ]\n  }\n]"
    else:
        candidate = text

    # 逐步添加闭合符
    closers = ["}", "]}", "]}]}", "}]}"]
    for suffix in ["", "]", "}]}", "]}]}"]:
        candidate = text.rstrip(", \t\n\r") + suffix
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            continue

    return None


def _parse_ai_response(content: str) -> List[TradeRecord]:
    """把 AI 返回的文本（支持嵌套 posts 或旧版 records 格式）解析成 TradeRecord 列表"""
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    content = content.strip()

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        fixed = _try_fix_truncated_json(content)
        if fixed:
            try:
                data = json.loads(fixed)
                logger.info("JSON 截断已自动修复")
            except json.JSONDecodeError as e:
                logger.error("AI 返回非 JSON 且无法修复: %s", str(e)[:100])
                return []
        else:
            logger.error("AI 返回非 JSON: %s", content[:500])
            return []

    if not isinstance(data, dict):
        return []

    # 新版嵌套格式：posts → 展平
    posts = data.get("posts")
    if isinstance(posts, list):
        return _flatten_posts(posts)

    # 兼容旧版扁平格式：records
    raw_records = data.get("records", [])
    if isinstance(raw_records, list):
        return _dicts_to_records(raw_records)

    return []


def _flatten_posts(posts: list) -> List[TradeRecord]:
    """将嵌套 posts JSON 展平为 TradeRecord 列表"""
    records: List[TradeRecord] = []
    for post in posts:
        if not isinstance(post, dict):
            continue

        # 提取帖子级字段
        post_fields = {
            "kol_name": post.get("kol_name"),
            "yield_rate": post.get("yield_rate"),
            "publish_time": post.get("publish_time"),
            "opinion_text": post.get("opinion_text"),
        }

        operations = post.get("operations", [])
        if not isinstance(operations, list):
            continue

        for op in operations:
            if not isinstance(op, dict):
                continue
            try:
                record = TradeRecord(
                    **post_fields,
                    operation_type=op.get("operation_type"),
                    operation_status=op.get("operation_status"),
                    fund_name=op.get("fund_name"),
                    buy_amount=_to_str_or_none(op.get("buy_amount")),
                    sell_shares=_to_str_or_none(op.get("sell_shares")),
                    remark=op.get("remark"),
                )
                records.append(record)
            except Exception as e:
                logger.warning("Pydantic 校验失败: %s", str(e)[:200])

    return records


def _to_str_or_none(val: Any) -> Optional[str]:
    """将值转为字符串，None/空保持 None"""
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _dicts_to_records(dicts: List[Dict[str, Any]]) -> List[TradeRecord]:
    """dict 列表 → TradeRecord 列表（兼容旧版扁平 records 格式）"""
    records: List[TradeRecord] = []
    for d in dicts:
        try:
            valid_fields = {k: d.get(k) for k in TradeRecord.model_fields if k in d}
            records.append(TradeRecord(**valid_fields))
        except Exception as e:
            logger.warning("记录转换失败: %s", str(e)[:200])
    return records
