"""Run AI parse, output to output/"""
import sys, logging
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

from src.parser import parse_md_to_excel

path, result = parse_md_to_excel("output/screen_dump_20260811_144955.md", use_ai=True)

print(f"\nExcel: {path}")
print(f"Records: {result.total_records}")
print(f"AI: {result.ai_used}")

kol_counts = {}
for r in result.records:
    k = r.kol_name or "匿名"
    kol_counts[k] = kol_counts.get(k, 0) + 1

print("\n大V分布:")
for k, v in sorted(kol_counts.items(), key=lambda x: -x[1]):
    print(f"  {k}: {v}")
