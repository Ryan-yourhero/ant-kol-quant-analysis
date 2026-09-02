"""小规模测试：只用前3页测试完整流程"""
import sys, logging, time
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

from src.parser.ai_parser import _parse_single, _parse_ai_response

# 用一个小测试样例验证解析管道
test_md = """# 页面1
光模块之王
近一年收益率26.49%
16:18
指数回落了一点，继续加仓。
定投确认中
广发全球精选股票(QDII)...
买入金额(元)
2,500.00
查看详情
转发
123
评论
45
点赞
678
展开今日全部4条操作
已有12人求解读
我也催一下

# 页面2
Bells
近一年收益率15.30%
15:55
今天也是慢慢买入的一天。
买入确认中
建信上海金ETF联接C...
买入金额(元)
1,000.00
查看详情
买入确认中
国富全球科技互联...
买入金额(元)
1,000.00
查看详情
展开今日全部4条操作
已有8人求解读
我也催一下"""

print(f"测试MD: {len(test_md)} 字符")
t0 = time.time()

records = _parse_single(test_md)
elapsed = time.time() - t0

print(f"耗时: {elapsed:.1f}s")
print(f"记录数: {len(records)}")
for r in records:
    print(f"  {r.kol_name} | {r.operation_type} | {r.fund_name} | 买入:{r.buy_amount} | today:{r.today_operation_count}")
