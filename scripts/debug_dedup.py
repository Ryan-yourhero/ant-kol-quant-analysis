"""Debug: check which records are being deduped"""
import sys
sys.path.insert(0, ".")

from src.parser.rule_parser import extract_segments_from_md
from src.parser.ai_parser import _dicts_to_records
from src.parser.deduplicator import _record_key
from collections import Counter

md_path = "output/screen_dump_20260811_144955.md"
with open(md_path, "r", encoding="utf-8") as f:
    md_text = f.read()

raw_records = extract_segments_from_md(md_text, md_path=md_path)

# Check Bells
bells = [r for r in raw_records if r.get("kol_name") == "Bells"]
print(f"=== Bells: {len(bells)} raw records ===")
for i, r in enumerate(bells):
    key = _record_key(r)
    print(f"  {i}: key={key[:60]}... page={r.get('page')}")

jinyin = [r for r in raw_records if r.get("kol_name") == "金银VS美丽Gu武恭"]
print(f"\n=== 金银VS美丽Gu武恭: {len(jinyin)} raw records ===")
for i, r in enumerate(jinyin):
    key = _record_key(r)
    print(f"  {i}: key={key[:60]}... page={r.get('page')}")

# Check all duplicate keys
print("\n=== All duplicate keys ===")
keys = [_record_key(r) for r in raw_records]
dup_keys = {k: v for k, v in Counter(keys).items() if v > 1}
for k, count in dup_keys.items():
    matching = [(i, r.get("kol_name"), r.get("fund_name"), r.get("page"))
                for i, r in enumerate(raw_records) if _record_key(r) == k]
    print(f"  key={k[:60]}... (x{count})")
    for m in matching:
        print(f"    idx={m[0]} kol={m[1]} fund={m[2]} page={m[3]}")
