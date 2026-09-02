import sys
sys.path.insert(0, ".")
from src.parser.rule_parser import extract_segments_from_md
from src.parser.deduplicator import deduplicate

f = open("output/screen_dump_20260811_144955.md", encoding="utf-8")
t = f.read()
f.close()
raw = extract_segments_from_md(t, md_path="output/screen_dump_20260811_144955.md")
raw = deduplicate(raw)

bells = [r for r in raw if r.get("kol_name") == "Bells"]
print(f"Bells records: {len(bells)}")
for r in bells:
    print(f"  op_count={r.get('today_operation_count')}, fund={r.get('fund_name')}, amt={r.get('buy_amount')}")

anon = [r for r in raw if (r.get("kol_name") or "") in ("未知KOL", "", "(未知)")]
print(f"\nAnonymous: {len(anon)}")
for r in anon[:15]:
    print(f"  candidate={r.get('kol_candidate')}, fund={r.get('fund_name')}, amt={r.get('buy_amount')}, sell={r.get('sell_shares')}")
