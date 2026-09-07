"""
screen_dump 镜像导出器 v3.4（严格"屏幕文字镜像"，不做任何识别/重组/总结）
====================================================================

v3.4 改动：
  - MD 只写 visible_nodes（visible_texts）
  - 完整 DOM/XML 另存 debug，不混入 MD
  - 一屏一页，跨页不重叠去重（50% 重叠是允许的）
  - 若页面有 visible_texts 字段则使用之，否则降级使用 texts

输入：raw_pages dict 或 raw_pages_*.json 文件路径
输出：output/screen_dump_YYYYMMDD_HHMMSS.md

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
#  系统 UI 噪声过滤（v3.5）
# ============================================================
_SYSTEM_NOISE = {
    "Android 系统通知",
    "微信通知",
    "WLAN",
    "中国移动",
    "中国电信",
    "中国联通",
    "正在充电",
    "振铃器静音",
    "免打扰",
    "侧屏幕面板",
    "K/s",
    "B/s",
    "MB/s",
    "GB/s",
}


def _is_system_noise(text: str) -> bool:
    """判断是否为系统 UI 噪声文本。"""
    t = text.strip()
    if not t:
        return True
    # 精确匹配
    if t in _SYSTEM_NOISE:
        return True
    # 前缀匹配（如 "WLAN 信号满格"、"正在充电，已完成百分之80"）
    for noise in _SYSTEM_NOISE:
        if t.startswith(noise):
            return True
    return False


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
    """把各种输入统一成 [{"page":N, "texts":[...], "visible_texts":[...]}, ...] 列表（按出现顺序）。"""
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
                visible_texts = (
                    p.get("visible_texts")
                    if isinstance(p.get("visible_texts"), list)
                    else []
                )
                page_dict: Dict[str, Any] = {
                    "page": int(p.get("page") or i),
                    "texts": [str(t) for t in texts],
                    "visible_texts": [str(t) for t in visible_texts],
                }
                # 保留已有额外字段
                for extra_key in ("source_round", "timestamp"):
                    if extra_key in p:
                        page_dict[extra_key] = p[extra_key]
                pages.append(page_dict)
            if pages:
                return pages
        # 单页 {page,texts} 结构
        if isinstance(payload.get("texts"), list):
            visible_texts = (
                payload.get("visible_texts")
                if isinstance(payload.get("visible_texts"), list)
                else []
            )
            return [{
                "page": int(payload.get("page") or 1),
                "texts": [str(t) for t in payload["texts"]],
                "visible_texts": [str(t) for t in visible_texts],
            }]

    raise TypeError(
        f"screen_dump 只接受 raw_pages dict / 单页 dict / JSON 文件路径；"
        f"收到: {type(payload)!r}"
    )


# ============================================================
#  渲染：严格镜像
# ============================================================

def render_screen_dump_md(src: Any) -> str:
    """v3.4: 把输入渲染成"屏幕文字镜像" Markdown 文本。

    若页面有 visible_texts 字段则使用之（一屏一页，不跨页去重），
    否则降级使用 texts（保持旧跨页去重逻辑）。
    """
    from collections import Counter

    pages = _as_pages(src)

    # 检测是否使用 visible_texts
    visible_pages = sum(
        1 for p in pages
        if isinstance(p.get("visible_texts"), list) and len(p.get("visible_texts", [])) > 0
    )
    use_visible = visible_pages > 0

    fallback_pages = len(pages) - visible_pages

    print(f"[SCREEN_DUMP]")
    print(f"  pages={len(pages)}")
    print(f"  use_visible={use_visible}")
    print(f"  visible_pages={visible_pages}")
    print(f"  fallback_pages={fallback_pages}")

    if not use_visible:
        print(f"[SCREEN_DUMP] WARNING: visible_texts unavailable, fallback to legacy texts mode")

    lines: List[str] = []

    if use_visible:
        # v3.4: 一屏一页，不跨页去重（50% 重叠是正常的）
        for p in pages:
            page_no = int(p.get("page") or 1)
            vtexts: List[str] = [
                str(t) for t in (p.get("visible_texts") or [])
                if t is not None and not _is_system_noise(str(t))
            ]
            if not vtexts:
                continue
            lines.append(f"# 页面{page_no}")
            lines.append("")
            for t in vtexts:
                lines.append(_escape_line(t))
                lines.append("")
    else:
        # 降级：使用 texts + 跨页去重
        emitted: Counter = Counter()
        for p in pages:
            page_no = int(p.get("page") or 1)
            texts: List[str] = [
                str(t) for t in (p.get("texts") or [])
                if t is not None and not _is_system_noise(str(t))
            ]

            cnt: Counter = Counter()
            new_texts: List[str] = []
            for t in texts:
                cnt[t] += 1
                if cnt[t] > emitted[t]:
                    new_texts.append(t)

            for t, c in cnt.items():
                if c > emitted[t]:
                    emitted[t] = c

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
