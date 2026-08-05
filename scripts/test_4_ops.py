"""
离线测试4种交易类型解析（无需手机，纯文本输入）
用法：python scripts/test_4_ops.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.data_extractor import OperationDataExtractor

ext = OperationDataExtractor()

# ---------- 测试用例 ----------
CASES = []

CASES.append(("1. 买入 BUY", [
    "童童读财",
    "14:39",
    "朱雀企业优胜股票C",
    "买入确认中",
    "买入金额(元)",
    "2,000.00元",
]))

CASES.append(("2. 卖出 SELL", [
    "量化小王子",
    "今天 10:22",
    "富国中证沪港深创新药ETF",
    "卖出确认中",
    "卖出份额(份)",
    "20,158.54份",
]))

CASES.append(("3. 转换 TRANSFER", [
    "稳健老王",
    "2026-08-04 11:00",
    "转换确认中",
    "卖出份额(份)",
    "富国中证消费电子主题ETF",
    "2,700份",
    "买入金额(元)",
    "广发沪港深精选混合C",
    "4,831.49元",
]))

CASES.append(("4. 买入撤销 BUY_CANCEL", [
    "新手韭菜",
    "5分钟前",
    "华夏沪深300ETF联接C",
    "买入金额(元)",
    "500.00元",
    "撤销",
]))

CASES.append(("5. 卖出撤销 SELL_CANCEL", [
    "稳健老王",
    "10分钟前",
    "汇添富全球医疗混合(QDII)",
    "卖出份额(份)",
    "888.00份",
    "已撤销",
]))

CASES.append(("6. 转换撤销 TRANSFER_CANCEL", [
    "波段高手",
    "昨天 14:05",
    "转换确认中",
    "景顺长城新兴成长混合",
    "1,200份",
    "易方达蓝筹精选混合",
    "5,000元",
    "撤销",
]))


def show(title, ops):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")
    if not ops:
        print("  ❌ 未识别出任何记录\n")
        return
    for j, op in enumerate(ops[:5], 1):
        d = op.to_dict()
        at = d.get("action_type")
        anchor_raw = op.raw_context.get("anchor_text", "") if isinstance(op.raw_context, dict) else ""
        print(f"  [{j}] action_type = {at}    (anchor={anchor_raw!r}, confidence={op.confidence:.3f})")
        print(f"      kol={op.kol_name!r}   @ timestamp={op.timestamp!r}")

        if at in ("BUY", "SELL"):
            print(f"      fund          = {d.get('fund')!r}")
            print(f"      amount        = {d.get('amount')!r}")
            print(f"      amount_value  = {d.get('amount_value')}")
            print(f"      unit          = {d.get('unit')!r}")
        elif at == "TRANSFER":
            print(f"      source_fund   = {d.get('source_fund')!r}")
            print(f"      source_amount = {d.get('source_amount')!r}  (val={d.get('source_amount_value')})")
            print(f"      target_fund   = {d.get('target_fund')!r}")
            print(f"      target_amount = {d.get('target_amount')!r}  (val={d.get('target_amount_value')})")
        elif at == "CANCEL":
            print(f"      cancel_type   = {d.get('cancel_type')!r}")
            print(f"      fund          = {d.get('fund')!r}")
            print(f"      source_fund   = {d.get('source_fund')!r}")
            print(f"      target_fund   = {d.get('target_fund')!r}")
            print(f"      amount/unit   = {d.get('amount')!r} / {d.get('unit')!r}")
        # --- 新增：展示原始快照 operation_text（多行）---
        snap = (op.operation_text or "").strip()
        if snap:
            print("      ── operation_text (原始快照，存库可复核) ──")
            for ln in snap.splitlines():
                print(f"      │ {ln}")
        print()


if __name__ == "__main__":
    for title, texts in CASES:
        ops = ext.extract_from_flat_texts(texts)
        show(title, ops)
    print("\n✅ 离线测试完成")
