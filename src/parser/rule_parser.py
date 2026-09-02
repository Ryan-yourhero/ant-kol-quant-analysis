"""
Python 规则解析器 — 从 screen_dump MD 文本中提取大V操作记录

MD 格式（screen_dump 屏幕文字镜像）：
  # 页面N
  KOL名称
  近X年/月收益率Y%
  HH:MM
  [观点长文]
  买入确认中 / 卖出确认中 / 转换确认中 / 撤销 / 定投确认中
  基金名称...
  买入金额(元) / 卖出份额(份)
  金额/份额数字
  查看详情
  ...
  转发 / 评论 / 点赞 / 求解读
  数字

策略：
  1. 按 # 页面N 分页
  2. 每页按 KOL 名称 → 收益率 → 时间 模式识别帖子边界
  3. 在帖子内识别操作锚点并提取基金名/金额/份额
  4. 识别互动统计
"""

from __future__ import annotations

import os
import re
import datetime
from typing import Any, Dict, List, Optional, Tuple

# ============================================================
#  常量 & 正则
# ============================================================

# 大V名称（2~12字中英文数字下划线、无标点）
_KOL_RE = re.compile(r"^[\u4e00-\u9fa5A-Za-z0-9_]{2,12}$")
_KOL_FORBIDDEN = re.compile(r"[，。！？、；：「」『』（）《》【】\s\.,;:!?\"'()<>%°℃]")
_KOL_BLACKLIST = {
    "首页", "理财", "资产", "消息", "我的", "返回", "搜索", "设置", "更多",
    "关闭", "取消", "确定", "关注", "发现", "讨论区", "热议话题", "学理财",
    "资讯", "同路人", "全部", "今日操作", "最新", "热门", "推荐",
    "我的关注", "粉丝", "关注者", "原创", "转发", "分享", "收藏", "评论",
    "点赞", "求解读", "回复", "催一下", "查看详情", "立即查看",
    "真实财有趣", "original", "加载中", "暂无数据", "暂无更多内容",
    "展开", "理财盘友圈", "记一下",
}

# 收益率行（含 %）
_YIELD_RE = re.compile(r"^(近[一二三半]?[年月周日天])?.*?([+-]?\d+\.?\d*%)$")

# 时间 HH:MM
_TIME_RE = re.compile(r"^(\d{1,2}:\d{2})$")

# 页面标题
_PAGE_HEADER_RE = re.compile(r"^#\s*页面\d+")

# 操作锚点（具体文本）
OP_ANCHOR_BUY = ("买入确认中",)
OP_ANCHOR_SELL = ("卖出确认中",)
OP_ANCHOR_TRANSFER = ("转换确认中",)
OP_ANCHOR_CANCEL = ("撤销",)
OP_ANCHOR_DINGTOU = ("定投确认中",)

# 金额/份额标签
_AMOUNT_LABEL_BUY = re.compile(r"^买入金额[（(]元[）)]$")
_AMOUNT_LABEL_SELL = re.compile(r"^卖出份额[（(]份[）)]$")

# 金额/份额值
_AMOUNT_VALUE_RE = re.compile(r"^[\d,]+(?:\.\d{1,2})?$")

# 纯数字（互动统计）
_PURE_NUMBER_RE = re.compile(r"^\d+$")
_PURE_NUMBER_K_RE = re.compile(r"^\d+\.?\d*[万wW]$")

# 页面尾标记
_PAGE_END_MARKER = "暂无更多内容"


def _is_kol(s: str) -> bool:
    s = s.strip()
    if not _KOL_RE.match(s):
        return False
    if _KOL_FORBIDDEN.search(s):
        return False
    if s in _KOL_BLACKLIST:
        return False
    if re.fullmatch(r"\d+", s):
        return False
    if any(k in s.lower() for k in ("100w", "img?", "fileid", "original")):
        return False
    return True


def _is_yield_line(s: str) -> bool:
    return bool(_YIELD_RE.search(s)) and len(s) <= 40


def _is_time_line(s: str) -> bool:
    return bool(_TIME_RE.match(s.strip()))


def _is_amount_value(s: str) -> bool:
    return bool(_AMOUNT_VALUE_RE.match(s.strip().replace(",", "")))


def _is_stat_number(s: str) -> bool:
    return bool(
        _PURE_NUMBER_RE.match(s.strip().replace(",", ""))
        or _PURE_NUMBER_K_RE.match(s.strip().replace(",", ""))
    )


def _get_op_type(anchor: str) -> str:
    if anchor in OP_ANCHOR_BUY or anchor in OP_ANCHOR_DINGTOU:
        return "买入"
    if anchor in OP_ANCHOR_SELL:
        return "卖出"
    if anchor in OP_ANCHOR_TRANSFER:
        return "转换"
    if anchor in OP_ANCHOR_CANCEL:
        return "撤销"
    return "买入"


