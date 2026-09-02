"""深入对比：检查今日是否有重复数据 / 异常"""
import re
import os
import json
from collections import Counter

# 读取两个 MD 文件
with open(r"e:\PM\PM\KOL-RICH\output\screen_dump_20260825_144551.md", 'r', encoding='utf-8') as f:
    md_25 = f.read()
with open(r"e:\PM\PM\KOL-RICH\output\screen_dump_20260826_144249.md", 'r', encoding='utf-8') as f:
    md_26 = f.read()

# 读取 raw_pages JSON 文件
import glob
raw_25_files = glob.glob(r"e:\PM\PM\KOL-RICH\output\raw_pages_20260825_*.json")
raw_26_files = glob.glob(r"e:\PM\PM\KOL-RICH\output\raw_pages_20260826_*.json")

if raw_25_files:
    with open(raw_25_files[-1], 'r', encoding='utf-8') as f:
        raw_25 = json.load(f)
    print(f"昨日 raw_pages: {raw_25_files[-1].split(chr(92))[-1]}")
    print(f"  总正式页数: {raw_25['total_pages']}")
    print(f"  总 dump 次数: {raw_25['pages_read']}")
    print(f"  滑动次数: {raw_25['swipe_count']}")
    print(f"  expand_clicks_success: {raw_25['expand_clicks_success']}")
    print(f"  expand_rounds: {raw_25.get('expand_rounds', 'N/A')}")
    print(f"  stop_reason: {raw_25['stop_reason']}")

if raw_26_files:
    with open(raw_26_files[-1], 'r', encoding='utf-8') as f:
        raw_26 = json.load(f)
    print(f"\n今日 raw_pages: {raw_26_files[-1].split(chr(92))[-1]}")
    print(f"  总正式页数: {raw_26['total_pages']}")
    print(f"  总 dump 次数: {raw_26['pages_read']}")
    print(f"  滑动次数: {raw_26['swipe_count']}")
    print(f"  expand_clicks_success: {raw_26['expand_clicks_success']}")
    print(f"  expand_rounds: {raw_26.get('expand_rounds', 'N/A')}")
    print(f"  stop_reason: {raw_26['stop_reason']}")

# ============ 重复数据检测 ============
print(f"\n{'='*60}")
print("  重复数据检测")
print(f"{'='*60}")

# 提取每笔交易的唯一键：KOL + 操作类型 + 基金名 + 金额
def extract_transactions(md_content, source_label):
    """从 MD 中提取每笔交易的唯一标识"""
    lines = md_content.split('\n')
    transactions = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # 检查是否是操作状态行
        if line in ['买入确认中', '卖出确认中', '定投确认中', '撤销确认中', '转换确认中']:
            op_type = line
            fund_name = ''
            amount = ''
            # 下一行应该是基金名
            if i+1 < len(lines) and lines[i+1].strip():
                fund_name = lines[i+1].strip()
            # 找金额/份额标签和值
            for j in range(i+1, min(i+5, len(lines))):
                l = lines[j].strip()
                if l in ['买入金额(元)', '卖出份额(份)']:
                    if j+1 < len(lines):
                        amount = lines[j+1].strip()
                    break
            if fund_name and amount:
                transactions.append(f"{op_type}|{fund_name}|{amount}")
        i += 1
    return transactions

tx_25 = extract_transactions(md_25, "8/25")
tx_26 = extract_transactions(md_26, "8/26")

print(f"\n  昨日提取交易数: {len(tx_25)}")
print(f"  今日提取交易数: {len(tx_26)}")

# 检查重复
dup_25 = len(tx_25) - len(set(tx_25))
dup_26 = len(tx_26) - len(set(tx_26))
print(f"  昨日重复数: {dup_25} ({dup_25/len(tx_25)*100:.1f}%)" if tx_25 else "  昨日重复数: 0")
print(f"  今日重复数: {dup_26} ({dup_26/len(tx_26)*100:.1f}%)" if tx_26 else "  今日重复数: 0")

# 最常出现的交易（重复交易）
counter_25 = Counter(tx_25)
counter_26 = Counter(tx_26)

print(f"\n  昨日 Top5 重复交易:")
for tx, count in counter_25.most_common(5):
    print(f"    [{count}次] {tx}")

