"""
页面原始文本提取器 (raw_text_extractor.py)

职责（v2 极简版 - 屏幕文字镜像）：
  1. 输入：uiautomator dump 的 XML 内容（str/bytes）或 XML 文件路径
  2. 深度优先遍历所有 UI 节点，按屏幕出现顺序取 text + content-desc 属性
  3. 清洗：
       - 只保留"前后空白归一化 + 压缩中间空白"（保证是人类可读的单字符串）
       - 只丢弃真正的空串（len(strip)==0）
       - 保留：按钮 / 金额 / 收益率 / 图片文字 / 系统控件文字 / 所有非空文本节点
       - 不再默认过滤：系统按钮、短文本、纯数字、纯符号等（这些都给后续 AI，不丢）
  4. 输出：
        {
          "page": 1,
          "texts": [ "童童读财", "14:39", "买入确认中", "朱雀企业优胜股票C", "买入2000元" ]
        }

注意：
  本文件**不做任何业务识别**（BUY/SELL/基金/大V/金额都不判断）。
  后续 AIParser 直接读取 screen_dump_*.md（屏幕文字镜像）即可。
"""

from __future__ import annotations

import os
import sys
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Iterable

# 项目路径（允许单独运行）
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(_HERE)
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

# 优先 lxml，回退标准库
try:
    import lxml.etree as _ET  # type: ignore
    _USE_LXML = True
except ImportError:
    import xml.etree.ElementTree as _ET  # type: ignore
    _USE_LXML = False


# ============================================================
#  旧过滤规则集合（仅保留给"强清洗模式"使用，默认不用）
# ============================================================
#  命中完全相等 → 整条丢弃（仅强清洗模式）
_SYS_BUTTON_EXACT_STRONG: set = {
    # 蚂蚁财富底部 tab
    "首页", "理财", "资产", "消息", "我的",
    # 导航 / 通用控件
    "返回", "搜索", "设置", "更多", "关闭", "取消", "确定", "同意", "允许", "拒绝",
    "去查看", "去看看", "去设置", "立即查看", "立即前往", "查看更多", "展开", "收起",
    "复制", "分享", "举报", "收藏", "点赞", "评论", "转发",
    # 权限 / 弹窗
    "知道了", "我知道了", "不再提醒", "以后再说", "稍后再说",
    "仅使用期间允许", "仅此次允许", "本次允许", "使用时允许",
    "始终允许", "始终拒绝",
    "确认", "下一步", "上一步", "完成", "跳过", "登录", "注册", "退出登录",
    "下载", "安装", "卸载", "更新", "升级",
    # 空控件常见占位
    "加载中", "加载中…", "加载中...", "加载失败", "点击重试", "重试",
    "暂无数据", "暂无内容", "为空", "空白", "•••", "…", "...", "·",
}

# 完全匹配正则（仅强清洗模式）
_DROP_RE_STRONG: List[re.Pattern] = [
    re.compile(r"^[\s\W_]+$"),
    re.compile(r"^[\d.]+$"),
]


def _normalize(text: str) -> str:
    """
    只做"人类可读"最小归一化：
      - 全角空格 -> 半角
      - 连续空白压缩为单个空格
      - 前后空白（含换行）去除
    注意：不去掉任何"非空白字符"，避免丢单数字 1/4/9、标点、度数、括号等内容。
    """
    if text is None:
        return ""
    t = text.replace("\u3000", " ")
    # 使用 split/join 方式：同时吃掉所有 Unicode 空格/换行
    parts = t.split()
    return " ".join(parts)


# v3.2 公开别名：统一命名风格，供其他模块 import
normalize_text = _normalize


def _is_drop_text_keep_all(s: str) -> bool:
    """v2 默认策略：只丢掉真正的空字符串。"""
    return s == ""


def _is_drop_text_strong(s: str) -> bool:
    """旧版强清洗（仅当调用方显式 opts.strong_filter=True 时使用）。"""
    if s == "":
        return True
    if s in _SYS_BUTTON_EXACT_STRONG:
        return True
    if len(s) < 1:
        return True
    for r in _DROP_RE_STRONG:
        if r.match(s):
            return True
    return False


