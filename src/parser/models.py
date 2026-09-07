"""
Pydantic 数据模型 — 大V操作记录
"""

from __future__ import annotations

from typing import Optional, List, Any
from datetime import datetime
from pydantic import BaseModel, Field, field_validator


def _coerce_to_str(v: Any) -> Optional[str]:
    """将 int/float 转为 str，None 保持 None，str 直接返回"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return str(v)
    return str(v) if v is not None else None


class TradeRecord(BaseModel):
    """一笔基金操作 = 一行 Excel"""

    kol_name: Optional[str] = Field(None, description="大V昵称")
    yield_period: Optional[str] = Field(None, description="收益率周期，如 近一年")
    yield_rate: Optional[str] = Field(None, description="收益率，如 26.49%")
    publish_time: Optional[str] = Field(None, description="发布时间，如 16:18")
    opinion_text: Optional[str] = Field(None, description="动态正文（观点长文）")
    operation_type: Optional[str] = Field(
        None, description="操作类型：买入 / 卖出 / 转换 / 撤销"
    )
    operation_status: Optional[str] = Field(None, description="操作状态，如 确认中")
    fund_name: Optional[str] = Field(None, description="基金名称")
    buy_amount: Optional[str] = Field(None, description="买入金额（元）")
    sell_shares: Optional[str] = Field(None, description="卖出份额（份）")
    convert_from_fund: Optional[str] = Field(None, description="转换前基金名称")
    convert_to_fund: Optional[str] = Field(None, description="转换后基金名称")
    repost_count: Optional[str] = Field(None, description="转发数")
    comment_count: Optional[str] = Field(None, description="评论数")
    like_count: Optional[str] = Field(None, description="点赞数")
    seek_interpret_count: Optional[str] = Field(None, description="求解读人数")
    collect_time: Optional[str] = Field(None, description="采集时间")
    today_operation_count: Optional[str] = Field(None, description="今日操作条数")
    remark: Optional[str] = Field(None, description="备注，如转换等")

    # 自动将 AI 返回的 int 转为 str
    _coerce_repost = field_validator("repost_count", mode="before")(_coerce_to_str)
    _coerce_comment = field_validator("comment_count", mode="before")(_coerce_to_str)
    _coerce_like = field_validator("like_count", mode="before")(_coerce_to_str)
    _coerce_seek = field_validator("seek_interpret_count", mode="before")(_coerce_to_str)
    _coerce_today = field_validator("today_operation_count", mode="before")(_coerce_to_str)
    _coerce_buy_amount = field_validator("buy_amount", mode="before")(_coerce_to_str)
    _coerce_sell_shares = field_validator("sell_shares", mode="before")(_coerce_to_str)


class ParseResult(BaseModel):
    """一次解析的整体结果"""

    records: List[TradeRecord] = Field(default_factory=list)
    parse_time: str = Field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )
    source_md: str = ""
    total_records: int = 0
    ai_used: bool = False

    def model_post_init(self, __context):
        self.total_records = len(self.records)
