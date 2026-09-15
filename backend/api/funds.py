"""
基金方向知识库 — 后台管理 API
=================================

端点（基金方向库页面使用）：
  GET    /funds                       列表 + 多条件筛选 + 分页（必带 status=unconfirmed/confirmed/all 等）
  POST   /funds                       人工新增（manual / verified=true）
  GET    /funds/{id}                  获取单只基金详情
  PUT    /funds/{id}                  人工编辑（强制 manual + verified=true）
  POST   /funds/{id}/confirm          人工确认（用于"待确认"/"临时判断" → "已确认"）
  POST   /funds/{id}/reanalyze        触发 direction_resolver 重跑（仅手动）
  GET    /funds/{id}/evidence         查看 AI 证据明细

  GET    /funds/unconfirmed           列出 verified=False 的基金（保留以兼容）
  GET    /funds/needs-reverify        列出需重新验证的基金（保留以兼容）
  GET    /funds/search?q=...          按基金名搜索（保留以兼容）
  POST   /funds/confirm               旧版 confirm 端点（保留）
  POST   /funds/drift-check           主动基金方向漂移检测（保留）
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_

from backend.services import fund_direction_repo as repo
from backend.services import direction_resolver as resolver

logger = logging.getLogger("backend.api.funds")

# ============================================================
#  中文 / 枚举映射（前端展示用）
# ============================================================

SOURCE_LABEL = {
    "manual": "人工确认",
    "rule": "规则识别",
    "web_search": "AI联网识别",
    "context": "上下文识别",
    "llm": "LLM识别",
    "unknown": "未知",
}

CONFIDENCE_LABEL = {
    "high": "高",
    "medium": "中",
    "low": "低",
}

FUND_TYPE_LABEL = {
    "index": "指数基金",
    "industry_theme": "行业主题基金",
    "active_equity": "主动权益",
    "mixed": "混合型",
    "quant": "量化基金",
    "qdii": "QDII",
    "broad_market": "宽基",
    "bond": "纯债",
    "pure_bond": "纯债",
    "other": "其他",
}

DIRECTION_LABEL = {
    "港股方向": "港股方向",
    "黄金": "黄金",
    "债券": "债券",
    "CPO/光模块": "CPO/光模块",
    "半导体/科创芯片": "半导体/科创芯片",
    "创新药/医药": "创新药/医药",
    "全球科技/QDII": "全球科技/QDII",
    "白酒/消费": "白酒/消费",
    "资源/有色金属": "资源/有色金属",
    "量化/全市场": "量化/全市场",
    "固收+/股债混合": "固收+/股债混合",
    "其他/待分类": "其他/待分类",
}


def _status_label(item: dict) -> str:
    """已确认 / 待确认（仅两档）

    - 已确认：人工确认过的（verified=True）
    - 待确认：AI 给的临时判断，或未识别方向（其他/待分类）
    """
    if item.get("verified"):
        return "已确认"
    return "待确认"


def _enrich(item: dict) -> dict:
    """附加前端用中文标签字段。"""
    out = dict(item)
    out["source_label"] = SOURCE_LABEL.get(item.get("classification_source"), item.get("classification_source") or "-")
    out["confidence_label"] = CONFIDENCE_LABEL.get(item.get("confidence"), item.get("confidence") or "-")
    out["fund_type_label"] = FUND_TYPE_LABEL.get(item.get("fund_type_detail") or item.get("fund_type"),
                                                item.get("fund_type_detail") or item.get("fund_type") or "-")
    out["direction_label"] = DIRECTION_LABEL.get(item.get("direction"), item.get("direction") or "-")
    out["status_label"] = _status_label(item)
    return out


# ============================================================
#  Router
# ============================================================

router = APIRouter(prefix="/funds", tags=["基金方向库"])


# ---- 列表（带筛选 + 分页）----
@router.get("")
def list_funds(
    keyword: Optional[str] = Query(None, description="基金名称模糊搜索"),
    direction: Optional[str] = Query(None, description="方向精确匹配"),
    fund_type: Optional[str] = Query(None, description="fund_type_detail 匹配"),
    status: Optional[str] = Query(None, description="状态过滤: confirmed/unconfirmed/all"),
    created_date: Optional[str] = Query(None, description="first_seen_at 日期 (YYYY-MM-DD)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """基金方向库列表 + 筛选 + 分页。"""
    from src.storage.db_storage import _get_session
    from src.storage.models import FundDirectionMaster
    from sqlalchemy import desc

    session = _get_session()
    try:
        q = session.query(FundDirectionMaster)

        if keyword:
            kw = keyword.strip()
            norm = repo.normalize_fund_name(kw)
            q = q.filter(or_(
                FundDirectionMaster.fund_name.like(f"%{kw}%"),
                FundDirectionMaster.normalized_name.like(f"%{norm}%"),
            ))
        if direction:
            q = q.filter(FundDirectionMaster.direction == direction.strip())
        if fund_type:
            q = q.filter(FundDirectionMaster.fund_type_detail == fund_type.strip())
        if status == "confirmed":
            q = q.filter(FundDirectionMaster.verified == True)  # noqa: E712
        elif status == "unconfirmed":
            q = q.filter(FundDirectionMaster.verified == False)  # noqa: E712
        if created_date:
            try:
                d = datetime.strptime(created_date, "%Y-%m-%d").date()
                q = q.filter(FundDirectionMaster.first_seen_at >= d)
            except ValueError:
                pass

        total = q.count()
        rows = (
            q.order_by(desc(FundDirectionMaster.first_seen_at), desc(FundDirectionMaster.id))
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        items = [_enrich(repo._to_dict(r)) for r in rows]
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "filters": {
                "direction": sorted(DIRECTION_LABEL.keys()),
                "fund_type": sorted(FUND_TYPE_LABEL.keys()),
                "status": ["confirmed", "unconfirmed", "all"],
            },
        }
    finally:
        session.close()


# ---- 导出（按当前筛选条件，返回 JSON 文件）----
@router.get("/export")
def export_funds(
    keyword: Optional[str] = Query(None),
    direction: Optional[str] = Query(None),
    fund_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    created_date: Optional[str] = Query(None),
    page_size: int = Query(500, ge=1, le=2000),
):
    """按当前筛选条件导出基金方向为 JSON（每行一条，含核心可编辑字段）。"""
    from src.storage.db_storage import _get_session
    from src.storage.models import FundDirectionMaster
    from sqlalchemy import desc
    from fastapi.responses import JSONResponse

    session = _get_session()
    try:
        q = session.query(FundDirectionMaster)
        if keyword:
            kw = keyword.strip()
            norm = repo.normalize_fund_name(kw)
            q = q.filter(or_(
                FundDirectionMaster.fund_name.like(f"%{kw}%"),
                FundDirectionMaster.normalized_name.like(f"%{norm}%"),
            ))
        if direction:
            q = q.filter(FundDirectionMaster.direction == direction.strip())
        if fund_type:
            q = q.filter(FundDirectionMaster.fund_type_detail == fund_type.strip())
        if status == "confirmed":
            q = q.filter(FundDirectionMaster.verified == True)  # noqa: E712
        elif status == "unconfirmed":
            q = q.filter(FundDirectionMaster.verified == False)  # noqa: E712
        if created_date:
            try:
                d = datetime.strptime(created_date, "%Y-%m-%d").date()
                q = q.filter(FundDirectionMaster.first_seen_at >= d)
            except ValueError:
                pass

        rows = q.order_by(desc(FundDirectionMaster.id)).limit(page_size).all()
        items = []
        for r in rows:
            items.append({
                "fund_name": r.fund_name,
                "fund_code": r.fund_code or "",
                "direction": r.direction,
                "sub_direction": r.sub_direction or "",
                "fund_type": r.fund_type_detail or r.fund_type or "",
                "evidence": r.evidence or "",
                "evidence_period": r.evidence_period or "",
            })
        return JSONResponse(content={
            "version": 1,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "total": len(items),
            "items": items,
        })
    finally:
        session.close()


# ---- 导入（按 fund_code 优先 → normalized_name 匹配覆盖/新增）----
class ImportBody(BaseModel):
    items: List[dict] = Field(default_factory=list)


@router.post("/import")
def import_funds(body: ImportBody):
    """导入基金方向 JSON（修改后覆盖）。

    每条记录支持字段：fund_name(*), fund_code, direction(*), sub_direction, fund_type, evidence, evidence_period
    - 匹配规则：fund_code 精确 → normalized_name 精确
    - 命中 → 覆盖（direction / sub_direction / fund_type / evidence / evidence_period / fund_code），保持人工属性（classification_source='manual', verified=true, confidence='high'）
    - 未命中 → 新增（manual / verified=true / high）
    """
    from src.storage.db_storage import _get_session
    from src.storage.models import FundDirectionMaster
    from sqlalchemy.exc import IntegrityError

    if not body.items:
        raise HTTPException(status_code=400, detail="items 不能为空")

    session = _get_session()
    added = 0
    updated = 0
    failed = 0
    errors = []
    now = datetime.now()

    for it in body.items:
        try:
            fund_name = (it.get("fund_name") or "").strip()
            if not fund_name:
                failed += 1
                errors.append({"fund_name": "", "msg": "fund_name 为空"})
                continue
            direction = (it.get("direction") or "").strip()
            if not direction:
                failed += 1
                errors.append({"fund_name": fund_name, "msg": "direction 为空"})
                continue
            if direction not in DIRECTION_LABEL:
                failed += 1
                errors.append({"fund_name": fund_name, "msg": f"direction 非法: {direction}"})
                continue

            fund_code = (it.get("fund_code") or "").strip() or None
            norm = repo.normalize_fund_name(fund_name)
            sub_dir = (it.get("sub_direction") or "").strip() or None
            fund_type = (it.get("fund_type") or "").strip() or None
            evidence = (it.get("evidence") or "").strip() or None
            evidence_period = (it.get("evidence_period") or "").strip() or None

            existing = None
            if fund_code:
                existing = session.query(FundDirectionMaster).filter_by(fund_code=fund_code).first()
            if existing is None:
                existing = session.query(FundDirectionMaster).filter_by(normalized_name=norm).first()

            if existing:
                existing.fund_name = fund_name
                existing.fund_code = fund_code or existing.fund_code
                existing.normalized_name = norm
                existing.direction = direction
                existing.sub_direction = sub_dir
                existing.fund_type = fund_type
                existing.fund_type_detail = fund_type
                # 导入 = 人工确认：强制 manual / verified
                existing.classification_source = "manual"
                existing.confidence = "high"
                existing.verified = True
                existing.evidence_period = evidence_period
                if evidence:
                    if "[人工确认]" not in (existing.evidence or ""):
                        existing.evidence = (existing.evidence or "") + "\n[人工导入] " + evidence
                    else:
                        existing.evidence = (existing.evidence or "") + " " + evidence
                existing.last_verified_at = now
                existing.needs_reverify = False
                updated += 1
            else:
                rec = FundDirectionMaster(
                    fund_name=fund_name,
                    fund_code=fund_code,
                    normalized_name=norm,
                    direction=direction,
                    sub_direction=sub_dir,
                    fund_type=fund_type,
                    fund_type_detail=fund_type,
                    classification_source="manual",
                    confidence="high",
                    evidence=evidence or "[人工导入]",
                    evidence_period=evidence_period,
                    verified=True,
                    first_seen_at=now,
                    last_verified_at=now,
                    last_seen_at=now,
                    seen_count=1,
                )
                session.add(rec)
                added += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            errors.append({"fund_name": it.get("fund_name"), "msg": str(e)[:200]})

    try:
        session.commit()
    except IntegrityError as e:
        session.rollback()
        raise HTTPException(status_code=409, detail=f"提交失败: {e}")

    return {
        "ok": True,
        "added": added,
        "updated": updated,
        "failed": failed,
        "errors": errors[:20],
    }


# ---- 枚举（下拉选项）----
@router.get("/options")
def get_options():
    """返回所有下拉选项的中文标签。"""
    return {
        "direction": [{"value": k, "label": v} for k, v in DIRECTION_LABEL.items()],
        "fund_type": [{"value": k, "label": v} for k, v in FUND_TYPE_LABEL.items()],
        "source": [{"value": k, "label": v} for k, v in SOURCE_LABEL.items()],
        "confidence": [{"value": k, "label": v} for k, v in CONFIDENCE_LABEL.items()],
        "status": [
            {"value": "confirmed", "label": "已确认"},
            {"value": "unconfirmed", "label": "待确认"},
            {"value": "all", "label": "全部"},
        ],
    }


# ---- 单只基金详情 ----
@router.get("/{fund_id}")
def get_fund(fund_id: int):
    from src.storage.db_storage import _get_session
    from src.storage.models import FundDirectionMaster
    from fastapi import HTTPException

    session = _get_session()
    try:
        rec = session.query(FundDirectionMaster).filter_by(id=fund_id).first()
        if not rec:
            raise HTTPException(status_code=404, detail="fund_id 不存在")
        return _enrich(repo._to_dict(rec))
    finally:
        session.close()


# ---- 人工新增 ----
class CreateBody(BaseModel):
    fund_name: str
    fund_code: Optional[str] = None
    direction: str
    fund_type: Optional[str] = None
    sub_direction: Optional[str] = None
    evidence: Optional[str] = None


@router.post("")
def create_fund(body: CreateBody):
    """人工新增基金方向。

    - classification_source = manual
    - confidence = high
    - verified = True
    - 若 DB 已存在（fund_code 优先 / normalized_name 次之）→ 409 + 已存在 id
    """
    from src.storage.db_storage import _get_session
    from src.storage.models import FundDirectionMaster

    fund_name = (body.fund_name or "").strip()
    if not fund_name:
        raise HTTPException(status_code=400, detail="fund_name 不能为空")
    direction = (body.direction or "").strip()
    if direction not in DIRECTION_LABEL:
        raise HTTPException(status_code=400, detail=f"direction 非法: {direction}")

    # 去重：fund_code 优先 → normalized_name
    norm = repo.normalize_fund_name(fund_name)
    session = _get_session()
    try:
        existing = None
        if body.fund_code:
            existing = session.query(FundDirectionMaster).filter_by(fund_code=body.fund_code.strip()).first()
        if existing is None:
            existing = session.query(FundDirectionMaster).filter_by(normalized_name=norm).first()
        if existing:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "该基金已存在，是否进入编辑？",
                    "existing_id": existing.id,
                    "existing_fund_name": existing.fund_name,
                },
            )

        now = datetime.now()
        rec = FundDirectionMaster(
            fund_name=fund_name,
            fund_code=(body.fund_code or None) and body.fund_code.strip() or None,
            normalized_name=norm,
            direction=direction,
            sub_direction=body.sub_direction,
            fund_type=body.fund_type,
            fund_type_detail=body.fund_type,
            classification_source="manual",
            confidence="high",
            evidence=body.evidence or "人工新增",
            verified=True,
            first_seen_at=now,
            last_verified_at=now,
            last_seen_at=now,
            seen_count=1,
        )
        session.add(rec)
        try:
            session.commit()
        except Exception:
            session.rollback()
            raise
        logger.info("人工新增: %s -> %s (manual)", fund_name, direction)
        return {"ok": True, "id": rec.id, "fund_name": fund_name, "direction": direction}
    finally:
        session.close()


# ---- 人工编辑 ----
class UpdateBody(BaseModel):
    fund_name: Optional[str] = None
    fund_code: Optional[str] = None
    direction: Optional[str] = None
    fund_type: Optional[str] = None
    sub_direction: Optional[str] = None
    evidence: Optional[str] = None


@router.put("/{fund_id}")
def update_fund(fund_id: int, body: UpdateBody):
    """人工编辑基金方向（强制 manual + verified=true）。"""
    from src.storage.db_storage import _get_session
    from src.storage.models import FundDirectionMaster
    from fastapi import HTTPException

    if body.direction and body.direction not in DIRECTION_LABEL:
        raise HTTPException(status_code=400, detail=f"direction 非法: {body.direction}")

    session = _get_session()
    try:
        rec = session.query(FundDirectionMaster).filter_by(id=fund_id).first()
        if not rec:
            raise HTTPException(status_code=404, detail="fund_id 不存在")

        # manual 已确认 → 任何修改都保持 manual 最高优先级
        prev_evidence = rec.evidence
        if body.fund_name:
            rec.fund_name = body.fund_name.strip()
            rec.normalized_name = repo.normalize_fund_name(rec.fund_name)
        if body.fund_code is not None:
            rec.fund_code = body.fund_code.strip() or None
        if body.direction:
            rec.direction = body.direction.strip()
        if body.fund_type is not None:
            rec.fund_type = body.fund_type
            rec.fund_type_detail = body.fund_type
        if body.sub_direction is not None:
            rec.sub_direction = body.sub_direction
        if body.evidence is not None:
            rec.evidence = body.evidence
        else:
            # 保留 AI 证据，但追加人工标记
            if prev_evidence and "[人工确认]" not in (prev_evidence or ""):
                rec.evidence = (prev_evidence or "") + "\n[人工确认]"

        # 人工编辑后强制 manual + verified + 刷新 verified_at
        rec.classification_source = "manual"
        rec.confidence = "high"
        rec.verified = True
        rec.last_verified_at = datetime.now()
        # next_reverify 清空（人工定期负责）
        rec.needs_reverify = False

        session.commit()
        logger.info("人工编辑: id=%d -> %s", fund_id, rec.direction)
        return {"ok": True, "id": rec.id}
    finally:
        session.close()


# ---- 人工确认（已确认 / 待确认 → 已确认）----
class ConfirmBody(BaseModel):
    fund_id: int
    direction: str
    sub_direction: Optional[str] = None
    fund_type: Optional[str] = None
    evidence: Optional[str] = None


@router.post("/{fund_id}/confirm")
def confirm_one(fund_id: int, body: ConfirmBody):
    """人工确认单只基金方向（强制 manual + verified=true）。"""
    return update_fund(
        fund_id,
        UpdateBody(
            direction=body.direction,
                sub_direction=body.sub_direction,
                fund_type=body.fund_type,
                evidence=body.evidence,
            ),
    )


# ---- 重新分析（仅 manual 以外的基金可触发）----
@router.post("/{fund_id}/reanalyze")
def reanalyze_fund(fund_id: int):
    """对单只基金重新跑 direction_resolver（不会覆盖 manual）。"""
    from src.storage.db_storage import _get_session
    from src.storage.models import FundDirectionMaster
    from fastapi import HTTPException

    session = _get_session()
    try:
        rec = session.query(FundDirectionMaster).filter_by(id=fund_id).first()
        if not rec:
            raise HTTPException(status_code=404, detail="fund_id 不存在")
        if rec.classification_source == "manual" and rec.verified:
            raise HTTPException(
                status_code=403,
                detail="人工确认的基金禁止 AI 重新分析。如需修改请人工编辑。",
            )
        # 重跑 resolver（不传 fund_type_hint，避免主库里旧的 hint 锁死）
        fund_name = rec.fund_name
        fund_code = rec.fund_code
        session.close()

        result = resolver.resolve_direction(
            fund_name, fund_code=fund_code, fund_type_hint=None
        )
        # 重新查库拿新数据
        session = _get_session()
        rec = session.query(FundDirectionMaster).filter_by(id=fund_id).first()
        if not rec:
            raise HTTPException(status_code=500, detail="重分析失败：记录丢失")
        return {
            "ok": True,
            "id": rec.id,
            "direction": rec.direction,
            "confidence": rec.confidence,
            "source": rec.classification_source,
            "evidence": rec.evidence,
            "verified": rec.verified,
        }
    finally:
        session.close()


# ---- 查看依据 ----
@router.get("/{fund_id}/evidence")
def get_evidence(fund_id: int):
    """查看 AI 判断依据（含 evidence_items / source_url）。"""
    from src.storage.db_storage import _get_session
    from src.storage.models import FundDirectionMaster
    from fastapi import HTTPException
    import json

    session = _get_session()
    try:
        rec = session.query(FundDirectionMaster).filter_by(id=fund_id).first()
        if not rec:
            raise HTTPException(status_code=404, detail="fund_id 不存在")

        items = []
        if rec.evidence_items_json:
            try:
                items = json.loads(rec.evidence_items_json)
            except Exception:
                pass

        return _enrich({
            "id": rec.id,
            "fund_name": rec.fund_name,
            "fund_code": rec.fund_code,
            "direction": rec.direction,
            "sub_direction": rec.sub_direction,
            "fund_type_detail": rec.fund_type_detail,
            "fund_type": rec.fund_type,
            "classification_source": rec.classification_source,
            "confidence": rec.confidence,
            "evidence": rec.evidence,
            "evidence_period": rec.evidence_period,
            "source_url": rec.source_url,
            "verified": rec.verified,
            "last_search_at": rec.last_search_at.isoformat() if rec.last_search_at else None,
            "next_reverify_at": rec.next_reverify_at.isoformat() if rec.next_reverify_at else None,
            "evidence_items": items,
        })
    finally:
        session.close()


# ============================================================
#  兼容旧端点（保留以兼容旧前端调用）
# ============================================================

@router.get("/unconfirmed")
def list_unconfirmed(limit: int = 100):
    return {"items": repo.list_unconfirmed(limit=limit)}


@router.get("/needs-reverify")
def list_needs_reverify(limit: int = 50):
    return {"items": repo.list_needs_reverify(limit=limit)}


@router.get("/search")
def search_fund(q: str, limit: int = 20):
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
        return {"items": [_enrich(repo._to_dict(r)) for r in rows]}
    finally:
        session.close()


class OldConfirmBody(BaseModel):
    fund_id: int
    direction: str
    sub_direction: Optional[str] = None
    fund_type: Optional[str] = None
    evidence: Optional[str] = None


@router.post("/confirm")
def confirm_legacy(body: OldConfirmBody):
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
    n = repo.detect_active_fund_drift()
    return {"ok": True, "marked": n}