"""
方向分类器 — 根据基金名称把一笔交易归入方向维度

规则：
  - 第一版使用关键词子串匹配（fund_name 常被 OCR 截断为 "..."，故用包含匹配而非精确匹配）。
  - 按 `_DIRECTION_KEYWORDS` 顺序匹配，命中即返回，优先级从具体到宽泛。
  - 无法明确命中任何方向关键词时，返回 "其他"（不强行归类凑数）。

后续可升级为 fund_name -> direction 持久映射表。
"""

from __future__ import annotations

from typing import Optional, Tuple

# 优先级从高到低：更具体、更不易混淆的方向放前面。
# 例如「港股医药」含"港股"和"医药"，应归港股方向 → 港股方向排在医药方向之前。
_DIRECTION_KEYWORDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "港股方向",
        ("恒生", "港股", "香港", "港股通"),
    ),
    (
        "黄金",
        ("黄金", "上海金"),
    ),
    (
        "债券",
        ("纯债", "中长债", "短债", "政金债", "国开债", "金融债", "利率债", "债券", "可转债"),
    ),
    (
        "CPO/光模块",
        ("光模块", "CPO", "光通信", "PCB"),
    ),
    (
        "半导体/科创芯片",
        ("半导体", "芯片", "科创芯片", "集成电路", "科创50", "科创100"),
    ),
    (
        "创新药/医药",
        ("创新药", "生物医药", "医疗保健", "CXO", "医疗", "医药", "生物科技", "健康"),
    ),
    (
        "全球科技/QDII",
        ("纳斯达克", "标普", "全球科技", "新兴市场", "全球精选", "全球", "海外", "QDII", "数字经济"),
    ),
    (
        "白酒/消费",
        ("白酒", "酒指数", "消费龙头", "酒", "消费", "食品饮料"),
    ),
    (
        "资源/有色金属",
        ("有色金属", "稀有金属", "锂矿", "稀土", "资源精选", "资源", "矿业", "煤炭", "石油"),
    ),
)

OTHER_DIRECTION = "其他"


def classify_fund(fund_name: Optional[str]) -> Tuple[str, Optional[str]]:
    """返回 (方向, 命中的关键词)。

    未命中任何关键词时返回 ("其他", None)。
    """
    if not fund_name:
        return OTHER_DIRECTION, None

    text = str(fund_name).strip()
    if not text:
        return OTHER_DIRECTION, None

    for direction, keywords in _DIRECTION_KEYWORDS:
        for kw in keywords:
            if kw in text:
                return direction, kw

    return OTHER_DIRECTION, None


def classify_fund_direction(fund_name: Optional[str]) -> str:
    """便捷方法：只返回方向名。"""
    return classify_fund(fund_name)[0]