def _get_op_anchor_type(line: str) -> Optional[str]:
    """返回操作锚点类型：'买入'/'卖出'/'转换'/'撤销'/'定投'"""
    if any(line.startswith(k) for k in OP_ANCHOR_BUY):
        return "买入确认中"
    if any(line.startswith(k) for k in OP_ANCHOR_SELL):
        return "卖出确认中"
    if any(line.startswith(k) for k in OP_ANCHOR_TRANSFER):
        return "转换确认中"
    if any(line.startswith(k) for k in OP_ANCHOR_CANCEL):
        return "撤销"
    if any(line.startswith(k) for k in OP_ANCHOR_DINGTOU):
        return "定投确认中"
    return None


# ============================================================
#  MD → 页面
# ============================================================

def _split_pages(md_text: str) -> List[Dict[str, Any]]:
    raw_lines = md_text.splitlines()
    pages: List[Dict[str, Any]] = []
    current_lines: List[str] = []
    current_page = 0

    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("\u200B"):
            line = line[1:]
        if not line:
            continue
        if _PAGE_HEADER_RE.match(line):
            if current_page > 0 or current_lines:
                pages.append({"page": current_page or 1, "lines": current_lines})
            m = re.search(r"页面(\d+)", line)
            current_page = int(m.group(1)) if m else len(pages) + 1
            current_lines = []
            continue
        current_lines.append(line)

    if current_lines:
        pages.append({"page": current_page or len(pages) + 1, "lines": current_lines})

    return pages


# ============================================================
#  单页 → 帖子片段（按 KOL 边界切分）
# ============================================================

def _find_post_boundaries(lines: List[str]) -> List[Tuple[int, int, Optional[str]]]:
    """
    找到每个帖子的起止位置。
    判断标准：发现 KOL名称 → 收益率 → 时间 的连续模式。

    Returns: [(start_idx, end_idx, kol_name), ...]
    """
    n = len(lines)
    boundaries: List[Tuple[int, int, Optional[str]]] = []

    i = 0
    while i < n:
        # 查找 KOL_NAME → YIELD → TIME 模式
        if (
            _is_kol(lines[i])
            and i + 2 < n
            and _is_yield_line(lines[i + 1])
            and _is_time_line(lines[i + 2])
        ):
            kol_name = lines[i]
            start = i

            # 找下一个 KOL 作为结束边界
            end = n
            for j in range(i + 3, n - 2):
                if (
                    _is_kol(lines[j])
                    and j + 2 < n
                    and _is_yield_line(lines[j + 1])
                    and _is_time_line(lines[j + 2])
                ):
                    end = j
                    break

            boundaries.append((start, end, kol_name))
            i = end  # 跳到下一个 KOL
        else:
            i += 1

    return boundaries


def _parse_stats(lines_slice: List[str]) -> Dict[str, Optional[str]]:
    """提取转发/评论/点赞/求解读 统计"""
    stats: Dict[str, Optional[str]] = {
        "repost": None, "comment": None, "like": None, "seek": None,
    }
    for idx, line in enumerate(lines_slice):
        if line == "转发" and idx + 1 < len(lines_slice):
            val = lines_slice[idx + 1].strip().replace(",", "")
            if _is_stat_number(val):
                stats["repost"] = val
        elif line == "评论" and idx + 1 < len(lines_slice):
            val = lines_slice[idx + 1].strip().replace(",", "")
            if _is_stat_number(val):
                stats["comment"] = val
        elif line == "点赞" and idx + 1 < len(lines_slice):
            val = lines_slice[idx + 1].strip().replace(",", "")
            if _is_stat_number(val):
                stats["like"] = val
        elif "求解读" in line:
            m = re.search(r"(\d+)人求解读", line)
            if m:
                stats["seek"] = m.group(1)

    return stats


def _find_fund_name_before(
    lines: List[str], anchor_idx: int, max_lookback: int = 15
) -> Optional[str]:
    """
    某些情况下基金名在锚点之前（如卖出/转换的源基金在确认中之前一行）
    找锚点之前的候选基金名。
    """
    for j in range(anchor_idx - 1, max(anchor_idx - max_lookback, -1), -1):
        candidate = lines[j]
        if not candidate or len(candidate) < 3 or len(candidate) > 50:
            continue
        # 排除明确非基金名的行
        if (
            _is_yield_line(candidate)
            or _is_time_line(candidate)
            or _is_kol(candidate)
            or _get_op_anchor_type(candidate)
            or _AMOUNT_LABEL_BUY.match(candidate)
            or _AMOUNT_LABEL_SELL.match(candidate)
            or _is_amount_value(candidate)
            or candidate in ("查看详情", "催一下", "转发", "评论", "点赞")
            or "展开今日" in candidate
            or "求解读" in candidate
            or "暂无更多内容" in candidate
        ):
            continue
        return candidate
    return None


