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
        把一页 {page, texts} 加入累计。
        返回 {"page", "texts", "new_texts": [只在本页第一次出现的text]}
        """
        page_no = int(page_dict.get("page", len(self.pages) + 1))
        texts = list(page_dict.get("texts") or [])
        new_texts: List[str] = []
        for s in texts:
            if s not in self.seen:
                self.seen.add(s)
                new_texts.append(s)
        page_item = {"page": page_no, "texts": texts, "new_texts": new_texts}
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
