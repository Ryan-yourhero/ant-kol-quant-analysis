"""
数据提取模块
负责：
1. 从解析后的UiNode树中，提取业务字段：
   - 大V名称 (kol_name)
   - 操作类型 (action_type：BUY / SELL / TRANSFER / CANCEL)
   - BUY/SELL：fund_name + amount + amount_unit(元/份)
   - TRANSFER：source_fund + source_amount + target_fund + target_amount
   - CANCEL：cancel_type (BUY_CANCEL / SELL_CANCEL / TRANSFER_CANCEL / UNKNOWN) + 关联fund
   - 时间 + 金额数值归一化
2. 将原始文本节点通过规则匹配+上下文邻近策略组合为结构化记录
3. 支持多规则匹配、置信度标记

注意：此模块是纯规则提取（不含AI分析），AI分析模块将在后续版本单独加入。
"""

import re
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple, Any

from config import settings
from .xml_parser import UiNode, UIXmlParser, Bounds

logger = logging.getLogger(__name__)


# ============================================================
#  数据结构
# ============================================================

@dataclass
class KolOperation:
    """大V操作记录（结构化输出）

    存储模式（按 action_type 不同使用不同字段组）：

    BUY:
        action_type = "BUY"
        fund_name, amount, amount_value, amount_unit("元") → 非空

    SELL:
        action_type = "SELL"
        fund_name, amount, amount_value, amount_unit("份") → 非空

    TRANSFER:
        action_type = "TRANSFER"
        source_fund, source_amount, source_amount_value, source_amount_unit("份") → 非空
        target_fund, target_amount, target_amount_value, target_amount_unit("元") → 非空

    CANCEL:
        action_type = "CANCEL"
        cancel_type = "BUY_CANCEL" | "SELL_CANCEL" | "TRANSFER_CANCEL" | "UNKNOWN"
        fund_name (被撤销关联基金，可选)
        amount / source_fund / target_fund (可选，视撤销内容而定)
    """

    # --- 通用字段 ---
    kol_name: str = ""                              # 大V名称
    action_type: str = ""                           # BUY / SELL / TRANSFER / CANCEL
    operation: str = ""                             # 旧兼容：buy/sell/transfer/cancel/unknown
    operation_text: str = ""                        # 原始操作文本（如"买入确认中"、"转换确认中"、"撤销"）
    timestamp: str = ""                             # 时间字符串
    confidence: float = 0.0                         # 提取置信度 0.0 ~ 1.0
    raw_context: Dict[str, Any] = field(default_factory=dict)

    # --- BUY / SELL 使用（兼容旧字段）---
    fund_name: str = ""
    amount: str = ""                                # 原始字符串
    amount_value: float = 0.0                       # 数值（元 或 份 的数值）
    amount_unit: str = ""                           # 元 / 份 / 万 / 千

    # --- TRANSFER 使用 ---
    source_fund: str = ""
    source_amount: str = ""
    source_amount_value: float = 0.0
    source_amount_unit: str = ""                    # 固定应为"份"
    target_fund: str = ""
    target_amount: str = ""
    target_amount_value: float = 0.0
    target_amount_unit: str = ""                    # 固定应为"元"

    # --- CANCEL 使用 ---
    cancel_type: str = ""                           # BUY_CANCEL / SELL_CANCEL / TRANSFER_CANCEL / UNKNOWN

    # ========================================================
    def to_dict(self) -> Dict[str, Any]:
        base = {
            "kol_name": self.kol_name,
            "action_type": self.action_type,
            "operation": self.operation,
            "operation_text": self.operation_text,
            "timestamp": self.timestamp,
            "confidence": round(self.confidence, 3),
        }
        if self.action_type in ("BUY", "SELL"):
            base.update({
                "fund": self.fund_name,
                "amount": self.amount,
                "amount_value": round(self.amount_value, 4),
                "unit": self.amount_unit,
            })
        elif self.action_type == "TRANSFER":
            base.update({
                "source_fund": self.source_fund,
                "source_amount": self.source_amount,
                "source_amount_value": round(self.source_amount_value, 4),
                "target_fund": self.target_fund,
                "target_amount": self.target_amount,
                "target_amount_value": round(self.target_amount_value, 4),
            })
        elif self.action_type == "CANCEL":
            base.update({
                "cancel_type": self.cancel_type,
                "fund": self.fund_name,
                "amount": self.amount,
                "unit": self.amount_unit,
                # 撤销若能对应到转换，也顺带存
                "source_fund": self.source_fund,
                "target_fund": self.target_fund,
            })
        else:
            # unknown：兜底输出旧字段
            base.update({
                "fund": self.fund_name,
                "amount": self.amount,
                "amount_value": round(self.amount_value, 4),
                "unit": self.amount_unit,
                "cancel_type": self.cancel_type,
                "source_fund": self.source_fund,
                "target_fund": self.target_fund,
            })
        return base

    def is_complete(self) -> bool:
        """不同 action_type 下的完整性要求"""
        if not self.kol_name:
            return False
        if self.action_type == "BUY":
            return bool(self.fund_name and (self.amount_value > 0 or self.amount))
        if self.action_type == "SELL":
            return bool(self.fund_name and (self.amount_value > 0 or self.amount))
        if self.action_type == "TRANSFER":
            return bool(self.source_fund and self.target_fund
                        and (self.source_amount_value > 0 or self.source_amount)
                        and (self.target_amount_value > 0 or self.target_amount))
        if self.action_type == "CANCEL":
            return bool(self.cancel_type)
        return False


# ============================================================
#  提取器
# ============================================================

