"""对比两个特定 MD 文件的质量指标"""
import re
import os
import sys
from collections import Counter

FILE_A = r"e:\PM\PM\KOL-RICH\output\screen_dump_20260825_144551.md"
FILE_B = r"e:\PM\PM\KOL-RICH\output\screen_dump_20260826_144249.md"

def load_md(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()

def analyze(content, path, label):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"  文件: {os.path.basename(path)}")
    print(f"{'='*60}")

    # 1. 基本统计
    lines = content.strip().split('\n')
    page_headers = re.findall(r'# 页面(\d+)', content)
    non_empty_lines = [l for l in lines if l.strip()]
    total_ops_found = re.findall(r'(买入确认中|卖出确认中|定投确认中|撤销确认中|转换确认中)', content)
    amounts = re.findall(r'(买入金额\(元\)|卖出份额\(份\))', content)
    amount_values = re.findall(r'\d{1,3}(?:,\d{3})*(?:\.\d{1,2})', content)
    fund_names = re.findall(r'(\S+?)(?:买入金额\(元\)|卖出份额\(份\))', content)

    # 2. 操作类型分布
    op_counter = Counter(total_ops_found)
    op_total = len(total_ops_found)

    # 3. 字段配对完整性
    # 每个操作状态后面应该跟: 基金名 + 金额/份额标签 + 金额值 + 查看详情
    complete_pairs = 0
    broken_pairs = []
    for i, op in enumerate(total_ops_found):
        # 在 op 之后查找基金名+金额+值
        start_pos = content.find(op)
        segment = content[start_pos:start_pos+200]
        has_amount_label = bool(re.search(r'买入金额\(元\)|卖出份额\(份\)', segment))
        has_amount_value = bool(re.search(r'\d+(?:,\d{3})*\.\d{2}', segment))
        has_fund = bool(re.search(r'[^\s]{4,}', segment.split('\n')[1] if '\n' in segment else ''))
        if has_amount_label and has_amount_value:
            complete_pairs += 1
        else:
            broken_pairs.append({
                'index': i,
                'type': op,
                'has_label': has_amount_label,
                'has_value': has_amount_value,
                'context': segment[:80].replace('\n', '|')
            })

    # 4. 噪音统计
    nav_items = ['关注', '发现', '讨论区', '热议话题', '学理财', '资讯', '同路人', '真实财有趣', '我的关注']
    nav_count = sum(len(re.findall(f'^{item}$', content, re.MULTILINE)) for item in nav_items)
    expand_btns = len(re.findall(r'展开今日全部\d+条操作', content))
    scroll_hints = len(re.findall(r'记一下|返回|理财盘友圈|更多', content))
    image_urls = len(re.findall(r'img\?fileid=|100w\?bz=', content))
    noise_lines = nav_count + expand_btns + scroll_hints + image_urls

    # 5. KOL 覆盖
    kol_names = []
    # 模式: KOL名 + 近一年收益率  的结构
    kol_blocks = re.findall(r'(\w+)\n近一年收益率', content)
    kol_counter = Counter(kol_blocks)

    # 6. 翻页分析
    page_numbers = [int(p) for p in page_headers]
    missing_pages = []
    if page_numbers:
        for i in range(1, max(page_numbers)+1):
            if i not in page_numbers and i % 2 == 1:  # 页面应该是连续奇数
                missing_pages.append(i)

    print(f"\n  【基本统计】")
    print(f"    总行数: {len(lines)}")
    print(f"    非空行数: {len(non_empty_lines)}")
    print(f"    页面数: {len(page_headers)} (页码: {page_numbers})")
    if missing_pages:
        print(f"    ⚠ 缺失页面: {missing_pages}")

    print(f"\n  【交易状态分布】")
    for k, v in op_counter.most_common():
        pct = v/op_total*100 if op_total else 0
        print(f"    {k}: {v} ({pct:.1f}%)")
    print(f"    操作状态总数: {op_total}")

    print(f"\n  【字段完整度】")
    print(f"    操作状态 × 金额/份额标签: {min(op_total, len(amounts))} / {op_total}")
    print(f"    完整配对数 (状态+标签+值): {complete_pairs} / {op_total}")
    if broken_pairs:
        print(f"    ⚠ 残缺配对: {len(broken_pairs)} 处")
        for bp in broken_pairs[:5]:
            print(f"      #{bp['index']} {bp['type']} label={bp['has_label']} value={bp['has_value']} ...{bp['context']}")
        if len(broken_pairs) > 5:
            print(f"      ... 还有 {len(broken_pairs)-5} 处")

    print(f"\n  【噪音分析】")
    print(f"    导航栏行: {nav_count}")
    print(f"    展开按钮残留: {expand_btns} 处")
    print(f"    UI元素(记一下/返回等): {scroll_hints}")
    print(f"    图片/缩略图URL: {image_urls}")
    print(f"    噪音行合计: {noise_lines}")

    # 7. 每 KOL 操作数
    print(f"\n  【KOL 覆盖】")
    print(f"    发现 KOL: {len(kol_counter)} 位")
    for k, v in kol_counter.most_common():
        print(f"      {k}: {v} 次出现")

    # 8. 估算 AI 解析可用率
    # 每个完整配对 ~6 行（状态+基金名+标签+值+查看详情+转发）
    usable_lines = complete_pairs * 6
    total_content_lines = len(non_empty_lines) - noise_lines
    if total_content_lines > 0:
        print(f"\n  【质量评估】")
        print(f"    可用交易记录: {complete_pairs}")
        print(f"    完整率: {complete_pairs/op_total*100:.1f}%" if op_total else "    完整率: N/A")
        print(f"    噪音占比: {noise_lines/len(non_empty_lines)*100:.1f}%")

    return {
        'file': os.path.basename(path),
        'pages': len(page_headers),
        'op_total': op_total,
        'complete_pairs': complete_pairs,
        'completeness_rate': complete_pairs/op_total if op_total else 0,
        'noise_lines': noise_lines,
        'kol_count': len(kol_counter),
        'op_types': dict(op_counter),
    }


