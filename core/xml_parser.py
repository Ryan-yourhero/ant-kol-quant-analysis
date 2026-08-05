"""
UI XML解析模块
负责：
1. 读取并解析uiautomator dump生成的XML文件
2. 遍历XML节点树，提取所有文本节点（text/content-desc）
3. 保留节点的层级结构、坐标信息（bounds）、资源ID等上下文
4. 提供多种查询接口（按属性、按坐标、按文本模糊匹配等）

设计思路：
- 使用标准库 xml.etree.ElementTree 作为主解析器
- 兼容可选的 lxml 加速（如果已安装）
- 每个节点解析为轻量级 dataclass，便于后续数据提取层使用
"""

import os
import re
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Callable, Tuple

from config import settings

logger = logging.getLogger(__name__)

# 优先使用lxml（性能更好），否则回退到标准库
try:
    import lxml.etree as ET  # type: ignore
    _USE_LXML = True
except ImportError:
    import xml.etree.ElementTree as ET  # type: ignore
    _USE_LXML = False


@dataclass
class Bounds:
    """节点边界坐标（像素）"""
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def center(self) -> Tuple[int, int]:
        return ((self.left + self.right) // 2, (self.top + self.bottom) // 2)

    @classmethod
    def from_string(cls, bounds_str: str) -> Optional["Bounds"]:
        """
        从uiautomator的bounds字符串解析坐标
        格式: "[left,top][right,bottom]"
        """
        if not bounds_str:
            return None
        match = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", bounds_str.strip())
        if not match:
            return None
        return cls(
            left=int(match.group(1)),
            top=int(match.group(2)),
            right=int(match.group(3)),
            bottom=int(match.group(4)),
        )

    def contains(self, other: "Bounds") -> bool:
        """判断当前bounds是否包含另一个bounds"""
        return (self.left <= other.left and
                self.top <= other.top and
                self.right >= other.right and
                self.bottom >= other.bottom)

    def y_overlap(self, other: "Bounds") -> int:
        """计算Y方向的重叠像素数（用于判断是否在同一行）"""
        overlap_top = max(self.top, other.top)
        overlap_bottom = min(self.bottom, other.bottom)
        return max(0, overlap_bottom - overlap_top)


@dataclass
class UiNode:
    """UI节点信息"""
    index: int                        # 同级节点索引
    text: str                         # text属性值
    content_desc: str                 # content-desc属性值
    resource_id: str                  # resource-id属性
    class_name: str                   # class属性（如 android.widget.TextView）
    package: str                      # package属性
    bounds: Optional[Bounds]          # 坐标边界
    clickable: bool                   # 是否可点击
    scrollable: bool                  # 是否可滚动
    enabled: bool                     # 是否启用
    selected: bool                    # 是否选中
    checked: bool                     # 是否勾选
    depth: int                        # 树深度（根节点为0）
    parent: Optional["UiNode"] = None  # 父节点引用
    children: List["UiNode"] = field(default_factory=list)  # 子节点

    @property
    def display_text(self) -> str:
        """获取用于展示的文本：优先text，其次content-desc"""
        return self.text.strip() or self.content_desc.strip()

    @property
    def has_text(self) -> bool:
        """是否含有有效文本"""
        return bool(self.display_text)

    def iter_all_text_nodes(self) -> List["UiNode"]:
        """收集当前节点及其所有后代中含文本的节点"""
        result = []
        if self.has_text:
            result.append(self)
        for child in self.children:
            result.extend(child.iter_all_text_nodes())
        return result

    def find_siblings(self) -> List["UiNode"]:
        """获取兄弟节点（同级）"""
        if not self.parent:
            return []
        return [s for s in self.parent.children if s is not self]

    def find_nearest_by_y(self, nodes: List["UiNode"], above: bool = True) -> Optional["UiNode"]:
        """
        在给定节点列表中，找Y坐标最近的一个（默认上方）

        Args:
            nodes: 候选节点列表
            above: True=找上方最近的, False=找下方最近的
        """
        if not self.bounds:
            return None
        candidates = []
        for n in nodes:
            if n is self or not n.bounds:
                continue
            if above:
                distance = self.bounds.top - n.bounds.bottom
                if distance <= 0:
                    continue  # 必须在上方
            else:
                distance = n.bounds.top - self.bounds.bottom
                if distance <= 0:
                    continue  # 必须在下方
            candidates.append((distance, n))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]

    def to_dict(self, include_children: bool = False) -> Dict:
        """转换为字典（用于调试/序列化）"""
        d = {
            "class": self.class_name,
            "text": self.text,
            "content_desc": self.content_desc,
            "resource_id": self.resource_id,
            "bounds": str(self.bounds) if self.bounds else None,
            "clickable": self.clickable,
            "depth": self.depth,
        }
        if include_children and self.children:
            d["children"] = [c.to_dict(True) for c in self.children]
        return d


