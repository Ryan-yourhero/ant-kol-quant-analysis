"""
MySQL 存储模块 — 将 TradeRecord 列表持久化到 MySQL
"""

import hashlib
import uuid
import os
import socket
import subprocess
import sys
import time
import logging
from datetime import date, datetime
from typing import List, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, CrawlRun, Kol, Post, Operation
from ..parser.models import TradeRecord

logger = logging.getLogger("storage.db")

# ---- 配置 ----
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

MYSQL_HOST = os.environ.get("MYSQL_HOST", "localhost")
MYSQL_PORT = os.environ.get("MYSQL_PORT", "3306")
MYSQL_USER = os.environ.get("MYSQL_USER", "root")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE", "kol_rich")

DATABASE_URL = (
    f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}"
    f"@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}?charset=utf8mb4"
)

_engine = None
_SessionLocal = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            DATABASE_URL,
            pool_pre_ping=True,
            pool_recycle=3600,
            echo=False,
        )
    return _engine


def _get_session() -> Session:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=_get_engine())
    return _SessionLocal()


def is_configured() -> bool:
    return bool(os.environ.get("MYSQL_HOST"))


# MySQL 服务名（Windows 服务 / Linux systemctl），可用 .env 的 MYSQL_SERVICE_NAME 覆盖
MYSQL_SERVICE_NAME = os.environ.get("MYSQL_SERVICE_NAME", "MySQL80")


def mysql_reachable(timeout: int = 3) -> bool:
    """检测 MySQL 是否可连接（TCP 层面）。"""
    try:
        sock = socket.create_connection((MYSQL_HOST, int(MYSQL_PORT)), timeout=timeout)
        sock.close()
        return True
    except OSError:
        return False


def _start_mysql_service() -> None:
    """尝试启动 MySQL 服务（Windows 服务 / Linux systemctl）。"""
    if sys.platform == "win32":
        for cmd in (["sc", "start", MYSQL_SERVICE_NAME], ["net", "start", MYSQL_SERVICE_NAME]):
            try:
                subprocess.run(cmd, check=False, capture_output=True, timeout=60)
            except Exception as e:
                logger.debug("启动命令 %s 失败: %s", cmd, e)
    else:
        try:
            subprocess.run(
                ["systemctl", "start", MYSQL_SERVICE_NAME],
                check=False, capture_output=True, timeout=60,
            )
        except Exception as e:
            logger.debug("systemctl 启动失败: %s", e)


def ensure_mysql_running(timeout: int = 30) -> bool:
    """确保 MySQL 正在运行：不可达则启动服务并等待就绪。"""
    if not is_configured():
        logger.info("MySQL 未配置，跳过启动")
        return False

    if mysql_reachable():
        logger.info("MySQL 已在运行 (%s:%s)", MYSQL_HOST, MYSQL_PORT)
        return True

    logger.info("MySQL 未运行，尝试启动服务 %s ...", MYSQL_SERVICE_NAME)
    _start_mysql_service()

    for _ in range(timeout):
        if mysql_reachable():
            logger.info("MySQL 已就绪 (%s:%s)", MYSQL_HOST, MYSQL_PORT)
            return True
        time.sleep(1)

    logger.warning("MySQL 启动后 %d 秒仍无法连接（服务名=%s）", timeout, MYSQL_SERVICE_NAME)
    return False


def init_db():
    """初始化数据库（自动建库 + 建表）"""
    if not is_configured():
        logger.info("MySQL 未配置，跳过建表")
        return

    # 先连到 MySQL 不指定库，自动建库
    _ensure_database()

    # 再连到目标库建表
    engine = _get_engine()
    Base.metadata.create_all(engine)

    # 迁移：给已有表加新列
    _migrate_crawl_runs(engine)
    logger.info("MySQL 表初始化完成")


def _migrate_crawl_runs(_engine):
    """给 crawl_runs 表补充缺失的列"""
    try:
        import pymysql
        conn = pymysql.connect(
            host=MYSQL_HOST, port=int(MYSQL_PORT), user=MYSQL_USER,
            password=MYSQL_PASSWORD, database=MYSQL_DATABASE, charset="utf8mb4",
        )
        cursor = conn.cursor()
        cursor.execute("SHOW COLUMNS FROM crawl_runs")
        existing = {row[0] for row in cursor.fetchall()}
        new_cols = [
            ("status", "VARCHAR(20) DEFAULT 'pending'"),
            ("started_at", "DATETIME NULL"),
            ("finished_at", "DATETIME NULL"),
            ("error_message", "TEXT NULL"),
        ]
        for col_name, col_def in new_cols:
            if col_name not in existing:
                cursor.execute(f"ALTER TABLE crawl_runs ADD COLUMN {col_name} {col_def}")
                logger.info("迁移: crawl_runs.%s 已添加", col_name)
        cursor.close()
        conn.close()
    except Exception as e:
        logger.warning("迁移检查失败（不影响运行）: %s", e)


