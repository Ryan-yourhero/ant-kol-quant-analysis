"""完整 MD 解析测试（嵌套 JSON 结构）"""
import sys, logging, time
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

from src.parser.ai_parser import parse_full_md

with open("output/screen_dump_20260811_144955.md", encoding="utf-8") as f:
    md_text = f.read()

print(f"MD: {len(md_text)} 字符")
t0 = time.time()

try:
    records = parse_full_md(md_text)
    elapsed = time.time() - t0
    print(f"\n解析完成，耗时: {elapsed:.1f}s")
    
    kol_counts = {}
    for r in records:
        k = r.kol_name or "未知KOL"
        kol_counts[k] = kol_counts.get(k, 0) + 1
    
    print(f"Total: {len(records)} 条")
    for k, v in sorted(kol_counts.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")
    
    # 输出详情
    print("\n记录详情:")
    for r in records:
        print(f"  {r.kol_name:20s} | {r.operation_type} | {r.fund_name or '(转换)'} | 买入:{r.buy_amount or '-'} | 卖出:{r.sell_shares or '-'} | today:{r.today_operation_count or '-'}")
except Exception as e:
    import traceback
    traceback.print_exc()
    print(f"错误: {type(e).__name__}: {e}")
    print(f"耗时: {time.time()-t0:.1f}s")
