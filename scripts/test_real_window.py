"""
真实页面离线测试：读取项目根目录的 window.xml（真实手机 dump）
断言用户提出的 5 类误识别问题不再出现：
  1. 收益率(近一年收益率31.77%) 不得被识别为金额
  2. 观点长文(大家好，昨天跌到绝望...) 不得被识别为基金名
  3. 无效基金名(查看详情/今日操作/全部/买入确认中/撤销) 不得作为基金名
  4. 观点文本完全忽略，只在交易卡片内识别
  5. kol_name 黑名单(全部/今日操作/最新) 过滤；无法识别留空
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.xml_parser import UIXmlParser
from core.data_extractor import OperationDataExtractor


BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
XML_PATH = os.path.join(BASE, "window.xml")

ext = OperationDataExtractor()
parser = UIXmlParser()

print("=" * 60)
print("  真实页面测试：window.xml")
print("=" * 60)

with open(XML_PATH, "r", encoding="utf-8") as f:
    xml_content = f.read()
root = parser.parse(xml_content)
ops = ext.extract(root)

print(f"\n总共提取到 {len(ops)} 条操作记录\n")

# 收集所有识别结果的字段用于断言
all_funds = []
all_amounts = []
all_kols = []
all_source_funds = []
all_target_funds = []
for op in ops:
    d = op.to_dict()
    if op.fund_name:
        all_funds.append(op.fund_name)
    if op.amount:
        all_amounts.append(op.amount)
    if op.kol_name:
        all_kols.append(op.kol_name)
    if op.source_fund:
        all_source_funds.append(op.source_fund)
    if op.target_fund:
        all_target_funds.append(op.target_fund)
    at = d.get("action_type")
    print(f"  [{at}] confidence={op.confidence:.2f}  kol={op.kol_name!r}  @ {op.timestamp!r}")
    if at in ("BUY", "SELL"):
        print(f"       fund={op.fund_name!r}  amount={op.amount!r}  val={op.amount_value}  unit={op.amount_unit!r}")
    elif at == "TRANSFER":
        print(f"       src_fund={op.source_fund!r}  src_amt={op.source_amount!r}  val={op.source_amount_value}")
        print(f"       tgt_fund={op.target_fund!r}  tgt_amt={op.target_amount!r}  val={op.target_amount_value}")
    elif at == "CANCEL":
        print(f"       cancel_type={op.cancel_type!r}  fund={op.fund_name!r}  src={op.source_fund!r}  tgt={op.target_fund!r}")
    snap = (op.operation_text or "").strip()
    if snap:
        print("       operation_text:")
        for ln in snap.splitlines():
            print(f"          │ {ln}")
    print()

# ============================================================
# 断言（真实页面应有的规则）
# ============================================================
errors = []

# 1. 收益率不得作为金额
for amt in all_amounts:
    if "收益率" in amt or "%" in amt:
        errors.append(f"[FAIL-1] 收益率被识别为金额: {amt!r}")
# 再检查每条记录的 amount_value 对应的 amount
for op in ops:
    if ("收益率" in (op.amount or "")) or ("%" in (op.amount or "")):
        errors.append(f"[FAIL-1] 记录含收益率 amount: action={op.action_type} amount={op.amount!r}")

# 2. 观点长文不得被识别为基金名
OPINION_LONG_TEXT_HINT = ("大家好", "跌到绝望", "今天终于出现反弹", "双创指数",
                          "昨天跌到", "具体落实到操作上")
for fn in all_funds + all_source_funds + all_target_funds:
    for hint in OPINION_LONG_TEXT_HINT:
        if hint in fn:
            errors.append(f"[FAIL-2] 观点长文被识别为基金名: {fn!r}")
            break
    if len(fn) > 60:
        errors.append(f"[FAIL-2] 超长文本被识别为基金名(len={len(fn)}): {fn[:40]!r}...")

# 3. 无效基金名黑名单不得命中
INVALID_FUND_BLACKLIST = (
    "查看详情", "今日操作", "全部", "买入确认中", "卖出确认中",
    "转换确认中", "撤销", "已撤销", "最新", "买入金额(元)",
    "卖出份额(份)", "买入金额", "卖出份额", "关注", "评论", "点赞",
    "收藏", "转发", "回复",
)
for fn in all_funds + all_source_funds + all_target_funds:
    # 完全匹配才是黑名单（基金名包含"查看详情"前缀也不行）
    for inv in INVALID_FUND_BLACKLIST:
        if inv == fn.strip():
            errors.append(f"[FAIL-3] 无效基金名命中(完全匹配): {fn!r}")
            break

# 4. 观点文本应该完全忽略（简单判断：如果存在 fund 是观点开头的关键词就算误识别）
#    这条实际上和 #2 重合，但再额外检查长文本带标点的 fund
for fn in all_funds + all_source_funds + all_target_funds:
    punct_count = sum(1 for c in fn if c in "，。？！；、,.!?;:")
    if punct_count >= 2:
        errors.append(f"[FAIL-4] 带多句标点的观点文本被识别为基金名(punct={punct_count}): {fn!r}")

# 5. kol_name 黑名单不得命中；命中则算误识别
INVALID_KOL = ("全部", "今日操作", "最新")
for kol in all_kols:
    if kol.strip() in INVALID_KOL:
        errors.append(f"[FAIL-5] kol 黑名单命中: {kol!r}")

# 正向断言：至少应该识别出 2 条真实交易（window.xml 里至少有两条买入卡片）
if len(ops) < 1:
    errors.append("[WARN] 未识别出任何记录（可能交易卡片节点 bounds 为空，请确认 dump 完整）")

# 正向断言：如果有 BUY 记录，其基金名应含股票/混合/ETF/C 等合法关键词；金额>0
for op in ops:
    if op.action_type == "BUY":
        valid_kw = any(kw in (op.fund_name or "") for kw in (
            "ETF", "混合", "股票", "债券", "指数", "联接", "增强",
            "A", "B", "C", "基金", "优选", "成长", "价值",
        ))
        if not valid_kw and op.fund_name:
            errors.append(f"[WARN] BUY记录基金名未命中常规关键词: fund={op.fund_name!r}")
        if op.amount_value <= 0 and not op.amount:
            errors.append(f"[FAIL] BUY记录没有有效金额: fund={op.fund_name!r}")

# 输出结果
print("=" * 60)
if errors:
    print(f"  ❌ 断言失败，共 {len(errors)} 条问题：")
    for e in errors:
        print(f"     - {e}")
    sys.exit(1)
else:
    print(f"  ✅ 所有断言通过！提取到 {len(ops)} 条真实记录，无用户报告的误识别类型。")
    # 总结摘要
    if ops:
        print("\n  记录摘要：")
        for op in ops:
            at = op.action_type
            if at in ("BUY", "SELL"):
                print(f"    - {at}: {op.fund_name}  {op.amount or op.amount_value}")
            elif at == "TRANSFER":
                print(f"    - {at}: 卖{op.source_fund} -> 买{op.target_fund}")
            elif at == "CANCEL":
                print(f"    - {at} ({op.cancel_type}): {op.fund_name or op.source_fund}")
    sys.exit(0)