print(f"\n  今日 Top5 重复交易:")
for tx, count in counter_26.most_common(5):
    print(f"    [{count}次] {tx}")

# ============ KOL 覆盖 ============
print(f"\n{'='*60}")
print("  KOL 覆盖对比")
print(f"{'='*60}")

# 提取所有 KOL 名
def extract_kols(md_content):
    """提取 KOL 名称"""
    # 模式: 某行是 KOL 名，下一行是"近一年收益率"
    lines = md_content.split('\n')
    kols = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if i+1 < len(lines) and '近一年收益率' in lines[i+1]:
            # 检查这一行是否是 KOL 名（不是导航项、不是展开按钮等）
            if stripped and stripped not in ['关注', '发现', '讨论区', '热议话题', '学理财', '资讯', '同路人', '真实财有趣', '我的关注', '全部', '今日操作', '最新']:
                if len(stripped) <= 20:
                    kols.append(stripped)
    return kols

kols_25 = extract_kols(md_25)
kols_26 = extract_kols(md_26)
unique_25 = set(kols_25)
unique_26 = set(kols_26)

print(f"\n  昨日发现 KOL: {len(unique_25)} 位 (总出现 {len(kols_25)} 次)")
print(f"  今日发现 KOL: {len(unique_26)} 位 (总出现 {len(kols_26)} 次)")
print(f"\n  昨日 KOL 列表:")
for k in sorted(unique_25):
    count = kols_25.count(k)
    print(f"    {k} (出现 {count} 次)")

print(f"\n  今日 KOL 列表:")
for k in sorted(unique_26):
    count = kols_26.count(k)
    print(f"    {k} (出现 {count} 次)")

# 今日独有 vs 昨日独有
only_25 = unique_25 - unique_26
only_26 = unique_26 - unique_25
common = unique_25 & unique_26
print(f"\n  共有 KOL: {len(common)} 位")
print(f"  昨日独有: {len(only_25)} 位 {list(only_25) if only_25 else '(无)'}")
print(f"  今日独有: {len(only_26)} 位 {list(only_26) if only_26 else '(无)'}")

# ============ 每页操作数 ============
print(f"\n{'='*60}")
print("  每页操作数分布")
print(f"{'='*60}")

def per_page_ops(md_content):
    """统计每页的操作状态数"""
    pages = re.split(r'# 页面(\d+)', md_content)
    result = {}
    # pages 结构: ['', '1', content1, '3', content3, ...]
    for i in range(1, len(pages), 2):
        page_num = int(pages[i])
        page_content = pages[i+1] if i+1 < len(pages) else ''
        ops = len(re.findall(r'(买入确认中|卖出确认中|定投确认中|撤销确认中|转换确认中)', page_content))
        result[page_num] = ops
    return result

pp_25 = per_page_ops(md_25)
pp_26 = per_page_ops(md_26)

print(f"\n  昨日每页操作数:")
for p, ops in sorted(pp_25.items()):
    print(f"    页面{p}: {ops} 条操作")

print(f"\n  今日每页操作数:")
for p, ops in sorted(pp_26.items()):
    print(f"    页面{p}: {ops} 条操作")

# ============ 总结 ============
print(f"\n{'='*60}")
print("  对比结论")
print(f"{'='*60}")

# 判断今日是否有问题
issues = []
if dup_26 > dup_25 * 2:
    issues.append(f"⚠ 今日重复率 ({dup_26/len(tx_26)*100:.1f}%) 明显高于昨日 ({dup_25/len(tx_25)*100:.1f}%)")
if len(tx_26) > len(tx_25) * 3:
    issues.append(f"⚠ 今日操作数 ({len(tx_26)}) 是昨日 ({len(tx_25)}) 的 {len(tx_26)/len(tx_25):.1f} 倍，可能包含重复或遗漏")
if dup_26 > len(tx_26) * 0.3:
    issues.append(f"⚠ 今日重复率超过 30%，存在大量重复数据")

if not issues:
    print("  ✓ 今日爬虫质量正常，数据量增加是因为爬取更完整")
else:
    for issue in issues:
        print(f"  {issue}")
    print(f"\n  建议：检查是否需要对今日数据进行去重处理")