# ===== 执行对比 =====
print("=" * 70)
print("  MD 质量对比工具")
print("=" * 70)

stats_a = analyze(load_md(FILE_A), FILE_A, "A. 昨日 (8/25 14:45)")
stats_b = analyze(load_md(FILE_B), FILE_B, "B. 今日 (8/26 14:42)")

# ===== 汇总对比 =====
print(f"\n{'='*70}")
print(f"  对比汇总")
print(f"{'='*70}")

print(f"\n  {'指标':<20} {'8/25 (昨日)':<15} {'8/26 (今日)':<15} {'变化':<10}")
print(f"  {'-'*60}")

def row(name, a, b, unit=""):
    if isinstance(a, float) or isinstance(b, float):
        diff = (b - a) / a * 100 if a else 0
        change = f"{diff:+.1f}%"
    else:
        change = f"{b - a:+d}" if isinstance(a, int) else "-"
    print(f"  {name:<18} {str(a)+unit:<15} {str(b)+unit:<15} {change:<10}")

row("页面数", stats_a['pages'], stats_b['pages'])
row("操作状态总数", stats_a['op_total'], stats_b['op_total'])
row("完整配对数", stats_a['complete_pairs'], stats_b['complete_pairs'])
row("字段完整率", f"{stats_a['completeness_rate']*100:.1f}%", f"{stats_b['completeness_rate']*100:.1f}%")
row("噪音行数", stats_a['noise_lines'], stats_b['noise_lines'])
row("KOL 覆盖数", stats_a['kol_count'], stats_b['kol_count'])

# 操作类型对比
print(f"\n  【操作类型分布对比】")
all_types = set(list(stats_a['op_types'].keys()) + list(stats_b['op_types'].keys()))
for t in sorted(all_types):
    a = stats_a['op_types'].get(t, 0)
    b = stats_b['op_types'].get(t, 0)
    print(f"    {t:<12} 8/25={a:>4}  8/26={b:>4}  {'↑' if b > a else '↓' if b < a else '·'} {abs(b-a)}")

# 结论
print(f"\n{'='*70}")
if stats_b['completeness_rate'] < stats_a['completeness_rate'] * 0.9:
    print("  ⚠ 今日爬虫字段完整率明显下降，可能存在问题！")
elif stats_b['op_total'] < stats_a['op_total'] * 0.7:
    print("  ⚠ 今日采集到的操作数量明显减少，可能存在遗漏！")
else:
    print("  ✓ 今日爬虫质量与昨日相当")
