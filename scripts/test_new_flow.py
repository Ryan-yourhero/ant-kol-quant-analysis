import sys, logging
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

from src.parser.ai_parser import parse_full_md

with open("output/screen_dump_20260811_144955.md", encoding="utf-8") as f:
    md_text = f.read()

print(f"MD: {len(md_text)} 字符")
records = parse_full_md(md_text)

kol_counts = {}
for r in records:
    k = r.kol_name or "未知KOL"
    kol_counts[k] = kol_counts.get(k, 0) + 1

print(f"\nTotal: {len(records)} 条")
for k, v in sorted(kol_counts.items(), key=lambda x: -x[1]):
    print(f"  {k}: {v}")
