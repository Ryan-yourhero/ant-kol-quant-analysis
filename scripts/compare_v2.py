"""简单直接对比：统计两个 MD 的交易数量和结构"""
import re
from collections import Counter

FILE_A = r"e:\PM\PM\KOL-RICH\output\screen_dump_20260825_144551.md"
FILE_B = r"e:\PM\PM\KOL-RICH\output\screen_dump_20260826_144249.md"

with open(FILE_A, 'r', encoding='utf-8') as f:
    md_a = f.read()
with open(FILE_B, 'r', encoding='utf-8') as f:
    md_b = f.read()

def analyze(md, label):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")

    # 统计所有操作状态
    op_states = re.findall(r'(买入确认中|卖出确认中|定投确认中|撤销确认中|转换确认中)', md)
    op_counter = Counter(op_states)
    
    # 统计基金名（操作状态后面紧接着的行）
    fund_pattern = r'(买入确认中|卖出确认中|定投确认中|撤销确认中|转换确认中)\s*\n(.+?)\n'
    fund_matches = re.findall(fund_pattern, md)
    
    # 统计金额/份额标签
    amount_labels = re.findall(r'(买入金额\(元\)|卖出份额\(份\))', md)
    
    # 提取基金名+金额对
    tx_pattern = r'(买入确认中|卖出确认中|定投确认中|撤销确认中|转换确认中)\s*\n(.+?)\s*\n(买入金额\(元\)|卖出份额\(份\))\s*\n([\d,]+\.?\d*)'
    transactions = re.findall(tx_pattern, md)
    
    # 计算唯一交易（基金名+金额）
    unique_tx = set()
    for op, fund, label, amount in transactions:
        unique_tx.add(f"{fund.strip()}|{amount.strip()}")
    
    # 每页统计
    pages = re.split(r'# 页面(\d+)', md)
    page_stats = {}
    for i in range(1, len(pages), 2):
        page_num = pages[i]
        page_content = pages[i+1] if i+1 < len(pages) else ''
        ops_in_page = len(re.findall(r'(买入确认中|卖出确认中|定投确认中|撤销确认中|转换确认中)', page_content))
        fund_count = len(set(re.findall(r'(.+?)\s*\n(?:买入金额\(元\)|卖出份额\(份\))', page_content)))
        page_stats[page_num] = {'ops': ops_in_page, 'funds': fund_count}
    
    print(f"\n  【基本数据】")
    print(f"    总行数: {len(md.split(chr(10)))}")
    print(f"    页面数: {len(page_stats)}")
    print(f"    操作状态出现次数: {len(op_states)}")
    print(f"    基金名+金额配对: {len(transactions)}")
    print(f"    唯一交易（基金+金额）: {len(unique_tx)}")

    print(f"\n  【操作类型分布】")
    for k, v in op_counter.most_common():
        pct = v/len(op_states)*100 if op_states else 0
        print(f"    {k}: {v} ({pct:.1f}%)")

    print(f"\n  【每页统计】")
    for page_num in sorted(page_stats.keys(), key=lambda x: int(x)):
        s = page_stats[page_num]
        print(f"    页面{page_num}: {s['ops']} 条操作, {s['funds']} 只基金")

    print(f"\n  【完整配对示例】前5条:")
    for op, fund, label, amount in transactions[:5]:
        print(f"    {op} | {fund.strip()} | {label} | {amount}")

    return {
        'pages': len(page_stats),
        'ops_total': len(op_states),
        'transactions': len(transactions),
        'unique_tx': len(unique_tx),
        'op_types': dict(op_counter),
        'dup_rate': (len(transactions) - len(unique_tx)) / len(transactions) * 100 if transactions else 0,
    }


stats_a = analyze(md_a, "8/25 (昨日 14:45)")
stats_b = analyze(md_b, "8/26 (今日 14:42)")

# 汇总对比
print(f"\n{'='*60}")
print(f"  汇总对比")
print(f"{'='*60}")

print(f"\n  {'指标':<20} {'8/25':<12} {'8/26':<12} {'差异'}")
print(f"  {'-'*55}")
print(f"  {'页面数':<18} {stats_a['pages']:<12} {stats_b['pages']:<12} {'+' if stats_b['pages']>stats_a['pages'] else ''}{stats_b['pages']-stats_a['pages']}")
print(f"  {'操作状态总数':<16} {stats_a['ops_total']:<12} {stats_b['ops_total']:<12} {'+' if stats_b['ops_total']>stats_a['ops_total'] else ''}{stats_b['ops_total']-stats_a['ops_total']}")
print(f"  {'基金+金额配对':<16} {stats_a['transactions']:<12} {stats_b['transactions']:<12} {'+' if stats_b['transactions']>stats_a['transactions'] else ''}{stats_b['transactions']-stats_a['transactions']}")
print(f"  {'唯一交易数':<16} {stats_a['unique_tx']:<12} {stats_b['unique_tx']:<12} {'+' if stats_b['unique_tx']>stats_a['unique_tx'] else ''}{stats_b['unique_tx']-stats_a['unique_tx']}")
print(f"  {'重复率':<16} {stats_a['dup_rate']:.1f}%{'':<8} {stats_b['dup_rate']:.1f}%")

# 操作类型对比
print(f"\n  【操作类型对比】")
all_types = set(list(stats_a['op_types'].keys()) + list(stats_b['op_types'].keys()))
for t in sorted(all_types):
    a = stats_a['op_types'].get(t, 0)
    b = stats_b['op_types'].get(t, 0)
    diff = b - a
    print(f"    {t:<12} 8/25={a:>4}  8/26={b:>4}  {'↑' if diff>0 else '↓' if diff<0 else '·'} {abs(diff)}")

# 结论
print(f"\n{'='*60}")
if stats_b['dup_rate'] > stats_a['dup_rate'] * 2 and stats_b['dup_rate'] > 10:
    print("  ⚠ 今日重复率明显偏高，存在大量重复数据！")
elif stats_b['unique_tx'] > stats_a['unique_tx'] * 2:
    print("  ✓ 今日采集了更多唯一交易，数据质量正常")
else:
    print("  ℹ 今日数据量差异需要人工判断")
