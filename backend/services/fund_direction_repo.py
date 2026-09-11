"""
基金方向知识库 — Service 层
===========================

唯一 Source of Truth：
  - 所有基金方向以 fund_direction_master 表的 (direction, classification_source, confidence) 为准
  - 业务模块不得自行重新判断基金方向

核心能力：
  1. normalize_fund_name()           标准化基金名（去 ".../C" 后缀/空白/小写），用于查重
  2. lookup_fund()                   按 normalized_name 查库（带命中状态）
  3. upsert_fund()                   写入或更新（含 verified / last_verified_at / 计数器）
  4. acquire_web_query_lock()        联网防重入：取锁，30 分钟内同基金不重复查
  5. list_unconfirmed()              列出 verified=False 的基金，供人工补全
  6. mark_verified()                 人工补全后标记为 verified
  7. needs_reverify()                主动基金方向漂移检测：fund_type=active_equity
                                    且距 last_verified_at 超过 N 天 → 重新验证
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any

from sqlalchemy import or_, and_, func
from sqlalchemy.exc import IntegrityError

from src.storage.db_storage import _get_session
from src.storage.models import FundDirectionMaster, Operation

logger = logging.getLogger("backend.fund_direction_repo")


# ============================================================
#  标准化
# ============================================================

# 末尾常见份额后缀：去 C/I/A/H/E 后再查重
_TRAILING_SHARE_SUFFIX = re.compile(r"([AHI])\b$")
# 收尾"混合/股票/联接/指数"等去重（保留，但归一不区分大小写）
_TRIM_PUNCT = str.maketrans("", "", " \t\u3000")


def normalize_fund_name(fund_name: Optional[str]) -> str:
    """把 '南方信息创新混合C...' 这类名标准化为查询键。

    规则：
      1. 去尾部 "..."、"…"
      2. 去掉首尾空白
      3. 统一小写
      4. 去掉末尾单字母份额后缀 C / A / H / I（注意：只在末尾且前后无汉字）

    例：
      "南方信息创新混合C" -> "南方信息创新混合c"
      "大成标普500等..."   -> "大成标普500等"
      "华宝纳斯达克精选股票(QDII..." -> "华宝纳斯达克精选股票(qdii..."
    """
    if not fund_name:
        return ""
    s = fund_name.strip()
    # 去尾部省略号
    s = s.rstrip(".").rstrip("…")
    s = s.strip()
    s = s.translate(_TRIM_PUNCT)
    s = s.lower()
    return s


# ============================================================
#  查询
# ============================================================

def lookup_fund(
    fund_name: Optional[str],
    fund_code: Optional[str] = None,
) -> Optional[FundDirectionSnapshot]:
    """按 normalized_name（优先）或 fund_code 查询。

    Returns:
        FundDirectionSnapshot（可能 verified=False / confidence=low），
        或 None（库中不存在）。
    """
    norm = normalize_fund_name(fund_name)
    if not norm and not fund_code:
        return None

    session = _get_session()
    try:
        q = session.query(FundDirectionMaster)
        conditions = []
        if norm:
            conditions.append(FundDirectionMaster.normalized_name == norm)
        if fund_code:
            conditions.append(FundDirectionMaster.fund_code == fund_code)
        if not conditions:
            return None
        rec = q.filter(or_(*conditions)).first()
        return _detach(rec) if rec else None
    finally:
        session.close()


def lookup_many(norm_names: List[str]) -> Dict[str, FundDirectionSnapshot]:
    """批量查询，返回 {normalized_name: FundDirectionSnapshot}。"""
    if not norm_names:
        return {}
    session = _get_session()
    try:
        rows = (
            session.query(FundDirectionMaster)
            .filter(FundDirectionMaster.normalized_name.in_(norm_names))
            .all()
        )
        return {r.normalized_name: _detach(r) for r in rows}
    finally:
        session.close()


# ============================================================
#  写入 / 更新
# ============================================================

def upsert_fund(
    fund_name: str,
    *,
    direction: str,
    sub_direction: Optional[str] = None,
    fund_type: Optional[str] = None,
    classification_source: str,
    confidence: str,
    evidence: Optional[str] = None,
    source_url: Optional[str] = None,
    fund_code: Optional[str] = None,
    verified: bool = False,
    set_lock: Optional[int] = None,
    fund_type_detail: Optional[str] = None,
    evidence_period: Optional[str] = None,
    evidence_items_json: Optional[str] = None,
    last_search_at: Optional[datetime] = None,
    next_reverify_at: Optional[datetime] = None,
) -> FundDirectionSnapshot:
    """写入或更新一只基金的方向信息。

    Args:
        fund_name: 原始基金名（可能带 "..."）
        classification_source: manual / rule / llm / web_search
        confidence: high / medium / low
        set_lock: 锁定秒数（用于 web_search 期间防重入）
        fund_type_detail: index / industry_theme / active_equity / mixed / quant / broad_market
        evidence_period: 季度报告期，如 2026Q2
        evidence_items_json: 序列化后的 evidence_items 列表
        last_search_at: 联网时间（用于 30 天防重入）
        next_reverify_at: 下一次允许联网时间
    """
    norm = normalize_fund_name(fund_name)
    if not norm:
        raise ValueError("fund_name 不能为空")

    now = datetime.now()
    lock_until = (now + timedelta(seconds=set_lock)) if set_lock else None
    # 默认 30 天内不重复联网
    if last_search_at and not next_reverify_at:
        next_reverify_at = last_search_at + timedelta(days=30)

    session = _get_session()
    try:
        rec = (
            session.query(FundDirectionMaster)
            .filter(FundDirectionMaster.normalized_name == norm)
            .first()
        )
        if rec is None:
            rec = FundDirectionMaster(
                fund_name=fund_name.strip(),
                fund_code=fund_code,
                normalized_name=norm,
                direction=direction,
                sub_direction=sub_direction,
                fund_type=fund_type,
                fund_type_detail=fund_type_detail,
                classification_source=classification_source,
                confidence=confidence,
                evidence=evidence,
                evidence_period=evidence_period,
                evidence_items_json=evidence_items_json,
                source_url=source_url,
                verified=verified,
                first_seen_at=now,
                last_verified_at=now if verified else None,
                last_seen_at=now,
                last_search_at=last_search_at,
                next_reverify_at=next_reverify_at,
                seen_count=1,
                web_query_locked_until=lock_until,
            )
            session.add(rec)
            try:
                session.commit()
                logger.info(
                    "fund_direction_master 写入: %s -> %s (%s, %s)",
                    norm, direction, classification_source, confidence,
                )
            except IntegrityError:
                session.rollback()
                rec = (
                    session.query(FundDirectionMaster)
                    .filter(FundDirectionMaster.normalized_name == norm)
                    .first()
                )
        else:
            # 已存在：仅当新来源更权威时更新
            if _should_update(rec, classification_source, confidence):
                rec.direction = direction
                rec.sub_direction = sub_direction
                rec.fund_type = fund_type
                rec.fund_type_detail = fund_type_detail or rec.fund_type_detail
                rec.classification_source = classification_source
                rec.confidence = confidence
                rec.evidence = evidence
                rec.evidence_period = evidence_period or rec.evidence_period
                rec.evidence_items_json = evidence_items_json or rec.evidence_items_json
                rec.source_url = source_url or rec.source_url
                if verified:
                    rec.verified = True
                    rec.last_verified_at = now
            # 总是更新：fund_code / last_seen_at / 计数器 / lock
            if fund_code and not rec.fund_code:
                rec.fund_code = fund_code
            rec.last_seen_at = now
            if last_search_at:
                rec.last_search_at = last_search_at
            if next_reverify_at:
                rec.next_reverify_at = next_reverify_at
            rec.seen_count = (rec.seen_count or 0) + 1
            if lock_until:
                rec.web_query_locked_until = lock_until
            session.commit()
        return _detach(rec)
    finally:
        session.close()


def _detach(rec: FundDirectionMaster) -> FundDirectionSnapshot:
    """把 ORM 对象转为可跨 session 使用的快照。"""
    return FundDirectionSnapshot(rec)


def _should_update(
    existing: FundDirectionMaster,
    new_source: str,
    new_confidence: str,
) -> bool:
    """判断新记录是否应覆盖现有 direction。

    优先级：manual > web_search > llm > rule
    且 confidence 更高时优先。
    """
    PRIORITY = {"manual": 4, "web_search": 3, "llm": 2, "rule": 1}
    CONF = {"high": 3, "medium": 2, "low": 1}

    cur_score = PRIORITY.get(existing.classification_source, 0) * 10 + CONF.get(existing.confidence, 0)
    new_score = PRIORITY.get(new_source, 0) * 10 + CONF.get(new_confidence, 0)
    return new_score > cur_score


# ============================================================
#  联网搜索防重入锁
# ============================================================

def acquire_web_query_lock(
    fund_name: str,
    ttl_seconds: int = 1800,
) -> bool:
    """尝试为该基金获取联网搜索锁。

    同时检查 next_reverify_at：30 天内即使锁已释放也拒绝重新联网。

    Returns:
        True: 取锁成功（可以发起联网搜索）
        False: 锁已被他人持有 / 未到 next_reverify_at 时间
    """
    norm = normalize_fund_name(fund_name)
    if not norm:
        return True

    now = datetime.now()
    session = _get_session()
    try:
        rec = (
            session.query(FundDirectionMaster)
            .filter(FundDirectionMaster.normalized_name == norm)
            .with_for_update()
            .first()
        )
        if rec is None:
            return True  # 库里没有，等调用方 upsert
        # 30 天防重入（优先级高于短时锁）
        if rec.next_reverify_at and rec.next_reverify_at > now and not rec.verified:
            logger.info(
                "基金 %s 距下次允许重验证还有 %s（next_reverify_at），跳过联网",
                norm, rec.next_reverify_at.isoformat(),
            )
            return False
        if rec.web_query_locked_until and rec.web_query_locked_until > now:
            logger.info(
                "基金 %s 联网锁未释放（%s 前），跳过",
                norm, rec.web_query_locked_until.isoformat(),
            )
            return False
        rec.web_query_locked_until = now + timedelta(seconds=ttl_seconds)
        session.commit()
        return True
    finally:
        session.close()


# ============================================================
#  未确认 / 待人工补全
# ============================================================

def list_unconfirmed(limit: int = 100) -> List[Dict[str, Any]]:
    """列出 verified=False 的基金，按 last_seen_at 倒序。"""
    session = _get_session()
    try:
        rows = (
            session.query(FundDirectionMaster)
            .filter(FundDirectionMaster.verified == False)  # noqa: E712
            .order_by(FundDirectionMaster.last_seen_at.desc())
            .limit(limit)
            .all()
        )
        return [_to_dict(r) for r in rows]
    finally:
        session.close()


def list_needs_reverify(limit: int = 50) -> List[Dict[str, Any]]:
    """列出需要重新验证的基金（主动基金方向漂移）。"""
    session = _get_session()
    try:
        rows = (
            session.query(FundDirectionMaster)
            .filter(
                or_(
                    FundDirectionMaster.needs_reverify == True,  # noqa: E712
                    and_(
                        FundDirectionMaster.fund_type == "active_equity",
                        FundDirectionMaster.last_verified_at < datetime.now() - timedelta(days=30),
                    ),
                )
            )
            .order_by(FundDirectionMaster.last_verified_at.asc())
            .limit(limit)
            .all()
        )
        return [_to_dict(r) for r in rows]
    finally:
        session.close()


def mark_verified(
    fund_id: int,
    direction: str,
    *,
    sub_direction: Optional[str] = None,
    fund_type: Optional[str] = None,
    evidence: Optional[str] = None,
) -> bool:
    """人工补全基金方向。"""
    session = _get_session()
    try:
        rec = session.query(FundDirectionMaster).filter_by(id=fund_id).first()
        if rec is None:
            return False
        rec.direction = direction
        rec.sub_direction = sub_direction
        rec.fund_type = fund_type
        rec.evidence = evidence or rec.evidence
        rec.verified = True
        rec.classification_source = "manual"
        rec.last_verified_at = datetime.now()
        rec.needs_reverify = False
        session.commit()
        return True
    finally:
        session.close()


def set_needs_reverify(fund_id: int, needs: bool = True) -> None:
    """标记某基金需要重新验证。"""
    session = _get_session()
    try:
        rec = session.query(FundDirectionMaster).filter_by(id=fund_id).first()
        if rec:
            rec.needs_reverify = needs
            session.commit()
    finally:
        session.close()


# ============================================================
#  辅助
# ============================================================

class FundDirectionSnapshot:
    """不可变快照：跨 session 安全，所有字段都是值类型。"""
    __slots__ = (
        "id", "fund_name", "fund_code", "normalized_name",
        "direction", "sub_direction", "fund_type",
        "classification_source", "confidence", "evidence", "source_url",
        "verified", "needs_reverify",
        "first_seen_at", "last_verified_at", "last_seen_at", "updated_at",
        "last_search_at", "next_reverify_at",
        "evidence_period", "fund_type_detail", "evidence_items_json",
        "seen_count",
    )

    def __init__(self, rec) -> None:
        self.id = rec.id
        self.fund_name = rec.fund_name
        self.fund_code = rec.fund_code
        self.normalized_name = rec.normalized_name
        self.direction = rec.direction
        self.sub_direction = rec.sub_direction
        self.fund_type = rec.fund_type
        self.classification_source = rec.classification_source
        self.confidence = rec.confidence
        self.evidence = rec.evidence
        self.source_url = rec.source_url
        self.verified = rec.verified
        self.needs_reverify = rec.needs_reverify
        self.first_seen_at = rec.first_seen_at
        self.last_verified_at = rec.last_verified_at
        self.last_seen_at = rec.last_seen_at
        self.updated_at = rec.updated_at
        self.last_search_at = getattr(rec, "last_search_at", None)
        self.next_reverify_at = getattr(rec, "next_reverify_at", None)
        self.evidence_period = getattr(rec, "evidence_period", None)
        self.fund_type_detail = getattr(rec, "fund_type_detail", None)
        self.evidence_items_json = getattr(rec, "evidence_items_json", None)
        self.seen_count = rec.seen_count

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "fund_name": self.fund_name,
            "fund_code": self.fund_code,
            "normalized_name": self.normalized_name,
            "direction": self.direction,
            "sub_direction": self.sub_direction,
            "fund_type": self.fund_type,
            "classification_source": self.classification_source,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "source_url": self.source_url,
            "verified": self.verified,
            "needs_reverify": self.needs_reverify,
            "first_seen_at": self.first_seen_at.isoformat() if self.first_seen_at else None,
            "last_verified_at": self.last_verified_at.isoformat() if self.last_verified_at else None,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_search_at": self.last_search_at.isoformat() if self.last_search_at else None,
            "next_reverify_at": self.next_reverify_at.isoformat() if self.next_reverify_at else None,
            "evidence_period": self.evidence_period,
            "fund_type_detail": self.fund_type_detail,
            "evidence_items_json": self.evidence_items_json,
            "seen_count": self.seen_count,
        }


def _to_dict(rec) -> Dict[str, Any]:
    """兼容：把 ORM 记录或 Snapshot 转为 dict。"""
    if isinstance(rec, FundDirectionSnapshot):
        return rec.to_dict()
    return {
        "id": rec.id,
        "fund_name": rec.fund_name,
        "fund_code": rec.fund_code,
        "normalized_name": rec.normalized_name,
        "direction": rec.direction,
        "sub_direction": rec.sub_direction,
        "fund_type": rec.fund_type,
        "classification_source": rec.classification_source,
        "confidence": rec.confidence,
        "evidence": rec.evidence,
        "source_url": rec.source_url,
        "verified": rec.verified,
        "needs_reverify": rec.needs_reverify,
        "first_seen_at": rec.first_seen_at.isoformat() if rec.first_seen_at else None,
        "last_verified_at": rec.last_verified_at.isoformat() if rec.last_verified_at else None,
        "last_seen_at": rec.last_seen_at.isoformat() if rec.last_seen_at else None,
        "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
        "last_search_at": rec.last_search_at.isoformat() if getattr(rec, "last_search_at", None) else None,
        "next_reverify_at": rec.next_reverify_at.isoformat() if getattr(rec, "next_reverify_at", None) else None,
        "evidence_period": getattr(rec, "evidence_period", None),
        "fund_type_detail": getattr(rec, "fund_type_detail", None),
        "evidence_items_json": getattr(rec, "evidence_items_json", None),
        "seen_count": rec.seen_count,
    }


# ============================================================
#  主动基金方向漂移检测
# ============================================================

def detect_active_fund_drift() -> int:
    """扫描所有 active_equity 基金，对距上次验证 >30 天的标记 needs_reverify=True。

    Returns: 被标记的基金数量。
    """
    cutoff = datetime.now() - timedelta(days=30)
    session = _get_session()
    try:
        rows = (
            session.query(FundDirectionMaster)
            .filter(
                FundDirectionMaster.fund_type == "active_equity",
                FundDirectionMaster.last_verified_at < cutoff,
                FundDirectionMaster.needs_reverify == False,  # noqa: E712
            )
            .all()
        )
        for r in rows:
            r.needs_reverify = True
        session.commit()
        logger.info("主动基金方向漂移检测: 标记 %d 条需重新验证", len(rows))
        return len(rows)
    finally:
        session.close()