def _ensure_database():
    """确保目标数据库存在，不存在则自动创建"""
    try:
        import pymysql
        conn = pymysql.connect(
            host=MYSQL_HOST,
            port=int(MYSQL_PORT),
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            charset="utf8mb4",
        )
        cursor = conn.cursor()
        cursor.execute(
            f"CREATE DATABASE IF NOT EXISTS `{MYSQL_DATABASE}` "
            f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        cursor.close()
        conn.close()
        logger.info("数据库 '%s' 已就绪", MYSQL_DATABASE)
    except Exception as e:
        logger.warning("自动建库失败（可能已存在）: %s", e)


def _compute_md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _get_or_create_kol(session: Session, name: str) -> Optional[Kol]:
    if not name:
        return None
    kol = session.query(Kol).filter_by(name=name).first()
    if not kol:
        kol = Kol(name=name)
        session.add(kol)
        session.flush()
    return kol


def _extract_date(records: List[TradeRecord]) -> date:
    """从 records 中提取采集日期"""
    for r in records:
        if r.collect_time:
            try:
                return datetime.fromisoformat(r.collect_time).date()
            except (ValueError, TypeError):
                pass
    return date.today()


def save_records(
    records: List[TradeRecord],
    md_text: str,
    md_path: str,
) -> bool:
    """
    将 TradeRecord 列表写入 MySQL。

    防重复：同一 MD 内容（md5）只入库一次。

    Returns:
        True: 写入成功
        False: 跳过（已存在）或失败
    """
    if not records:
        logger.info("无记录，跳过 MySQL 写入")
        return False

    if not is_configured():
        logger.info("MySQL 未配置，跳过写入")
        return False

    md_hash = _compute_md5(md_text)
    session = _get_session()

    try:
        # ---- 防重复1：同一 MD 内容（md5）只入库一次 ----
        existing = session.query(CrawlRun).filter_by(md_hash=md_hash).first()
        if existing:
            logger.info("MD 已入库 (run_id=%d, %s)，跳过", existing.id, existing.collect_date)
            return False

        collect_date = _extract_date(records)

        # ---- 防重复2：同一 collect_date 只保留最新一次，旧的整批级联删除 ----
        #   用户规则："一天的数据以最新一次为准，别的覆盖就行了"
        same_date_runs = (
            session.query(CrawlRun)
            .filter(CrawlRun.collect_date == collect_date)
            .all()
        )
        if same_date_runs:
            drop_ids = [r.id for r in same_date_runs]
            for r in same_date_runs:
                session.delete(r)  # cascade=all,delete-orphan 会清掉 posts+operations
            logger.info(
                "同日期(%s)已存在 %d 个旧 run %s，级联删除后用新采集覆盖",
                collect_date, len(drop_ids), drop_ids,
            )
            session.flush()

        # ---- 创建 CrawlRun ----
        crawl_run = CrawlRun(
            md_file_path=md_path,
            md_hash=md_hash,
            collect_date=collect_date,
            total_records=len(records),
        )
        session.add(crawl_run)
        session.flush()  # 获取 id

        # ---- 按 (kol_name, publish_time, yield_rate) 聚合帖子 ----
        # 使用有序列表保持帖子间的相对顺序
        post_groups: List[tuple] = []  # [(post_key, [records])]
        seen_keys = set()

        for r in records:
            key = (
                r.kol_name or "",
                r.publish_time or "",
                r.yield_rate or "",
            )
            if key not in seen_keys:
                seen_keys.add(key)
                post_groups.append((key, [r]))
            else:
                for pk, recs in post_groups:
                    if pk == key:
                        recs.append(r)
                        break

        # ---- 逐 post 写入 ----
        for (kol_name, pub_time, yield_rate), post_records in post_groups:
            if not kol_name:
                continue

            kol = _get_or_create_kol(session, kol_name)
            if not kol:
                continue

            # 取第一条的 opinion_text（同一帖子共用）
            opinion = post_records[0].opinion_text or None

            post = Post(
                crawl_run_id=crawl_run.id,
                kol_id=kol.id,
                publish_time=pub_time or None,
                yield_rate=yield_rate or None,
                opinion_text=opinion,
            )
            session.add(post)
            session.flush()

            # ---- 处理转换配对：相邻 remark="转换" 的卖出/买入共享 group_id ----
            conv_pairs = _pair_conversions(post_records)

            for r in post_records:
                group_id = conv_pairs.get(id(r))  # 转换操作 → 共用 UUID

                op = Operation(
                    crawl_run_id=crawl_run.id,
                    post_id=post.id,
                    kol_id=kol.id,
                    collect_date=collect_date,
                    publish_time=r.publish_time,
                    operation_type=r.operation_type,
                    operation_status=r.operation_status,
                    fund_name=r.fund_name,
                    buy_amount=r.buy_amount,
                    sell_shares=r.sell_shares,
                    remark=r.remark,
                    original_operation_type="转换" if r.remark == "转换" else None,
                    operation_group_id=group_id,
                )
                session.add(op)

        session.commit()
        logger.info(
            "MySQL 写入成功: run_id=%d, %d 条操作, %d 篇帖子",
            crawl_run.id,
            len(records),
            len(post_groups),
        )
        return True

    except Exception as e:
        session.rollback()
        logger.error("MySQL 写入失败（不影响 Excel）: %s", e, exc_info=True)
        return False
    finally:
        session.close()


def _pair_conversions(
    post_records: List[TradeRecord],
) -> dict:
    """
    将同一帖子内 remark="转换" 的卖出/买入配对，共享同一 UUID。

    配对策略：相邻的 卖出(remark=转换) + 买入(remark=转换) 为一对。
    """
    pairs: dict = {}  # id(record) → group_id

    i = 0
    while i < len(post_records):
        r = post_records[i]
        if r.remark == "转换":
            # 找到一起的转换记录
            group = []
            j = i
            while j < len(post_records) and post_records[j].remark == "转换":
                group.append(post_records[j])
                j += 1
            # 两两配对（卖出→买入）
            group_id = None
            for k in range(0, len(group) - 1, 2):
                if group_id is None:
                    group_id = str(uuid.uuid4())
                pairs[id(group[k])] = group_id
                pairs[id(group[k + 1])] = group_id
                group_id = None  # 下一对重新生成
            # 如果落单（奇数个转换记录），单独生成
            if len(group) % 2 == 1 and id(group[-1]) not in pairs:
                pairs[id(group[-1])] = str(uuid.uuid4())
            i = j
        else:
            i += 1

    return pairs