# ============================================================
#  核心：XML → 文本列表
# ============================================================
@dataclass
class ExtractOptions:
    include_text_attr: bool = True
    include_content_desc: bool = True

    # 同节点 text == content-desc 是否只保留一条
    #   默认 False（镜像模式：两个属性各自独立输出，不做节点内去重，
    #   确保"图片文字 / content-desc 描述"也完整保留给 AI）
    dedup_same_node: bool = False

    # 相邻相同文本是否去重
    #   默认 False（镜像模式保留重复原貌，比如连续两个"点赞"按钮都输出）
    dedup_adjacent: bool = False

    # v2 默认 False：不对"系统按钮/短文本/纯符号"等做业务过滤，全部留给 AI
    # 如需旧版强清洗（调试用）可置 True（**不要在正式采集里打开**）
    strong_filter: bool = False


def _iter_text_attrs(elem, opts: ExtractOptions) -> Iterable[str]:
    """对单个节点按顺序产出 text/content-desc 两条（如果都非空且满足策略）"""
    is_drop = _is_drop_text_strong if opts.strong_filter else _is_drop_text_keep_all

    text_norm = ""
    if opts.include_text_attr:
        t = _normalize(elem.get("text") or "")
        if t and not is_drop(t):
            yield t
            text_norm = t
    if opts.include_content_desc:
        c = _normalize(elem.get("content-desc") or "")
        if c and not is_drop(c):
            if opts.dedup_same_node and opts.include_text_attr:
                if text_norm and c == text_norm:
                    # 同一节点 text 与 content-desc 相同 → 只输出一次
                    return
            yield c


def _iter_all_elems(root) -> Iterable:
    """按文档顺序（深度优先）产出所有元素。"""
    if _USE_LXML:
        yield from root.iter()
    else:
        yield from root.iter()


def extract_texts(
    xml_content,
    *,
    page: int = 1,
    opts: Optional[ExtractOptions] = None,
) -> dict:
    """
    从 XML 内容提取页面文本（屏幕文字镜像）。

    Args:
        xml_content: str | bytes (uiautomator dump 的完整 XML)
        page: 调用方传的页码，原样写入返回 dict
        opts:  提取选项（默认：不过滤业务文本 / 不做相邻去重）

    Returns:
        {"page": int, "texts": List[str]}
    """
    opts = opts or ExtractOptions()

    if isinstance(xml_content, (bytes, bytearray)):
        xml_bytes = bytes(xml_content)
        parser = _ET.XMLParser(recover=True, encoding="utf-8") if _USE_LXML else None
        root_elem = (
            _ET.fromstring(xml_bytes, parser=parser)
            if parser is not None
            else _ET.fromstring(xml_bytes)
        )
    elif isinstance(xml_content, str):
        if _USE_LXML:
            parser = _ET.XMLParser(recover=True, encoding="utf-8")
            root_elem = _ET.fromstring(xml_content.encode("utf-8"), parser=parser)
        else:
            root_elem = _ET.fromstring(xml_content.encode("utf-8"))
    else:
        raise TypeError(f"不支持的 xml_content 类型: {type(xml_content)!r}")

    raw: List[str] = []
    for elem in _iter_all_elems(root_elem):
        for s in _iter_text_attrs(elem, opts):
            raw.append(s)

    if opts.dedup_adjacent:
        deduped: List[str] = []
        last = object()
        for s in raw:
            if s != last:
                deduped.append(s)
                last = s
        raw = deduped

    return {"page": int(page), "texts": raw}


def extract_texts_from_file(
    xml_file: str,
    *,
    page: int = 1,
    opts: Optional[ExtractOptions] = None,
) -> dict:
    if not os.path.isfile(xml_file):
        raise FileNotFoundError(f"XML 文件不存在: {xml_file}")
    with open(xml_file, "rb") as f:
        data = f.read()
    return extract_texts(data, page=page, opts=opts)


# ============================================================
#  v3.4: 可见节点提取 — 增强版（visible-to-user / scrollable 父节点裁剪 / xml_index）
# ============================================================
def _parse_bounds_attr(bounds_str: str):
    """解析 elem.attrib 返回的 bounds 值（如 '[0,0][1440,3120]'）。"""
    if not bounds_str:
        return None
    m = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))


def _bounds_visible(bounds, screen_w: int, screen_h: int) -> bool:
    """bounds 是否与屏幕可视区域有交集。"""
    x1, y1, x2, y2 = bounds
    if (x1, y1, x2, y2) == (0, 0, 0, 0):
        return False
    return x2 > 0 and y2 > 0 and x1 < screen_w and y1 < screen_h