def _is_garbage_record(rec: Dict[str, Any]) -> bool:
    """判断是否为垃圾记录（匿名页面噪声）"""
    fund = rec.get("fund_name") or ""
    buy = rec.get("buy_amount")
    sell = rec.get("sell_shares")

    # 基金名为空
    if not fund.strip():
        pass

    # 基金名是时间格式（如 10:12, 11:51）
    if re.match(r"^\d{1,2}:\d{2}$", fund.strip()):
        return True

    # 基金名是无意义文本
    if "暂无更多内容" in fund:
        return True

    # 完全没有基金名且没有金额 → 空操作
    if not fund.strip() and not buy and not sell:
        return True

    return False


def _parse_post(lines: List[str], start: int, end: int, kol_name: str) -> List[Dict[str, Any]]:
    """
    解析一个帖子，返回操作记录列表。

    每个 record dict:
      {
        "kol_name": str,
        "yield_rate": str|None,
        "yield_period": str|None,
        "publish_time": str|None,
        "opinion_text": str|None,
        "operation_type": str,
        "operation_status": str|None,
        "fund_name": str|None,
        "buy_amount": str|None,
        "sell_shares": str|None,
        "convert_from_fund": str|None,
        "convert_to_fund": str|None,
        "repost_count": str|None,
        "comment_count": str|None,
        "like_count": str|None,
        "seek_interpret_count": str|None,
        "today_operation_count": str|None,
      }
    """
    records: List[Dict[str, Any]] = []
    post_lines = lines[start:end]

    # 提取收益率和周期
    yield_rate = None
    yield_period = None
    if len(post_lines) > 2 and _is_yield_line(post_lines[1]):
        rt = post_lines[1]
        yield_rate = rt
        m = re.match(r"^(近[一二三半]?[年月周日天])", rt)
        if m:
            yield_period = m.group(1)
        else:
            # 整个收益率字符串中提取 % 前的部分
            pct_m = re.search(r"([+-]?\d+\.?\d*%)", rt)
            if pct_m:
                yield_rate = pct_m.group(1)

    # 提取时间
    publish_time = None
    if len(post_lines) > 2 and _is_time_line(post_lines[2]):
        publish_time = post_lines[2]

    # 提取观点文本（在时间和第一个操作锚点之间的长文本）
    opinion_text = None
    first_op_idx = None
    for idx, line in enumerate(post_lines):
        if _get_op_anchor_type(line):
            first_op_idx = idx
            break

    if first_op_idx is not None and first_op_idx > 3:
        for idx in range(3, first_op_idx):
            if len(post_lines[idx]) >= 12 and not _is_yield_line(post_lines[idx]):
                opinion_text = post_lines[idx]
                break

    # 提取"展开今日全部N条操作" → today_operation_count
    today_op_count = None
    for line in post_lines:
        m = re.search(r"展开今日全部(\d+)条操作", line)
        if m:
            today_op_count = m.group(1)
            break

    # 提取互动统计
    stats = _parse_stats(post_lines)

    # ---- 提取操作记录 ----
    # 找所有操作锚点
    op_anchors: List[Tuple[int, str, str]] = []  # (idx, anchor_text, op_type)
    for idx, line in enumerate(post_lines):
        anchor = _get_op_anchor_type(line)
        if anchor:
            op_anchors.append((idx, anchor, _get_op_type(anchor)))

    if not op_anchors:
        return records

    # 逐个解析操作
    for ai, (anchor_idx, anchor_text, op_type) in enumerate(op_anchors):
        record = {
            "kol_name": kol_name,
            "yield_rate": yield_rate,
            "yield_period": yield_period,
            "publish_time": publish_time,
            "opinion_text": opinion_text,
            "operation_type": op_type,
            "operation_status": (
                "确认中" if op_type in ("买入", "卖出", "转换")
                else None
            ),
            "fund_name": None,
            "buy_amount": None,
            "sell_shares": None,
            "convert_from_fund": None,
            "convert_to_fund": None,
            "repost_count": stats["repost"],
            "comment_count": stats["comment"],
            "like_count": stats["like"],
            "seek_interpret_count": stats["seek"],
            "today_operation_count": today_op_count,
        }

        # 确定这个操作块的结束位置（下一个操作锚点或帖子结尾）
        next_anchor_idx = op_anchors[ai + 1][0] if ai + 1 < len(op_anchors) else len(post_lines)

        # 扫描锚点之后的行，提取基金名和金额
        block = post_lines[anchor_idx:next_anchor_idx]

        # 1. 基金名：锚点之后第一个非标签非数字行（且在金额标签之前）
        fund_name = None
        for j in range(1, len(block)):
            candidate = block[j]
            # 跳过标签、数字、已知非基金名
            if (
                _AMOUNT_LABEL_BUY.match(candidate)
                or _AMOUNT_LABEL_SELL.match(candidate)
                or _is_amount_value(candidate)
                or candidate == "查看详情"
                or candidate == "催一下"
                or _get_op_anchor_type(candidate)
                or "求解读" in candidate
            ):
                continue
            if 3 <= len(candidate) <= 50:
                fund_name = candidate
                break

        # 如果锚点后没找到基金名，在锚点前找（有些布局基金名在确认中之前）
        if fund_name is None:
            fund_name = _find_fund_name_before(post_lines, start + anchor_idx)

        record["fund_name"] = fund_name

        # 2. 金额/份额
        buy_amount = None
        sell_shares = None
        for j, line in enumerate(block):
            if _AMOUNT_LABEL_BUY.match(line) and j + 1 < len(block):
                val = block[j + 1].strip().replace(",", "")
                if _is_amount_value(val):
                    buy_amount = val
            if _AMOUNT_LABEL_SELL.match(line) and j + 1 < len(block):
                val = block[j + 1].strip().replace(",", "")
                if _is_amount_value(val):
                    sell_shares = val

        record["buy_amount"] = buy_amount
        record["sell_shares"] = sell_shares

        # 3. 转换特殊处理
        if op_type == "转换":
            if fund_name:
                record["convert_from_fund"] = fund_name
                record["fund_name"] = None
            if sell_shares:
                record["sell_shares"] = sell_shares
            if buy_amount:
                record["buy_amount"] = buy_amount
            # 转换的目标基金在卖出份额/查看详情之后、买入金额之前
            for j in range(1, len(block)):
                if block[j] == "查看详情":
                    # 查看详情后面可能有目标基金名
                    for k in range(j + 1, len(block)):
                        candidate = block[k]
                        if (
                            _AMOUNT_LABEL_BUY.match(candidate)
                            or _AMOUNT_LABEL_SELL.match(candidate)
                            or _get_op_anchor_type(candidate)
                            or _is_amount_value(candidate)
                            or candidate == "查看详情"
                        ):
                            break
                        if 3 <= len(candidate) <= 50:
                            record["convert_to_fund"] = candidate
                            break
                    break

        records.append(record)

    return records


