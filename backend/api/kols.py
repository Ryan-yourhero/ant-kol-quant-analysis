"""大V API"""
from fastapi import APIRouter, Query

from backend.services import queries

router = APIRouter(prefix="/kols", tags=["大V"])


@router.get("")
def get_kols():
    """所有大V列表"""
    return queries.query_kols()


@router.get("/{kol_id}/operations")
def kol_operations(kol_id: int, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    """某大V的操作记录"""
    return queries.query_kol_operations(kol_id, page=page, page_size=page_size)
