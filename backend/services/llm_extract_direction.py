"""
LLM 抽取基金方向 — 身份校验 + fund_type 感知
==============================================

输入：
  - 目标基金: fund_name, fund_code
  - 联网结果: List[evidence_item] = [
      {url, title, snippet, source_type, matched_fund_code, matched_fund_company, identity_confidence}
    ]
  - fund_type_hint: index / industry_theme / active_equity / mixed / quant / broad_market

输出：
  {
    fund_name, fund_code,
    direction, sub_direction, fund_type, fund_type_detail,
    confidence, evidence, evidence_period,
    source_url, identity_verified,
    matched_evidence_count,    # 真正身份匹配且可支撑结论的项数
    should_write_to_db,
    used_evidence_items: [{url, title, source_type, identity_confidence, snippet_excerpt}]
  }
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from src.parser.llm_client import chat_completion, is_configured

logger = logging.getLogger("backend.llm_extract")

ALLOWED_DIRECTIONS = (
    "港股方向", "黄金", "债券", "CPO/光模块",
    "半导体/科创芯片", "创新药/医药", "全球科技/QDII",
    "白酒/消费", "资源/有色金属", "其他/待分类",
)
ALLOWED_FUND_TYPES_DETAIL = (
    "index", "industry_theme", "active_equity", "mixed", "quant", "broad_market", "other",
)
ALLOWED_FUND_TYPES_LEGACY = ("index", "industry_theme", "active_equity", "mixed", "qdii", "other")

EXTRACT_SYSTEM_PROMPT = """你是基金方向识别助手。输入目标基金 + 联网结果，只输出 JSON。

【关键：身份校验】
- 每条 evidence_item 已预识别 matched_fund_code / matched_fund_company / identity_confidence
- 只有 identity_confidence=high 的项才能作为 high confidence direction 的核心证据
- identity_confidence=low 的项只能作为辅助参考
- 如果目标基金有 fund_code，但结果中出现其他 6 位代码，必须视为**不是同基金**

【输出格式】严格 JSON：
{
  "fund_name": "<原名>",
  "fund_code": "<6位代码或空字符串>",
  "direction": "<合法方向>",
  "sub_direction": "<细分子方向，如'半导体设备'，否则空字符串>",
  "fund_type_detail": "index | industry_theme | active_equity | mixed | quant | broad_market | other",
  "fund_type_hint_accepted": true | false,
  "confidence": "high | medium | low",
  "evidence": "<50字以内的关键证据>",
  "evidence_period": "<季度报告期，如 2026Q2，或空字符串>",
  "source_url": "<最权威来源URL>",
  "identity_verified": true | false,
  "should_write_to_db": true | false,
  "used_evidence_indices": [0, 2]
}

【合法方向列表】只能从下列选一个：
- 港股方向
- 黄金
- 债券
- CPO/光模块
- 半导体/科创芯片
- 创新药/医药
- 全球科技/QDII
- 白酒/消费
- 资源/有色金属
- 其他/待分类

【fund_type 判定（重要）】
- index：跟踪具体指数（沪深 300 / 半导体指数 / 纳指）→ 方向由指数决定
- industry_theme：行业主题基金（半导体主题/医药主题/CPO主题）→ 看基金合同投资范围
- active_equity：普通股票型/偏股混合 → 看最新季报重仓股 + 行业配置
- mixed：混合型 → 同 active_equity，但配置更灵活
- quant：量化选股（持仓跨多行业）→ 归到「其他/待分类」除非持仓集中
- broad_market：宽基（全市场/沪深 300/中证 500）→ 「其他/待分类」/「综合策略」

如果 fund_type_hint_accepted=false（系统给的 fund_type 与实际不符），以你看到的为准。

【confidence 规则】
- high：至少 1 条 identity=high 的 announcement/official + 方向明确
- medium：identity=medium 但来源是 platform；或 identity=high 但来源是 platform/media
- low：身份不明（identity=low）、来源是 media/other、或方向模糊

【should_write_to_db】
- high/medium → true
- low → true（仍写入，但 verified=false，让人工补全/待重验证）
- 注意：v2 改动——low 也允许入库（写入"其他/待分类"+ verified=false）