class UIXmlParser:
    """
    UI XML解析器

    使用示例：
        parser = UIXmlParser()
        root = parser.parse(xml_content)
        all_texts = parser.get_all_text_nodes(root)
        matches = parser.find_nodes_by_text(root, "基金")
    """

    # bounds属性名常量
    _ATTR_BOUNDS = "bounds"

    def __init__(self, recover_mode: bool = True):
        """
        Args:
            recover_mode: XML损坏时是否尝试自动修复并解析（默认True）
        """
        self.recover_mode = recover_mode
        self._last_parse_file: Optional[str] = None

    # ============================================================
    #  核心解析方法
    # ============================================================

    def parse(self, xml_content) -> UiNode:
        """
        解析XML字符串/字节为UiNode树

        Args:
            xml_content: uiautomator dump出的XML内容（支持 str 或 bytes）
                - 若是bytes：优先直接传给ElementTree（可正确处理<?xml encoding声明）
                - 若是str：自动剥除<?xml ...?>声明头，避免"Unicode strings with encoding
                  declaration are not supported"错误

        Returns:
            根节点 UiNode

        Raises:
            ValueError: XML解析失败且无法恢复
        """
        tree = self._parse_xml_tree(xml_content)
        root_elem = tree.getroot()
        return self._build_node_tree(root_elem, parent=None, depth=0)

    def parse_file(self, file_path: str) -> UiNode:
        """从本地XML文件解析（推荐：直接读bytes，避免encoding声明问题）"""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"XML文件不存在: {file_path}")
        self._last_parse_file = file_path
        with open(file_path, "rb") as f:   # bytes 模式读取，交给解析器自己解码
            return self.parse(f.read())

    @staticmethod
    def _strip_xml_declaration(xml_str: str) -> str:
        """
        如果XML字符串第一行带 <?xml ... encoding=... ?> 声明，则把它整行删掉。
        ElementTree 接受 Python str 时，如果含 encoding declaration 会报错：
          "Unicode strings with encoding declaration are not supported."
        """
        if not xml_str:
            return xml_str
        # 声明通常在最开头，正则匹配并整行替换（兼容 BOM 和 换行\r\n）
        return re.sub(r"^\s*<\?xml[\s\S]*?\?>", "", xml_str, count=1)

    def _parse_xml_tree(self, xml_content):
        """
        内部：解析XML（同时支持 str / bytes）

        优先级：
          1) bytes 直接解析（ElementTree 会按 <?xml encoding=?> 自行解码，最可靠）
          2) str 先剥除 XML 声明，再解析
          3) 仍失败 → recover 模式再试
        """
        # ---------- Case A：bytes ----------
        if isinstance(xml_content, (bytes, bytearray)):
            try:
                return ET.ElementTree(ET.fromstring(bytes(xml_content)))
            except ET.ParseError as e:
                first_err = e
                if not self.recover_mode or not _USE_LXML:
                    raise ValueError(f"XML解析失败(bytes): {e}") from e
                try:
                    parser = ET.XMLParser(recover=True)
                    return ET.ElementTree(ET.fromstring(bytes(xml_content), parser=parser))
                except Exception as e2:
                    raise ValueError(
                        f"XML解析失败(bytes, recover模式也失败): {e2}\n"
                        f"首次错误: {first_err}"
                    ) from e2

        # ---------- Case B：str ----------
        if not isinstance(xml_content, str):
            raise TypeError(
                f"xml_content 必须是 str 或 bytes，实际: {type(xml_content).__name__}"
            )

        clean = self._strip_xml_declaration(xml_content)
        try:
            return ET.ElementTree(ET.fromstring(clean))
        except ET.ParseError as e:
            first_err = e
            if not self.recover_mode or not _USE_LXML:
                raise ValueError(f"XML解析失败(str): {e}") from e
            try:
                parser = ET.XMLParser(recover=True)
                return ET.ElementTree(ET.fromstring(clean, parser=parser))
            except Exception as e2:
                raise ValueError(
                    f"XML解析失败(str, recover模式也失败): {e2}\n"
                    f"首次错误: {first_err}"
                ) from e2

    def _build_node_tree(
        self,
        element,
        parent: Optional[UiNode],
        depth: int,
    ) -> UiNode:
        """递归构建UiNode树"""
        attr = element.attrib

        bounds = Bounds.from_string(attr.get(self._ATTR_BOUNDS, ""))
        node = UiNode(
            index=int(attr.get("index", "0") or "0"),
            text=attr.get("text", "") or "",
            content_desc=attr.get("content-desc", "") or "",
            resource_id=attr.get("resource-id", "") or "",
            class_name=attr.get("class", "") or "",
            package=attr.get("package", "") or "",
            bounds=bounds,
            clickable=self._str2bool(attr.get("clickable", "false")),
            scrollable=self._str2bool(attr.get("scrollable", "false")),
            enabled=self._str2bool(attr.get("enabled", "false")),
            selected=self._str2bool(attr.get("selected", "false")),
            checked=self._str2bool(attr.get("checked", "false")),
            depth=depth,
            parent=parent,
        )

        # 递归处理子节点
        for i, child_elem in enumerate(element):
            child_node = self._build_node_tree(child_elem, parent=node, depth=depth + 1)
            child_node.index = i  # 覆盖默认的xml index，用实际兄弟位置
            node.children.append(child_node)

        return node

    @staticmethod
    def _str2bool(val: str) -> bool:
        return str(val).lower() in ("true", "1", "yes")

    # ============================================================
    #  查询工具方法
    # ============================================================

    def get_all_text_nodes(self, root: UiNode) -> List[UiNode]:
        """获取整棵树中所有含文本的节点（按文档顺序）"""
        return root.iter_all_text_nodes()

    def get_nodes_sorted_by_position(self, nodes: List[UiNode]) -> List[UiNode]:
        """
        将节点按从上到下、从左到右的阅读顺序排序：
        先按bounds.top分组（同一行），再按bounds.left排序。

        WebView/H5 特殊处理：若节点没有有效 bounds，保留其在传入 nodes 列表中的
        原始顺序（即 XML 文档遍历顺序），而不是全部排到最后。
        这样保证在蚂蚁财富 H5 理财盘友圈里上下文窗口配对仍然正确。
        """
        def _is_valid_bounds(b) -> bool:
            if b is None:
                return False
            return not (b.left == b.right == 0 and b.top == b.bottom == 0)

        pos_index = {id(n): i for i, n in enumerate(nodes)}
        max_pos = len(nodes)

        def sort_key(n: UiNode):
            has_valid = _is_valid_bounds(n.bounds)
            if has_valid:
                # 同一行判定：top差距在节点高度的1/2以内视为同一行
                row_key = n.bounds.top // max(n.bounds.height, 1)
                # 前两个 0 让有 bounds 的节点按 bounds 排在前面，但与无 bounds 节点的
                # 文档顺序仍然可以交叉（通过加入 pos_index 分量保持稳定）。
                # 这里为了简单：有 bounds 的按 bounds，无 bounds 的按 pos_index 穿插排序
                # 但为了不打乱与 bounds 节点的相对顺序，这里仍分两组，但对无 bounds
                # 组用 pos_index 而不是超大值，让它们按文档顺序排列。
                return (0, 0, row_key, n.bounds.left, pos_index.get(id(n), max_pos))
            else:
                # 无 bounds：按文档原始顺序排列；排在有 bounds 之后
                return (1, 1, pos_index.get(id(n), max_pos), 0, 0)
        result = sorted(nodes, key=sort_key)
        return result

    def find_nodes_by_text(
        self,
        root: UiNode,
        keyword: str,
        regex: bool = False,
        ignore_case: bool = True,
        use_content_desc: bool = True,
    ) -> List[UiNode]:
        """
        按文本关键词查找节点

        Args:
            root: 根节点
            keyword: 关键词或正则表达式
            regex: 是否使用正则匹配
            ignore_case: 是否忽略大小写
            use_content_desc: 是否同时检查content-desc属性
        """
        results = []
        pattern = None
        flags = re.IGNORECASE if ignore_case else 0
        if regex:
            pattern = re.compile(keyword, flags=flags)

        def check(text: str) -> bool:
            if not text:
                return False
            if regex:
                return bool(pattern.search(text))
            if ignore_case:
                return keyword.lower() in text.lower()
            return keyword in text

        for node in self.get_all_text_nodes(root):
            if check(node.text):
                results.append(node)
            elif use_content_desc and check(node.content_desc):
                results.append(node)
        return results

    def find_nodes_by_resource_id(
        self,
        root: UiNode,
        rid_pattern: str,
        regex: bool = False,
    ) -> List[UiNode]:
        """按resource-id查找节点"""
        results = []
        compiled = re.compile(rid_pattern) if regex else None
        for node in self._iter_all_nodes(root):
            rid = node.resource_id
            if not rid:
                continue
            if regex:
                if compiled.search(rid):
                    results.append(node)
            else:
                if rid == rid_pattern or rid_pattern in rid:
                    results.append(node)
        return results

    def find_nodes_by_class(
        self,
        root: UiNode,
        class_name: str,
        exact: bool = False,
    ) -> List[UiNode]:
        """按class名查找节点"""
        results = []
        for node in self._iter_all_nodes(root):
            if exact:
                if node.class_name == class_name:
                    results.append(node)
            else:
                if class_name in node.class_name:
                    results.append(node)
        return results

    def find_nodes_in_same_row(self, target: UiNode, nodes: List[UiNode]) -> List[UiNode]:
        """
        找出与target在同一行的节点
        判定逻辑：Y方向重叠高度 >= 双方较小高度的50%
        """
        if not target.bounds:
            return []
        results = []
        target_h = target.bounds.height
        for n in nodes:
            if n is target or not n.bounds:
                continue
            overlap = target.bounds.y_overlap(n.bounds)
            min_h = min(target_h, n.bounds.height)
            if min_h > 0 and overlap >= min_h * 0.5:
                results.append(n)
        return results

    def group_nodes_by_rows(self, nodes: List[UiNode]) -> List[List[UiNode]]:
        """
        将节点按行分组（每一行内部从左到右排序）。

        特殊处理 WebView/H5 页面：
        若超过 30% 的节点没有有效 bounds（即 left==right 或 top==bottom），
        则把无 bounds 的节点按照其在传入 nodes 列表中的**出现顺序**（即 XML 文档遍历顺序）
        逐行分配，每 N 个连续节点一行（默认 1 节点 1 行），而不是全部堆到最后一行。
        这种处理在蚂蚁财富理财盘友圈的 H5 节点 dump 下尤为关键，否则上下文配对完全失效。

        Returns:
            二维列表：rows[i] = 第i行的所有节点（已按X排序）
        """
        if not nodes:
            return []

        def _is_valid_bounds(b) -> bool:
            if b is None:
                return False
            return not (b.left == b.right == 0 and b.top == b.bottom == 0)

        with_bounds = [n for n in nodes if _is_valid_bounds(n.bounds)]
        without_bounds = [n for n in nodes if not _is_valid_bounds(n.bounds)]
        total = len(nodes)
        invalid_ratio = len(without_bounds) / total if total > 0 else 0

        sorted_by_y = sorted(
            with_bounds,
            key=lambda n: (n.bounds.top, n.bounds.left),
        )

        rows: List[List[UiNode]] = []
        current_row: List[UiNode] = []
        current_row_bottom = 0
        current_row_height = 0

        for n in sorted_by_y:
            if not current_row:
                current_row.append(n)
                current_row_bottom = n.bounds.bottom
                current_row_height = n.bounds.height
                continue

            # 判断是否同一行：top在当前行的 mid 范围以内
            overlap = current_row_bottom - n.bounds.top
            min_h = min(current_row_height, n.bounds.height)
            same_row = (min_h > 0 and overlap >= min_h * 0.4) or (n.bounds.top <= current_row_bottom)

            if same_row:
                current_row.append(n)
                current_row_bottom = max(current_row_bottom, n.bounds.bottom)
                current_row_height = max(current_row_height, n.bounds.height)
            else:
                # 新行
                current_row.sort(key=lambda x: x.bounds.left)
                rows.append(current_row)
                current_row = [n]
                current_row_bottom = n.bounds.bottom
                current_row_height = n.bounds.height

        if current_row:
            current_row.sort(key=lambda x: x.bounds.left)
            rows.append(current_row)

        # ===== 无 bounds 的节点处理 =====
        if without_bounds:
            if invalid_ratio >= 0.30:
                # 大量节点无效 bounds → 按 nodes 顺序逐行分配
                # 先构建 nodes 列表的位置索引
                pos_index = {id(n): i for i, n in enumerate(nodes)}
                # 按出现在 nodes 中的顺序排序 without_bounds 中的节点
                ordered = sorted(without_bounds, key=lambda n: pos_index.get(id(n), 10**9))
                # 简单策略：1 个节点 1 行 → 每一行对应一个信息块
                for n in ordered:
                    rows.append([n])
            else:
                # 少量节点无 bounds → 堆在最后
                rows.append(without_bounds)

        return rows

    # ============================================================
    #  辅助方法
    # ============================================================

    def _iter_all_nodes(self, root: UiNode):
        """遍历所有节点（含不含文本的）"""
        stack = [root]
        while stack:
            node = stack.pop()
            yield node
            # reverse以保持原顺序（先入后出）
            stack.extend(reversed(node.children))

    def dump_tree_text(self, root: UiNode, max_depth: int = None) -> str:
        """
        以缩进格式输出节点文本（用于调试快速预览页面内容）

        Args:
            root: 根节点
            max_depth: 最大深度，None不限制
        """
        lines = []

        def walk(node: UiNode):
            indent = "  " * node.depth
            marker = f"[{node.class_name.split('.')[-1]}]" if node.class_name else ""
            if node.has_text:
                lines.append(f"{indent}{marker} {node.display_text!r}"
                             + (f"  bounds={node.bounds}" if node.bounds else "")
                             + (f"  id={node.resource_id.split('/')[-1]}" if node.resource_id else ""))
            elif max_depth is None or node.depth < max_depth:
                # 允许输出带坐标或id的非文本节点
                if node.bounds and (node.clickable or node.resource_id):
                    lines.append(f"{indent}{marker} (no text)"
                                 + f"  bounds={node.bounds}"
                                 + (f"  id={node.resource_id.split('/')[-1]}" if node.resource_id else "")
                                 + ("  [CLICKABLE]" if node.clickable else ""))
            if max_depth is not None and node.depth >= max_depth:
                return
            for c in node.children:
                walk(c)

        walk(root)
        return "\n".join(lines)
