"""
FastAPI 后端入口
"""
import logging
import os
import sys
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# 配置根 logger，确保 backend.pipeline 等自定义 logger 能输出
_root = logging.getLogger()
if not _root.handlers:
    _root.setLevel(logging.INFO)
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", datefmt="%H:%M:%S"))
    _root.addHandler(_h)

app = FastAPI(title="基金大V AI量化分析系统")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 延迟导入路由（避免循环依赖）
from backend.api import runs, operations, kols, excel, reports  # noqa: E402

app.include_router(runs.router, prefix="/api")
app.include_router(operations.router, prefix="/api")
app.include_router(kols.router, prefix="/api")
app.include_router(excel.router, prefix="/api")
app.include_router(reports.router, prefix="/api")


@app.get("/api/health")
def health():
    return {"status": "ok"}
