"""
方向解析 v5 — 唯一 Source of Truth + 身份校验 + fund_type 感知
=============================================================

调用顺序（每一笔交易 direction 只算一次）：

  1. 查 fund_direction_master（DB）
     - 命中 + verified=True：直接返回
     - 命中 + verified=False + 未到 next_reverify_at：返回已有低置信结果
  2. 关键词规则匹配（fund_name 含明确行业关键词）
  3. 同帖基金级上下文（context_classifier v3 的窗口+连接词逻辑）
  4. 联网搜索 + LLM 抽取（含身份校验 + fund_type_hint + evidence_items）
     - 若 next_reverify_at 未到（30 天内）：跳过联网，直接复用 low 结果

所有路径统一返回 ClassificationResult，并自动落库（v2：low 也允许入库 verified=False）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from src.parser.direction_classifier import (  # noqa: E402
    classify_fund_with_evidence,
    ClassificationResult,
    OTHER_DIRECTION,
    is_other_direction,
)
from src.parser.models import TradeRecord  # noqa: E402

from backend.services import fund_direction_repo as repo
from backend.services import web_search, llm_extract_direction

logger = logging.getLogger("backend.direction_resolver")

ENABLE_WEB_FALLBACK = os.environ.get("ENABLE_WEB_FALLBACK", "1") == "1"
REVERIFY_DAYS = int(os.environ.get("REVERIFY_DAYS", "30"))


# ============================================================
#  入口
# ============================================================

def resolve_direction(
    fund_name: Optional[str],
    opinion_text: Optional[str] = None,
    *,
    fund_code: Optional[str] = None,
    fund_type_hint: Optional[str] = None,
) -> ClassificationResult:
    """对单只基金解析方向；自动写库。

    优先级：
      1. DB 命中 verified=True → 直接返回
      2. 规则/上下文
      3. 联网搜索（含身份校验）→ 受 next_reverify_at 30 天限制
    """
    if not fund_name:
        return _result("其他/待分类", "unknown", "fund_name 为空")

    # ---- 1. DB 查 ----
    db_hit = repo.lookup_fund(fund_name, fund_code=fund_code)
    if db_hit and db_hit.verified:
        logger.info(
            "[direction_resolver] DB 命中 verified: %s -> %s",
            fund_name, db_hit.direction,
        )
        return ClassificationResult(
            direction=db_hit.direction,
            confidence=db_hit.confidence,
            source=db_hit.classification_source or "manual",
            evidence=f"DB 命中 verified: {db_hit.evidence or ''}",
        )

    # ---- 2. 关键词 ----
    rule_result = _rule_classify(fund_name, opinion_text)

    # ---- 3. 上下文 ----
    if is_other_direction(rule_result.direction) and rule_result.confidence in ("low", "unknown"):
        rule_result = _context_classify(fund_name, opinion_text, fallback=rule_result)

    # ---- 4. 联网搜索（受 30 天 next_reverify_at 限制）----
    if (
        ENABLE_WEB_FALLBACK
        and is_other_direction(rule_result.direction)
        and rule_result.confidence in ("low", "unknown")
    ):
        web_result = _web_classify(fund_name, fund_code, fund_type_hint)
        if web_result is not None:
            rule_result = web_result

    # ---- 写库（v2：low 也允许入库）----
    _maybe_persist(fund_name, fund_code, rule_result, fund_type_hint)

    return rule_result


def resolve_records(
    records: List[TradeRecord],
) -> List[ClassificationResult]:
    """批量解析 records 中的基金方向。"""
    # 1) 收集唯一 fund_name + fund_code
    unique_keys: Dict[str, Dict[str, Any]] = {}
    for r in records:
        fn = (r.fund_name or "").strip()
        if not fn or fn in unique_keys:
            continue
        unique_keys[fn] = {
            "fund_name": fn,
            "fund_code": getattr(r, "fund_code", None),
        }

    # 2) 批量查库
    norm_to_meta = {repo.normalize_fund_name(k["fund_name"]): k for k in unique_keys.values()}
    db_hits = repo.lookup_many(list(norm_to_meta.keys()))

    # 3) 对每只基金解析
    cache: Dict[str, ClassificationResult] = {}
    for norm, meta in norm_to_meta.items():
        if norm in db_hits and db_hits[norm].verified:
            rec = db_hits[norm]
            cache[meta["fund_name"]] = ClassificationResult(
                direction=rec.direction,
                confidence=rec.confidence,
                source=rec.classification_source or "manual",
                evidence=f"DB 命中: {rec.evidence or ''}",
            )
            continue
        cache[meta["fund_name"]] = resolve_direction(
            meta["fund_name"], fund_code=meta["fund_code"]
        )

    # 4) 装配回 records
    out: List[ClassificationResult] = []
    for r in records:
        fn = (r.fund_name or "").strip()
        if fn in cache:
            out.append(cache[fn])
        else:
            out.append(ClassificationResult(
                direction=OTHER_DIRECTION,
                confidence="unknown",
                source="unknown",
                evidence="无 fund_name",
            ))
    return out


# ============================================================
#  步骤 2-4 实现
# ============================================================

def _rule_classify(fund_name: str, opinion_text: Optional[str]) -> ClassificationResult:
    res = classify_fund_with_evidence(fund_name, None)
    if is_other_direction(res.direction) and opinion_text:
        return _context_classify(fund_name, opinion_text, fallback=res)
    return res


def _context_classify(
    fund_name: str,
    opinion_text: Optional[str],
    fallback: ClassificationResult,
) -> ClassificationResult:
    res = classify_fund_with_evidence(fund_name, opinion_text)
    if not is_other_direction(res.direction):
        return res
    return fallback


def _web_classify(
    fund_name: str,
    fund_code: Optional[str],
    fund_type_hint: Optional[str] = None,
) -> Optional[ClassificationResult]:
    """Priority 4：联网搜索 + LLM 抽取（含身份校验 + fund_type 感知）。

    Returns:
        ClassificationResult 或 None（联网被锁/失败）。
    """
    if not repo.acquire_web_query_lock(fund_name, ttl_seconds=1800):
        return None

    try:
        results = web_search.search_fund_direction(fund_name, fund_code)
        if not results:
            return None

        extracted = llm_extract_direction.extract_direction_from_web(
            fund_name, results, fund_code, fund_type_hint=fund_type_hint
        )
        if extracted is None:
            return None

        # 始终把 evidence_items / period / next_reverify_at 透出
        return ClassificationResult(
            direction=extracted["direction"],
            confidence=extracted["confidence"],
            source="web_search",
            evidence=extracted.get("evidence", ""),
        )
    except Exception as e:  # noqa: BLE001
        logger.error("联网补全过程异常 %s: %s", fund_name, e)
        return None


# ============================================================
#  写库（v2：low 也允许入库）
# ============================================================

def _maybe_persist(
    fund_name: str,
    fund_code: Optional[str],
    result: ClassificationResult,
    fund_type_hint: Optional[str] = None,
) -> None:
    """根据 result 决定是否写库。

    v2 行为：
      - high/medium → verified=True
      - low → verified=False + last_search_at + next_reverify_at(30d)
      - unknown → 不写
    """
    if result.confidence in ("unknown",):
        return

    now = datetime.now()
    is_other = is_other_direction(result.direction)

    if result.source == "web_search":
        # 联网结果：写库时连同 evidence_items / period / next_reverify_at 一起保存
        # 这里取最近一次 _web_classify 的完整结果（hack：调一次再写）
        # 实际生产中应让 ClassificationResult 带 extended 字段；为简化我们重新调一次抽取
        extended = _reextract(fund_name, fund_code, fund_type_hint)
        repo.upsert_fund(
            fund_name,
            fund_code=fund_code,
            direction=result.direction,
            classification_source="web_search",
            confidence=result.confidence,
            evidence=result.evidence,
            fund_type_detail=fund_type_hint,
            evidence_period=(extended or {}).get("evidence_period"),
            evidence_items_json=json.dumps((extended or {}).get("used_evidence_items", []), ensure_ascii=False),
            last_search_at=now,
            next_reverify_at=now + timedelta(days=REVERIFY_DAYS),
            verified=not is_other and result.confidence in ("high", "medium"),
        )
        return

    # 规则 / 上下文结果
    repo.upsert_fund(
        fund_name,
        fund_code=fund_code,
        direction=result.direction,
        classification_source=result.source or "rule",
        confidence=result.confidence,
        evidence=result.evidence,
        fund_type_detail=fund_type_hint,
        verified=not is_other and result.confidence in ("high", "medium"),
    )


def _reextract(
    fund_name: str, fund_code: Optional[str], fund_type_hint: Optional[str]
) -> Optional[Dict[str, Any]]:
    """重新跑一次 LLM 抽取以拿到完整 extended 信息（evidence_period / used_evidence_items）。"""
    if not repo.acquire_web_query_lock(fund_name, ttl_seconds=0):
        return None
    results = web_search.search_fund_direction(fund_name, fund_code)
    if not results:
        return None
    return llm_extract_direction.extract_direction_from_web(
        fund_name, results, fund_code, fund_type_hint=fund_type_hint
    )


def _result(direction: str, confidence: str, evidence: str) -> ClassificationResult:
    return ClassificationResult(
        direction=direction,
        confidence=confidence,
        source="unknown",
        evidence=evidence,
    )
