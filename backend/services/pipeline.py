"""
采集管道 — 封装每日完整流程为可调用函数

架构：浏览器点击 → pipeline → subprocess 启动 main.py → 解析 CRAWL_RESULT → AI → MySQL
main.py 是唯一爬虫入口，pipeline 不再直接调用 ScrollManager。
"""
import json
import os
import subprocess
import sys
import threading
import logging
from datetime import datetime
from typing import Optional, Dict, Any

logger = logging.getLogger("backend.pipeline")

# 确保项目根目录在路径中
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

MAIN_PY = os.path.join(BASE_DIR, "main.py")

# ---- 线程安全的状态管理 ----
_lock = threading.Lock()
_current_status: Dict[str, Any] = {
    "status": "idle",  # idle / starting_db / crawling / parsing / saving / success / failed
    "message": "",
    "run_id": None,
    "total_records": 0,
    "total_buy": 0,
    "total_sell": 0,
    "excel_path": None,
    "error": None,
    "started_at": None,
    "finished_at": None,
}


def get_current_status() -> dict:
    with _lock:
        return dict(_current_status)


def _set_status(**kwargs):
    with _lock:
        _current_status.update(kwargs)


def is_running() -> bool:
    with _lock:
        return _current_status["status"] not in ("idle", "success", "failed")


def run_daily_pipeline() -> Dict[str, Any]:
    """
    启动每日完整采集流程（异步，在后台线程运行）。

    Returns:
        {"ok": True/False, "message": str}
    """
    if is_running():
        return {"ok": False, "message": "已有任务正在运行，请等待完成"}

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return {"ok": True, "message": "采集任务已启动"}


def _run():
    """后台线程执行完整流程"""
    logger.info("[PIPELINE] 任务启动")
    _set_status(
        status="starting_db",
        message="检测/启动数据库...",
        run_id=None,
        total_records=0,
        total_buy=0,
        total_sell=0,
        excel_path=None,
        error=None,
        started_at=datetime.now().isoformat(),
        finished_at=None,
    )
    run_id = None

    try:
        logger.info("[PIPELINE] 检查运行锁 (is_running=%s)", is_running())

        # ---- Step 1: 检测/启动数据库（先检查，避免白爬） ----
        logger.info("[PIPELINE] 检查数据库")
        from src.storage.db_storage import ensure_mysql_running

        if not ensure_mysql_running():
            raise RuntimeError(
                "MySQL 无法连接且启动失败，请确认 MySQL80 服务存在（必要时以管理员身份运行后端）"
            )

        # ---- Step 2: 通过 subprocess 启动 main.py 爬虫 ----
        _set_status(status="crawling", message="正在采集手机数据...")
        logger.info("[PIPELINE] 准备启动 main.py")
        logger.info("[PIPELINE] MAIN_PY=%s", MAIN_PY)
        logger.info("[PIPELINE] sys.executable=%s", sys.executable)
        logger.info("[PIPELINE] cwd=%s", BASE_DIR)
        logger.info("[PIPELINE] MAIN_PY exists=%s", os.path.exists(MAIN_PY))

        if not os.path.exists(MAIN_PY):
            raise RuntimeError(f"main.py 不存在: {MAIN_PY}")

        # v3.5: 强制子进程使用 UTF-8 编码，避免 Windows GBK 控制台无法处理 emoji
        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"

        proc = subprocess.Popen(
            [sys.executable, MAIN_PY],
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=child_env,
        )

        logger.info("[PIPELINE] main.py 已启动 PID=%d", proc.pid)
        logger.info("[PIPELINE] 开始读取 main.py stdout")

        crawl_result = None
        all_output: list[str] = []

        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            all_output.append(line)
            logger.info("[main.py] %s", line)
            if line.startswith('{"CRAWL_RESULT"'):
                try:
                    crawl_result = json.loads(line)
                except json.JSONDecodeError:
                    pass

        proc.wait()
        logger.info("[PIPELINE] main.py 结束 returncode=%d", proc.returncode)

        if proc.returncode != 0:
            tail = "\n".join(all_output[-10:])
            raise RuntimeError(
                f"main.py 异常退出 (code={proc.returncode})\n最近输出:\n{tail}"
            )

        if not crawl_result or not crawl_result.get("success"):
            tail = "\n".join(all_output[-10:])
            raise RuntimeError(
                f"爬取未成功 (success={crawl_result})\n最近输出:\n{tail}"
            )

        screen_md = crawl_result.get("md_path", "")
        if not screen_md or not os.path.exists(screen_md):
            raise RuntimeError(f"MD 文件不存在: {screen_md}")

        logger.info("爬虫完成: %s", screen_md)

        # ---- Step 3: AI 解析 ----
        logger.info("[PIPELINE] 开始 AI 解析")
        _set_status(status="parsing", message="AI 正在解析...")

        from src.parser import parse_md_to_excel

        excel_path, parse_result = parse_md_to_excel(screen_md)

        # ---- Step 4: 统计 + 写入数据库 ----
        logger.info("[PIPELINE] 开始写 MySQL")
        _set_status(status="saving", message="正在写入数据库...")

        records = parse_result.records
        buy_count = sum(1 for r in records if r.operation_type == "买入")
        sell_count = sum(1 for r in records if r.operation_type == "卖出")

        from src.storage.db_storage import _get_session, _compute_md5
        from src.storage.models import CrawlRun

        with open(screen_md, "r", encoding="utf-8") as f:
            md_hash = _compute_md5(f.read())

        session = _get_session()
        try:
            crawl_run = session.query(CrawlRun).filter_by(md_hash=md_hash).first()
            if crawl_run:
                run_id = crawl_run.id
        finally:
            session.close()

        logger.info("[PIPELINE] 完成")
        _set_status(
            status="success",
            message="采集完成",
            run_id=run_id,
            total_records=parse_result.total_records,
            total_buy=buy_count,
            total_sell=sell_count,
            excel_path=excel_path,
            finished_at=datetime.now().isoformat(),
        )
        logger.info("流程完成: %d 条记录", parse_result.total_records)

    except Exception as e:
        logger.error("采集失败: %s", e, exc_info=True)
        _set_status(
            status="failed",
            message="采集失败",
            error=str(e),
            finished_at=datetime.now().isoformat(),
        )
