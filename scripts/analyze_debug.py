"""分析 8/26 debug XML 的滑动有效性"""
import re
import os

DEBUG = r"e:\PM\PM\KOL-RICH\debug"
files = sorted([f for f in os.listdir(DEBUG) if f.endswith('.xml')])

print("8/26 debug XML 分析（按 dump 顺序）:")
print("=" * 90)
print(f"{'文件':<16} {'vis_nodes':>10} {'ops':>5} {'expand':>7} {'anchors':>8} {'anchor_top':>12} {'anchor_bot':>12} {'anchor_range':>12}")
print("=" * 90)

prev_anchor_bot = None
for fname in files:
    path = os.path.join(DEBUG, fname)
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        xml = f.read()

    visible_nodes = len(re.findall(r'visible="true"', xml))
    op_count = len(re.findall(r'(买入确认中|卖出确认中|定投确认中|撤销确认中|转换确认中)', xml))
    expand_btns = len(re.findall(r'展开今日全部\d+条操作', xml))

    anchor_y = re.findall(r'llm_feed_item_root"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
    anchor_ys_top = [int(y1) for _, y1, _, _ in anchor_y]
    anchor_ys_bot = [int(y2) for _, _, _, y2 in anchor_y]

    anchor_top = min(anchor_ys_top) if anchor_ys_top else None
    anchor_bot = max(anchor_ys_bot) if anchor_ys_bot else None
    anchor_range = (anchor_bot - anchor_top) if (anchor_top and anchor_bot) else None

    drift = ""
    if prev_anchor_bot is not None and anchor_bot is not None:
        delta = anchor_bot - prev_anchor_bot
        drift = f"{'↑' if delta < 0 else '↓' if delta > 0 else '·'}{delta}"
    prev_anchor_bot = anchor_bot

    print(f"{fname:<16} {visible_nodes:>10} {op_count:>5} {expand_btns:>7} {len(anchor_y):>8} {str(anchor_top):>12} {str(anchor_bot):>12} {str(anchor_range):>12} {drift}")

print()
print("说明:")
print("  anchor_y_top: 第一个帖子容器顶部 Y 坐标")
print("  anchor_y_bot: 最后一个帖子容器底部 Y 坐标")
print("  ↑负数 = 帖子向上移动(滑动成功)")
print("  ↓正数 = 帖子向下移动(滑动失败)")
print("  ·0 = 位置完全没变(滑动完全失效)")
