"""Excel 下载 API"""
import os
from fastapi import APIRouter
from fastapi.responses import FileResponse

from backend.services.pipeline import get_current_status

router = APIRouter(prefix="/excel", tags=["Excel"])


@router.get("/today")
def download_today():
    """下载今日 Excel"""
    status = get_current_status()
    excel_path = status.get("excel_path")
    if excel_path and os.path.exists(excel_path):
        return FileResponse(
            excel_path,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=os.path.basename(excel_path),
        )
    return {"ok": False, "message": "今日 Excel 未生成"}