class OperationDataExtractor:
    """
    [DEPRECATED 架构v2]
    本规则引擎（复杂交易规则判断）已从采集链路停用。

    新架构：
      - ScrollManager 只负责 dump XML → 提取页面 texts（raw_text_extractor.py），输出 raw_pages_*.json
      - 交易结构化判断交由后续 AIParser 模块：raw_text -> structured_trade JSON

    保留本文件与类定义：
      1) 向后兼容：旧入口脚本/直接调用 OperationDataExtractor(...) 不崩
      2) 后续若做规则对比，可继续复用 extract_from_flat_texts() 等方法做离线比对
    """

    _DEPRECATED_WARNED = False  # 类级标记，避免每个实例打印一遍

    def __init__(self):
        if not OperationDataExtractor._DEPRECATED_WARNED:
            print(
                "[DEPRECATED][OperationDataExtractor] "
                "复杂交易规则已从采集链路停用；"
                "爬虫阶段只采集原始文本，结构化判断留给后续 AIParser(raw_text -> structured_trade)。"
            )
            OperationDataExtractor._DEPRECATED_WARNED = True
        self._operation_keywords = settings.OPERATION_KEYWORDS
        self._amount_pattern = re.compile(settings.AMOUNT_PATTERN)
        self._time_patterns = [re.compile(p) for p in settings.TIME_PATTERNS]

        # ============ 从 settings 加载严格提取配置（新增真实页面优化）============
        # 基金名必须命中：关键词 或 末尾份额后缀
        self._fund_name_keywords = tuple(getattr(settings, "FUND_NAME_KEYWORDS", ()))
        self._fund_tail_suffixes = tuple(getattr(settings, "FUND_NAME_TAIL_SUFFIXES", ()))
        # 基金名/大V名黑名单
        self._invalid_fund_names = tuple(getattr(settings, "INVALID_FUND_NAMES", ()))
        self._invalid_kol_names = tuple(getattr(settings, "INVALID_KOL_NAMES", ()))
        # 金额严格格式 + 黑名单
        self._amount_strict_patterns = [
            re.compile(p) for p in getattr(settings, "AMOUNT_STRICT_PATTERNS", [])
        ]
        self._amount_blacklist_contains = tuple(
            getattr(settings, "AMOUNT_BLACKLIST_CONTAINS", ())
        )
        # 观点长文过滤
        self._fund_name_max_len = int(getattr(settings, "FUND_NAME_MAX_LEN", 60))
        self._opinion_punct_threshold = int(
            getattr(settings, "OPINION_PUNCT_COUNT_THRESHOLD", 3)
        )
        self._opinion_punct_re = re.compile(r"[，。？！；：、,.!?;:]")

        # 旧兼容：基金后缀集合（保留为严格关键词子集）
        self._fund_suffixes = self._fund_name_keywords

        # 大V名称常见特征
        self._kol_name_pattern = re.compile(r"^[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9_\-·\.]{1,19}$")

        # 单位换算
        self._unit_multiplier = {
            "万": 10000.0,
            "千": 1000.0,
            "元": 1.0,
            "块": 1.0,
            "份": 1.0,
            "万份": 10000.0,
            "": 1.0,
        }

    # ============================================================
    #  主入口
    # ============================================================
    def extract(self, root: UiNode) -> List[KolOperation]:
        parser = UIXmlParser()
        all_text_nodes = parser.get_all_text_nodes(root)
        sorted_nodes = parser.get_nodes_sorted_by_position(all_text_nodes)

        # Step 1: 找出所有操作锚点节点 (node, action_type, matched_text)
        anchor_nodes = self._find_operation_anchors(sorted_nodes)
        logger.info(f"找到 {len(anchor_nodes)} 个操作锚点节点")

        # Step 2: 构建记录
        rows = parser.group_nodes_by_rows(sorted_nodes)

        operations: List[KolOperation] = []
        # 用于 TRANSFER 去重：如果一个转换锚点已经生成了 TRANSFER 记录，避免旁边的"卖出份额"/"买入金额"节点再各生成 BUY/SELL 记录
        consumed_nodes: set = set()

        for idx, (anchor, op_type, op_text) in enumerate(anchor_nodes):
            if id(anchor) in consumed_nodes:
                continue
            try:
                if op_type == "transfer":
                    op, consumed = self._build_transfer_operation(
                        anchor=anchor, op_text=op_text,
                        all_nodes=sorted_nodes, rows=rows,
                        anchor_nodes_window=anchor_nodes[max(0, idx - 3): idx + 4],
                    )
                    for n in consumed:
                        consumed_nodes.add(id(n))
                elif op_type == "cancel":
                    op, consumed = self._build_cancel_operation(
                        anchor=anchor, op_text=op_text,
                        all_nodes=sorted_nodes, rows=rows,
                    )
                    for n in consumed:
                        consumed_nodes.add(id(n))
                else:
                    # buy / sell
                    op = self._build_simple_operation(
                        anchor=anchor, operation=op_type, op_text=op_text,
                        all_nodes=sorted_nodes, rows=rows,
                    )
                if op is None:
                    continue
                op.confidence = self._calc_confidence(op)
                operations.append(op)
            except Exception as e:
                logger.warning(f"构建操作记录失败({op_text})，跳过: {e}")
                continue

        # 按置信度降序
        operations.sort(key=lambda x: x.confidence, reverse=True)

        # ---- 按去重 key 合并重复记录（同 key 只保留置信度最高的一条）----
        # 关键业务字段匹配即可，kol/timestamp 常为空或不一致，不作为去重 key
        def make_dedup_key(op: KolOperation) -> str:
            at = op.action_type
            parts = [at]
            if at in ("BUY", "SELL"):
                parts += [op.fund_name.strip(),
                          f"{op.amount_value:.4f}", op.amount_unit.strip()]
            elif at == "TRANSFER":
                parts += [op.source_fund.strip(), f"{op.source_amount_value:.4f}",
                          op.target_fund.strip(), f"{op.target_amount_value:.4f}"]
            elif at == "CANCEL":
                parts += [op.cancel_type.strip(),
                          op.fund_name.strip(),
                          op.source_fund.strip(), op.target_fund.strip(),
                          f"{op.amount_value:.4f}",
                          f"{op.source_amount_value:.4f}",
                          f"{op.target_amount_value:.4f}"]
            return "||".join(parts)

        seen: dict = {}
        dedup: List[KolOperation] = []
        for op in operations:
            k = make_dedup_key(op)
            if k and k not in seen:
                seen[k] = True
                dedup.append(op)

        # ---- CANCEL 抑制：撤销记录对应的原始操作（买/卖/转换）若匹配则删除 ----
        cancellations = [op for op in dedup if op.action_type == "CANCEL"]
        if cancellations:
            filtered: List[KolOperation] = []
            for op in dedup:
                if op.action_type in ("BUY", "SELL", "TRANSFER"):
                    # 看看有没有匹配的 CANCEL（基金名/源目标基金匹配 即判定为同一个操作的撤销）
                    drop = False
                    for c in cancellations:
                        if c.cancel_type == f"{op.action_type}_CANCEL":
                            if op.action_type == "TRANSFER":
                                if (c.source_fund.strip() == op.source_fund.strip()
                                        and c.target_fund.strip() == op.target_fund.strip()):
                                    drop = True
                                    break
                            else:
                                if c.fund_name.strip() and c.fund_name.strip() == op.fund_name.strip():
                                    drop = True
                                    break
                    if drop:
                        continue
                filtered.append(op)
            dedup = filtered

        # ---- 新增：最终严格剔除（真实页面防误识别） ----
        # 规则：
        # 1) BUY/SELL → 基金名必须通过严格准入(_score_fund_node>0) 且 amount_value>0 才保留
        # 2) TRANSFER → source/target基金都必须严格准入，且至少一方amount_value>0
        # 3) CANCEL → cancel_type=UNKNOWN时，至少有严格准入的基金名才保留；若fund命中黑名单则剔除
        # 4) kol_name → 最后一次重校验：不符合_looks_like_kol_name的置空
        # 5) 金额再次核对：含黑名单字符(收益率/%/img?fileid) 或 值<=0时置空
        cleaned: List[KolOperation] = []
        for op in dedup:
            at = (op.action_type or "").upper()
            drop = False

            # --- kol_name 最终置空 ---
            if op.kol_name and not self._looks_like_kol_name(op.kol_name):
                op.kol_name = ""

            # --- 金额/基金名黑名单再次核对（含收益率/%/等）---
            def _bad_amount(amt_text: str) -> bool:
                if not amt_text:
                    return False
                return any(bl in amt_text for bl in self._amount_blacklist_contains)

            if _bad_amount(op.amount):
                op.amount = ""
                op.amount_value = 0.0
                op.amount_unit = ""
            if _bad_amount(op.source_amount):
                op.source_amount = ""
                op.source_amount_value = 0.0
            if _bad_amount(op.target_amount):
                op.target_amount = ""
                op.target_amount_value = 0.0

            # --- BUY / SELL 准入要求 ---
            if at in ("BUY", "SELL"):
                fn = (op.fund_name or "").strip()
                fund_valid = bool(fn) and self._score_fund_node_fake(fn) > 0
                amt_valid = (op.amount_value is not None and op.amount_value > 0) or bool(
                    (op.amount or "").strip())
                if not (fund_valid and amt_valid):
                    drop = True

            # --- TRANSFER 准入要求 ---
            elif at == "TRANSFER":
                sf = (op.source_fund or "").strip()
                tf = (op.target_fund or "").strip()
                src_valid = bool(sf) and self._score_fund_node_fake(sf) > 0
                tgt_valid = bool(tf) and self._score_fund_node_fake(tf) > 0
                amt_ok = ((op.source_amount_value or 0) > 0 or
                          (op.target_amount_value or 0) > 0 or
                          bool(op.source_amount or op.target_amount))
                if not (src_valid and tgt_valid and amt_ok):
                    drop = True

            # --- CANCEL 准入要求 ---
            elif at == "CANCEL":
                ct = (op.cancel_type or "").upper()
                fn = (op.fund_name or "").strip()
                fund_valid = bool(fn) and self._score_fund_node_fake(fn) > 0
                sf = (op.source_fund or "").strip()
                tf = (op.target_fund or "").strip()
                src_tgt_valid = (bool(sf) and self._score_fund_node_fake(sf) > 0
                                 and bool(tf) and self._score_fund_node_fake(tf) > 0)
                if ct == "UNKNOWN":
                    # 未知撤销类型：至少要有严格准入的 fund 或 source/target 配对
                    if not (fund_valid or src_tgt_valid):
                        drop = True
                else:
                    # 已知类型：至少有 fund_valid 或 src_tgt_valid 或 有明确 amount
                    has_any_amt = ((op.amount_value or 0) > 0
                                   or (op.source_amount_value or 0) > 0
                                   or (op.target_amount_value or 0) > 0
                                   or bool(op.amount or op.source_amount or op.target_amount))
                    if not (fund_valid or src_tgt_valid or has_any_amt):
                        drop = True
            else:
                # UNKNOWN 类型全部丢弃（真实页面不应有 unknown）
                drop = True

            if not drop:
                # --- 金额文本裁剪（避免把整段观点长文写进 amount） ---
                if op.amount:
                    op.amount = self._extract_amount_snippet(
                        op.amount, op.amount_value, op.amount_unit)
                if op.source_amount:
                    op.source_amount = self._extract_amount_snippet(
                        op.source_amount, op.source_amount_value, op.source_amount_unit)
                if op.target_amount:
                    op.target_amount = self._extract_amount_snippet(
                        op.target_amount, op.target_amount_value, op.target_amount_unit)
                cleaned.append(op)

        dedup = cleaned

        # ---- 统一填充 operation_text 原始多行快照（解析错时可人工复核）----
        for op in dedup:
            self._fill_operation_text_snapshot(op)

        logger.info(f"成功提取 {len(dedup)} 条操作记录（去重前 {len(operations)} 条）")
        return dedup

    # ============================================================
    #  辅助：基于纯文本的基金准入打分（用于最终剔除时对字段做二次核对）
    # ============================================================
    def _score_fund_node_fake(self, text: str) -> int:
        """用纯文本模拟 _score_fund_node 的打分与准入门槛，只返回 score（>0 算准入）"""
        if not text:
            return 0
        fake = UiNode(
            index=0, text=text, content_desc="", resource_id="",
            class_name="", package="", bounds=None,
            clickable=False, scrollable=False, enabled=True,
            selected=False, checked=False, depth=0,
            parent=None, children=[],
        )
        return self._score_fund_node(fake)

    # ============================================================
    #  辅助：金额文本裁剪（避免把整段观点长文写进 amount 字段）
    # ============================================================
    def _extract_amount_snippet(
        self, raw_text: str, amount_value: float, amount_unit: str,
    ) -> str:
        """
        当 amount_node 是一整段观点长文时，raw_text 会非常长，包含"加仓2000元$XX基金$..."等无关信息。
        此函数从 raw_text 中裁剪出「数字+单位」或「动作+数字+单位」的最短匹配片段；
        若无法裁剪或原始文本本身就干净简短，直接返回 value 格式化后的标准字符串（如 "2,000.00元"）。
        """
        clean = (raw_text or "").strip()
        unit = amount_unit or ""
        val = amount_value or 0.0

        # ---- 1) 如果原文本身很短且无观点标点，直接信任 ----
        if clean and len(clean) <= 40 and not self._is_opinion_text(clean):
            # 检查：开头/结尾是否直接像金额格式
            if re.search(r"[\d,]+(?:\.\d+)?", clean):
                return clean

        # ---- 2) 尝试从原文提取「动作+数字+单位」的小片段（优先长词动作：买入/卖出/加仓/减仓/申购/赎回/认购） ----
        snippet = ""
        if clean:
            action_pat = r"(?:买入|卖出|加仓|减仓|申购|赎回|认购|购买|建仓|清仓|止盈|止损|定投|转换)?\s*([\d,]+(?:\.\d+)?)\s*(万份|份|万|千|元|块)?"
            for m in re.finditer(action_pat, clean):
                try:
                    v = float(m.group(1).replace(",", ""))
                except ValueError:
                    continue
                mu = m.group(2) or ""
                # 检查数值（考虑单位换算）是否接近 amount_value
                actual_v = v * self._unit_multiplier.get(mu, 1.0) if mu else v
                if actual_v <= 0:
                    continue
                rel_err = abs(actual_v - val) / max(val, 1.0)
                if rel_err < 0.01:  # 误差在 1% 内认为匹配
                    snippet = m.group(0).strip()
                    break
            if snippet:
                # 保证末尾有单位
                if unit and not any(u in snippet for u in ("元", "份", "万", "千", "块")):
                    snippet += unit
                return snippet

        # ---- 3) 兜底：用 value + unit 格式化标准字符串 ----
        if val <= 0:
            return clean
        # 根据 val 是否整数选择小数位数
        if abs(val - round(val)) < 1e-6:
            num_str = f"{int(round(val)):,}"
        else:
            num_str = f"{val:,.2f}"
        if unit in ("万份", "份", "万", "千", "元", "块"):
            return f"{num_str}{unit}"
        if unit:
            return f"{num_str}{unit}"
        return num_str  # 没单位就只输出数字

    @staticmethod
    def _fill_operation_text_snapshot(op: KolOperation) -> None:
        """
        将识别到的结构化字段拼接为多行"原始操作快照"，写入 op.operation_text。
        格式：第一行保留原始锚点（若有），后续行按类型展示基金+金额明细。
        以后解析逻辑调整时，可以不重新爬直接从快照重算。
        """
        anchor_hint = (op.operation_text or "").strip()   # 原来的锚点文本（如"转换确认中"）
        lines: List[str] = []
        if anchor_hint:
            lines.append(anchor_hint)

        at = (op.action_type or "").upper()

        if at == "BUY":
            if op.fund_name:
                lines.append(op.fund_name)
            amt = op.amount or (f"{op.amount_value:,.2f}" if op.amount_value else "")
            unit = op.amount_unit or "元"
            if amt:
                lines.append(f"买入{amt}{unit}" if unit not in amt else f"买入{amt}")

        elif at == "SELL":
            if op.fund_name:
                lines.append(op.fund_name)
            amt = op.amount or (f"{op.amount_value:,.2f}" if op.amount_value else "")
            unit = op.amount_unit or "份"
            if amt:
                lines.append(f"卖出{amt}{unit}" if unit not in amt else f"卖出{amt}")

        elif at == "TRANSFER":
            src_fund = op.source_fund
            src_amt = op.source_amount or (
                f"{op.source_amount_value:,.2f}" if op.source_amount_value else "")
            src_unit = op.source_amount_unit or "份"
            tgt_fund = op.target_fund
            tgt_amt = op.target_amount or (
                f"{op.target_amount_value:,.2f}" if op.target_amount_value else "")
            tgt_unit = op.target_amount_unit or "元"

            if src_fund:
                lines.append(src_fund + ("..." if len(src_fund) >= 12 else ""))
            if src_amt:
                src_str = src_amt + (src_unit if src_unit not in src_amt else "")
                lines.append(f"卖出{src_str}")
            if tgt_fund:
                lines.append(f"转换至{tgt_fund}")
            if tgt_amt:
                tgt_str = tgt_amt + (tgt_unit if tgt_unit not in tgt_amt else "")
                lines.append(f"买入{tgt_str}")

        elif at == "CANCEL":
            sub = op.cancel_type or "UNKNOWN"
            cancel_prefix = {
                "BUY_CANCEL": "买入撤销",
                "SELL_CANCEL": "卖出撤销",
                "TRANSFER_CANCEL": "转换撤销",
            }.get(sub, "撤销")

            if op.source_fund and op.target_fund:
                # 转换撤销：先源再目标
                src_amt = op.source_amount or (
                    f"{op.source_amount_value:,.2f}" if op.source_amount_value else "")
                src_unit = op.source_amount_unit or "份"
                tgt_amt = op.target_amount or (
                    f"{op.target_amount_value:,.2f}" if op.target_amount_value else "")
                tgt_unit = op.target_amount_unit or "元"
                if op.source_fund:
                    lines.append(op.source_fund)
                if src_amt:
                    s = src_amt + (src_unit if src_unit not in src_amt else "")
                    lines.append(f"{cancel_prefix} 卖出{s}")
                if op.target_fund:
                    lines.append(f"转换至{op.target_fund}")
                if tgt_amt:
                    t = tgt_amt + (tgt_unit if tgt_unit not in tgt_amt else "")
                    lines.append(f"{cancel_prefix} 买入{t}")
            elif op.fund_name:
                # 买卖撤销
                lines.append(op.fund_name)
                amt = op.amount or (f"{op.amount_value:,.2f}" if op.amount_value else "")
                unit = op.amount_unit or ""
                if not unit:
                    unit = "份" if sub == "SELL_CANCEL" else "元"
                if amt:
                    a = amt + (unit if unit not in amt else "")
                    lines.append(f"{cancel_prefix}{a}")
            else:
                # 只有撤销锚点的，追加 cancel_type
                lines.append(cancel_prefix)

        else:
            # 未知类型：兜底把所有非空字段都拼出来
            if op.fund_name:
                lines.append(op.fund_name)
            if op.amount:
                lines.append(f"{op.operation or at} {op.amount}{op.amount_unit}")

        # 去掉前后空行，用换行拼接；若无内容则保留原始锚点
        cleaned = [ln.strip() for ln in lines if ln and ln.strip()]
        snapshot = "\n".join(cleaned) if cleaned else anchor_hint
        op.operation_text = snapshot

    # ----------------------------------------------------------
    #  extract_from_flat_texts（便于离线测试）
    # ----------------------------------------------------------
    def extract_from_flat_texts(self, texts: List[str]) -> List[KolOperation]:
        fake_nodes: List[UiNode] = []
        for i, t in enumerate(texts):
            fake_nodes.append(UiNode(
                index=i, text=t, content_desc="", resource_id="",
                class_name="", package="",
                bounds=Bounds(0, i * 100, 1000, (i + 1) * 100 - 10),
                clickable=False, scrollable=False, enabled=True,
                selected=False, checked=False, depth=0,
                parent=None, children=[],
            ))
        for j in range(len(fake_nodes)):
            if j > 0:
                fake_nodes[j].parent = fake_nodes[0]
        if fake_nodes:
            fake_nodes[0].children = fake_nodes[1:]
        root = UiNode(
            0, "", "", "", "", "", None, False, False, True, False, False, 0
        )
        if fake_nodes:
            root.children = [fake_nodes[0]]
            fake_nodes[0].parent = root
        return self.extract(root)

    # ============================================================
    #  操作锚点识别
    # ============================================================
    def _find_operation_anchors(
        self,
        nodes: List[UiNode],
    ) -> List[Tuple[UiNode, str, str]]:
        """Returns: [(node, op_type, matched_kw)]"""
        results = []
        for node in nodes:
            text = node.display_text
            if not text:
                continue
            op_type, kw = self._match_operation(text)
            if op_type:
                results.append((node, op_type, kw))
        return results

    def _match_operation(self, text: str) -> Tuple[str, str]:
        """按 keywords 顺序优先匹配更具体的长词（如"买入确认中" > "买入"）"""
        best = ("", "")
        best_len = 0
        for op_type, keywords in self._operation_keywords.items():
            for kw in keywords:
                if kw in text and len(kw) > best_len:
                    best = (op_type, kw)
                    best_len = len(kw)
        return best

    # ============================================================
    #  BUY / SELL 记录构建
    # ============================================================
    def _build_simple_operation(
        self,
        anchor: UiNode,
        operation: str,                 # "buy" / "sell"
        op_text: str,
        all_nodes: List[UiNode],
        rows: List[List[UiNode]],
    ) -> KolOperation:
        # 单位推断：买→元；卖→份；如果上下文有明确单位则覆盖
        default_unit = "元" if operation == "buy" else "份"

        op = KolOperation(
            action_type=operation.upper(),   # BUY / SELL
            operation=operation,
            operation_text=op_text,
            amount_unit=default_unit,
        )

        parser = UIXmlParser()
        rows = rows or parser.group_nodes_by_rows(all_nodes)
        anchor_row_idx = self._find_row_index(anchor, rows)
        # 扩大上下文：WebView/H5 节点 1 个/行，所以 ±8 行等价于前后 ±8 个节点；
        # 原生页面的真实 bounds 节点也兼容（因为一行可能包含多个节点）。
        context = self._collect_context_rows(rows, anchor_row_idx, above=8, below=8)
        context = [n for n in context if n is not anchor]
        same_row = ([n for n in rows[anchor_row_idx] if n is not anchor]
                    if anchor_row_idx is not None else [])

        # ---- 基金名称 ----
        fund_node = self._find_fund_name(anchor, same_row, context)
        if fund_node:
            op.fund_name = fund_node.display_text

        # ---- 金额/份额 + 单位 ----
        amount_node, value, unit = self._find_amount(
            anchor, same_row, context,
            prefer_unit=None,                    # 让数据自己说单位
        )
        if amount_node:
            op.amount = amount_node.display_text
            op.amount_value = value
            if unit:
                op.amount_unit = unit
            else:
                op.amount_unit = default_unit

        # ---- 时间 ----
        time_node = self._find_timestamp(anchor, context, anchor_row_idx, rows)
        if time_node:
            op.timestamp = time_node.display_text

        # ---- 大V ----
        kol_node = self._find_kol_name(anchor, all_nodes, anchor_row_idx, rows)
        if kol_node:
            op.kol_name = kol_node.display_text

        op.raw_context = self._build_debug_ctx(anchor, same_row, context)
        return op

    # ============================================================
    #  TRANSFER 记录构建（源基金+目标基金配对成1条）
    # ============================================================
    def _build_transfer_operation(
        self,
        anchor: UiNode,                # 锚点：转换确认中
        op_text: str,
        all_nodes: List[UiNode],
        rows: List[List[UiNode]],
        anchor_nodes_window,           # 附近的锚点窗口（剔除已被转换吸收的买/卖锚点）
    ) -> Tuple[Optional[KolOperation], List[UiNode]]:
        """
        返回：(记录, [被消费的节点列表])

        策略：
        1. 在锚点上下2~4行内，同时找"卖出份额(份)"(源) 和 "买入金额(元)"(目标)
        2. 源：匹配包含"卖出份额(份)"或紧邻单位"份"金额的基金 + 金额
        3. 目标：匹配包含"买入金额(元)"或紧邻单位"元"金额的基金 + 金额
        4. 找不到完整配对则尝试只匹配"买入金额/卖出份额"独立文本 + 基金名
        """
        consumed: List[UiNode] = []
        parser = UIXmlParser()
        rows = rows or parser.group_nodes_by_rows(all_nodes)
        anchor_row_idx = self._find_row_index(anchor, rows)
        # 转换场景：源/目标可能跨越多行，故扩大行范围；同时合并 flat 节点列表（前后120个）
        rows_context = self._collect_context_rows(rows, anchor_row_idx, above=12, below=12)
        rows_context = [n for n in rows_context if n is not anchor]
        flat_context: List[UiNode] = []
        if all_nodes:
            anchor_i = next((i for i, n in enumerate(all_nodes) if n is anchor), None)
            if anchor_i is not None:
                s = max(0, anchor_i - 120)
                e = min(len(all_nodes), anchor_i + 120)
                flat_context = [n for n in all_nodes[s:e] if n is not anchor]
        ctx_ids = {id(n): n for n in (rows_context + flat_context)}
        context = list(ctx_ids.values())

        op = KolOperation(
            action_type="TRANSFER",
            operation="transfer",
            operation_text=op_text,
            source_amount_unit="份",
            target_amount_unit="元",
        )

        # ---- 源：卖出份额(份) ----
        src_fund_node, src_amount_node, src_val, src_unit = self._find_pair_in_context(
            context=context,
            amount_keywords=("卖出份额", "卖出份额(份)", "卖出份额（份）"),
            prefer_unit="份",
            exclude_nodes=[anchor],
        )
        if src_fund_node:
            op.source_fund = src_fund_node.display_text
            consumed.append(src_fund_node)
        if src_amount_node:
            op.source_amount = src_amount_node.display_text
            op.source_amount_value = src_val
            if src_unit:
                op.source_amount_unit = src_unit
            consumed.append(src_amount_node)

        # ---- 目标：买入金额(元) ----
        tgt_fund_node, tgt_amount_node, tgt_val, tgt_unit = self._find_pair_in_context(
            context=context,
            amount_keywords=("买入金额", "买入金额(元)", "买入金额（元）"),
            prefer_unit="元",
            exclude_nodes=[anchor] + [src_fund_node, src_amount_node],
        )
        if tgt_fund_node:
            op.target_fund = tgt_fund_node.display_text
            consumed.append(tgt_fund_node)
        if tgt_amount_node:
            op.target_amount = tgt_amount_node.display_text
            op.target_amount_value = tgt_val
            if tgt_unit:
                op.target_amount_unit = tgt_unit
            consumed.append(tgt_amount_node)

        # 兜底：如果没找到"卖出份额(份)"标签，在上下文找含"份"的金额+最近基金
        if not op.source_fund or (op.source_amount_value <= 0 and not op.source_amount):
            self._fill_source_or_target_as_fallback(
                context=context, op=op, which="source",
                prefer_unit="份", exclude=consumed + [anchor],
            )

        if not op.target_fund or (op.target_amount_value <= 0 and not op.target_amount):
            self._fill_source_or_target_as_fallback(
                context=context, op=op, which="target",
                prefer_unit="元", exclude=consumed + [anchor],
            )

        # ---- 时间 + 大V ----
        time_node = self._find_timestamp(anchor, context, anchor_row_idx, rows)
        if time_node:
            op.timestamp = time_node.display_text
        kol_node = self._find_kol_name(anchor, all_nodes, anchor_row_idx, rows)
        if kol_node:
            op.kol_name = kol_node.display_text

        op.raw_context = self._build_debug_ctx(anchor, [], context)

        # 消费：窗口里的 buy/sell 锚点（已匹配节点位置）+ 上下文中的买入金额/卖出份额/买卖确认标签
        # 避免转换下的买/卖被再次独立识别
        TRANSFER_CONSUME_TEXTS = ("买入金额(元)", "买入金额", "卖出份额(份)", "卖出份额",
                                  "买入确认中", "卖出确认中", "买入金额（元）", "卖出份额（份）")
        for n in context:
            if n is anchor or id(n) in {id(x) for x in consumed}:
                continue
            if n.display_text and (n.display_text in TRANSFER_CONSUME_TEXTS
                                   or "买入金额" in n.display_text
                                   or "卖出份额" in n.display_text
                                   or "买入确认" in n.display_text
                                   or "卖出确认" in n.display_text):
                consumed.append(n)
        for an, ot, _kw in anchor_nodes_window:
            if ot in ("buy", "sell") and id(an) not in {id(x) for x in consumed}:
                consumed.append(an)
        # 至少有 source+target 基金名才算有效转换
        if not op.source_fund and not op.target_fund:
            return None, consumed
        return op, consumed

    # ============================================================
    #  CANCEL 记录构建
    # ============================================================
    def _build_cancel_operation(
        self,
        anchor: UiNode,
        op_text: str,
        all_nodes: List[UiNode],
        rows: List[List[UiNode]],
    ) -> Tuple[Optional[KolOperation], List[UiNode]]:
        consumed: List[UiNode] = []
        parser = UIXmlParser()
        rows = rows or parser.group_nodes_by_rows(all_nodes)
        anchor_row_idx = self._find_row_index(anchor, rows)

        # 扩大搜索范围（CANCEL 可能离买入/卖出/转换标签较远）
        rows_context = self._collect_context_rows(rows, anchor_row_idx, above=8, below=8)
        rows_context = [n for n in rows_context if n is not anchor]
        # 同时扩大 flat 范围：前后 100 个节点
        flat_context: List[UiNode] = []
        if all_nodes:
            anchor_i = next((i for i, n in enumerate(all_nodes) if n is anchor), None)
            if anchor_i is not None:
                s = max(0, anchor_i - 100)
                e = min(len(all_nodes), anchor_i + 100)
                flat_context = [n for n in all_nodes[s:e] if n is not anchor]
        # 取并集
        ctx_ids = {id(n): n for n in (rows_context + flat_context)}
        context = list(ctx_ids.values())
        same_row = ([n for n in rows[anchor_row_idx] if n is not anchor]
                    if anchor_row_idx is not None else [])

        ctx_texts = [n.display_text for n in (same_row + context)]
        big_text = "\n".join(ctx_texts) + "\n" + (anchor.display_text or "")

        op = KolOperation(
            action_type="CANCEL",
            operation="cancel",
            operation_text=op_text,
        )

        # ---- 识别撤销类型 ----
        buy_sigs = ("买入", "申购", "购买", "加仓", "建仓", "定投", "认购")
        sell_sigs = ("卖出", "赎回", "减仓", "清仓", "止盈", "止损")
        transfer_sigs = ("转换", "基金转换")
        has_buy = any(s in big_text for s in buy_sigs)
        has_sell = any(s in big_text for s in sell_sigs)
        has_transfer = any(s in big_text for s in transfer_sigs)

        if has_transfer:
            op.cancel_type = "TRANSFER_CANCEL"
        elif has_buy and has_sell:
            if "转换" in big_text or "转" in big_text:
                op.cancel_type = "TRANSFER_CANCEL"
            else:
                op.cancel_type = "UNKNOWN"
        elif has_buy:
            op.cancel_type = "BUY_CANCEL"
        elif has_sell:
            op.cancel_type = "SELL_CANCEL"
        else:
            op.cancel_type = "UNKNOWN"

        # ---- 按撤销类型填字段 ----
        if op.cancel_type == "TRANSFER_CANCEL":
            # 源：卖出份额(份)
            src_fn, src_an, src_v, src_u = self._find_pair_in_context(
                context=context,
                amount_keywords=("卖出份额", "卖出份额(份)", "卖出份额（份）"),
                prefer_unit="份",
                exclude_nodes=[anchor],
            )
            if src_fn:
                op.source_fund = src_fn.display_text
                consumed.append(src_fn)
            if src_an:
                op.source_amount = src_an.display_text
                op.source_amount_value = src_v
                if src_u:
                    op.source_amount_unit = src_u
                consumed.append(src_an)
            # 目标：买入金额(元)
            tgt_exclude = [anchor] + [src_fn, src_an]
            tgt_fn, tgt_an, tgt_v, tgt_u = self._find_pair_in_context(
                context=context,
                amount_keywords=("买入金额", "买入金额(元)", "买入金额（元）"),
                prefer_unit="元",
                exclude_nodes=tgt_exclude,
            )
            if tgt_fn:
                op.target_fund = tgt_fn.display_text
                consumed.append(tgt_fn)
            if tgt_an:
                op.target_amount = tgt_an.display_text
                op.target_amount_value = tgt_v
                if tgt_u:
                    op.target_amount_unit = tgt_u
                consumed.append(tgt_an)
            # 兜底：只有两个基金名时按出现顺序填充 source/target
            if not op.source_fund and not op.target_fund:
                fund_candidates = [(self._score_fund_node(n), n) for n in same_row + context]
                fund_candidates = [(s, n) for s, n in fund_candidates if s > 0]
                fund_candidates.sort(key=lambda x: x[0], reverse=True)
                if fund_candidates:
                    op.source_fund = fund_candidates[0][1].display_text
                    if len(fund_candidates) >= 2:
                        op.target_fund = fund_candidates[1][1].display_text
            # 消费：转换相关的上下文锚点标签
            for n in context:
                if n is anchor or id(n) in {id(x) for x in consumed}:
                    continue
                if not n.display_text:
                    continue
                if ("转换确认" in n.display_text
                        or "买入金额" in n.display_text
                        or "卖出份额" in n.display_text
                        or "买入确认" in n.display_text
                        or "卖出确认" in n.display_text):
                    consumed.append(n)

        elif op.cancel_type == "BUY_CANCEL":
            fn, an, v, u = self._find_pair_in_context(
                context=context,
                amount_keywords=("买入金额", "买入金额(元)", "买入金额（元）"),
                prefer_unit="元",
                exclude_nodes=[anchor],
            )
            if fn:
                op.fund_name = fn.display_text
                consumed.append(fn)
            if an:
                op.amount = an.display_text
                op.amount_value = v
                op.amount_unit = u or "元"
                consumed.append(an)
            # 兜底
            if not op.fund_name:
                fund_candidates = [(self._score_fund_node(n), n) for n in same_row + context]
                fund_candidates = [(s, n) for s, n in fund_candidates if s > 0]
                fund_candidates.sort(key=lambda x: x[0], reverse=True)
                if fund_candidates:
                    op.fund_name = fund_candidates[0][1].display_text
            if op.amount_value <= 0:
                an2, v2, u2 = self._find_amount(anchor, same_row, context)
                if an2:
                    op.amount = an2.display_text
                    op.amount_value = v2
                    op.amount_unit = u2 or "元"
            # 消费：买入相关的上下文锚点标签
            for n in context:
                if n is anchor or id(n) in {id(x) for x in consumed}:
                    continue
                if not n.display_text:
                    continue
                if ("买入金额" in n.display_text
                        or "买入确认" in n.display_text):
                    consumed.append(n)

        elif op.cancel_type == "SELL_CANCEL":
            fn, an, v, u = self._find_pair_in_context(
                context=context,
                amount_keywords=("卖出份额", "卖出份额(份)", "卖出份额（份）"),
                prefer_unit="份",
                exclude_nodes=[anchor],
            )
            if fn:
                op.fund_name = fn.display_text
                consumed.append(fn)
            if an:
                op.amount = an.display_text
                op.amount_value = v
                op.amount_unit = u or "份"
                consumed.append(an)
            # 兜底
            if not op.fund_name:
                fund_candidates = [(self._score_fund_node(n), n) for n in same_row + context]
                fund_candidates = [(s, n) for s, n in fund_candidates if s > 0]
                fund_candidates.sort(key=lambda x: x[0], reverse=True)
                if fund_candidates:
                    op.fund_name = fund_candidates[0][1].display_text
            if op.amount_value <= 0:
                an2, v2, u2 = self._find_amount(anchor, same_row, context)
                if an2:
                    op.amount = an2.display_text
                    op.amount_value = v2
                    op.amount_unit = u2 or "份"
            # 消费：卖出相关的上下文锚点标签
            for n in context:
                if n is anchor or id(n) in {id(x) for x in consumed}:
                    continue
                if not n.display_text:
                    continue
                if ("卖出份额" in n.display_text
                        or "卖出确认" in n.display_text):
                    consumed.append(n)

        else:  # UNKNOWN
            fund_candidates = [(self._score_fund_node(n), n) for n in same_row + context]
            fund_candidates = [(s, n) for s, n in fund_candidates if s > 0]
            fund_candidates.sort(key=lambda x: x[0], reverse=True)
            if fund_candidates:
                op.fund_name = fund_candidates[0][1].display_text
            an2, v2, u2 = self._find_amount(anchor, same_row, context)
            if an2:
                op.amount = an2.display_text
                op.amount_value = v2
                op.amount_unit = u2

        # ---- 时间 + 大V ----
        time_node = self._find_timestamp(anchor, context, anchor_row_idx, rows)
        if time_node:
            op.timestamp = time_node.display_text
        kol_node = self._find_kol_name(anchor, all_nodes, anchor_row_idx, rows)
        if kol_node:
            op.kol_name = kol_node.display_text

        op.raw_context = self._build_debug_ctx(anchor, same_row, context)

        # 如果既没有 fund_name 也没有 source/target 且没有 amount，可以认为识别失败
        if (not op.fund_name and not op.source_fund and not op.target_fund
                and op.amount_value <= 0
                and op.source_amount_value <= 0
                and op.target_amount_value <= 0):
            return None, consumed

        return op, consumed

    # ============================================================
    #  子函数：基金/金额通用
    # ============================================================
    # ============================================================
    #  子函数：严格金额解析（真实页面优化，过滤%/收益率/图片URL等）
    # ============================================================
    def _parse_amount(self, text: str) -> Tuple[float, str]:
        """
        严格金额解析：
          1) 黑名单检查（含%/收益率/img?fileid/URL等 → 直接0）
          2) 命中 AMOUNT_STRICT_PATTERNS 之一才返回数值
          3) 兜底：如果是标签行（例如"买入金额(元)"本身）返回0而不是报异常
        不再接受"看起来像数字"的模糊文本（例如近一年收益率31.77%）
        """
        if not text:
            return 0.0, ""
        # ---- 0) 先像时间戳的不要 ----
        if self._looks_like_timestamp(text):
            return 0.0, ""
        t = text.strip()
        # ---- 1) 黑名单：含任何非法字符/关键词直接返回 0 ----
        for bl in self._amount_blacklist_contains:
            if bl in t:
                return 0.0, ""
        # ---- 2) 逐一匹配严格格式正则（取最前面的一条匹配） ----
        for pat in self._amount_strict_patterns:
            m = pat.search(t)
            if not m:
                continue
            # groups: 通常最后两个 group 是 (数字字符串, 单位)
            # 考虑不同正则的 group 数量：先找最后两个 group，数字的那组作为数值
            groups = [g for g in m.groups() if g is not None]
            if not groups:
                continue
            # 找第一个纯数字（千分位含逗号）group
            num_str: Optional[str] = None
            unit = ""
            for g in groups:
                g_clean = g.replace(",", "").replace("，", "").strip()
                if num_str is None:
                    try:
                        float(g_clean)
                        num_str = g_clean
                        continue
                    except ValueError:
                        pass
                # 非数字 group 认为是单位
                if not unit:
                    unit = g.strip()
            if num_str is None:
                continue
            try:
                value = float(num_str) * self._unit_multiplier.get(unit, 1.0)
                if value <= 0:
                    continue
                return value, unit
            except ValueError:
                pass
        # ---- 3) 兜底：如果是"纯金额数字"但没单位（例如 50000.00 独立一行作为值对） ----
        # 仅当 上下文明确来自"买入金额(元)/卖出份额(份)"标签时才接受，这里先保守拒绝
        # 所以这里不做兜底，避免把长文中的 "31.77%" 等误判
        return 0.0, ""

    # ============================================================
    #  子函数：观点长文判定
    # ============================================================
    def _is_opinion_text(self, text: str) -> bool:
        """
        判断是否是观点/讨论长文（不参与基金/金额匹配）。
        规则：
          a) 超过 FUND_NAME_MAX_LEN 字 → 是
          b) 句子标点数量 >= OPINION_PUNCT_COUNT_THRESHOLD → 是
          c) 包含"我""我们""大家""市场""今天""目前"等典型观点开头词 + 标点数 >=2 → 是
        """
        if not text:
            return False
        if len(text) > self._fund_name_max_len:
            return True
        punct_count = len(self._opinion_punct_re.findall(text))
        if punct_count >= self._opinion_punct_threshold:
            return True
        opinion_opens = ("我", "我们", "大家", "市场", "今天", "目前",
                         "昨天", "今天大盘", "今天A股", "今日", "当前")
        if any(text.startswith(w) for w in opinion_opens) and punct_count >= 2:
            return True
        return False

    # ============================================================
    #  子函数：基金名严格打分（真实页面优化）
    # ============================================================
    def _score_fund_node(self, n: UiNode) -> int:
        text = n.display_text or ""
        if not text:
            return 0
        text_stripped = text.strip()

        # ---- 真实页面优化：UI常截断基金名末尾加"..."，先去掉再判断 ----
        # 例如"汇添富科技领先混..." → 去掉...后再判断关键词/后缀
        normalized = text_stripped
        while normalized.endswith((".", "…")):
            normalized = normalized[:-1].rstrip()

        # ---- 0) 观点长文：直接 0 分（不再打分） ----
        if self._is_opinion_text(text_stripped):
            return -1000

        # ---- 1) 黑名单命中（精确匹配或包含） → 直接负分 ----
        for inv in self._invalid_fund_names:
            if not inv:
                continue
            if inv == text_stripped or inv == normalized:
                return -500
            if len(inv) >= 2 and (inv in text_stripped or inv in normalized):
                # 部分命中的情况（如"查看详情..."）也大幅减分，避免误判
                return -300

        # ---- 2) 图片URL / 收益率特征：含 ? 或 & 且长度>30 → 判无效 ----
        if ("img?fileid" in text_stripped or (
                len(text_stripped) > 30 and ("?" in text_stripped or "&" in text_stripped))):
            return -400

        # ---- 3) 时间戳：必排除 ----
        if self._looks_like_timestamp(text_stripped):
            return -500

        # ---- 4) 必须命中：基金关键词 或 末尾份额后缀（严格准入） ----
        # 关键词检查用 normalized（去掉末尾省略号后更准）
        has_keyword = any(kw in text_stripped for kw in self._fund_name_keywords)
        if not has_keyword:
            has_keyword = any(kw in normalized for kw in self._fund_name_keywords)
        has_tail_suffix = any(text_stripped.endswith(suf) for suf in self._fund_tail_suffixes)
        if not has_tail_suffix and normalized != text_stripped:
            has_tail_suffix = any(normalized.endswith(suf) for suf in self._fund_tail_suffixes)
        # 真实基金名常以"C"或"A"结尾，要求前面至少有4个字符（避免"A"/"C"独立一行被误算）
        if has_tail_suffix and len(text_stripped) < 5:
            has_tail_suffix = False
        # "基金"二字出现也可作为弱关键词（真实文本如"XX基金"）
        has_fund_word = ("基金" in text_stripped) or ("基金" in normalized)

        if not (has_keyword or has_tail_suffix or has_fund_word):
            # 不满足准入要求 → 不是基金，直接判0
            return 0

        s = 0
        if has_fund_word:
            s += 15
        if has_keyword:
            # 命中的关键词越多越好
            s += 8 * sum(1 for kw in self._fund_name_keywords if kw in normalized or kw in text_stripped)
        if has_tail_suffix:
            s += 5
        # 长度加分：真实基金名通常 6~30 字（允许末尾省略号的 5~36 字）
        if 5 <= len(text_stripped) <= 40:
            s += 3
        elif len(text_stripped) > 55:
            s -= 5

        # 金额：当**不是基金**时才减分；基金名可以含"300""500"等指数数字
        if not (has_keyword or has_fund_word):
            if self._looks_like_amount(text_stripped):
                s -= 200
        # 像大V名的非基金文本减分
        if self._looks_like_kol_name(text_stripped) and not (has_keyword or has_fund_word):
            s -= 80
        # 金额/单位标签（再次兜底，防止漏网）
        if text_stripped in ("买入金额(元)", "卖出份额(份)", "买入金额（元）", "卖出份额（份）",
                             "买入金额", "卖出份额", "确认中"):
            s -= 300
        return s

    # ============================================================
    #  子函数：KOL 名严格过滤（真实页面优化）
    # ============================================================
    def _looks_like_kol_name(self, text: str) -> bool:
        """
        严格判定：像大V昵称 且 不在 INVALID_KOL_NAMES 黑名单里。
        识别不到就返回 False，不硬填默认值。
        新增：如果文本命中了「基金名称关键词」（ETF/混合/指数/...），直接视为非kol名（防基金名误识别）。
        新增真实页面优化：含句标点(，。？！；：、) 超过 1 个 或 含"，"等评论语气的文本直接视为评论/观点，非昵称。
        """
        if not text:
            return False
        t = text.strip()
        if len(t) < 2 or len(t) > 20:
            return False
        # ---- 黑名单（精确匹配） ----
        if t in self._invalid_kol_names:
            return False
        # ---- 黑名单：包含（例如"全部动态"、"最新评论"） ----
        for inv in self._invalid_kol_names:
            if inv and inv in t and len(inv) >= 2 and len(t) < 10:
                return False
        # ---- 新增：评论/观点句标点 > 0 个就不是昵称（防"已有10人求解读，我也催一下"这类） ----
        punct_count = len(self._opinion_punct_re.findall(t))
        if punct_count >= 1:
            return False
        # ---- 昵称里不能含"也、都、求、催、的、了、啊、吗、呢"这类语气/助词 ----
        opinion_particles = ("也", "都", "求", "催", "的", "了", "啊", "吗", "呢",
                             "吧", "呀", "哇", "哈哈", "嘿嘿", "解读", "一下",
                             "说明", "证明", "证明", "解读")
        if any(p in t for p in opinion_particles):
            return False
        # ---- 基金关键词命中 → 判定为基金名，不是 kol 名 ----
        if any(kw in t for kw in self._fund_name_keywords):
            return False
        if t.endswith(self._fund_tail_suffixes) and len(t) >= 6:
            return False
        if "基金" in t:
            return False
        # ---- 关键词排除（含有"基金/金额/份额/买入/卖出" → 不是昵称） ----
        exclude_keywords = (
            "买入", "卖出", "基金", "金额", "份额", "转换", "撤销",
            "时间", "操作", "分享", "评论", "点赞", "收藏", "转发",
            "展开", "查看", "回复", "确认中", "收益率", "净值",
        )
        if any(kw in t for kw in exclude_keywords):
            return False
        if t[0].isdigit() and not self._kol_name_pattern.match(t):
            return False
        if self._looks_like_amount(t) or self._looks_like_timestamp(t):
            return False
        return True

    def _find_kol_name(
        self,
        anchor: UiNode,
        all_nodes: List[UiNode],
        anchor_row_idx: Optional[int],
        rows: List[List[UiNode]],
    ) -> Optional[UiNode]:
        """
        找大V名称。严格要求：命中 _looks_like_kol_name，否则返回 None（识别不到就留空）。
        不再兜底硬填默认值。
        """
        if anchor_row_idx is None:
            anchor_pos = 0
            try:
                anchor_pos = all_nodes.index(anchor)
            except ValueError:
                pass
            for n in reversed(all_nodes[:anchor_pos]):
                if self._is_operation_node(n):
                    break
                if self._looks_like_kol_name(n.display_text):
                    return n
            return None
        # 按行向上搜索：最多找 20 行
        for r in range(anchor_row_idx - 1, max(-1, anchor_row_idx - 25), -1):
            row_nodes = rows[r]
            # 遇到上一个交易锚点 → 停止（避免串到别人的动态）
            for n in row_nodes:
                if self._is_operation_node(n) and n is not anchor:
                    return None
            candidates = []
            for n in row_nodes:
                txt = (n.display_text or "").strip()
                score = 0
                if self._looks_like_kol_name(txt):
                    score += 10
                # 同一行有"粉丝"/"关注"/"持仓"等标识 → 高度可信
                if any(kw in other.display_text
                       for other in row_nodes
                       for kw in ("粉丝", "关注", "自选", "持仓", "大V", "V认证")):
                    # 前提是 txt 本身看起来像 kol 名才加分，避免"粉丝 100w"被误识别
                    if score > 0:
                        score += 8
                if score > 0:
                    candidates.append((score, id(n), n))
            if candidates:
                candidates.sort(key=lambda x: (-x[0], x[1]))
                return candidates[0][2]
        return None

    # ============================================================
    #  子函数：基金/金额通用（重写：严格版）
    # ============================================================
    def _find_fund_name(
        self,
        anchor: UiNode,
        same_row: List[UiNode],
        context: List[UiNode],
    ) -> Optional[UiNode]:
        """
        严格基金名查找。
        策略：同行 → 上下文 ±3 行，只选择分数 > 0（即严格准入条件命中）的节点。
        找不到即返回 None。
        """
        scored = [(self._score_fund_node(n), id(n), n) for n in same_row]
        scored = [(s, i, n) for s, i, n in scored if s > 0]
        if scored:
            scored.sort(key=lambda x: (-x[0], x[1]))
            return scored[0][2]
        scored = [(self._score_fund_node(n), id(n), n) for n in context]
        scored = [(s, i, n) for s, i, n in scored if s > 0]
        if scored:
            scored.sort(key=lambda x: (-x[0], x[1]))
            return scored[0][2]
        return None

    def _find_amount(
        self,
        anchor: UiNode,
        same_row: List[UiNode],
        context: List[UiNode],
        prefer_unit: Optional[str] = None,
    ) -> Tuple[Optional[UiNode], float, str]:
        candidates = same_row + context
        matches = []
        for n in candidates:
            if n is anchor:
                continue
            value, unit = self._parse_amount(n.display_text)
            if value <= 0:
                continue
            score = 0
            if prefer_unit and prefer_unit == unit:
                score += 15
            elif prefer_unit and not unit:
                # 单位缺失但期望单位（通过锚点判断是买/卖），给一点基础分
                score += 1
            elif not prefer_unit:
                score += 3
            # 距离越近越优先
            distance = self._node_distance(anchor, n)
            matches.append((score, distance, id(n), n, value, unit))
        if not matches:
            return None, 0.0, ""
        matches.sort(key=lambda x: (-x[0], x[1], x[2]))
        _s, _d, _i, node, value, unit = matches[0]
        return node, value, unit

    def _is_operation_node(self, n: UiNode) -> bool:
        op_type, _ = self._match_operation(n.display_text)
        return bool(op_type)

    # ============================================================
    #  辅助
    # ============================================================
    @staticmethod
    def _find_row_index(node: UiNode, rows: List[List[UiNode]]) -> Optional[int]:
        for i, row in enumerate(rows):
            if node in row:
                return i
        return None

    @staticmethod
    def _collect_context_rows(
        rows: List[List[UiNode]],
        anchor_idx: Optional[int],
        above: int = 2,
        below: int = 2,
    ) -> List[UiNode]:
        if anchor_idx is None:
            result = []
            for r in rows[:10]:
                result.extend(r)
            return result
        start = max(0, anchor_idx - above)
        end = min(len(rows), anchor_idx + below + 1)
        result = []
        for r in range(start, end):
            result.extend(rows[r])
        return result

    @staticmethod
    def _node_distance(a: UiNode, b: UiNode) -> int:
        if not a.bounds or not b.bounds:
            return 10**9
        ax, ay = a.bounds.center
        bx, by = b.bounds.center
        return abs(ax - bx) + abs(ay - by)

    @staticmethod
    def _build_debug_ctx(anchor, same_row, context) -> dict:
        return {
            "anchor_text": anchor.display_text,
            "anchor_bounds": str(anchor.bounds) if anchor.bounds else None,
            "same_row_texts": [n.display_text for n in same_row],
            "context_texts": [n.display_text for n in context[:30]],
        }

    @staticmethod
    def _calc_confidence(op: KolOperation) -> float:
        score = 0.0
        if op.kol_name:
            score += 0.2
        if op.action_type:
            score += 0.15
        if op.action_type == "BUY" and op.fund_name and op.amount_value > 0:
            score += 0.35
        elif op.action_type == "SELL" and op.fund_name and op.amount_value > 0:
            score += 0.35
        elif op.action_type == "TRANSFER":
            if op.source_fund:
                score += 0.15
            if op.target_fund:
                score += 0.15
            if op.source_amount_value > 0:
                score += 0.05
            if op.target_amount_value > 0:
                score += 0.05
        elif op.action_type == "CANCEL":
            if op.cancel_type and op.cancel_type != "UNKNOWN":
                score += 0.25
            if op.fund_name:
                score += 0.1
        else:
            if op.fund_name:
                score += 0.2
            if op.amount_value > 0:
                score += 0.15
        if op.timestamp:
            score += 0.1
        return min(1.0, score)

    # ============================================================
    #  子函数：缺失方法补齐 + 严格版（真实页面优化）
    # ============================================================
    def _looks_like_timestamp(self, text: str) -> bool:
        """模糊判断文本是否像时间戳（用于金额/基金/kol名的排除）"""
        if not text:
            return False
        t = text.strip()
        for pat in self._time_patterns:
            if pat.fullmatch(t) or pat.search(t):
                # 只在"整条文本都是时间"时才判定为时间戳
                m = pat.search(t)
                if m and (m.start() <= 2 and len(t) - m.end() <= 2):
                    return True
        return False

    def _looks_like_amount(self, text: str) -> bool:
        """模糊判断文本是否像"金额行"（仅用于kol名/基金名的反向排除，不作为金额接受依据）"""
        if not text:
            return False
        t = text.strip()
        # 包含单位或数字
        has_unit = any(u in t for u in ("元", "块", "份", "万", "千"))
        has_number = bool(re.search(r"\d", t))
        if has_unit and has_number:
            return True
        # 纯数字（千分位）格式：例如 2,000.00
        if re.fullmatch(r"[\d,]+\.\d+", t):
            return True
        return False

    def _find_pair_in_context(
        self,
        context: List[UiNode],
        amount_keywords: Tuple[str, ...],      # 例如 ("买入金额(元)", "买入金额", ...)
        prefer_unit: Optional[str],            # "元" 或 "份"
        exclude_nodes: List[UiNode],
    ) -> Tuple[Optional[UiNode], Optional[UiNode], float, str]:
        """
        严格版「基金+金额」配对查找（强锚点优先 + 距离加权）：
        1) 先找 amount_keywords 命中的"标签节点"（如"买入金额(元)"）
        2) 以该标签为锚，在缩窄的窗口（标签前后 ±4 个节点）内寻找：
             a) 标签后 0~4 节点的"纯数值/明确金额"；
             b) 附近的基金名：以(基金分 - 距离*2)打分，**最近**的合法基金名优先。
        3) 没找到标签锚点时，仅兜底用"明确动作+金额"的严格 parse_amount。
        返回：(fund_node, amount_node, amount_value, amount_unit)
        """
        exclude_ids = {id(n) for n in exclude_nodes if n}
        ctx = [n for n in context if id(n) not in exclude_ids]
        if not ctx:
            return None, None, 0.0, ""

        # ---- Step 1: 找 amount_keywords 的标签节点（强锚点） ----
        label_node: Optional[UiNode] = None
        label_idx = -1
        for i, n in enumerate(ctx):
            t = (n.display_text or "").strip()
            if not t:
                continue
            # 命中任一个关键词（完整匹配优先）
            hit_exact = False
            hit_partial = False
            for kw in amount_keywords:
                if kw == t:
                    hit_exact = True
                    break
                if kw in t:
                    hit_partial = True
                    break
            if hit_exact or hit_partial:
                label_node = n
                label_idx = i
                if hit_exact:
                    break  # 完整匹配立即停

        # ---- Step 2: 找到标签 → 缩窄窗口 ±4 内找数值 + 距离加权的基金名 ----
        value_node: Optional[UiNode] = None
        value_raw = 0.0
        value_unit = prefer_unit or ""
        fund_node: Optional[UiNode] = None
        best_fund_score = -1_000_000   # 允许负值以便比较距离

        if label_node is not None:
            # 缩窄窗口：前后 ±4 节点（避免 source/target 串位）
            window_start = max(0, label_idx - 4)
            window_end = min(len(ctx), label_idx + 5)
            window_nodes = ctx[window_start:window_end]
            # --- a) 找数值：标签后 0~4 节点内优先（紧贴标签的数值） ---
            for j in range(label_idx, min(label_idx + 5, len(ctx))):
                cand = ctx[j]
                if cand is label_node:
                    continue
                txt = (cand.display_text or "").strip()
                if not txt or self._is_opinion_text(txt):
                    continue
                v, u = self._parse_amount(txt)
                if v > 0:
                    value_node = cand
                    value_raw = v
                    value_unit = u or (prefer_unit or "")
                    break
                # 兜底：纯数值（千分位）+ prefer_unit 标签 → 直接乘单位
                if prefer_unit and re.fullmatch(r"[\d,]+\.\d+|[\d,]+", txt):
                    try:
                        v = float(txt.replace(",", ""))
                        if v > 0:
                            value_node = cand
                            value_raw = v * self._unit_multiplier.get(prefer_unit, 1.0)
                            value_unit = prefer_unit
                            break
                    except ValueError:
                        pass
            # --- b) 找基金名：分数 - 距离惩罚 *3；距离优先 + 反向标签阻断 ---
            # 1) 先判断这是 source 侧还是 target 侧查找，确定"阻断关键词"
            look_for_source = (prefer_unit == "份")
            if look_for_source:
                # 找 source（卖出份额）：遇到「买入金额」类关键词就停止向后搜索
                block_keywords = ("买入金额", "买入金额(元)", "买入金额（元）", "买入确认中", "转换至", "转换到")
            else:
                # 找 target（买入金额）：遇到「卖出份额」类关键词就停止向前搜索
                block_keywords = ("卖出份额", "卖出份额(份)", "卖出份额（份）", "卖出确认中")
            # 2) 找到 value_node / label_node 在 ctx 中的索引
            value_ctx_idx = None
            if value_node is not None:
                value_ctx_idx = next((i for i, n in enumerate(ctx) if n is value_node), None)
            # 3) 候选基金的索引允许范围
            if value_ctx_idx is not None:
                # 核心区域：label_idx - 2 到 value_ctx_idx + 1（紧贴标签到数值的±1）
                fund_start = max(0, label_idx - 2)
                fund_end = min(len(ctx), value_ctx_idx + 2)
            else:
                fund_start = max(0, label_idx - 2)
                fund_end = min(len(ctx), label_idx + 4)
            # 4) 在允许范围内，按反向阻断关键词进一步收缩
            blocked_at_front = False
            blocked_at_back = False
            for bi in range(label_idx, min(label_idx + 8, len(ctx))):
                bt = (ctx[bi].display_text or "").strip()
                if any(bk == bt or bk in bt for bk in block_keywords):
                    # 碰到反向关键词：source 侧把 fund_end 收缩到反向关键词之前
                    if look_for_source:
                        fund_end = min(fund_end, bi)
                        blocked_at_back = True
                        break
            for bi in range(label_idx, max(-1, label_idx - 8), -1):
                bt = (ctx[bi].display_text or "").strip()
                if any(bk == bt or bk in bt for bk in block_keywords):
                    # target 侧：把 fund_start 扩张限制到反向关键词之后
                    if not look_for_source:
                        fund_start = max(fund_start, bi + 1)
                        blocked_at_front = True
                        break
            # 5) 在 fund_start:fund_end 区间内挑得分最高（距离加权）的合法基金
            ref_ctx_idx = value_ctx_idx if value_ctx_idx is not None else label_idx
            for cand_idx in range(fund_start, fund_end):
                cand = ctx[cand_idx]
                if cand is label_node or cand is value_node:
                    continue
                s_base = self._score_fund_node(cand)
                if s_base <= 0:
                    continue  # 严格准入：不过准入直接跳过
                distance = abs(cand_idx - ref_ctx_idx)
                s_weighted = s_base - distance * 3  # 越近越优，惩罚加大
                if s_weighted > best_fund_score:
                    best_fund_score = s_weighted
                    fund_node = cand
            # 修正 best_fund_score 供最后判断（>0才合法）
            if fund_node is not None:
                base_s = self._score_fund_node(fund_node)
                best_fund_score = base_s

        # ---- Step 3: 未找到标签锚点 → 兜底用"明确动作+金额"（严格 parse_amount） ----
        if label_node is None:
            amount_candidates: List[Tuple[int, UiNode, float, str]] = []
            for i, n in enumerate(ctx):
                txt = (n.display_text or "").strip()
                if not txt or self._is_opinion_text(txt):
                    continue
                v, u = self._parse_amount(txt)
                if v > 0:
                    # 仅接受 单位与 prefer_unit 方向匹配的（source→份/万份；target→元/块/万/千）
                    if prefer_unit == "份" and u not in ("份", "万份", ""):
                        continue
                    if prefer_unit == "元" and u not in ("元", "块", "万", "千", ""):
                        continue
                    amount_candidates.append((i, n, v, u))
            if amount_candidates:
                ai, an, av, au = amount_candidates[0]
                value_node, value_raw, value_unit = an, av, au or (prefer_unit or "")
                # 窗口缩窄到 ±4 节点，同样距离加权
                ws = max(0, ai - 4)
                we = min(len(ctx), ai + 5)
                for cand in ctx[ws:we]:
                    if cand is an:
                        continue
                    s_base = self._score_fund_node(cand)
                    if s_base <= 0:
                        continue
                    cand_ctx_idx = next((i for i, n in enumerate(ctx) if n is cand), ai)
                    distance = abs(cand_ctx_idx - ai)
                    s_weighted = s_base - distance * 2
                    if s_weighted > best_fund_score:
                        best_fund_score = s_base
                        fund_node = cand

        # 最终：基金必须通过严格准入（base score > 0）
        final_fund_score = self._score_fund_node(fund_node) if fund_node is not None else -1
        if fund_node is not None and final_fund_score <= 0:
            fund_node = None

        return fund_node, value_node, value_raw, value_unit

    def _fill_source_or_target_as_fallback(
        self,
        context: List[UiNode],
        op: KolOperation,
        which: str,                      # "source" 或 "target"
        prefer_unit: str,                # source=份 / target=元
        exclude: List[UiNode],
    ) -> None:
        """
        TRANSFER 场景兜底：
        当「转换确认中」附近没有明确的"卖出份额(份)"/"买入金额(元)"标签时，
        尝试在剩余上下文里找"带明确单位的金额"+邻近严格准入的基金名，填入 source/target 字段。
        要求：必须满足严格金额格式（parse_amount > 0），否则不动。
        """
        amount_keywords_map = {
            "source": ("卖出份额", "卖出份额(份)", "卖出份额（份）", "卖出"),
            "target": ("买入金额", "买入金额(元)", "买入金额（元）", "买入"),
        }
        kws = amount_keywords_map.get(which, ())
        fn, an, v, u = self._find_pair_in_context(
            context=context,
            amount_keywords=kws,
            prefer_unit=prefer_unit,
            exclude_nodes=exclude,
        )

        if which == "source":
            if not op.source_fund and fn:
                op.source_fund = fn.display_text
            if (op.source_amount_value <= 0 and not op.source_amount) and an:
                op.source_amount = an.display_text
                op.source_amount_value = v
                op.source_amount_unit = u or prefer_unit
        else:
            if not op.target_fund and fn:
                op.target_fund = fn.display_text
            if (op.target_amount_value <= 0 and not op.target_amount) and an:
                op.target_amount = an.display_text
                op.target_amount_value = v
                op.target_amount_unit = u or prefer_unit

    # ============================================================
    #  子函数：_find_timestamp 缺失补齐
    # ============================================================
    def _find_timestamp(
        self,
        anchor: UiNode,
        context: List[UiNode],
        anchor_row_idx: Optional[int],
        rows: List[List[UiNode]],
    ) -> Optional[UiNode]:
        """
        在锚点附近（同行/上下文/后续行）找最近的时间戳节点。
        命中标准：符合 TIME_PATTERNS 之一。
        """
        # 先查上下文窗口（含同行）
        for n in context:
            if n is anchor:
                continue
            t = (n.display_text or "").strip()
            if not t:
                continue
            for pat in self._time_patterns:
                if pat.search(t):
                    return n
        # 再按行往后查最多 10 行（时间常出现在操作卡片下方）
        if rows and anchor_row_idx is not None:
            for r in range(anchor_row_idx, min(len(rows), anchor_row_idx + 12)):
                for n in rows[r]:
                    if n is anchor:
                        continue
                    t = (n.display_text or "").strip()
                    if not t:
                        continue
                    for pat in self._time_patterns:
                        if pat.search(t):
                            return n
        return None
