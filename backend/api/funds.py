"""
基金方向知识库 — 后台管理 API
=================================

端点：
  GET  /funds/unconfirmed           列出 verified=False 的基金（待人工补全）
  GET  /funds/needs-reverify        列出需重新验证的基金（主动基金方向漂移）
  GET  /funds/search?q=...          按基金名搜索
  POST /funds/confirm               人工补全后回写（验证）
  POST /funds/drift-check           主动基金方向漂移检测（手动触发）
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services import fund_direction_repo as repo

router = APIRouter(prefix="/funds", tags=["基金方向库"])


@router.get("/unconfirmed")
def list_unconfirmed(limit: int = 100):
    """列出 verified=False 的基金，按最近一次出现时间倒序。"""
    return {"items": repo.list_unconfirmed(limit=limit)}


@router.get("/needs-reverify")
def list_needs_reverify(limit: int = 50):
    """列出需重新验证的基金（主动基金方向漂移 / 人工标记）。"""
    return {"items": repo.list_needs_reverify(limit=limit)}


@router.get("/search")
def search_fund(q: str, limit: int = 20):
    """按基金名/normalized_name 模糊搜索。"""
    from src.storage.db_storage import _get_session
    from src.storage.models import FundDirectionMaster
    from sqlalchemy import or_

    if not q or not q.strip():
        return {"items": []}

    norm = repo.normalize_fund_name(q)
    session = _get_session()
    try:
        rows = (
            session.query(FundDirectionMaster)
            .filter(
                or_(
                    FundDirectionMaster.fund_name.like(f"%{q}%"),
                    FundDirectionMaster.normalized_name.like(f"%{norm}%"),
                )
            )
            .limit(limit)
            .all()
        )
        return {"items": [repo._to_dict(r) for r in rows]}
    finally:
        session.close()


class ConfirmBody(BaseModel):
    fund_id: int
    direction: str
    sub_direction: Optional[str] = None
    fund_type: Optional[str] = None
    evidence: Optional[str] = None


@router.post("/confirm")
def confirm_fund(body: ConfirmBody):
    """人工补全基金方向（标记 verified=True）。"""
    ok = repo.mark_verified(
        fund_id=body.fund_id,
        direction=body.direction,
        sub_direction=body.sub_direction,
        fund_type=body.fund_type,
        evidence=body.evidence,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="fund_id 不存在")
    return {"ok": True}


@router.post("/drift-check")
def drift_check():
    """触发主动基金方向漂移检测（fund_type=active_equity + 30 天未验证 → 标记 needs_reverify）。"""
    n = repo.detect_active_fund_drift()
    return {"ok": True, "marked": n}
