"""Detailed orphan check"""
import sys, logging
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

from src.parser.rule_parser import extract_segments_from_md
from src.parser.deduplicator import deduplicate

md_path = "output/screen_dump_20260811_144955.md"
with open(md_path, "r", encoding="utf-8") as f:
    md_text = f.read()

raw = extract_segments_from_md(md_text, md_path=md_path)
print(f"Raw: {len(raw)} records")
deduped = deduplicate(raw)
print(f"Deduped: {len(deduped)} records")

# Show unknown KOL records
unknown = [r for r in deduped if r.get("kol_name") in ("未知KOL", None, "") or not r.get("kol_name")]
print(f"\n未知KOL: {len(unknown)} records:")
for i, r in enumerate(unknown):
    print(f"  {i}: page={r.get('page')} time={r.get('publish_time')} op={r.get('operation_type')} fund={r.get('fund_name')} buy={r.get('buy_amount')} sell={r.get('sell_shares')} today={r.get('today_operation_count')} candidate={r.get('kol_candidate')}")

# Named KOL counts
from collections import Counter
kol_counts = Counter(r.get("kol_name") for r in deduped if r.get("kol_name") != "未知KOL")
print(f"\n命名KOL分布:")
for k, v in kol_counts.most_common():
    print(f"  {k}: {v}")
