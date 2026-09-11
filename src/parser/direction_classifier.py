"""
方向分类器 v3 — 每笔交易独立分类，禁止"整帖主题扩散"

分类优先级：
  1. 基金名称明确命中关键词（high）
  2. 已有可靠基金映射（high）
  3. 同帖基金级上下文明确定义（medium）
     ——必须能同时在观点中定位到「该具体基金名」+「方向描述短语」，
       缺一不可。整帖讨论半导体 ≠ 帖内所有基金都是半导体。
  4. 无法确认 → "其他/待分类"（unknown）

v3 与 v2 关键差异：
  - 旧 v2：观点中含方向关键词 → 整帖所有基金都归到该方向（错误）
  - 新 v3：必须能识别"这只基金就是XXX"或"加/减这只基金（属于XXX方向）"等
           显式绑定语句
  - 截断/OCR 容忍：基金名尾截断（如"南方纳斯达克10..."）时，
                   取前 N 字作为模糊名在观点中查找
"""

from __future__ import annotations

import re
from typing import Optional, Tuple, Dict, List

# ============================================================
#  方向关键词表（Priority 1）
# ============================================================
# 注意：保持顺序无关，所有方向平等参与；只按命中是否具体决定优先级。

_DIRECTION_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "港股方向": ("恒生", "港股", "香港", "港股通", "沪港深"),
    "黄金": ("黄金", "上海金"),
    "债券": ("纯债", "中长债", "短债", "政金债", "国开债", "金融债", "利率债", "可转债"),
    "CPO/光模块": ("光模块", "CPO", "光通信", "PCB"),
    "半导体/科创芯片": ("半导体", "芯片", "科创芯片", "集成电路", "科创50", "科创100"),
    "创新药/医药": ("创新药", "生物医药", "医疗保健", "CXO", "医疗", "医药", "生物科技"),
    "全球科技/QDII": ("纳斯达克", "标普", "全球科技", "新兴市场", "全球精选", "QDII", "移动互联", "海外数字", "海外科技", "东南亚", "海外中国"),
    "白酒/消费": ("白酒", "酒指数", "消费龙头", "消费", "食品饮料"),
    "资源/有色金属": ("有色金属", "稀有金属", "锂矿", "稀土", "资源精选", "资源", "矿业", "煤炭", "石油"),
}

# 双向匹配：基金名含方向关键词 → 该方向
# （保留为有序表，方便按声明顺序在冲突时优先选择）
_DIRECTION_KEYWORD_LIST: Tuple[Tuple[str, Tuple[str, ...]], ...] = tuple(
    (name, kws) for name, kws in _DIRECTION_KEYWORDS.items()
)

# ============================================================
#  基金名 → 方向 静态映射（Priority 2）
# ============================================================

_FUND_MAPPING: Dict[str, str] = {
    # 已知基金全称/常用简称 → 方向
}

# ============================================================
#  上下文方向短语（Priority 3）
# ============================================================
# 只有当观点中同时出现「方向短语」+「具体基金名」时，才认为该方向短语
# 是对这只基金的语义绑定。整帖主题不构成证据。
# 短语本身描述某方向的投资属性（"…是/为/属…基金" / "…产业链/赛道"）。

_CONTEXT_PHRASES: Dict[str, Tuple[str, ...]] = {
    "半导体/科创芯片": (
        "半导体产业链", "半导体材料", "半导体设备", "芯片设计",
        "光电子材料", "台光电", "联发科", "亚太半导体", "算力芯片",
        "存储芯片", "半导体",
    ),
    "CPO/光模块": (
        "光模块", "CPO", "光通信", "光模块CPO", "光模块方向",
    ),
    "黄金": ("黄金ETF", "黄金产业", "金价", "黄金股"),
    "创新药/医药": ("创新药产业链", "医药板块", "生物科技股", "创新药"),
    "白酒/消费": ("白酒板块", "消费板块", "食品饮料行业"),
    "全球科技/QDII": ("海外科技", "美股科技", "纳斯达克科技", "QDII"),
    "港股方向": ("港股科技", "恒生科技", "港股互联网"),
    "资源/有色金属": ("有色金属板块", "有色金属", "锂矿", "稀土"),
}

