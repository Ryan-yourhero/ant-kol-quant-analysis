import sys
sys.path.insert(0, ".")

from src.parser.rule_parser import extract_segments_from_md
from src.parser.deduplicator import deduplicate
from src.parser.ai_parser import _dicts_to_records
from src.parser.excel_exporter import export_to_excel

with open("output/screen_dump_20260811_144955.md", encoding="utf-8") as f:
    t = f.read()

raw = extract_segments_from_md(t, md_path="output/screen_dump_20260811_144955.md")
print(f"Rule: {len(raw)}")
raw = deduplicate(raw)
print(f"Dedup: {len(raw)}")
recs = _dicts_to_records(raw)

def sk(r):
    n = r.kol_name or ""
    return (n.replace("(待定)", ""), "(待定)" in n, n)
recs.sort(key=sk)

export_to_excel(recs, "output/20260811.xlsx")

for k, v in sorted({(r.kol_name or "??"): 0 for r in recs}.items()):
    pass
for r in recs:
    pass
print("Done")
