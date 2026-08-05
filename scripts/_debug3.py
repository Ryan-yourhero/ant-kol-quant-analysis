"""完全复现 extract() 调用路径"""
import sys, os
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
# 使用 extract_from_flat_texts 内部完全一样的构造
fake_nodes = []
for i, t in enumerate(texts):
    fake_nodes.append(UiNode(
        index=i, text=t, content_desc="", resource_id="", class_name="", package="",
        bounds=Bounds(0, i * 100, 1000, (i + 1) * 100 - 10),
        clickable=False, scrollable=False, enabled=True, selected=False, checked=False,
        depth=0, parent=None, children=[],
    ))
for j in range(len(fake_nodes)):
    if j > 0:
        fake_nodes[j].parent = fake_nodes[0]
if fake_nodes:
    fake_nodes[0].children = fake_nodes[1:]
root_arg = fake_nodes[0]

# 完全模拟 extract() 内部
parser = UIXmlParser()
all_text_nodes = parser.get_all_text_nodes(root_arg)
print(f"[all_text_nodes] {len(all_text_nodes)} 第0个id={id(all_text_nodes[0])} 与 fake_nodes[0]同? {all_text_nodes[0] is fake_nodes[0]}")

sorted_nodes = parser.get_nodes_sorted_by_position(all_text_nodes)
rows = parser.group_nodes_by_rows(sorted_nodes)
print(f"rows len={len(rows)}  rows[0] type={type(rows[0])} rows[0] is list? {isinstance(rows[0], list)}")

# 找锚点
anchors = ext._find_operation_anchors(sorted_nodes)
for node, opt, opt_text in anchors:
    print(f"\n锚点：{opt_text} id={id(node)}")
    ri = ext._find_row_index(node, rows)
    print(f"  _find_row_index 返回 ri={ri}")
    # 关键检查：传进来的 rows 到底是什么？
    print(f"  rows 类型：{type(rows)}，元素类型：{[type(r).__name__ for r in rows[:3]]}")
    try:
        ctx = ext._collect_context_rows(rows, ri, above=3, below=3)
        print(f"  _collect_context_rows 成功，ctx 有 {len(ctx)} 个节点")
    except Exception as e:
        import traceback
        print(f"  _collect_context_rows 出错: {e}")
        traceback.print_exc()

    # 再进一步：调用 _build_simple_operation
    try:
        op = ext._build_simple_operation(node, opt, opt_text, sorted_nodes, rows)
        print(f"  操作构建成功：{op.action_type} fund={op.fund_name} amt={op.amount}")
    except Exception as e:
        import traceback
        print(f"  _build_simple_operation 出错：{e}")
        traceback.print_exc()
