"""操作记录 API"""
from typing import Optional
from fastapi import APIRouter, Query

from backend.services import queries

router = APIRouter(prefix="/operations", tags=["操作记录"])


@router.get("/today")
def today_operations(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    """今日操作"""
    return queries.query_operations(page=page, page_size=page_size)


@router.get("/history")
def history_operations(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    kol_name: Optional[str] = Query(None),
    operation_type: Optional[str] = Query(None),
    fund_name: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """历史操作（筛选）"""
    return queries.query_operations(
        date_from=date_from,
        date_to=date_to,
        kol_name=kol_name,
        operation_type=operation_type,
        fund_name=fund_name,
        page=page,
        page_size=page_size,
        today_only=False,
    )
