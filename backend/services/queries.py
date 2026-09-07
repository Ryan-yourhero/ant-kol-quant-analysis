"""
Web 层数据库查询服务
"""
from datetime import date
from typing import Optional
from sqlalchemy.orm import joinedload
from sqlalchemy import desc, func

from src.storage.db_storage import _get_session
from src.storage.models import CrawlRun, Kol, Operation


def query_operations(
    *,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    kol_name: Optional[str] = None,
    operation_type: Optional[str] = None,
    fund_name: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    today_only: bool = True,
) -> dict:
    """查询操作记录（分页）

    today_only=True: 不传 date_from 时默认只查今天（用于 /operations/today）
    today_only=False: 不传 date_from 时查全部历史（用于 /operations/history）
    """
    session = _get_session()
    try:
        q = session.query(Operation).join(Kol).options(
            joinedload(Operation.kol), joinedload(Operation.post)
        )

        if date_from:
            q = q.filter(Operation.collect_date >= date.fromisoformat(date_from))
        elif today_only:
            q = q.filter(Operation.collect_date == date.today())

        if date_to:
            q = q.filter(Operation.collect_date <= date.fromisoformat(date_to))

        if kol_name:
            q = q.filter(Kol.name.like(f"%{kol_name}%"))

        if operation_type:
            q = q.filter(Operation.operation_type == operation_type)

        if fund_name:
            q = q.filter(Operation.fund_name.like(f"%{fund_name}%"))

        total = q.count()
        items = (
            q.order_by(desc(Operation.collect_date), desc(Operation.id))
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )

        return {
            "items": [_op_to_dict(op) for op in items],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    finally:
        session.close()


def _op_to_dict(op: Operation) -> dict:
    post = op.post
    return {
        "id": op.id,
        "kol_id": op.kol_id,
        "collect_date": op.collect_date.isoformat() if op.collect_date else None,
        "kol_name": op.kol.name if op.kol else None,
        "yield_rate": post.yield_rate if post else None,
        "publish_time": op.publish_time,
        "opinion_text": post.opinion_text if post else None,
        "operation_type": op.operation_type,
        "operation_status": op.operation_status,
        "fund_name": op.fund_name,
        "buy_amount": op.buy_amount,
        "sell_shares": op.sell_shares,
        "remark": op.remark,
    }


def query_kols() -> list:
    """查询所有大V"""
    session = _get_session()
    try:
        kols = session.query(Kol).order_by(Kol.name).all()
        result = []
        for k in kols:
            op_count = session.query(func.count(Operation.id)).filter(Operation.kol_id == k.id).scalar()
            result.append({
                "id": k.id,
                "name": k.name,
                "operation_count": op_count or 0,
            })
        return result
    finally:
        session.close()


def query_kol_operations(kol_id: int, page: int = 1, page_size: int = 20) -> dict:
    """查询某大V的操作记录"""
    session = _get_session()
    try:
        q = session.query(Operation).filter(Operation.kol_id == kol_id).options(
            joinedload(Operation.kol), joinedload(Operation.post)
        )
        total = q.count()
        items = (
            q.order_by(desc(Operation.collect_date), desc(Operation.id))
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        kol = session.query(Kol).filter_by(id=kol_id).first()
        return {
            "kol": {"id": kol.id, "name": kol.name} if kol else None,
            "items": [_op_to_dict(op) for op in items],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    finally:
        session.close()


def query_today_stats() -> dict:
    """查询今日统计"""
    session = _get_session()
    try:
        today = date.today()
        ops = session.query(Operation).filter(Operation.collect_date == today)
        total = ops.count()
        buy = ops.filter(Operation.operation_type == "买入").count()
        sell = ops.filter(Operation.operation_type == "卖出").count()
        return {"total": total, "buy": buy, "sell": sell}
    finally:
        session.close()


def query_latest_run() -> Optional[dict]:
    """查询最近一次 CrawlRun"""
    session = _get_session()
    try:
        run = session.query(CrawlRun).order_by(desc(CrawlRun.id)).first()
        if not run:
            return None
        return {
            "id": run.id,
            "collect_date": run.collect_date.isoformat() if run.collect_date else None,
            "total_records": run.total_records,
            "status": run.status,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        }
    finally:
        session.close()