def _intersect_bounds(b1, b2):
    """返回两个 bounds 矩形的交集，无交集返回 None。"""
    x1 = max(b1[0], b2[0])
    y1 = max(b1[1], b2[1])
    x2 = min(b1[2], b2[2])
    y2 = min(b1[3], b2[3])
    if x1 >= x2 or y1 >= y2:
        return None
    return (x1, y1, x2, y2)


def _walk_visible_nodes(elem, scrollable_stack, screen_w, screen_h,
                         idx_counter, results):
    """
    递归遍历 XML 树，提取可见节点。

    v3.4 规则：
      A. visible-to-user="false" → 整个节点及其子树排除
      B. bounds=[0,0][0,0] → 排除
      C. 与屏幕无交集 → 排除
      D. 与最近 scrollable 父节点 bounds 求交集；无交集 → 排除
    """
    # ── A. visible-to-user ──
    vtu_attr = (elem.attrib.get("visible-to-user") or "").strip().lower()
    if vtu_attr == "false":
        # 节点及其子树对用户不可见，直接跳过
        return

    # ── 节点属性 ──
    is_scrollable = elem.attrib.get("scrollable") == "true"
    bounds_str = elem.attrib.get("bounds") or ""
    elem_bounds = _parse_bounds_attr(bounds_str)

    # ── 压入 scrollable 栈 ──
    pushed_scrollable = False
    if is_scrollable and elem_bounds and elem_bounds != (0, 0, 0, 0):
        scrollable_stack.append(elem_bounds)
        pushed_scrollable = True

    # ── B. zero-bounds 排除 ──
    if elem_bounds and elem_bounds != (0, 0, 0, 0):
        # ── C. 屏幕交集 ──
        if _bounds_visible(elem_bounds, screen_w, screen_h):
            # ── D. scrollable 父节点裁剪 ──
            effective_bounds = elem_bounds
            is_visible = True

            if scrollable_stack:
                sa_bounds = scrollable_stack[-1]
                clipped = _intersect_bounds(elem_bounds, sa_bounds)
                if clipped is None:
                    is_visible = False  # 位于 scrollable 容器外
                else:
                    effective_bounds = clipped

            if is_visible:
                # 确定 visible_to_user 三元值
                if vtu_attr == "true":
                    vtu_val = True
                elif vtu_attr == "false":
                    vtu_val = False
                else:
                    vtu_val = None

                clickable = elem.attrib.get("clickable") == "true"
                scrollable = is_scrollable

                text_val = _normalize(elem.attrib.get("text") or "")
                cd_val = _normalize(elem.attrib.get("content-desc") or "")

                idx = idx_counter[0]
                idx_counter[0] += 1

                if text_val:
                    results.append({
                        "text": text_val,
                        "bounds": effective_bounds,
                        "center_y": (effective_bounds[1] + effective_bounds[3]) // 2,
                        "source": "text",
                        "clickable": clickable,
                        "scrollable": scrollable,
                        "visible_to_user": vtu_val,
                        "xml_index": idx,
                    })
                if cd_val and cd_val != text_val:
                    results.append({
                        "text": cd_val,
                        "bounds": effective_bounds,
                        "center_y": (effective_bounds[1] + effective_bounds[3]) // 2,
                        "source": "content-desc",
                        "clickable": clickable,
                        "scrollable": scrollable,
                        "visible_to_user": vtu_val,
                        "xml_index": idx,
                    })

    # ── 递归子节点 ──
    for child in elem:
        _walk_visible_nodes(child, scrollable_stack, screen_w, screen_h,
                           idx_counter, results)

    # ── 弹出 scrollable 栈 ──
    if pushed_scrollable:
        scrollable_stack.pop()


