"""
SQLAlchemy ORM 模型
"""
from sqlalchemy import (
    Boolean, Column, Integer, String, Date, DateTime, Text, ForeignKey,
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
    fund_code = Column(String(20), index=True)               # 基金 6 位代码（如 161725）
    fund_direction_id = Column(Integer, ForeignKey("fund_direction_master.id"), index=True)  # ← 关联方向库
    buy_amount = Column(String(20))
    sell_shares = Column(String(20))
    remark = Column(String(50))
    original_operation_type = Column(String(10))
    operation_group_id = Column(String(36))  # UUID，转换操作对共用

    crawl_run = relationship("CrawlRun", back_populates="operations")
    kol = relationship("Kol", back_populates="operations")
    post = relationship("Post", back_populates="operations")
    fund_direction = relationship("FundDirectionMaster")

    __table_args__ = (
        Index("ix_ops_kol_date", "kol_id", "collect_date"),
        Index("ix_ops_fund_direction", "fund_direction_id"),
    )


class FundDirectionMaster(Base):
    """基金方向知识库（Source of Truth）

    关键设计：
    - normalized_name 唯一索引（去 .../空白/常见后缀）→ 一只基金一行
    - direction + confidence + classification_source 固化方向来源
    - verified = True 视为可信，下次直接命中
    - low confidence 仍写入（verified=False），供人工补全
    - last_verified_at 用于主动基金方向漂移检测
    - web_query_locked_until：联网防重入锁
    """
    __tablename__ = "fund_direction_master"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # ---- 基金标识 ----
    fund_name = Column(String(200), nullable=False, index=True)        # 用户提供的原始基金名（可能带 ...）
    fund_code = Column(String(20), index=True)                          # 6 位基金代码（如 161725）；若无法识别则为 NULL
    normalized_name = Column(String(200), nullable=False, index=True)   # 标准化名（去 "..."、去 "C" 等后缀、小写）

    # ---- 方向 ----
    direction = Column(String(50), nullable=False, default="其他/待分类", index=True)
    sub_direction = Column(String(100))                                 # 细分子方向（如 "半导体设备"、"创新药CXO"）
    fund_type = Column(String(50))                                      # index / industry_theme / active_equity / mixed / qdii / other

    # ---- 来源 / 证据 ----
    classification_source = Column(String(20), nullable=False)          # manual / rule / llm / web_search
    confidence = Column(String(10), nullable=False)                    # high / medium / low
    evidence = Column(Text)                                             # 命中关键词/搜索摘要
    source_url = Column(String(500))                                    # 联网来源 URL

    # ---- 状态 ----
    verified = Column(Boolean, default=False, index=True)               # 是否人工/权威确认
    needs_reverify = Column(Boolean, default=False, index=True)          # 主动基金方向漂移 → 重新验证

    # ---- 时间戳 ----
    first_seen_at = Column(DateTime, server_default=func.now(), index=True)    # 首次入库
    last_verified_at = Column(DateTime, server_default=func.now())            # 最近一次验证（重要：主动基金方向可能漂移）
    last_seen_at = Column(DateTime, server_default=func.now())                # 最近一次在交易中出现
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # ---- 反重入锁 ----
    web_query_locked_until = Column(DateTime)                            # 联网搜索锁定：避免同基金被并发/短时间内重复查

    # ---- 联网/重验证时间 ----
    last_search_at = Column(DateTime)                                    # 最近一次联网搜索时间
    next_reverify_at = Column(DateTime)                                  # 下一次允许重新联网搜索时间（默认 30 天后）

    # ---- 证据周期 ----
    evidence_period = Column(String(20))                                 # 季度报告期，如 2026Q2
    fund_type_detail = Column(String(50))                                # 细分子类型：index / industry_theme / active_equity / mixed / quant / broad_market

    # ---- 计数/最新一次证据明细（JSON 字符串，便于审计）----
    evidence_items_json = Column(Text)                                   # 最近一次联网的 evidence_items（list of dict）
    seen_count = Column(Integer, default=1)                             # 在交易中出现的次数

    __table_args__ = (
        UniqueConstraint("normalized_name", name="uq_fund_direction_normalized_name"),
        Index("ix_fdm_verified_needs", "verified", "needs_reverify"),
        Index("ix_fdm_direction_verified", "direction", "verified"),
        Index("ix_fdm_next_reverify", "next_reverify_at"),
    )
