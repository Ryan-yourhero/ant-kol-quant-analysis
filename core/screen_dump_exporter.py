"""
screen_dump 镜像导出器（严格"屏幕文字镜像"，不做任何识别/重组/总结）
====================================================================

输入：raw_pages dict 或 raw_pages_*.json 文件路径
输出：output/screen_dump_YYYYMMDD_HHMMSS.md

只做一件事：
  - 按页序输出：# 页面N → 每个 text 一行 → 每行之间空一行
  - 不做 BUY/SELL/基金/大V/金额/AI 任何识别
  - 不重新组织，不总结，不删除任何业务文本

跨页去重（v2）：
  盘友圈是 WebView，dump 拿到的是"整个页面 DOM"而不是当前可视区域，
  滚动只是把新内容追加进 DOM → 相邻两页的 texts 大面积重复。
  因此按"出现次数差量"去重：某文本在第 N 页第 k 次出现，
  只有当 k 超过前面各页累计已输出的次数时才输出，位置保持页内原序。
  这样既不会把整页重复内容输出 N 遍，也不会误删每个帖子都有的
  "买入确认中"等重复状态行（它们分属不同帖子，各自保留）。
  完全没有新增内容的纯重复页直接跳过（不输出空的 # 页面N）。

示例输入 texts:
  ["光模块之王", "14:09", "指数回落了一点...", "买入确认中",
   "平安半导体领航精...", "买入金额(元)", "30000"]
示例输出：
  # 页面1
  <空行>
  光模块之王
  <空行>
  14:09
  <空行>
  指数回落了一点...
  <空行>
  买入确认中
  <空行>
  平安半导体领航精...
  <空行>
  买入金额(元)
  <空行>
  30000
"""

from __future__ import annotations

import os
import sys
import json
import re
import argparse
import datetime
from typing import Any, Dict, List, Optional


_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


# ============================================================
#  最小转义：避免原始文本恰好以 "# " 或 "- " 开头被误认为 markdown 结构
# ============================================================
_MD_STRUCT_PREFIX_RE = re.compile(r"^(#{1,6}\s|[-*+]\s|>\s?|\d+\.\s)")


def _escape_line(s: str) -> str:
    """
    屏幕镜像"字符保真优先"，只转义"会破坏 md 结构"的行首前缀。
    用零宽字符 \u200B 做前缀，保证显示时视觉一致。
    """
    if s is None:
        return ""
    if _MD_STRUCT_PREFIX_RE.match(s):
        return "\u200B" + s
    return s


# ============================================================
#  加载：兼容 raw_pages dict / 单页 {page,texts} dict / JSON 文件路径
# ============================================================

def _as_pages(payload: Any) -> List[Dict[str, Any]]:
    """把各种输入统一成 [{"page":N, "texts":[...]}, ...] 列表（按出现顺序）。"""
    # JSON 文件路径
    if isinstance(payload, str) and os.path.exists(payload):
        with open(payload, "r", encoding="utf-8-sig") as f:
            payload = json.loads(f.read())

    if isinstance(payload, dict):
        # 多页 raw_pages 结构：pages[]
        if isinstance(payload.get("pages"), list):
            pages: List[Dict[str, Any]] = []
            for i, p in enumerate(payload["pages"], 1):
                if not isinstance(p, dict):
                    continue
                texts = p.get("texts") if isinstance(p.get("texts"), list) else []
                pages.append({
                    "page": int(p.get("page") or i),
                    "texts": [str(t) for t in texts],
                })
            if pages:
                return pages
        # 单页 {page,texts} 结构
        if isinstance(payload.get("texts"), list):
            return [{
                "page": int(payload.get("page") or 1),
                "texts": [str(t) for t in payload["texts"]],
            }]

    raise TypeError(
        f"screen_dump 只接受 raw_pages dict / 单页 dict / JSON 文件路径；"
        f"收到: {type(payload)!r}"
    )


# ============================================================
#  渲染：严格镜像
# ============================================================