【关键原则】
1. 基金主体：结果确实在描述这只基金，不是同名基金
2. 方向证据：必须找到"该基金投资/跟踪/重仓 XXX"的语句
3. 不得猜测：方向不明 → "其他/待分类"
4. 跨行业重仓的量化/宽基 → 严禁归到某个行业

只返回 JSON，不要解释。"""

USER_PROMPT_TEMPLATE = """目标基金：{fund_name}
{fund_code_line}fund_type 提示：{fund_type_hint}

联网搜索结果（按权威性从高到低，含身份预识别）：

{results_block}

请按指令输出 JSON。"""


def extract_direction_from_web(
    fund_name: str,
    search_results: List[Dict[str, Any]],
    fund_code: Optional[str] = None,
    fund_type_hint: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """调用 LLM 抽取结构化方向 + 身份校验。

    Returns:
        dict（含 should_write_to_db / used_evidence_items）或 None（LLM 失败）。
    """
    if not is_configured():
        logger.warning("LLM 未配置，跳过联网抽取")
        return None

    if not search_results:
        return _default_unknowable(fund_name, fund_code)

    # 序列化：每项带身份信息
    results_block_lines = []
    for i, r in enumerate(search_results[:8]):
        results_block_lines.append(
            f"[{i}] source_type={r.get('source_type','?')}  "
            f"identity={r.get('identity_confidence','?')}\n"
            f"    title: {r.get('title','')}\n"
            f"    url: {r.get('url','')}\n"
            f"    matched_fund_code: {r.get('matched_fund_code') or '-'}\n"
            f"    matched_fund_company: {r.get('matched_fund_company') or '-'}\n"
            f"    snippet: {r.get('snippet','')[:200]}"
        )
    results_block = "\n\n".join(results_block_lines)

    fund_code_line = f"目标基金代码：{fund_code}\n" if fund_code else ""
    fund_type_hint = fund_type_hint or "unknown"

    user_prompt = USER_PROMPT_TEMPLATE.format(
        fund_name=fund_name,
        fund_code_line=fund_code_line,
        fund_type_hint=fund_type_hint,
        results_block=results_block,
    )

    result = chat_completion(
        [
            {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.1,
        max_tokens=600,
        timeout=60,
        response_format={"type": "json_object"},
    )

    if not result["ok"]:
        logger.error("LLM 抽取失败: %s", result["error"])
        return _default_unknowable(fund_name, fund_code)

    parsed = _safe_parse_json(result["content"])
    if parsed is None:
        logger.error("LLM 输出非 JSON: %s", result["content"][:200])
        return _default_unknowable(fund_name, fund_code)

    return _normalize_parsed(parsed, fund_name, fund_code, search_results)


def _safe_parse_json(content: str) -> Optional[Dict[str, Any]]:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return None


def _normalize_parsed(
    parsed: Dict[str, Any],
    fund_name: str,
    fund_code: Optional[str],
    search_results: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """校验 + 身份校验 + fund_type 兜底。"""
    direction = parsed.get("direction", "").strip()
    if direction not in ALLOWED_DIRECTIONS:
        direction = "其他/待分类"

    fund_type_detail = parsed.get("fund_type_detail", "").strip()
    if fund_type_detail not in ALLOWED_FUND_TYPES_DETAIL:
        # 兼容旧版 "qdii"
        if fund_type_detail == "qdii":
            fund_type_detail = "industry_theme"
        elif fund_type_detail in ALLOWED_FUND_TYPES_LEGACY:
            fund_type_detail = fund_type_detail
        else:
            fund_type_detail = "other"

    confidence = parsed.get("confidence", "low").strip().lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "low"

    # ---- 强制身份校验：fund_code 出现冲突 → 降级 confidence ----
    target_code = fund_code or parsed.get("fund_code", "") or ""
    used_indices: List[int] = parsed.get("used_evidence_indices", []) or []
    used_items: List[Dict[str, Any]] = []
    identity_verified_count = 0

    for idx in used_indices:
        if not isinstance(idx, int) or idx < 0 or idx >= len(search_results):
            continue
        r = search_results[idx]
        rid = r.get("identity_confidence", "low")
        if rid == "low":
            continue
        # 若有 target_code 且本条 matched_fund_code 与之不同 → 不计
        if target_code and r.get("matched_fund_code") and r["matched_fund_code"] != target_code:
            logger.info(
                "evidence %d 基金代码不匹配: target=%s matched=%s，剔除",
                idx, target_code, r["matched_fund_code"],
            )
            continue
        identity_verified_count += 1
        used_items.append({
            "url": r.get("url", ""),
            "title": r.get("title", ""),
            "source_type": r.get("source_type", "other"),
            "identity_confidence": rid,
            "matched_fund_code": r.get("matched_fund_code"),
            "matched_fund_company": r.get("matched_fund_company"),
            "snippet_excerpt": (r.get("snippet", "") or "")[:120],
        })

    # 若声称 high 但没有任何 identity=high 证据 → 降级
    if confidence == "high" and identity_verified_count == 0:
        logger.warning("LLM 声称 high 但无 identity 匹配证据 → 降级 medium")
        confidence = "medium"
    if confidence == "medium" and identity_verified_count == 0:
        logger.warning("LLM 声称 medium 但无 identity 匹配证据 → 降级 low")
        confidence = "low"

    # fund_type=quant / broad_market：禁止 high（持仓分散不应归某个行业）
    if fund_type_detail in ("quant", "broad_market") and direction != "其他/待分类":
        if confidence == "high":
            confidence = "medium"

    # evidence_period 推断（从 snippet 中找 2026Q1/Q2 等）
    evidence_period = (parsed.get("evidence_period", "") or "").strip() or _extract_evidence_period(search_results)

    # source_url
    source_url = (parsed.get("source_url", "") or "").strip() or (
        used_items[0]["url"] if used_items else
        (search_results[0].get("url", "") if search_results else "")
    )

    # low 也允许写入（v2 新行为）
    should_write = bool(parsed.get("should_write_to_db", False)) or confidence in ("high", "medium", "low")

    # quant / broad_market 归到"其他/待分类"
    if fund_type_detail in ("quant", "broad_market") and confidence == "low":
        direction = "其他/待分类"

    return {
        "fund_name": fund_name,
        "fund_code": target_code or "",
        "direction": direction,
        "sub_direction": (parsed.get("sub_direction", "") or "").strip() or None,
        "fund_type": parsed.get("fund_type", "other"),
        "fund_type_detail": fund_type_detail,
        "classification_source": "web_search",
        "confidence": confidence,
        "evidence": (parsed.get("evidence", "") or "").strip()[:500],
        "evidence_period": evidence_period or None,
        "source_url": source_url[:500],
        "identity_verified": identity_verified_count > 0,
        "matched_evidence_count": identity_verified_count,
        "used_evidence_items": used_items,
        "should_write_to_db": should_write,
    }


def _extract_evidence_period(search_results: List[Dict[str, Any]]) -> str:
    """从 snippet 中找季度报告期。"""
    pat = re.compile(r"(20\d{2})\s*年?\s*[Qq]\s*([1-4])|(20\d{2})Q([1-4])")
    for r in search_results[:5]:
        snippet = r.get("snippet", "") + " " + r.get("title", "")
        m = pat.search(snippet)
        if m:
            g = m.groups()
            if g[0] and g[1]:
                return f"{g[0]}Q{g[1]}"
            if g[2] and g[3]:
                return f"{g[2]}Q{g[3]}"
    return ""


def _default_unknowable(fund_name: str, fund_code: Optional[str]) -> Dict[str, Any]:
    return {
        "fund_name": fund_name,
        "fund_code": fund_code or "",
        "direction": "其他/待分类",
        "sub_direction": None,
        "fund_type": "other",
        "fund_type_detail": "other",
        "classification_source": "web_search",
        "confidence": "low",
        "evidence": "未取到可用结果",
        "evidence_period": None,
        "source_url": "",
        "identity_verified": False,
        "matched_evidence_count": 0,
        "used_evidence_items": [],
        "should_write_to_db": True,  # v2：low 也写入
    }
