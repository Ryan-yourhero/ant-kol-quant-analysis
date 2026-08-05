"""
raw_pages 原始文本 → Markdown 导出器（仅展示，不做金融解析）
================================================================

目标：
  - 将 `output/raw_pages_YYYYMMDD_HHMMSS.json` 同步转换为同名 `.md`
  - 便于人工阅读 & 后续喂给 LLM 做分析
  - **不判断 BUY/SELL/TRANSFER**，**不做基金/金额结构化**

格式（严格按用户要求）：
  1. 标题：# 蚂蚁财富大V操作采集
  2. 元信息：采集时间 / 页面数量 / 文本数量
  3. 主体：按页面顺序输出；每页尝试定位一个「大V名称」做二级标题；
     在该大V下展示 - 收益率 - 时间 - 观点文本 - 操作相关文本

弱识别规则（仅展示用途，不回写 JSON / 不影响数据源）：
  - 大V名称候选：2~8 个汉字/字母数字混排（排除系统词、排除纯数字、排除带 %/元/份 的金额/收益率词）
  - 收益率：包含 % 号的数字串（如 +1.23% / -0.45% / 近1年 +15.3%）
  - 时间：HH:MM / MM-DD / YYYY-MM-DD / 今天 HH:MM 等
  - 观点文本：长度 >= 12 且不落入「操作相关」「收益率」「时间」的文本
  - 操作相关文本：命中「买入/卖出/转换/撤销/确认中/金额/份额/基金/定投/赎回/加仓/减仓/清仓」等弱关键词

注：若一页找不到可靠大V名，则退化为 `## 页面 N（未识别到用户名）`。
"""

from __future__ import annotations

import os
import re
import sys
import json
import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---- 兼容直接脚本运行 / 作为包导入 ----
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


# ============================================================
#  弱识别：仅用于 Markdown 展示
# ============================================================

# 弱操作词（命中即归到「操作相关文本」）
OP_HINT_WORDS = (
    "买入", "卖出", "转换", "撤销",
    "确认中", "确认", "金额", "份额", "基金",
    "定投", "赎回", "申购", "认购",
    "加仓", "减仓", "止盈", "止损", "清仓", "建仓",
    "元", "份",
)

# 强操作词：包含这些词才算"操作相关"（避免"再加仓一点"这种自然观点被错误归类）
OP_HINT_WORDS_STRONG = (
    # 具体动作 + 确认/金额/份额/基金/价格 组合
    "买入确认中", "买入金额", "买入",
    "卖出确认中", "卖出份额", "卖出",
    "转换确认中", "转换",
    "撤销", "取消",
    "金额", "份额", "基金",
    "元)", "份)", "(元)", "(份)",
    "定投", "赎回", "申购", "认购",
    "加仓", "减仓", "止盈", "止损", "清仓", "建仓",
)

# 短操作词：长度 <=4 且完全相等时也算
OP_HINT_WORDS_SHORT = (
    "买入", "卖出", "转换", "撤销",
    "定投", "赎回", "申购", "认购",
    "加仓", "减仓", "止盈", "止损", "清仓", "建仓",
    "金额", "份额", "基金",
)

# 系统词黑名单（不会被当成大V名）
BLACKLIST_KOL_TOKENS = {
    # App tab / 通用控件
    "首页", "理财", "资产", "消息", "我的", "返回", "搜索", "设置", "更多",
    "关闭", "取消", "确定", "同意", "允许", "拒绝", "知道了", "我知道了",
    "登录", "注册", "下载", "安装", "加载中", "暂无数据", "暂无内容",
    # 常见栏目 / 状态
    "观点", "盘友圈", "讨论", "评论", "点赞", "转发", "分享", "收藏", "关注",
    "粉丝", "关注者", "全部", "最新", "热门",
    # 时间/日期常见
    "今天", "昨天", "前天", "刚刚",
    # 按钮
    "去查看", "立即购买", "立即前往", "立即查看", "查看详情",
    # 通用
    "蚂蚁财富", "蚂蚁", "支付宝",
    # 系统状态常见（避免"当前温度和天气"这种长句被误命中为用户名）
    "当前温度和天气", "当前温度", "天气", "温度",
    "录音机", "录音", "闹钟", "近期的闹钟",
    "通知", "条通知", "电池", "充电",
}

