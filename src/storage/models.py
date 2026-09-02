"""
SQLAlchemy ORM 模型
"""

from sqlalchemy import (
    Column, Integer, String, Date, DateTime, Text, ForeignKey,
    Index, UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func

Base = declarative_base()


class CrawlRun(Base):
    """每次采集运行"""
    __tablename__ = "crawl_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    md_file_path = Column(String(500), nullable=False)
    md_hash = Column(String(64), nullable=False, unique=True, index=True)
    collect_date = Column(Date, nullable=False)
    total_records = Column(Integer, default=0)
    status = Column(String(20), default="pending")  # pending / crawling / parsing / saving / success / failed
    started_at = Column(DateTime)
    finished_at = Column(DateTime)
    error_message = Column(Text)
    created_at = Column(DateTime, server_default=func.now())

    posts = relationship("Post", back_populates="crawl_run", cascade="all, delete-orphan")
    operations = relationship("Operation", back_populates="crawl_run", cascade="all, delete-orphan")


class Kol(Base):
    """大V"""
    __tablename__ = "kols"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, unique=True)
    first_seen_at = Column(DateTime, server_default=func.now())

    posts = relationship("Post", back_populates="kol")
    operations = relationship("Operation", back_populates="kol")


class Post(Base):
    """动态帖子"""
    __tablename__ = "posts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    crawl_run_id = Column(Integer, ForeignKey("crawl_runs.id"), nullable=False)
    kol_id = Column(Integer, ForeignKey("kols.id"), nullable=False)
    publish_time = Column(String(10))
    yield_rate = Column(String(20))
    opinion_text = Column(Text)

    crawl_run = relationship("CrawlRun", back_populates="posts")
    kol = relationship("Kol", back_populates="posts")
    operations = relationship("Operation", back_populates="post", cascade="all, delete-orphan")


class Operation(Base):
    """每笔基金操作"""
    __tablename__ = "operations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    crawl_run_id = Column(Integer, ForeignKey("crawl_runs.id"), nullable=False)
    post_id = Column(Integer, ForeignKey("posts.id"), nullable=False)
    kol_id = Column(Integer, ForeignKey("kols.id"), nullable=False)

    collect_date = Column(Date, nullable=False, index=True)
    publish_time = Column(String(10))
    operation_type = Column(String(10), nullable=False)  # 买入 / 卖出 / 撤销 / 定投
    operation_status = Column(String(20))
    fund_name = Column(String(200))
    buy_amount = Column(String(20))
    sell_shares = Column(String(20))
    remark = Column(String(50))
    original_operation_type = Column(String(10))
    operation_group_id = Column(String(36))  # UUID，转换操作对共用

    crawl_run = relationship("CrawlRun", back_populates="operations")
    kol = relationship("Kol", back_populates="operations")
    post = relationship("Post", back_populates="operations")

    __table_args__ = (
        Index("ix_ops_kol_date", "kol_id", "collect_date"),
    )
