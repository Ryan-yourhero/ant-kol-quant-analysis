"""Full AI test with orphan matching prompt"""
import sys, logging
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

from src.parser import parse_md_to_excel

md_path = "output/screen_dump_20260811_144955.md"

print("AI 解析 + 孤儿匹配")
print("=" * 60)
try:
    excel_path, result = parse_md_to_excel(md_path, use_ai=True)
except PermissionError:
    excel_path = None
    result = None
    print("Excel 文件被锁定，请关闭 Excel 后重试")

if excel_path:
    print(f"\nExcel: {excel_path}")
    print(f"总记录: {result.total_records}")
    print(f"AI 辅助: {'是' if result.ai_used else '否'}")

    kol_counts = {}
    for r in result.records:
        k = r.kol_name or "未知KOL"
        kol_counts[k] = kol_counts.get(k, 0) + 1
    print("大V分布:")
    for k, v in sorted(kol_counts.items(), key=lambda x: -x[1]):
        marker = ""
        if k == "未知KOL":
            marker = " [仍需匹配]"
        elif k == "Bells" and v == 4:
            marker = " [OK: 2+2孤儿]"
        elif k == "金银VS美丽Gu武恭" and v == 4:
            marker = " [OK: 2+2孤儿]"
        print(f"  {k}: {v}{marker}")
else:
    print("\n请关闭 Excel 后重试")
