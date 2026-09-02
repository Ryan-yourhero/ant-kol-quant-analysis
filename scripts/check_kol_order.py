import re

md_text = open(r"output\screen_dump_20260811_144955.md", encoding="utf-8").read()

_blacklist = {
    "关注", "发现", "讨论区", "热议话题", "学理财", "资讯", "同路人",
    "真实财有趣", "我的关注", "最新", "热门", "全部", "推荐",
    "今日操作", "原创", "转发", "分享", "收藏", "评论", "点赞",
    "求解读", "回复", "催一下", "查看详情", "立即查看",
    "加载中", "暂无数据", "暂无更多内容", "展开", "理财盘友圈", "记一下",
    "首页", "理财", "资产", "消息", "我的", "返回", "搜索", "设置",
    "更多", "关闭", "取消", "确定",
}

order = {}
pos = 0
lines = md_text.split("\n")
for i, line in enumerate(lines):
    s = line.strip()
    if (
        len(s) >= 2 and len(s) <= 12
        and re.match(r"^[\u4e00-\u9fa5A-Za-z0-9_]+$", s)
        and s not in _blacklist
        and not re.fullmatch(r"\d+", s)
    ):
        has_yield = any("%" in lines[j] for j in range(i + 1, min(i + 6, len(lines))))
        if has_yield:
            if s not in order:
                order[s] = pos
                pos += 1

for k, v in sorted(order.items(), key=lambda x: x[1]):
    print(f"  {k}: {v}")
print(f"Total: {len(order)} KOLs")