def extract_visible_nodes(
    xml_content,
    screen_w: int,
    screen_h: int,
) -> List[Dict[str, Any]]:
    """
    v3.4: 提取当前屏幕可视区域内对用户可见的文本节点。

    可见判断优先级：
      A. visible-to-user="false" → 直接排除（含子树）
      B. bounds=[0,0][0,0] → 排除
      C. 与屏幕无交集 → 排除
      D. 位于 scrollable 父节点中 → 与父节点 bounds 求交集；无交集则排除

    每个节点包含：
      {"text": str, "bounds": (x1,y1,x2,y2), "center_y": int,
       "source": "text"|"content-desc",
       "clickable": bool, "scrollable": bool,
       "visible_to_user": True|False|None, "xml_index": int}
    """
    results: List[Dict[str, Any]] = []
    if not xml_content:
        return results

    try:
        if _USE_LXML:
            root = _ET.fromstring(
                xml_content.encode("utf-8") if isinstance(xml_content, str) else xml_content,
                parser=_ET.XMLParser(recover=True, encoding="utf-8"),
            )
        else:
            root = _ET.fromstring(
                xml_content.encode("utf-8") if isinstance(xml_content, str) else xml_content,
            )
    except (_ET.ParseError, TypeError, ValueError):
        return results

    idx_counter = [0]
    scrollable_stack: List = []
    _walk_visible_nodes(root, scrollable_stack, screen_w, screen_h,
                        idx_counter, results)
    return results


def visible_signature(
    visible_nodes: List[Dict[str, Any]],
    y_bucket: int = 200,
) -> str:
    """
    基于可见节点生成签名，用于判断滑动前后页面是否变化。

    签名包含：文本 + Y 位置分桶 + 顺序信息。
    即使 DOM 总文本完全相同，只要可见内容的 Y 位置变化，签名就会不同。
    """
    import hashlib
    _NUM_RE = re.compile(r"\d[\d,\.]*")
    parts = []
    for n in visible_nodes:
        txt = _NUM_RE.sub("N", n["text"])
        yb = n["center_y"] // y_bucket
        parts.append(f"{txt}@y={yb}")
    joined = "\n".join(parts)
    return hashlib.md5(joined.encode("utf-8")).hexdigest()


# v3.1 别名：统一命名风格
build_visible_signature = visible_signature


# ============================================================
#  v3.2: scroll_signature — 排除动态变化噪声（点赞数/评论数等）
#  专门用于判断页面是否真的发生了滑动
# ============================================================
# 噪声模式 — 这些内容变化不代表页面真正滑动
_SCROLL_NOISE_RES: List[re.Pattern] = [
    re.compile(r"^\d+$"),                          # 纯数字（点赞数/评论数）
    re.compile(r"^\d+[万亿kw]?$", re.IGNORECASE),  # 100w, 200, 3k
    re.compile(r"^[\d.]+\+[\d.]+%\d+$"),             # +23.45%100
    re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$"),        # HH:MM / HH:MM:SS
    re.compile(r"^(刚刚|\d+分钟前|\d+小时前|\d+天前|昨天|前天)$"),
    re.compile(r"^(点赞|评论|求解读|关注|转发|收藏|回复)\d*$"),
    re.compile(r"^\d+(点赞|评论|求解读|关注|转发|收藏|回复)$"),
    re.compile(r"^(original|关注|推荐|精华|热门|置顶)$", re.IGNORECASE),
]

# 顶部固定导航（y < 150px 区域始终存在，对滑动检测无贡献）
_SCROLL_TOPFIXED_Y_THRESHOLD = 150


def _is_scroll_noise(text: str, center_y: int) -> bool:
    """判断节点文本是否为滑动检测噪声（点赞数/时间等动态内容 + 顶部固定导航）。"""
    # 顶部固定区域
    if center_y < _SCROLL_TOPFIXED_Y_THRESHOLD:
        return True
    for r in _SCROLL_NOISE_RES:
        if r.match(text):
            return True
    return False


def scroll_signature(
    visible_nodes: List[Dict[str, Any]],
    y_bucket: int = 200,
) -> str:
    """
    专门判断页面滚动的签名 — 排除动态变化的噪声文本。

    基于可见节点的：
      - 非噪声文本
      - Y 位置分桶
      - 节点顺序

    与 visible_signature 的区别：
      - scroll_signature 过滤掉点赞数、评论数、时间、顶部导航等易变内容
      - 保留大V昵称、收益率、操作状态、基金名称、帖子正文等有定位意义的内容
      - 主要用于判断页面是否真正发生了滑动（非内容动态刷新）
    """
    import hashlib
    _NUM_RE = re.compile(r"\d[\d,\.]*")
    parts = []
    for n in visible_nodes:
        txt = n.get("text", "")
        cy = n.get("center_y", 0)
        if _is_scroll_noise(txt, cy):
            continue  # 跳过噪声节点
        txt = _NUM_RE.sub("N", txt)
        yb = cy // y_bucket
        parts.append(f"{txt}@y={yb}")
    joined = "\n".join(parts)
    return hashlib.md5(joined.encode("utf-8")).hexdigest()


