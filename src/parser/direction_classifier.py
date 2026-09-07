"""
方向分类器 v2 — 根据基金名称 + 帖子上下文把交易归入方向维度

优先级：
  1. 基金名称明确命中关键词（high）
  2. 同帖基金级上下文明确定义（medium）
  3. 已有可靠基金映射（high）
  4. 无法确认 → "其他/待分类"（unknown）

不强行归类。基金名被截断时，必须证据充分才分类。
"""

from __future__ import annotations

import re
from typing import Optional, Tuple, Dict

# ============================================================
#  方向关键词表（优先级从高到低）
# ============================================================

_DIRECTION_KEYWORDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "港股方向",
        ("恒生", "港股", "香港", "港股通", "沪港深"),
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
        ("创新药", "生物医药", "医疗保健", "CXO", "医疗", "医药", "生物科技"),
    ),
    (
        "全球科技/QDII",
        ("纳斯达克", "标普", "全球科技", "新兴市场", "全球精选", "QDII"),
    ),
    (
        "白酒/消费",
        ("白酒", "酒指数", "消费龙头", "消费", "食品饮料"),
    ),
    (
        "资源/有色金属",
        ("有色金属", "稀有金属", "锂矿", "稀土", "资源精选", "资源", "矿业", "煤炭", "石油"),
    ),
)

# ============================================================
#  基金名 → 方向 静态映射（Priority 3）
# ============================================================

_FUND_MAPPING: Dict[str, str] = {
    # 示例：已知基金全称/常用简称 → 方向
    # "易方达蓝筹精选混合": "白酒/消费",
}

# ============================================================
#  上下文关键词（Priority 2：同帖观点中描述基金投资方向）
# ============================================================

# 这些短语出现在观点中，且与某笔操作直接关联时，可辅助判断方向
_CONTEXT_DIRECTION_PATTERNS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "半导体/科创芯片",
        ("半导体产业链", "半导体材料", "半导体设备", "芯片设计", "光电子材料",
         "台光电", "联发科", "亚太半导体"),
    ),
    (
        "黄金",
        ("黄金ETF", "黄金产业", "金价", "黄金股"),
    ),
    (
        "创新药/医药",
        ("创新药产业链", "医药板块", "生物科技股"),
    ),
    (
        "白酒/消费",
        ("白酒板块", "消费板块", "食品饮料行业"),
    ),
    (
        "全球科技/QDII",
        ("海外科技", "美股科技", "纳斯达克科技"),
    ),
    (
        "港股方向",
        ("港股科技", "恒生科技", "港股互联网"),
    ),
)

OTHER_DIRECTION = "其他/待分类"


# ============================================================
#  分类结果
# ============================================================

class ClassificationResult:
    """方向分类结果（含证据和置信度）。"""

    def __init__(
        self,
        direction: str,
        confidence: str,
        source: str,
        evidence: str = "",
    ):
        self.direction = direction
        self.confidence = confidence  # high / medium / low / unknown
        self.source = source          # fund_name / post_context / mapping / unknown
        self.evidence = evidence

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "classification_confidence": self.confidence,
            "classification_source": self.source,
            "classification_evidence": self.evidence,
        }


# ============================================================
#  核心分类逻辑
# ============================================================

def _match_fund_name(fund_name: str) -> Tuple[Optional[str], Optional[str]]:
    """Priority 1：基金名称关键词匹配。

    返回 (方向, 命中关键词) 或 (None, None)。
    注意：基金名常被 OCR 截断（如"南方上证科创板芯..."），
    所以用包含匹配，但要求关键词长度 >= 2 以避免单字误判。
    """
    text = fund_name.strip()
    if not text:
        return None, None

    for direction, keywords in _DIRECTION_KEYWORDS:
        for kw in keywords:
            # 单字关键词（如"酒"、"黄"）需要更严格的上下文
            if len(kw) == 1:
                # 单字必须前后有基金名字特征（如"酒指数"、"黄金ETF"）
                # 这里跳过单字，避免"黄"→黄金的误判
                continue
            if kw in text:
                return direction, kw

    return None, None


def _match_context(opinion_text: str, fund_name: str) -> Tuple[Optional[str], Optional[str]]:
    """Priority 2：同帖观点中基金级上下文明确定义。

    只有观点中明确描述"该基金投资XXX"、"这是一只XXX基金"等时，才辅助判断。
    返回 (方向, 命中关键词) 或 (None, None)。
    """
    if not opinion_text:
        return None, None

    text = opinion_text.strip()
    if not text:
        return None, None

    for direction, keywords in _CONTEXT_DIRECTION_PATTERNS:
        for kw in keywords:
            if kw in text:
                return direction, kw

    return None, None


def _match_mapping(fund_name: str) -> Optional[str]:
    """Priority 3：基金名 → 方向 静态映射。"""
    if not fund_name:
        return None
    text = fund_name.strip()
    # 精确匹配
    if text in _FUND_MAPPING:
        return _FUND_MAPPING[text]
    # 子串匹配（基金名可能被截断）
    for full_name, direction in _FUND_MAPPING.items():
        if full_name in text or text in full_name:
            return direction
    return None


def classify_fund(
    fund_name: Optional[str],
    opinion_text: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """返回 (方向, 命中的关键词)。

    兼容旧接口：只传 fund_name 时，退化为纯名称匹配。
    """
    result = classify_fund_with_evidence(fund_name, opinion_text)
    return result.direction, result.evidence if result.source != "unknown" else None


def classify_fund_with_evidence(
    fund_name: Optional[str],
    opinion_text: Optional[str] = None,
) -> ClassificationResult:
    """完整分类：返回 ClassificationResult（含置信度、来源、证据）。"""

    # Priority 1: 基金名称明确命中
    if fund_name:
        direction, kw = _match_fund_name(fund_name)
        if direction:
            return ClassificationResult(
                direction=direction,
                confidence="high",
                source="fund_name",
                evidence=f"基金名称含'{kw}'",
            )

    # Priority 3: 静态映射
    if fund_name:
        direction = _match_mapping(fund_name)
        if direction:
            return ClassificationResult(
                direction=direction,
                confidence="high",
                source="mapping",
                evidence=f"基金映射: {fund_name} → {direction}",
            )

    # Priority 2: 同帖上下文
    if opinion_text and fund_name:
        direction, kw = _match_context(opinion_text, fund_name)
        if direction:
            return ClassificationResult(
                direction=direction,
                confidence="medium",
                source="post_context",
                evidence=f"同帖观点含'{kw}'，关联基金'{fund_name}'",
            )

    # Priority 4: 无法确认
    return ClassificationResult(
        direction=OTHER_DIRECTION,
        confidence="unknown",
        source="unknown",
        evidence=fund_name or "(无基金名)",
    )


def classify_fund_direction(fund_name: Optional[str]) -> str:
    """便捷方法：只返回方向名（兼容旧接口）。"""
    return classify_fund(fund_name)[0]


def is_other_direction(direction: str) -> bool:
    """判断是否为「其他/待分类」方向。"""
    return direction == OTHER_DIRECTION or direction == "其他"