# 时间 HH:MM / HH:MM:SS / MM-DD / YYYY-MM-DD / 今天 HH:MM / 昨天 HH:MM / 刚刚 等
_TIME_RE = re.compile(
    r"^(?:"
    r"\d{1,2}:\d{2}(?::\d{2})?"
    r"|\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?"
    r"|\d{1,2}[-/月]\d{1,2}日?"
    r"|(?:今天|昨天|前天)\s*\d{0,2}:?\d{0,2}"
    r"|刚刚|\d+\s*(?:秒|分|小时|天)前"
    r")$"
)

# 收益率（包含 %，数字/正负号，允许前后中文修饰）
_YIELD_RE = re.compile(r"%")

# 大V名候选：2~12 字，中文/字母数字/下划线；
# 但整条里不能包含逗号/空格/度数°等非名字符号，避免把"当前温度, 34°, 晴"这类句子误判。
_KOL_CANDIDATE_RE = re.compile(r"^[\u4e00-\u9fa5A-Za-z0-9_]{2,12}$")
# 明确拒绝包含：标点符号（中英文逗号/句号/冒号/括号）、空白、°、%、元/份 等
_KOL_FORBIDDEN_CHAR_RE = re.compile(r"[，。！？、；：“”‘’（）《》\s\.,;:!?\"'()<>%°℃]")


def _is_time_text(s: str) -> bool:
    return bool(_TIME_RE.match(s.strip()))


def _is_yield_text(s: str) -> bool:
    return bool(_YIELD_RE.search(s)) and len(s) <= 40


def _is_operation_text(s: str) -> bool:
    s = s.strip()

    # (1) 强短语：包含"买入/卖出/转换/撤销 + 确认/金额/份额"，或明确的金额/份额后缀
    strong_phrases = (
        "买入确认中", "卖出确认中", "转换确认中",
        "买入金额", "卖出份额",
        "金额(元)", "金额（元）", "份额(份)", "份额（份）",
    )
    has_strong = any(k in s for k in strong_phrases)

    # (2) 短句：包含动词且本身不长（≤16字）
    action_verbs = (
        "买入", "卖出", "赎回", "申购", "认购", "定投", "转换", "撤销",
        "加仓", "减仓", "止盈", "止损", "清仓", "建仓",
    )
    starts_with_verb_short = (
        len(s) <= 16
        and any(s.startswith(v) for v in action_verbs)
    )

    # (3) 金额/份额句：以 元/份 结尾且含数字；或"像基金名"的短文本
    #     常见格式：含"基金"二字、或末尾带"股票A/股票C/指数A/联接A/混合A/债券A/C/E/I"等基金命名后缀
    fund_tail_re = re.compile(r"(股票|指数|联接|混合|债券|货币|QDII|FOF|ETF|LOF)[ABCDEIH]?$")
    amount_or_fund = (
        (s.endswith("元") and len(s) <= 16 and any(c.isdigit() for c in s))
        or (s.endswith("份") and len(s) <= 16 and any(c.isdigit() for c in s))
        or ("基金" in s and len(s) <= 40)
        or (fund_tail_re.search(s) and len(s) <= 40)
    )

    # (4) 完全等于某个操作词（不是自然句子）
    exact_op = s in {
        "买入", "卖出", "转换", "撤销",
        "定投", "赎回", "申购", "认购",
        "加仓", "减仓", "止盈", "止损", "清仓", "建仓",
        "买入确认中", "卖出确认中", "转换确认中",
        "金额", "份额", "基金",
    }

    if has_strong or starts_with_verb_short or amount_or_fund or exact_op:
        return True
    return False


def _is_opinion_text(s: str) -> bool:
    # 长度 >= 12 且不属于收益率/时间/操作
    if len(s) < 12:
        return False
    if _is_yield_text(s) or _is_time_text(s) or _is_operation_text(s):
        return False
    return True


def _is_kol_candidate(s: str) -> bool:
    s = s.strip()
    if not _KOL_CANDIDATE_RE.match(s):
        return False
    if s in BLACKLIST_KOL_TOKENS:
        return False
    # 排除包含"非名字字符"的候选（逗号/空格/°/% 等）
    if _KOL_FORBIDDEN_CHAR_RE.search(s):
        return False
    # 排除全数字 / 像时间
    if re.fullmatch(r"[\d]+", s):
        return False
    if _is_time_text(s):
        return False
    # 排除像收益率或金额这种（有 % 或 末尾是元/份）
    if "%" in s or s.endswith(("元", "份")):
        return False
    # 排除纯操作词
    if _is_operation_text(s) and len(s) <= 4:
        return False
    return True


