"""
恢复历史数据：从 output/*.xlsx 导入到 MySQL

用途：
  MySQL 未启动期间爬虫写入全部失败，历史记录为空。
  启动 MySQL 后，用本脚本把已有的 Excel 解析结果重新导入 MySQL。

用法：
  python scripts/restore_db.py
"""

import os
import sys
import glob
import re
import logging
from datetime import datetime

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger("restore")

import openpyxl
from src.parser.models import TradeRecord
from src.storage.db_storage import (
    init_db, save_records, is_configured, _get_session,
)
from src.storage.models import CrawlRun, Kol, Post, Operation

OUTPUT_DIR = os.path.join(_PROJECT_ROOT, "output")

# Excel 列顺序（与 excel_exporter.EXCEL_COLUMNS 一致）
_COL_KEYS = [
    "kol_name", "yield_rate", "publish_time", "opinion_text",
    "operation_type", "operation_status", "fund_name",
    "buy_amount", "sell_shares", "collect_time", "remark",
]


def read_excel_records(xlsx_path: str) -> list:
    """读取一个 Excel，返回 TradeRecord 列表。"""
    wb = openpyxl.load_workbook(xlsx_path, read_only=True)
    ws = wb.active
    records = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        vals = list(row) + [None] * len(_COL_KEYS)
        data = {_COL_KEYS[i]: (vals[i] if i < len(vals) else None)
                for i in range(len(_COL_KEYS))}
        # 跳过全空行
        if not any(data.get(k) for k in ("kol_name", "fund_name", "buy_amount", "sell_shares")):
            continue
        try:
            records.append(TradeRecord(**data))
        except Exception as e:
            logger.warning("记录转换失败 %s: %s", os.path.basename(xlsx_path), e)
    wb.close()
    return records


def _date_from_filename(path: str) -> str:
    m = re.search(r"(\d{8})", os.path.basename(path))
    return m.group(1) if m else datetime.now().strftime("%Y%m%d")


def find_md_for_date(date_str: str) -> str:
    """找到指定日期最新的一份 screen_dump MD。"""
    md_files = sorted(glob.glob(os.path.join(OUTPUT_DIR, f"screen_dump_{date_str}_*.md")))
    return md_files[-1] if md_files else None


def main():
    if not is_configured():
        print("MySQL 未配置（.env 缺少 MYSQL_HOST），退出")
        return

    # 1. 初始化数据库
    init_db()

    # 2. 打印当前状态
    session = _get_session()
    try:
        print(f"[DB] 导入前: runs={session.query(CrawlRun).count()} "
              f"kols={session.query(Kol).count()} "
              f"posts={session.query(Post).count()} "
              f"operations={session.query(Operation).count()}")
    finally:
        session.close()

    # 3. 遍历 Excel 导入
    xlsx_files = sorted(glob.glob(os.path.join(OUTPUT_DIR, "*.xlsx")))
    total_imported = 0
    for xlsx in xlsx_files:
        name = os.path.basename(xlsx)
        records = read_excel_records(xlsx)
        print(f"\n[导入] {name}: {len(records)} 条记录")
        if not records:
            continue

        # 补全 collect_time（保证日期正确）
        date_str = _date_from_filename(xlsx)
        for r in records:
            if not r.collect_time:
                r.collect_time = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}T00:00:00"

        # 找对应 MD（用于 md5 去重与 md_path）
        md_path = find_md_for_date(date_str)
        if md_path:
            with open(md_path, encoding="utf-8") as f:
                md_text = f.read()
        else:
            # 无 MD：用 Excel 内容生成唯一 md_text
            md_text = f"EXCEL:{name}:{len(records)}"
            md_path = xlsx
            print("  (无对应 MD，使用 Excel 标识作为去重键)")

        ok = save_records(records, md_text, md_path)
        if ok:
            total_imported += len(records)
            print(f"  ✓ 写入成功 {len(records)} 条")
        else:
            print("  - 跳过（已存在）或失败")

    # 4. 复查
    session = _get_session()
    try:
        print(f"\n[DB] 导入后: runs={session.query(CrawlRun).count()} "
              f"kols={session.query(Kol).count()} "
              f"posts={session.query(Post).count()} "
              f"operations={session.query(Operation).count()}")
    finally:
        session.close()

    print(f"\n完成，共导入 {total_imported} 条操作")


if __name__ == "__main__":
    main()
