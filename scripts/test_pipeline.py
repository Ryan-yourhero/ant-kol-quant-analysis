"""完整流程测试：MD → AI 解析 → 校验去重 → Excel"""
import sys, logging, time
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

from src.parser.excel_exporter import parse_md_to_excel

md_path = "output/screen_dump_20260811_144955.md"
print(f"输入: {md_path}")

t0 = time.time()
excel_path, result = parse_md_to_excel(md_path)
elapsed = time.time() - t0

print(f"\n=== 结果 ===")
print(f"Excel: {excel_path}")
print(f"记录数: {result.total_records}")
print(f"AI 辅助: {result.ai_used}")
print(f"总耗时: {elapsed:.1f}s")

# 按大V统计
kol_counts = {}
for r in result.records:
    k = r.kol_name or "未知KOL"
    kol_counts[k] = kol_counts.get(k, 0) + 1
print(f"\n大V分布 ({len(result.records)}条):")
for k, v in sorted(kol_counts.items(), key=lambda x: -x[1]):
    print(f"  {k}: {v}")

# 检查 collect_time
if result.records:
    sample = result.records[0]
    print(f"\n采集时间: {sample.collect_time}")
