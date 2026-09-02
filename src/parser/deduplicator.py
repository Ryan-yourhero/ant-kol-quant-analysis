"""
去重器 — 删除重复页面产生的相同操作（规则 9）

策略：
  - 同页内连续完全相同的操作 = 独立操作，保留全部（如连续两笔同基金同金额的定投）
  - 跨页完全相同的操作 = 滚动导致的重复，只保留第一次出现
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List


def _content_key(record: Dict[str, Any]) -> str:
    """
    计算一条记录的内容指纹。
    相同操作 = 同大V + 同操作类型 + 同基金名 + 同金额/份额 + 同时间
    """
    parts = [
        record.get("kol_name") or "",
        record.get("operation_type") or "",
        record.get("fund_name") or "",
        record.get("buy_amount") or "",
        record.get("sell_shares") or "",
        record.get("convert_from_fund") or "",
        record.get("convert_to_fund") or "",
        record.get("publish_time") or "",
    ]
    key = "|".join(parts)
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def deduplicate(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    去重：
      - 同一页内：保留所有记录（即使是完全相同的连续操作也视为独立操作）
      - 跨页：相同内容只保留第一次出现的记录（滚动产生的重复页面）

    Args:
        records: 扁平化后的操作记录列表（每个 dict 应包含 "page" 字段）

    Returns:
        去重后的记录列表
    """
    # 按 page 分组，保持组内原始顺序
    page_groups: Dict[int, List[Dict[str, Any]]] = {}
    page_order: List[int] = []
    for rec in records:
        page = rec.get("_page") or rec.get("page") or 0
        if page not in page_groups:
            page_groups[page] = []
            page_order.append(page)
        page_groups[page].append(rec)

    # 跨页去重：记录每个 content_key 首次出现的 page
    key_first_page: Dict[str, int] = {}

    result: List[Dict[str, Any]] = []
    for page in page_order:
        for rec in page_groups[page]:
            ck = _content_key(rec)
            first_page = key_first_page.get(ck)
            if first_page is None:
                # 首次出现 → 保留
                key_first_page[ck] = page
                result.append(rec)
            elif first_page == page:
                # 同页再次出现 → 保留（连续相同的独立操作）
                result.append(rec)
            else:
                # 已经在其他页出现过 → 跳过（跨页重复）
                pass

    return result
