"""任务管理 API"""
from fastapi import APIRouter

from backend.services.pipeline import run_daily_pipeline, get_current_status

router = APIRouter(prefix="/runs", tags=["任务"])


@router.post("/start")
def start_run():
    """启动每日采集"""
    if get_current_status()["status"] not in ("idle", "success", "failed"):
        return {"ok": False, "message": "已有任务正在运行"}
    return run_daily_pipeline()


@router.get("/current")
def current_run():
    """获取当前任务状态"""
    return get_current_status()