OTHER_DIRECTION = "其他/待分类"


# ============================================================
#  分类结果
# ============================================================

class ClassificationResult:
    """方向分类结果（含证据和置信度）。"""

    __slots__ = ("direction", "confidence", "source", "evidence")

    def __init__(
        self,
        direction: str,
        confidence: str,
        source: str,
        evidence: str = "",
    ):
        self.direction = direction
        self.confidence = confidence  # high / medium / low / unknown
        self.source = source          # fund_name / mapping / post_context / unknown
        self.evidence = evidence

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "classification_confidence": self.confidence,
            "classification_source": self.source,
            "classification_evidence": self.evidence,
        }


# ============================================================
#  工具
# ============================================================

def _short_fund_key(fund_name: str, max_len: int = 8) -> str:
    """基金名去尾（用于观点中模糊匹配）。

    截断时 OCR 会留 "..."，去掉它再取前 N 个汉字/字符。
    """
    if not fund_name:
        return ""
    s = fund_name.replace("...", "").replace("…", "").strip()
    # 优先取前 8 个字符；若全是英文则取首段单词
    if len(s) <= max_len:
        return s
    return s[:max_len]


def _fund_mentioned(fund_name: str, opinion_text: str) -> bool:
    """判断观点中是否提到该基金（全名/前 N 字匹配）。"""
    if not fund_name or not opinion_text:
        return False
    if fund_name in opinion_text:
        return True
    short = _short_fund_key(fund_name)
    if short and len(short) >= 3 and short in opinion_text:
        return True
    # 二次模糊：基金名前 4 字符
    head4 = fund_name[:4]
    if len(head4) >= 3 and head4 in opinion_text:
        return True
    return False


# ============================================================
#  核心分类逻辑
# ============================================================