def render_screen_dump_md(src: Any) -> str:
    """把输入渲染成"屏幕文字镜像" Markdown 文本字符串（跨页按次数差量去重）。"""
    from collections import Counter

    pages = _as_pages(src)
    lines: List[str] = []

    # emitted[t] = 到目前为止，文本 t 已输出的次数
    emitted: Counter = Counter()

    for p in pages:
        page_no = int(p.get("page") or 1)
        texts: List[str] = [str(t) for t in (p.get("texts") or []) if t is not None]

        cnt: Counter = Counter()
        new_texts: List[str] = []
        for t in texts:
            cnt[t] += 1
            # 本页第 cnt[t] 次出现；只有超过"之前累计已输出次数"才是真新增
            if cnt[t] > emitted[t]:
                new_texts.append(t)

        # 更新已输出计数为本页与历史的较大值（DOM 只会增，不会少算）
        for t, c in cnt.items():
            if c > emitted[t]:
                emitted[t] = c

        # 纯重复页（没有任何新增文本）→ 跳过，不输出空的 # 页面N
        if not new_texts:
            continue

        lines.append(f"# 页面{page_no}")
        lines.append("")
        for t in new_texts:
            lines.append(_escape_line(t))
            lines.append("")

    return "\n".join(lines)


# ============================================================
#  便捷写文件
# ============================================================

def export_screen_dump_md(
    src: Any,
    md_path: Optional[str] = None,
) -> str:
    """
    Args:
        src: raw_pages dict / {page,texts} dict / raw_pages_*.json 路径
        md_path: 输出路径；不传则：
                 - 若 src 是 JSON 文件 → 改为 screen_dump_ 前缀 + 同名时间戳
                 - 否则 → output/screen_dump_YYYYMMDD_HHMMSS.md
    Returns:
        写入的 md 文件绝对路径
    """
    md_text = render_screen_dump_md(src)

    if not md_path:
        if isinstance(src, str) and os.path.exists(src):
            # 基于 JSON 文件名推导：
            #   raw_pages_20260805_143800.json -> screen_dump_20260805_143800.md
            #   raw_page_20260805_143800.json  -> screen_dump_20260805_143800.md
            base = os.path.splitext(os.path.basename(src))[0]
            # 去掉前缀：raw_pages_ / raw_page_ / kol_operations_
            for prefix in ("raw_pages_", "raw_page_", "kol_operations_"):
                if base.startswith(prefix):
                    base = base[len(prefix):]
                    break
            ts = base if base else datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            md_path = os.path.join(_PROJECT_ROOT, "output", f"screen_dump_{ts}.md")
        else:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            md_path = os.path.join(_PROJECT_ROOT, "output", f"screen_dump_{ts}.md")

    md_path = os.path.abspath(md_path)
    os.makedirs(os.path.dirname(md_path) or ".", exist_ok=True)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_text)
    return md_path


# ============================================================
#  CLI：python core/screen_dump_exporter.py output/raw_pages_xxx.json
# ============================================================

def _cli():
    ap = argparse.ArgumentParser(
        description="raw_pages JSON → 屏幕文字镜像 MD（严格顺序、不识别、不重组、不总结）"
    )
    ap.add_argument("json_path", nargs="?", help="raw_pages_*.json 路径")
    ap.add_argument("-o", "--output", dest="md", metavar="PATH", help="输出 .md 路径")
    args = ap.parse_args()

    if not args.json_path:
        ap.print_help()
        sys.exit(2)
    if not os.path.exists(args.json_path):
        print(f"[ERROR] JSON 不存在: {args.json_path}")
        sys.exit(3)

    md_path = export_screen_dump_md(args.json_path, md_path=args.md)
    pages = _as_pages(args.json_path)
    total = sum(len(p.get("texts") or []) for p in pages)
    print("✅ screen_dump 镜像导出完成：")
    print(f"   JSON   : {os.path.abspath(args.json_path)}")
    print(f"   MD     : {md_path}")
    print(f"   页面数 : {len(pages)}")
    print(f"   总文本 : {total} 行")


if __name__ == "__main__":
    _cli()
