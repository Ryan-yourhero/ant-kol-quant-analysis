"""
基金方向主库导入脚本
======================

来源：人工整理的 Excel（如 fund_direction_master_全量最终分类_20260911.xlsx）
目标：fund_direction_master 表

原则（来自用户指令）：
  - 本次 Excel 视为权威结果
  - classification_source = "manual" / confidence = "high" / verified = true
  - 不调用 Tavily / DuckDuckGo / LLM / 关键词分类 / 不基于 opinion_text
  - 仅按 Excel 内容写入数据库
  - 已有库记录按 fund_code → normalized_name → fund_name 优先级匹配更新

执行：
  python scripts/import_fund_direction_master.py "fund_direction_master_全量最终分类_20260911.xlsx" --dry-run
  python scripts/import_fund_direction_master.py "fund_direction_master_全量最终分类_20260911.xlsx" --apply
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# 允许脚本直接执行
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("import_fund_directions")


# ============================================================
#  读取 Excel
# ============================================================

def read_excel(path: str) -> List[Dict[str, Any]]:
    """读取 Excel 基金方向 sheet → 标准化为 dict 列表。"""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True)
    if "基金方向" not in wb.sheetnames:
        raise ValueError(f"Excel 缺少 '基金方向' sheet，实际: {wb.sheetnames}")
    ws = wb["基金方向"]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    headers = list(rows[0])
    out = []
    for r in rows[1:]:
        if r is None or all(c is None for c in r):
            continue
        d = dict(zip(headers, r))
        # 字段名兼容
        d = {k: v for k, v in d.items() if k}
        # 必填字段
        fn = (d.get("fund_name") or "").strip()
        if not fn:
            continue
        # 测试基金 过滤
        if fn.startswith("测试") or "测试基金" in fn:
            logger.warning("跳过测试基金: %s", fn)
            continue
        d["fund_name"] = fn
        d["fund_code"] = (d.get("fund_code") or None) or None
        if d["fund_code"] is not None:
            d["fund_code"] = str(d["fund_code"]).strip()
        d["direction"] = (d.get("direction") or "").strip()
        d["sub_direction"] = (d.get("sub_direction") or None) or None
        d["fund_type"] = (d.get("fund_type") or None) or None
        d["fund_type_detail"] = (d.get("fund_type_detail") or None) or None
        d["evidence_period"] = (d.get("evidence_period") or None) or None
        d["source_url"] = (d.get("source_url") or None) or None
        d["evidence"] = (d.get("最终分类依据") or d.get("evidence") or None) or None
        out.append(d)
    return out


# ============================================================
#  标准化（与 repo 对齐）
# ============================================================

def normalize_name(name: str) -> str:
    from backend.services.fund_direction_repo import normalize_fund_name
    return normalize_fund_name(name)


# ============================================================
#  备份
# ============================================================

def backup_current_db(backup_dir: str) -> str:
    """把当前 fund_direction_master 导出为 CSV/Excel 作为备份。"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    from src.storage.db_storage import _get_session
    from src.storage.models import FundDirectionMaster
    from backend.services import fund_direction_repo as repo

    os.makedirs(backup_dir, exist_ok=True)
    session = _get_session()
    try:
        rows = session.query(FundDirectionMaster).order_by(FundDirectionMaster.id).all()
        data = [repo._to_dict(r) for r in rows]
    finally:
        session.close()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(backup_dir, f"fund_direction_master_backup_{ts}.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "fund_direction_master"
    if data:
        headers = list(data[0].keys())
        ws.append(headers)
        for r in data:
            ws.append([r.get(h, "") for h in headers])
    wb.save(path)
    logger.info("已备份 %d 行 → %s", len(data), path)
    return path


# ============================================================
#  导入主逻辑
# ============================================================

# 写入时统一为：source=manual, confidence=high, verified=true
SOURCE = "manual"
CONFIDENCE = "high"
VERIFIED = True


def import_one(record: Dict[str, Any], session) -> str:
    """处理一行 Excel → DB。

    Returns: "new" | "updated" | "skipped" | "error:<msg>"
    """
    from src.storage.models import FundDirectionMaster
    from backend.services import fund_direction_repo as repo

    fund_name = record["fund_name"]
    fund_code = record.get("fund_code")
    norm = normalize_name(fund_name)
    if not norm:
        return f"error:fund_name 无法标准化: {fund_name!r}"

    # 1) 匹配优先级: fund_code → normalized_name → fund_name
    matched = None
    if fund_code:
        matched = session.query(FundDirectionMaster).filter_by(fund_code=fund_code).first()
    if matched is None:
        matched = session.query(FundDirectionMaster).filter_by(normalized_name=norm).first()
    if matched is None:
        matched = session.query(FundDirectionMaster).filter_by(fund_name=fund_name).first()

    now = datetime.now()
    direction = record.get("direction") or "其他/待分类"
    sub_direction = record.get("sub_direction")
    fund_type = record.get("fund_type")
    fund_type_detail = record.get("fund_type_detail")
    evidence = record.get("evidence")
    source_url = record.get("source_url")
    evidence_period = record.get("evidence_period")

    if matched is None:
        # 新增
        rec = FundDirectionMaster(
            fund_name=fund_name,
            fund_code=fund_code,
            normalized_name=norm,
            direction=direction,
            sub_direction=sub_direction,
            fund_type=fund_type,
            fund_type_detail=fund_type_detail,
            classification_source=SOURCE,
            confidence=CONFIDENCE,
            evidence=evidence,
            source_url=source_url,
            evidence_period=evidence_period,
            verified=VERIFIED,
            first_seen_at=now,
            last_verified_at=now,
            last_seen_at=now,
            seen_count=1,
        )
        session.add(rec)
        return "new"

    # 更新：manual 是最高优先级 → 直接覆盖
    matched.fund_name = fund_name
    if fund_code:
        matched.fund_code = fund_code
    matched.normalized_name = norm
    matched.direction = direction
    matched.sub_direction = sub_direction
    matched.fund_type = fund_type
    matched.fund_type_detail = fund_type_detail
    matched.classification_source = SOURCE
    matched.confidence = CONFIDENCE
    matched.evidence = evidence
    matched.source_url = source_url
    matched.evidence_period = evidence_period
    matched.verified = VERIFIED
    matched.last_verified_at = now
    matched.last_seen_at = now
    return "updated"


def run_import(excel_path: str, dry_run: bool) -> Dict[str, Any]:
    """执行导入流程。

    Returns: 统计字典（含 new/updated/skipped/error/preview 列表）
    """
    from src.storage.db_storage import _get_session

    records = read_excel(excel_path)
    logger.info("Excel 读取到 %d 行（已过滤测试基金）", len(records))

    # Excel 内部去重校验
    seen_norms = Counter()
    for r in records:
        seen_norms[normalize_name(r["fund_name"])] += 1
    dups = {k: v for k, v in seen_norms.items() if v > 1}
    if dups:
        logger.warning("Excel 内部存在重复 normalized_name: %s", dups)

    if dry_run:
        # 预览
        session = _get_session()
        try:
            stats = {"new": 0, "updated": 0, "skipped": 0, "error": 0}
            preview = []
            for r in records:
                fund_code = r.get("fund_code")
                norm = normalize_name(r["fund_name"])
                matched = None
                if fund_code:
                    matched = session.query.filter_by(fund_code=fund_code) if False else None  # placeholder
                # 用 ORM
                from src.storage.models import FundDirectionMaster
                q = session.query(FundDirectionMaster)
                m = q.filter_by(fund_code=fund_code).first() if fund_code else None
                if m is None:
                    m = q.filter_by(normalized_name=norm).first()
                if m is None:
                    m = q.filter_by(fund_name=r["fund_name"]).first()
                if m is None:
                    stats["new"] += 1
                    action = "new"
                else:
                    stats["updated"] += 1
                    action = "updated"
                preview.append({
                    "fund_name": r["fund_name"],
                    "fund_code": r.get("fund_code") or "-",
                    "direction": r.get("direction"),
                    "matched": m.fund_name if m else None,
                    "matched_id": m.id if m else None,
                    "action": action,
                })
            return {
                "stats": stats,
                "preview": preview,
                "duplicates": dups,
                "total": len(records),
                "dry_run": True,
            }
        finally:
            session.close()

    # 正式写入（不开 transaction，整个批次一起 commit）
    session = _get_session()
    try:
        stats = {"new": 0, "updated": 0, "skipped": 0, "error": 0}
        errors = []
        for r in records:
            try:
                action = import_one(r, session)
                if action == "new":
                    stats["new"] += 1
                elif action == "updated":
                    stats["updated"] += 1
                elif action.startswith("error"):
                    stats["error"] += 1
                    errors.append({"fund": r["fund_name"], "msg": action})
                else:
                    stats[action] = stats.get(action, 0) + 1
            except Exception as e:  # noqa: BLE001
                stats["error"] += 1
                errors.append({"fund": r["fund_name"], "msg": str(e)})
        session.commit()
        return {
            "stats": stats,
            "errors": errors,
            "duplicates": dups,
            "total": len(records),
            "dry_run": False,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ============================================================
#  导入后校验
# ============================================================

def post_import_verify(excel_path: str) -> Dict[str, Any]:
    """导入后 10 项校验。"""
    from src.storage.db_storage import _get_session
    from src.storage.models import FundDirectionMaster

    excel_records = read_excel(excel_path)
    excel_norms = {normalize_name(r["fund_name"]): r for r in excel_records}

    session = _get_session()
    try:
        db_rows = session.query(FundDirectionMaster).all()
        db_by_norm = {r.normalized_name: r for r in db_rows}
        db_by_code = {r.fund_code: r for r in db_rows if r.fund_code}
        db_by_name = {r.fund_name: r for r in db_rows}

        # 1) Excel 中全部正式基金均存在于 DB
        missing = [n for n in excel_norms if n not in db_by_norm]

        # 2) direction 与 Excel 完全一致
        dir_mismatch = []
        for n, ex in excel_norms.items():
            if n in db_by_norm:
                if db_by_norm[n].direction != ex.get("direction"):
                    dir_mismatch.append({
                        "fund": ex["fund_name"],
                        "excel_dir": ex.get("direction"),
                        "db_dir": db_by_norm[n].direction,
                    })

        # 3) classification_source = manual
        non_manual = [
            {"fund": r.fund_name, "source": r.classification_source, "norm": r.normalized_name}
            for r in db_rows if r.normalized_name in excel_norms and r.classification_source != "manual"
        ]

        # 4) confidence = high
        non_high = [
            {"fund": r.fund_name, "conf": r.confidence, "norm": r.normalized_name}
            for r in db_rows if r.normalized_name in excel_norms and r.confidence != "high"
        ]

        # 5) verified = true
        not_verified = [
            {"fund": r.fund_name, "verified": r.verified, "norm": r.normalized_name}
            for r in db_rows if r.normalized_name in excel_norms and not r.verified
        ]

        # 6) 无测试基金
        test_rows = [r for r in db_rows if "测试" in r.fund_name]

        # 7) 无重复基金（fund_code 唯一 + normalized_name 唯一）
        code_dups = {c: n for c, n in Counter([r.fund_code for r in db_rows if r.fund_code]).items() if n > 1}
        norm_dups = {n: c for n, c in Counter([r.normalized_name for r in db_rows]).items() if c > 1}

        # 8) 同一基金不得因名称截断产生多条
        #    （按"去前 6 字"分组，对比同组下多条记录）
        truncated_groups = defaultdict(list)
        for r in db_rows:
            n = r.normalized_name
            if len(n) > 6:
                key = n[:6]
                truncated_groups[key].append(r)
        multi_truncated = {k: v for k, v in truncated_groups.items() if len(v) > 1}

        # 各 direction 数量
        dir_dist = Counter([r.direction for r in db_rows])

        return {
            "total_db": len(db_rows),
            "total_excel": len(excel_records),
            "missing_in_db": missing,
            "direction_mismatch": dir_mismatch,
            "non_manual_classified": non_manual,
            "non_high_confidence": non_high,
            "not_verified": not_verified,
            "test_funds": test_rows,
            "duplicate_fund_codes": code_dups,
            "duplicate_normalized_names": norm_dups,
            "truncated_multi_records": multi_truncated,
            "direction_distribution": dict(dir_dist),
        }
    finally:
        session.close()


# ============================================================
#  CLI
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="基金方向主库导入（人工权威 Excel → fund_direction_master）"
    )
    parser.add_argument("excel", help="Excel 文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只预览不写库")
    parser.add_argument("--apply", action="store_true", help="正式写入")
    parser.add_argument("--backup-dir", default=None, help="备份目录（默认 output/backups）")
    args = parser.parse_args()

    if not args.dry_run and not args.apply:
        args.dry_run = True  # 默认 dry-run，安全
        logger.warning("未指定 --dry-run / --apply，默认 dry-run")

    if not os.path.exists(args.excel):
        logger.error("Excel 不存在: %s", args.excel)
        return 1

    # 1) 导入前备份
    backup_dir = args.backup_dir or os.path.join(BASE_DIR, "output", "backups")
    backup_path = backup_current_db(backup_dir)
    print(f"📦 已备份当前 fund_direction_master → {backup_path}")

    # 2) 导入前 DB 总数
    from src.storage.db_storage import _get_session
    from src.storage.models import FundDirectionMaster
    s = _get_session()
    try:
        pre_count = s.query(FundDirectionMaster).count()
    finally:
        s.close()
    print(f"\n📊 导入前 fund_direction_master 总数: {pre_count}")

    # 3) 跑导入
    result = run_import(args.excel, dry_run=args.dry_run)
    stats = result["stats"]
    print(f"\n📋 Excel 基金数: {result['total']}")
    print(f"   新增: {stats['new']}")
    print(f"   更新: {stats['updated']}")
    print(f"   跳过: {stats.get('skipped', 0)}")
    print(f"   异常: {stats['error']}")
    if result.get("duplicates"):
        print(f"   ⚠ Excel 内部重复: {result['duplicates']}")

    if args.dry_run:
        print("\n[DRY-RUN] 预览前 20 条：")
        for it in result.get("preview", [])[:20]:
            print(f"  [{it['action']:8s}] {it['fund_name']:30s} code={it['fund_code']:8s} → {it['direction']:18s}  matched={it['matched']}")
        if len(result.get("preview", [])) > 20:
            print(f"  ... 还有 {len(result['preview']) - 20} 条")
        return 0

    if result.get("errors"):
        print(f"\n⚠ 错误明细：")
        for e in result["errors"][:10]:
            print(f"  {e}")

    # 4) 导入后校验
    print(f"\n🔍 导入后 10 项校验 ...")
    verify = post_import_verify(args.excel)
    print(f"\n   1) Excel 基金数:           {verify['total_excel']}")
    print(f"   2) 导入后 DB 总数:        {verify['total_db']}")
    print(f"   3) Excel 基金 DB 缺失:    {len(verify['missing_in_db'])}")
    print(f"   4) direction 不一致:     {len(verify['direction_mismatch'])}")
    print(f"   5) classification_source != manual: {len(verify['non_manual_classified'])}")
    print(f"   6) confidence != high:   {len(verify['non_high_confidence'])}")
    print(f"   7) verified != true:      {len(verify['not_verified'])}")
    print(f"   8) 测试基金:              {len(verify['test_funds'])}")
    print(f"   9) fund_code 重复:       {len(verify['duplicate_fund_codes'])}")
    print(f"   10) normalized_name 重复: {len(verify['duplicate_normalized_names'])}")
    print(f"   11) 截断同组多条:         {len(verify['truncated_multi_records'])}")
    print(f"\n   各 direction 数量:")
    for d, n in sorted(verify["direction_distribution"].items(), key=lambda x: -x[1]):
        print(f"     {d:20s}  {n}")

    if verify["missing_in_db"]:
        print(f"\n   ⚠ DB 缺失: {verify['missing_in_db'][:5]}")
    if verify["direction_mismatch"]:
        print(f"\n   ⚠ direction 不一致:")
        for it in verify["direction_mismatch"][:5]:
            print(f"     {it}")
    if verify["non_manual_classified"]:
        print(f"\n   ⚠ non_manual:")
        for it in verify["non_manual_classified"][:5]:
            print(f"     {it}")
    if verify["duplicate_fund_codes"]:
        print(f"\n   ⚠ fund_code 重复: {verify['duplicate_fund_codes']}")
    if verify["duplicate_normalized_names"]:
        print(f"\n   ⚠ normalized_name 重复: {verify['duplicate_normalized_names']}")
    if verify["truncated_multi_records"]:
        print(f"\n   ⚠ 截断同组多条:")
        for k, v in verify["truncated_multi_records"].items():
            print(f"     {k}: {[r.fund_name for r in v]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
