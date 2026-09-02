import sys, logging, re, openpyxl, os
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

from src.parser.rule_parser import extract_segments_from_md
from src.parser.deduplicator import deduplicate
from src.parser.ai_parser import ai_parse_records, _dicts_to_records
from src.parser.models import TradeRecord, ParseResult
from src.parser.excel_exporter import _write_excel

md_path = "output/screen_dump_20260811_144955.md"
with open(md_path, "r", encoding="utf-8") as f:
    md_text = f.read()

# Parse + dedup + AI
raw = extract_segments_from_md(md_text, md_path=md_path)
print(f"Rule: {len(raw)}")
raw = deduplicate(raw)
print(f"Dedup: {len(raw)}")
records, ai_used = ai_parse_records(raw, md_text)
print(f"AI Parsed: {len(records)}, AI={ai_used}")

# Sort
def sort_key(rec):
    name = rec.kol_name or ""
    base = name.replace("(待定)", "")
    pending = "(待定)" in name
    return (base, pending, name)

records.sort(key=sort_key)

# Export
out = "output/20260811.xlsx"
_write_excel(out, records)

kol_counts = {}
for r in records:
    k = r.kol_name or "匿名"
    kol_counts[k] = kol_counts.get(k, 0) + 1

print(f"\nExcel: {out}")
for k, v in sorted(kol_counts.items(), key=lambda x: -x[1]):
    print(f"  {k}: {v}")
