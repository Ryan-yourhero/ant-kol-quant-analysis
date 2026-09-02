"""Output to different name"""
import sys, logging
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

from src.parser import parse_md_to_excel
import os

path, result = parse_md_to_excel(
    "output/screen_dump_20260811_144955.md", 
    use_ai=False,  # skip AI, just re-export with sort
    excel_dir="output",
)

# Overwrite with different name
if path and os.path.exists(path):
    target = path.replace(".xlsx", "_v2.xlsx")
    os.replace(path, target)
    print(f"Excel: {target}")

kol_counts = {}
for r in result.records:
    k = r.kol_name or "匿名"
    kol_counts[k] = kol_counts.get(k, 0) + 1

for k, v in sorted(kol_counts.items(), key=lambda x: -x[1]):
    print(f"  {k}: {v}")
