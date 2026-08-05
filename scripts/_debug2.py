"""深入调试 rows 结构"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.xml_parser import UIXmlParser, UiNode, Bounds
from core.data_extractor import OperationDataExtractor

texts = [
    "童童读财",
    "14:39",
    "朱雀企业优胜股票C",
    "买入确认中",
    "买入金额(元)",
    "2,000.00元",
]

ext = OperationDataExtractor()

# 手动模拟 extract_from_flat_texts 并打印结构
fake_nodes: list[UiNode] = []
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
root = UiNode(0, "", "", "", "", "", None, False, False, True, False, False, 0)
if fake_nodes:
    root.children = [fake_nodes[0]]
    fake_nodes[0].parent = root

# 模拟 extract() 开头
parser = UIXmlParser()
all_text_nodes = parser.get_all_text_nodes(root)
print(f"[1] all_text_nodes 共 {len(all_text_nodes)} 个节点（fake_nodes 有 {len(fake_nodes)}）")
for i, n in enumerate(all_text_nodes):
    print(f"      {i}. {n.display_text!r}  bounds={n.bounds}")

sorted_nodes = parser.get_nodes_sorted_by_position(all_text_nodes)
print(f"[2] sorted_nodes 共 {len(sorted_nodes)}")

rows = parser.group_nodes_by_rows(sorted_nodes)
print(f"[3] group 返回了 rows 长度={len(rows)}")
for i, row in enumerate(rows):
    t = type(row).__name__
    if isinstance(row, list):
        print(f"      row[{i}] = list len={len(row)}:  "
              f"{[getattr(n, 'display_text', '???') for n in row]}")
    else:
        print(f"      row[{i}] = <type={t}> {row!r}")

# 看 anchor
anchors = ext._find_operation_anchors(sorted_nodes)
print(f"[4] 锚点数 = {len(anchors)}:")
for node, opt, opt_text in anchors:
    print(f"      node.text={node.display_text!r} type={opt} opt_text={opt_text!r}")
    # 看这个 node 在 rows 里是哪一行
    found = False
    for ri, row in enumerate(rows):
        if isinstance(row, list):
            if node in row:
                print(f"      → row index = {ri}")
                found = True
                break
    if not found:
        print(f"      → NOT FOUND in rows！ node id={id(node)}")
        # 检查每个 sorted_nodes 的 id
        print("      rows 中的节点 id:")
        for ri, row in enumerate(rows):
            if isinstance(row, list):
                for n in row:
                    print(f"        row[{ri}] node id={id(n)} text={n.display_text!r}")
        print("      sorted_nodes 中的节点 id:")
        for si, n in enumerate(sorted_nodes):
            print(f"        sorted[{si}] id={id(n)} text={n.display_text!r}")
