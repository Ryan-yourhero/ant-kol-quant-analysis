"""Verify Bells dedup fix - no AI for speed"""
import sys, logging
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

from src.parser import parse_md_to_excel

md_path = "output/screen_dump_20260811_144955.md"
excel_path, result = parse_md_to_excel(md_path, use_ai=False)

print(f"\nTotal: {result.total_records} records")
# Check Bells
bells = [r for r in result.records if r.kol_name == "Bells"]
print(f"Bells: {len(bells)} records")
for r in bells:
    print(f"  {r.operation_type} | {r.fund_name} | {r.buy_amount}")

jinyin = [r for r in result.records if r.kol_name == "金银VS美丽Gu武恭"]
print(f"金银VS美丽Gu武恭: {len(jinyin)} records")
for r in jinyin:
    print(f"  {r.operation_type} | {r.fund_name} | {r.buy_amount}")
