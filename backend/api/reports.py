"""每日 AI 分析报告 API"""
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from backend.services import report_service

router = APIRouter(prefix="/reports", tags=["每日分析报告"])


class GenerateBody(BaseModel):
    date: Optional[str] = None  # 例如 "2026-08-25"；不传则生成全部


@router.get("")
def list_reports():
    """列出所有有数据的日期及报告生成状态"""
    return report_service.list_report_history()


@router.post("/generate")
def generate_reports(body: GenerateBody):
    """异步生成报告：传 date 生成单日，不传生成全部"""
    dates = [body.date] if body.date else None
    return report_service.generate_reports_async(dates)


@router.get("/{date_str}/content")
def get_report_content(date_str: str):
    """读取某日期的报告 Markdown 内容"""
    content = report_service.get_report_content(date_str)
    if content is None:
        return {"ok": False, "message": "报告不存在"}
    return {"ok": True, "date": date_str, "content": content}
