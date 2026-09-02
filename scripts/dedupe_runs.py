"""
同日期去重：每个 collect_date 只保留 id 最大的那一次 CrawlRun，其余整批级联删除。
事务保证：要么全删要么不动。
"""
import sys
import os

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from collections import defaultdict
from src.storage.db_storage import _get_session
from src.storage.models import CrawlRun, Operation, Post
from sqlalchemy import func


def main(target_date=None, dry_run=False):
    session = _get_session()
    try:
        runs = session.query(CrawlRun).order_by(
            CrawlRun.collect_date, CrawlRun.id
        ).all()

        by_date = defaultdict(list)
        for r in runs:
            by_date[r.collect_date].append(r)

        if target_date:
            import datetime as dt
            d = dt.date.fromisoformat(target_date)
            by_date = {d: by_date.get(d, [])}

        print("=== 待处理日期 ===")
        for date_val, rs in sorted(by_date.items()):
            ids = [r.id for r in rs]
            print(f"  {date_val}: runs={ids}")

        keep_ids = set()
        delete_ids = []
        for date_val, rs in sorted(by_date.items()):
            ids = [r.id for r in rs]
            keep = max(ids)
            keep_ids.add(keep)
            for i in ids:
                if i != keep:
                    delete_ids.append(i)

        print()
        print(f"保留 run_ids: {sorted(keep_ids)}")
        print(f"删除 run_ids: {delete_ids if delete_ids else '(无)'}")
        print()

        if not delete_ids:
            print("没有重复，无需处理")
            return

        for rid in delete_ids:
            r = session.get(CrawlRun, rid)
            op_c = (
                session.query(func.count(Operation.id))
                .filter(Operation.crawl_run_id == rid).scalar()
            )
            po_c = (
                session.query(func.count(Post.id))
                .filter(Post.crawl_run_id == rid).scalar()
            )
            print(
                f"  [计划] run_id={rid} ({r.collect_date}) "
                f"posts={po_c} ops={op_c}  {r.md_file_path}"
            )

        if dry_run:
            print()
            print("dry-run 模式，未执行删除")
            return

        print()
        print("开始事务删除...")
        for rid in delete_ids:
            r = session.get(CrawlRun, rid)
            if r:
                session.delete(r)

        session.commit()
        print("事务提交成功")

        print()
        print("=== 清理后 ===")
        runs_after = session.query(func.count(CrawlRun.id)).scalar()
        posts_after = session.query(func.count(Post.id)).scalar()
        ops_after = session.query(func.count(Operation.id)).scalar()
        print(f"总 runs={runs_after}  posts={posts_after}  operations={ops_after}")
        print()
        rows = (
            session.query(
                Operation.collect_date.label("d"), func.count().label("cnt")
            )
            .group_by("d").order_by("d").all()
        )
        print("按 collect_date 分布：")
        for r in rows:
            print(f"  {r.d}  {r.cnt}")
        print()
        print("每个日期保留的 run：")
        for date_val, rs in sorted(by_date.items()):
            keep = max([r.id for r in rs])
            r = session.get(CrawlRun, keep)
            print(
                f"  {date_val}: run_id={keep} total_records={r.total_records}  "
                f"{r.md_file_path}"
            )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    target = None
    dry = False
    for a in sys.argv[1:]:
        if a == "--dry-run":
            dry = True
        elif a.startswith("--date="):
            target = a.split("=", 1)[1]
        else:
            target = a
    main(target_date=target, dry_run=dry)