# ============================================================
#  加载：支持 raw_pages dict / JSON 文件路径 / 单页 {page,texts} dict
# ============================================================

def _normalize_payload(src: Any) -> Dict[str, Any]:
    """
    统一把输入规范成：
      {
        "generated_at": str,
        "pages": [ {"page":N, "texts":[...]}, ... ],     # 多页
        "all_unique_texts": [...],                       # 可选
      }
    """
    if isinstance(src, str) and os.path.exists(src):
        with open(src, "r", encoding="utf-8-sig") as f:
            raw_text = f.read()
        payload = json.loads(raw_text)
    else:
        payload = src

    if not isinstance(payload, dict):
        raise TypeError(f"markdown export 只接受 dict 或 JSON 路径，收到: {type(payload)!r}")

    # 单页模式：{page, texts} → 包成 pages 数组
    if isinstance(payload.get("texts"), list) and "pages" not in payload:
        pages = [{"page": int(payload.get("page") or 1), "texts": list(payload["texts"])}]
    else:
        raw_pages = payload.get("pages")
        if isinstance(raw_pages, list):
            pages = []
            for p in raw_pages:
                if isinstance(p, dict) and isinstance(p.get("texts"), list):
                    pages.append({
                        "page": int(p.get("page") or (len(pages) + 1)),
                        "texts": list(p["texts"]),
                    })
        else:
            pages = []

    generated_at = (
        payload.get("generated_at")
        or payload.get("collected_at")
        or datetime.datetime.now().isoformat(timespec="seconds")
    )
    return {
        "generated_at": str(generated_at),
        "pages": pages,
        "all_unique_texts": (
            list(payload["all_unique_texts"])
            if isinstance(payload.get("all_unique_texts"), list)
            else None
        ),
    }


# ============================================================
#  单页内分组：找到第一个大V名候选；其余文本分别归类
# ============================================================

def _classify_page(texts: List[str]) -> Dict[str, Any]:
    """
    返回：
      {
        "kol_name": Optional[str],
        "yields": List[str],
        "times": List[str],
        "opinions": List[str],
        "operations": List[str],
        "others": List[str],
      }
    注意：所有列表都保留原始文档顺序。
    """
    kol_name: Optional[str] = None
    yields: List[str] = []
    times: List[str] = []
    opinions: List[str] = []
    operations: List[str] = []
    others: List[str] = []

    for t in texts:
        # 大V名：取本页第一个命中的候选，只取一次
        if kol_name is None and _is_kol_candidate(t):
            kol_name = t
            continue

        if _is_time_text(t):
            times.append(t)
            continue
        if _is_yield_text(t):
            yields.append(t)
            continue
        if _is_operation_text(t):
            operations.append(t)
            continue
        if _is_opinion_text(t):
            opinions.append(t)
            continue
        others.append(t)

    return {
        "kol_name": kol_name,
        "yields": yields,
        "times": times,
        "opinions": opinions,
        "operations": operations,
        "others": others,
    }


# ============================================================
#  Markdown 生成
# ============================================================

def _md_escape(text: str) -> str:
    """最小转义：避免单行 | 开头 或 # 开头 被当成 markdown 结构"""
    if not text:
        return ""
    t = text
    # 用户要求"保留原始顺序/原始文本"，所以只转义行首可能破坏结构的字符
    if t.startswith(("#", "##", "###", "####", "- ", "* ", "> ")):
        # 加一个零宽不换行空格做前缀（实际显示无差异）
        t = "\u200B" + t
    return t


def _render_bullets(items: Iterable[str], *, title: str) -> List[str]:
    lines: List[str] = []
    first = True
    for it in items:
        s = _md_escape(str(it))
        if not s:
            continue
        if first:
            lines.append(f"**{title}**：")
            first = False
        lines.append(f"- {s}")
    if not first:
        lines.append("")  # 段落空行
    return lines


