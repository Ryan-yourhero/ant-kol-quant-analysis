"""Full test with AI, generate corrected Excel"""
import sys, logging
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

from src.parser import parse_md_to_excel

md_path = "output/screen_dump_20260811_144955.md"
excel_path = "data/excel/20260811_v2.xlsx"

print("=" * 60)
print("AI 解析（含孤儿操作匹配 + 去重修复）")
print("=" * 60)
excel_path, result = parse_md_to_excel(md_path, use_ai=True)

print(f"\nExcel: {excel_path}")
print(f"总记录: {result.total_records}")
print(f"AI 辅助: {'是' if result.ai_used else '否'}")

kol_counts = {}
for r in result.records:
    k = r.kol_name or "未知KOL"
    kol_counts[k] = kol_counts.get(k, 0) + 1
print("大V分布:")
for k, v in sorted(kol_counts.items(), key=lambda x: -x[1]):
    print(f"  {k}: {v}")