# v3.2 别名
build_scroll_signature = scroll_signature


# ============================================================
#  v3.6: scroll_top_signature — 只看顶部 N 个非噪声节点
#  用于验证滑动是否真正移动了页面（而非仅底部新增了内容）
# ============================================================
_SCROLL_TOP_N = 5  # 检查前 N 个非噪声节点


def scroll_top_signature(
    visible_nodes: List[Dict[str, Any]],
    top_n: int = _SCROLL_TOP_N,
    y_bucket: int = 200,
) -> str:
    """只取前 top_n 个非噪声节点生成签名。
    用于判断滑动是否真正移动了页面顶部内容。
    如果滑动前后 top_signature 相同，说明页面没有有效滚动。
    """
    import hashlib
    _NUM_RE = re.compile(r"\d[\d,\.]*")
    count = 0
    parts = []
    for n in visible_nodes:
        if count >= top_n:
            break
        txt = n.get("text", "")
        cy = n.get("center_y", 0)
        if _is_scroll_noise(txt, cy):
            continue
        txt = _NUM_RE.sub("N", txt)
        yb = cy // y_bucket
        parts.append(f"{txt}@y={yb}")
        count += 1
    if not parts:
        return ""
    joined = "\n".join(parts)
    return hashlib.md5(joined.encode("utf-8")).hexdigest()


build_scroll_top_signature = scroll_top_signature


# ============================================================
#  便捷：多页合并的去重集合（给 ScrollManager/main.py 用）
# ============================================================
class TextAccumulator:
    """
    跨页累积去重。
    - seen:       全局所有出现过的行，用来算「累计新增行」
    - pages:      每一页的原始 dict（page/texts），用户后续 AIParser 要用
    """

    def __init__(self) -> None:
        self.seen: set = set()
        self.pages: List[dict] = []

    def add_page(self, page_dict: dict) -> dict:
        """
        把一页 {page, texts, visible_texts} 加入累计。
        返回 {"page", "texts", "visible_texts", "new_texts": [只在本页第一次出现的text]}

        visible_texts 必须是真实的「可见文本」，不得回退/重算为完整 texts。
        """
        page_no = int(page_dict.get("page", len(self.pages) + 1))
        texts = list(page_dict.get("texts") or [])
        visible_texts = (
            list(page_dict.get("visible_texts") or [])
            if isinstance(page_dict.get("visible_texts"), list)
            else []
        )
        new_texts: List[str] = []
        for s in texts:
            if s not in self.seen:
                self.seen.add(s)
                new_texts.append(s)
        page_item = {
            "page": page_no,
            "texts": texts,
            "visible_texts": visible_texts,
            "new_texts": new_texts,
        }
        self.pages.append(page_item)
        return page_item

    def summary(self) -> dict:
        return {
            "total_pages": len(self.pages),
            "total_unique_texts": len(self.seen),
            "last_page": self.pages[-1]["page"] if self.pages else 0,
        }


# ============================================================
#  CLI 自测
# ============================================================
if __name__ == "__main__":
    import argparse
    import json

    p = argparse.ArgumentParser(description="uiautomator XML → 页面原始文本列表 (raw text)")
    p.add_argument("xml_file", nargs="?", default=None, help="XML 文件路径（不传就从 window.xml 读）")
    p.add_argument("--page", type=int, default=1, help="写入结果的 page 字段")
    p.add_argument("--pretty", action="store_true", help="格式化 JSON 输出")
    p.add_argument("--sys", action="store_true", help="打印系统按钮停用词列表然后退出")
    p.add_argument("--strong-filter", action="store_true",
                   help="(调试用) 开启旧版强清洗：去系统按钮/短文本/纯符号（正式采集不建议）")
    args = p.parse_args()

    if args.sys:
        print("强清洗模式下的系统按钮 / 停用词 (完全相等即丢弃):")
        for w in sorted(_SYS_BUTTON_EXACT_STRONG):
            print(f"  - {w}")
        sys.exit(0)

    opts = ExtractOptions(strong_filter=args.strong_filter)

    path = args.xml_file or os.path.join(_PROJ, "window.xml")
    result = extract_texts_from_file(path, page=args.page, opts=opts)
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    print(f"\n# page={result['page']}, texts={len(result['texts'])}", file=sys.stderr)