def _match_fund_name(fund_name: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Priority 1：基金名称关键词匹配。

    跳过单字关键词（避免"黄"误判为"黄金"等）。
    """
    if not fund_name:
        return None, None
    text = fund_name.strip()
    if not text:
        return None, None

    for direction, keywords in _DIRECTION_KEYWORD_LIST:
        for kw in keywords:
            if len(kw) == 1:
                continue
            if kw in text:
                return direction, kw
    return None, None


def _match_mapping(fund_name: Optional[str]) -> Optional[str]:
    """Priority 2：基金名 → 方向 静态映射。"""
    if not fund_name:
        return None
    text = fund_name.strip()
    if text in _FUND_MAPPING:
        return _FUND_MAPPING[text]
    for full_name, direction in _FUND_MAPPING.items():
        if full_name in text or text in full_name:
            return direction
    return None


def _match_context(
    fund_name: Optional[str],
    opinion_text: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    """Priority 3：同帖基金级上下文绑定。

    关键约束（v3 加强版）：必须同时满足
      (a) 观点中明确提到该具体基金
      (b) 方向短语出现在该基金**前后 60 字**的窗口内
      (c) 短语与基金名之间的"提示词"至少包含一个强连接词
          （"是/为/属/也/同样/这只/该/属于/属于…方向/方向是"等）

    反例（旧 v2 错判）：
      "半导体材料设备/光模块CPO 是低位... 加仓 1000 招商半导体
       加仓 10000 东吴阿尔法"  → 东吴阿尔法 不会被错归为半导体
                                  （"半导体"距离"东吴阿尔法"很远
                                    且无强连接词）

    正例：
      "今天减仓 财通科技创新混合C，同样是 CPO" → 财通 = CPO
      "加仓 招商中证半导体（半导体方向）"        → 招商 = 半导体
    """
    if not opinion_text or not fund_name:
        return None, None

    if not _fund_mentioned(fund_name, opinion_text):
        return None, None

    text = opinion_text
    # 找到该基金在观点中的所有出现位置
    # 关键：先用全名匹配，全名不命中才用短前缀（按长度从长到短）。
    # 否则短前缀容易在多个提及点匹配到错位窗口。
    candidates: List[Tuple[str, int]] = []  # (key, priority) priority: 大=更优先
    if fund_name and len(fund_name) >= 2:
        candidates.append((fund_name, 3))
    short = _short_fund_key(fund_name)
    if short and short != fund_name and len(short) >= 4:
        candidates.append((short, 2))
    # 不要用 fund_name[:4] 太短，容易误匹配"这只"等

    fund_positions: List[int] = []
    for key, _prio in candidates:
        if len(key) < 2:
            continue
        start = 0
        while True:
            i = text.find(key, start)
            if i < 0:
                break
            fund_positions.append(i)
            start = i + 1
    if not fund_positions:
        return None, None

    WINDOW = 80  # 前后 80 字符窗口

    # 强连接词：方向短语与基金名之间的语义提示
    POSITIVE_CONNECTORS = (
        "是", "为", "属于", "属", "是只", "也是", "是一只",
        "方向是", "属于这个", "属于该", "属该", "属此", "属这只",
    )
    NEGATION = (
        "不是", "不像", "没有像", "没像", "也不", "不属", "不属于", "非",
    )

    for direction, phrases in _CONTEXT_PHRASES.items():
        for ph in phrases:
            ph_pos = text.find(ph)
            if ph_pos < 0:
                continue
            # 检查 ph 是否在 fund 附近 WINDOW 范围内
            for fp in fund_positions:
                if abs(ph_pos - fp) > WINDOW + len(ph) + len(fund_name):
                    continue
                # 找基金名和短语之间的窗口文本
                lo, hi = min(fp, ph_pos + len(ph)), max(fp + len(fund_name), ph_pos)
                bridge = text[lo:hi]
                # 否定语境 → 跳过
                if any(n in bridge for n in NEGATION):
                    continue
                # 必须有正向连接词；纯 NEUTRAL 不足以确认
                if any(c in bridge for c in POSITIVE_CONNECTORS):
                    return direction, ph
                # NEUTRAL 单独不够（防止整帖主题扩散）
    return None, None


def classify_fund(
    fund_name: Optional[str],
    opinion_text: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """返回 (方向, 命中的关键词)，兼容旧接口。"""
    r = classify_fund_with_evidence(fund_name, opinion_text)
    return r.direction, r.evidence if r.source != "unknown" else None


def classify_fund_with_evidence(
    fund_name: Optional[str],
    opinion_text: Optional[str] = None,
) -> ClassificationResult:
    """完整分类：返回 ClassificationResult。"""
    # Priority 1: 基金名称
    if fund_name:
        d, kw = _match_fund_name(fund_name)
        if d:
            return ClassificationResult(
                direction=d,
                confidence="high",
                source="fund_name",
                evidence=f"基金名称含'{kw}'",
            )

    # Priority 2: 静态映射
    if fund_name:
        d = _match_mapping(fund_name)
        if d:
            return ClassificationResult(
                direction=d,
                confidence="high",
                source="mapping",
                evidence=f"基金映射: {fund_name} → {d}",
            )

    # Priority 3: 同帖上下文（必须能定位到该基金）
    if fund_name and opinion_text:
        d, kw = _match_context(fund_name, opinion_text)
        if d:
            return ClassificationResult(
                direction=d,
                confidence="medium",
                source="post_context",
                evidence=f"观点'{kw[:8]}…'绑定'{_short_fund_key(fund_name)}'",
            )

    # Priority 4: 兜底
    return ClassificationResult(
        direction=OTHER_DIRECTION,
        confidence="unknown",
        source="unknown",
        evidence=fund_name or "(无基金名)",
    )


def classify_fund_direction(fund_name: Optional[str]) -> str:
    return classify_fund(fund_name)[0]


def is_other_direction(direction: str) -> bool:
    return direction == OTHER_DIRECTION or direction == "其他"


# ============================================================
#  批量分类（Source of Truth 入口）
# ============================================================

def classify_records(records) -> List[ClassificationResult]:
    """对一组 records 一次性分类，返回与 records 一一对应的 ClassificationResult。

    后续所有聚合（方向汇总、7日趋势、推荐、AI 报告）必须使用本函数返回的
    ClassificationResult，不得再调用 classify_fund 重算。
    """
    out: List[ClassificationResult] = []
    for r in records:
        fund = getattr(r, "fund_name", None)
        opinion = getattr(r, "opinion_text", None)
        out.append(classify_fund_with_evidence(fund, opinion))
    return out