# ============================================================
#  主入口
# ============================================================

def extract_segments_from_md(md_text: str, *, md_path: str = "") -> List[Dict[str, Any]]:
    """
    从 screen_dump MD 文本中提取所有大V操作记录。

    Args:
        md_text: MD 文本内容
        md_path: MD 文件路径（用于提取采集时间）

    Returns:
        List[dict]: 扁平化的操作记录列表，每个 dict 包含所有字段。
    """
    pages = _split_pages(md_text)
    all_records: List[Dict[str, Any]] = []

    # 提取采集时间：优先从文件名提取日期
    collect_time = datetime.datetime.now().isoformat(timespec="seconds")
    if md_path:
        date_m = re.search(r"(\d{8})[_-](\d{6})", os.path.basename(md_path))
        if date_m:
            date_str = date_m.group(1)
            time_str = date_m.group(2)
            collect_time = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}T{time_str[:2]}:{time_str[2:4]}:{time_str[4:6]}"

    # 跨页 KOL 上下文：当前页面没有 KOL 头部时，继承上一页最后一个已知 KOL
    last_known_kol: Optional[str] = None

    for page in pages:
        page_no = page["page"]
        lines = page["lines"]

        boundaries = _find_post_boundaries(lines)

        if boundaries:
            # 有 KOL 头部 → 按边界解析
            for start, end, kol_name in boundaries:
                last_known_kol = kol_name
                post_records = _parse_post(lines, start, end, kol_name)
                for rec in post_records:
                    rec["collect_time"] = collect_time
                    rec["page"] = page_no
                all_records.extend(post_records)
        else:
            # 无 KOL 头部（被滑动上去了）→ 整页扫描操作锚点，标记为「未知KOL」
            # 后续可由 AI 通过"展开今日全部N条操作"等信息补全归属
            post_records = _parse_post(lines, 0, len(lines), "未知KOL")
            for rec in post_records:
                rec["collect_time"] = collect_time
                rec["page"] = page_no
            # 过滤垃圾：基金名为时间、无意义文本、无基金名的记录
            post_records = [
                r for r in post_records
                if not _is_garbage_record(r)
            ]
            if post_records:
                # 过滤：没有买入金额/卖出份额的空记录（噪声）
                post_records = [
                    r for r in post_records
                    if r.get("buy_amount") or r.get("sell_shares")
                ]
            all_records.extend(post_records)

    return all_records