def render_markdown(payload: Any) -> str:
    """把规范化前/后的 raw_pages payload 渲染成 Markdown 文本。"""
    data = _normalize_payload(payload)
    pages = data["pages"]
    total_texts = sum(len(p["texts"]) for p in pages)

    # ---------- 头 ----------
    out: List[str] = []
    out.append("# 蚂蚁财富大V操作采集")
    out.append("")
    out.append("> 本文档由 raw_pages JSON 自动导出，仅用于人工阅读与后续 LLM 分析。")
    out.append("> **未经过交易类型判断**（BUY/SELL/TRANSFER 等结构化解析留给后续 AIParser 模块）。")
    out.append("")
    out.append("## 采集概览")
    out.append("")
    out.append(f"- 采集时间：{data['generated_at']}")
    out.append(f"- 页面数量：{len(pages)}")
    out.append(f"- 文本数量：{total_texts}")
    if data.get("all_unique_texts") is not None:
        out.append(f"- 累计唯一文本：{len(data['all_unique_texts'])} 行")
    out.append("")

    # ---------- 每页输出 ----------
    out.append("---")
    out.append("")

    seen_kol: set = set()
    for idx, page in enumerate(pages, 1):
        page_no = int(page.get("page") or idx)
        texts = page.get("texts") or []
        g = _classify_page(texts)

        # 标题：优先大V名；名字重复加页码后缀；否则写未识别
        title_name = g.get("kol_name")
        header: str
        if title_name:
            if title_name in seen_kol:
                header = f"## {title_name}（第{page_no}页）"
            else:
                header = f"## {title_name}"
                seen_kol.add(title_name)
        else:
            header = f"## 页面 {page_no}（未识别到用户名）"
        out.append(header)
        out.append("")
        out.append(f"_第 {page_no} 页 / 共 {len(page['texts'])} 行文本_")
        out.append("")

        # 各分区（顺序严格按用户要求：收益率 → 时间 → 观点文本 → 操作相关文本）
        out.extend(_render_bullets(g["yields"], title="收益率"))
        out.extend(_render_bullets(g["times"], title="时间"))
        out.extend(_render_bullets(g["opinions"], title="观点文本"))
        out.extend(_render_bullets(g["operations"], title="操作相关文本"))

        # 剩余其他文本（保底展示，避免弱分类丢掉任何原始信息）
        if g["others"]:
            out.append("**其它文本**：")
            for s in g["others"]:
                out.append(f"- {_md_escape(str(s))}")
            out.append("")

    out.append("---")
    out.append("")
    out.append("_End of raw_pages export._")
    out.append("")

    return "\n".join(out)


# ============================================================
#  便捷函数：导出 → .md 文件
# ============================================================

def export_raw_pages_to_markdown(
    src: Any,
    md_path: Optional[str] = None,
) -> str:
    """
    Args:
        src:   raw_pages dict | {page,texts} dict | JSON 文件路径 str
        md_path: 输出 md 路径；为空时按 JSON 路径同名 .md 或
                 <project_root>/output/raw_pages_YYYYMMDD_HHMMSS.md
    Returns:
        写入的 md 文件绝对路径
    """
    md_text = render_markdown(src)

    if not md_path:
        if isinstance(src, str) and os.path.exists(src):
            base = os.path.splitext(src)[0]
            md_path = base + ".md"
        else:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            md_path = os.path.join(_PROJECT_ROOT, "output", f"raw_pages_{ts}.md")

    md_path = os.path.abspath(md_path)
    os.makedirs(os.path.dirname(md_path) or ".", exist_ok=True)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_text)
    return md_path


# ============================================================
#  CLI：python core/markdown_exporter.py output/raw_pages_xxx.json
# ============================================================

def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="raw_pages JSON → Markdown（仅展示，不做金融解析）")
    ap.add_argument("json_path", nargs="?", help="raw_pages_*.json 路径")
    ap.add_argument("-o", "--output", dest="md", metavar="PATH", help="输出 .md 路径（默认同名 .md）")
    args = ap.parse_args()

    if not args.json_path:
        ap.print_help()
        sys.exit(2)

    if not os.path.exists(args.json_path):
        print(f"[ERROR] JSON 不存在: {args.json_path}")
        sys.exit(3)

    md_path = export_raw_pages_to_markdown(args.json_path, md_path=args.md)
    print("✅ Markdown 导出完成：")
    print(f"   JSON: {os.path.abspath(args.json_path)}")
    print(f"   MD   : {md_path}")


if __name__ == "__main__":
    _cli()
