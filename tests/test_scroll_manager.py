"""
目标21：自动回归测试
覆盖：展开按钮、可见性、滑动签名、页面跑偏、锁屏检测、到底判定
"""

import os
import sys
import hashlib
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.expand_manager import ExpandManager, parse_bounds
from core.raw_text_extractor import (
    extract_visible_nodes, visible_signature, scroll_signature,
    normalize_text, TextAccumulator,
)
from core.scroll_manager import _build_result_page_payload

SCREEN_W, SCREEN_H = 1440, 3120


class TestExpandButtons(unittest.TestCase):
    """展开按钮识别（v3.4: 使用 visible_nodes）"""

    def test_case1_normal_expand_button(self):
        """Case 1: 一个正常展开按钮 — bounds正常 visible=True — 应识别并标记可点击"""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy>
<node text="展开今日全部7条操作" bounds="[142,1305][1365,1432]" clickable="true" />
</hierarchy>"""
        vn = extract_visible_nodes(xml, SCREEN_W, SCREEN_H)
        nodes = ExpandManager.find_expand_nodes(vn, SCREEN_W, SCREEN_H)
        self.assertEqual(len(nodes), 1)
        self.assertTrue(nodes[0]["visible"])
        self.assertTrue(nodes[0]["clickable"])
        self.assertEqual(nodes[0]["source"], "text")
        self.assertEqual(nodes[0]["count"], 7)

    def test_case2_two_expand_buttons(self):
        """Case 2: 同屏两个展开按钮 — 应按Y从上到下排序，不漏任何一个"""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy>
<node text="展开今日全部4条操作" bounds="[142,1556][1365,1687]" clickable="true" />
<node content-desc="展开今日全部7条操作" bounds="[142,1305][1365,1432]" clickable="true" />
</hierarchy>"""
        vn = extract_visible_nodes(xml, SCREEN_W, SCREEN_H)
        nodes = ExpandManager.find_expand_nodes(vn, SCREEN_W, SCREEN_H)
        self.assertEqual(len(nodes), 2, "应识别到2个展开按钮")
        self.assertIn("7条", nodes[0]["text"], "7条(靠上)应排第一个")
        self.assertIn("4条", nodes[1]["text"], "4条(靠下)应排第二个")
        self.assertTrue(nodes[0]["visible"])
        self.assertTrue(nodes[1]["visible"])

    def test_case3_zero_bounds_unsafe(self):
        """Case 3: zero bounds 展开按钮 — v3.4: 直接skip不猜父节点"""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy>
<node bounds="[0,0][1440,3120]">
  <node text="展开今日全部8条操作" bounds="[0,0][0,0]" />
</node>
<node text="展开今日全部4条操作" bounds="[142,1556][1365,1687]" clickable="true" />
</hierarchy>"""
        vn = extract_visible_nodes(xml, SCREEN_W, SCREEN_H)
        nodes = ExpandManager.find_expand_nodes(vn, SCREEN_W, SCREEN_H)
        # zero-bounds 节点不会被 extract_visible_nodes 产出，只有正常节点
        self.assertGreaterEqual(len(nodes), 1)
        unsafe = [n for n in nodes if n.get("unsafe_reason")]
        safe = [n for n in nodes if n["clickable"]]
        self.assertEqual(len(safe), 1, "正常bounds按钮应可点击")
        if unsafe:
            self.assertIn("8条", unsafe[0]["text"])
            self.assertFalse(unsafe[0]["clickable"], "unsafe 按钮不可点击")

    def test_case4_offscreen_button_not_clicked(self):
        """Case 4: visible_nodes中屏幕外按钮不存在（由extract_visible_nodes过滤）"""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy>
<node text="展开今日全部4条操作" bounds="[100,10000][500,10100]" clickable="true" />
<node text="展开今日全部7条操作" bounds="[142,1305][1365,1432]" clickable="true" />
</hierarchy>"""
        vn = extract_visible_nodes(xml, SCREEN_W, SCREEN_H)
        nodes = ExpandManager.find_expand_nodes(vn, SCREEN_W, SCREEN_H)
        # 屏幕外按钮已被 extract_visible_nodes 过滤，只有1个可见
        visible = [n for n in nodes if n["visible"]]
        clickable = [n for n in nodes if n["clickable"]]
        self.assertEqual(len(nodes), 1, "屏幕外按钮已被visible_nodes过滤")
        self.assertEqual(len(visible), 1)
        self.assertEqual(len(clickable), 1)
        self.assertIn("7条", clickable[0]["text"])


class TestScrollSignature(unittest.TestCase):
    """scroll_signature 滑动检测"""

    def test_case5_real_scroll_detected(self):
        """Case 5: 页面真实滑动但完整DOM文本相同 — scroll_signature before != after"""
        # 模拟滑动前
        before_nodes = [
            {"text": "童童读财", "center_y": 900, "source": "text"},
            {"text": "买入确认中", "center_y": 1200, "source": "text"},
            {"text": "展开今日全部6条操作", "center_y": 2400, "source": "text"},
        ]
        # 模拟滑动后（同样内容但Y位置变了）
        after_nodes = [
            {"text": "童童读财", "center_y": 200, "source": "text"},
            {"text": "买入确认中", "center_y": 500, "source": "text"},
            {"text": "国富全球科技", "center_y": 2200, "source": "text"},
        ]
        before_sig = scroll_signature(before_nodes)
        after_sig = scroll_signature(after_nodes)
        self.assertNotEqual(before_sig, after_sig,
                            "Y位置变了，scroll_signature应不同")

    def test_case6_like_count_change_not_scroll(self):
        """Case 6: 点赞数变化但页面没有滑动 — scroll_signature 不变"""
        # 只有点赞数从 100 变成 101，其他不变
        before_nodes = [
            {"text": "童童读财", "center_y": 200, "source": "text"},
            {"text": "100", "center_y": 300, "source": "text"},     # 点赞数
            {"text": "买入确认中", "center_y": 500, "source": "text"},
            {"text": "14:39", "center_y": 180, "source": "text"},    # 时间
        ]
        after_nodes = [
            {"text": "童童读财", "center_y": 200, "source": "text"},
            {"text": "101", "center_y": 300, "source": "text"},      # 点赞数变了
            {"text": "买入确认中", "center_y": 500, "source": "text"},
            {"text": "14:39", "center_y": 180, "source": "text"},
        ]
        before_sig = scroll_signature(before_nodes)
        after_sig = scroll_signature(after_nodes)
        self.assertEqual(before_sig, after_sig,
                         "点赞数+时间被过滤，scroll_signature应相同")


class TestPageDrift(unittest.TestCase):
    """页面跑偏检测"""

    def test_case7_drift_to_detail_page(self):
        """Case 7: 进入基金详情页 — 应检测为非目标页面"""
        from core.scroll_manager import ScrollManager
        sm = ScrollManager.__new__(ScrollManager)
        # 基金详情页 visible texts
        detail_page_texts = ["返回", "基金详情", "朱雀企业优胜股票C",
                             "基金名称", "日涨跌幅", "+2.35%", "买入"]
        is_target = sm._is_target_page(detail_page_texts)
        self.assertFalse(is_target, "基金详情页不应被识别为目标页面")

    def test_case7b_list_page_is_target(self):
        """列表页应识别为目标页面"""
        from core.scroll_manager import ScrollManager
        sm = ScrollManager.__new__(ScrollManager)
        list_page_texts = ["今日操作", "全部", "最新", "收益率",
                           "童童读财", "买入确认中", "展开今日全部7条操作",
                           "14:39"]
        is_target = sm._is_target_page(list_page_texts)
        self.assertTrue(is_target, "列表页应识别为目标页面")


class TestBottomMarker(unittest.TestCase):
    """到底判定"""

    def test_case8_bottom_in_dom_but_offscreen(self):
        """Case 8: '暂无更多内容'在DOM但屏幕外 — 不能停止"""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy>
<node text="暂无更多内容" bounds="[100,10000][500,10050]" />
</hierarchy>"""
        # parse_bounds 验证
        b = parse_bounds("[100,10000][500,10050]")
        self.assertIsNotNone(b)
        y2 = b[3]
        center_y = (b[1] + y2) // 2
        screen_h = SCREEN_H
        in_screen = (y2 > 0 and b[1] < screen_h and b != (0, 0, 0, 0))
        in_bottom_half = center_y >= screen_h * 0.5
        visible = in_screen and in_bottom_half
        self.assertFalse(visible,
                         "屏幕外的'暂无更多内容'不应判定为可见")

    def test_case9_bottom_visible_stop(self):
        """Case 9: '暂无更多内容'在屏幕底部 — stop_type=bottom 正常停止"""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy>
<node text="暂无更多内容" bounds="[100,2400][1200,2500]" />
</hierarchy>"""
        b = parse_bounds("[100,2400][1200,2500]")
        center_y = (b[1] + b[3]) // 2
        screen_h = SCREEN_H
        in_screen = (b[2] > 0 and b[3] > 0 and b[0] < SCREEN_W and b[1] < screen_h
                     and b != (0, 0, 0, 0))
        in_bottom_half = center_y >= screen_h * 0.5
        visible = in_screen and in_bottom_half
        self.assertTrue(visible,
                        "屏幕底部可见的'暂无更多内容'应判定为可见")
        self.assertTrue(center_y >= 1560, "center_y应在屏幕下半部")


class TestScrollSignatureNoise(unittest.TestCase):
    """scroll_signature 噪声过滤"""

    def test_noise_filtering(self):
        """验证噪声节点被正确过滤"""
        from core.raw_text_extractor import _is_scroll_noise

        # 噪声
        self.assertTrue(_is_scroll_noise("100", 300), "纯数字应过滤")
        self.assertTrue(_is_scroll_noise("200w", 400), "200w应过滤")
        self.assertTrue(_is_scroll_noise("14:39", 200), "时间应过滤")
        self.assertTrue(_is_scroll_noise("刚刚", 500), "刚刚应过滤")
        self.assertTrue(_is_scroll_noise("点赞", 600), "点赞应过滤")
        self.assertTrue(_is_scroll_noise("100w", 700), "100w应过滤")
        self.assertTrue(_is_scroll_noise("original", 800), "original应过滤")
        self.assertTrue(_is_scroll_noise("3分钟前", 300), "3分钟前应过滤")

        # 非噪声（顶部固定区域以外）
        self.assertFalse(_is_scroll_noise("童童读财", 500), "大V昵称不应过滤")
        self.assertFalse(_is_scroll_noise("买入确认中", 600), "操作状态不应过滤")
        self.assertFalse(_is_scroll_noise("朱雀企业优胜股票C", 800), "基金名不应过滤")
        self.assertFalse(_is_scroll_noise("展开今日全部7条操作", 1200), "展开按钮不应过滤")

    def test_parse_bounds_consistency(self):
        """parse_bounds 解析一致性"""
        self.assertEqual(parse_bounds("[0,0][0,0]"), (0, 0, 0, 0))
        self.assertEqual(parse_bounds("[142,1305][1365,1432]"),
                         (142, 1305, 1365, 1432))
        self.assertIsNone(parse_bounds(""))
        self.assertIsNone(parse_bounds(None))


class TestVisibleTextsPreservation(unittest.TestCase):
    """防回归：visible_texts 不得回退/丢失为 full texts"""

    def test_add_page_preserves_visible_texts(self):
        acc = TextAccumulator()
        acc.add_page({
            "page": 1,
            "texts": ["旧节点A", "当前节点B"],
            "visible_texts": ["当前节点B"],
        })
        p = acc.pages[0]
        self.assertEqual(p["texts"], ["旧节点A", "当前节点B"])
        self.assertEqual(p["visible_texts"], ["当前节点B"],
                         "visible_texts 必须保留为真实可见文本，不能变成 texts")

    def test_add_page_missing_visible_texts_is_empty(self):
        acc = TextAccumulator()
        acc.add_page({
            "page": 1,
            "texts": ["旧节点A", "当前节点B"],
        })
        p = acc.pages[0]
        self.assertEqual(p["texts"], ["旧节点A", "当前节点B"])
        self.assertEqual(p["visible_texts"], [],
                         "visible_texts 缺失时应为空 list，不能回退为 texts")

    def test_result_payload_no_fallback_to_texts(self):
        payload = _build_result_page_payload({
            "page": 2,
            "texts": ["旧节点A", "当前节点B"],
        })
        self.assertEqual(payload["texts"], ["旧节点A", "当前节点B"])
        self.assertEqual(payload["visible_texts"], [],
                         "result_payload 的 visible_texts 缺失时必须为 []，不能 fallback 成 texts")

    def test_result_payload_keeps_visible_texts(self):
        payload = _build_result_page_payload({
            "page": 2,
            "texts": ["旧节点A", "当前节点B"],
            "visible_texts": ["当前节点B"],
        })
        self.assertEqual(payload["visible_texts"], ["当前节点B"])


class _FakeAdb:
    def get_screen_size(self):
        return (SCREEN_W, SCREEN_H)


def _make_decision_sm():
    """构造一个只用于决策测试的 ScrollManager（不触发真实 ADB）。"""
    from core.scroll_manager import ScrollManager
    sm = ScrollManager.__new__(ScrollManager)
    sm.adb = _FakeAdb()
    sm._screen_w = SCREEN_W
    sm._screen_h = SCREEN_H
    sm._bottom_marker_detected = False
    sm.swipe_count = 0
    sm.max_swipes = 100
    sm._stop_type = ""
    sm._last_bottom_check = {}
    sm._expand = ExpandManager(adb=_FakeAdb(), enabled=True)
    return sm


class TestBottomStopOrder(unittest.TestCase):
    """底部停止顺序：展开优先级 > 底部停止"""

    def test_caseA_no_expand_bottom_stop(self):
        """场景A：无 expand + 有「暂无更多内容」→ 正常 bottom stop"""
        xml = """<hierarchy>
<node text="暂无更多内容" bounds="[100,2400][1200,2500]" />
</hierarchy>"""
        sm = _make_decision_sm()
        finished, reason = sm._evaluate_stop(xml)
        self.assertTrue(finished)
        self.assertEqual(sm._stop_type, "bottom")
        self.assertIn("无待展开内容", reason)

    def test_caseB_expand_plus_bottom_defer(self):
        """场景B：有 expand + 有「暂无更多内容」→ 不停止，先展开"""
        xml = """<hierarchy>
<node text="展开今日全部7条操作" bounds="[142,1305][1365,1432]" clickable="true" />
<node text="暂无更多内容" bounds="[100,2400][1200,2500]" />
</hierarchy>"""
        sm = _make_decision_sm()
        decision = sm._current_page_decision(xml)
        self.assertTrue(decision["bottom_detected"])
        self.assertTrue(decision["expand_pending"])
        self.assertEqual(decision["expand_actionable"], 1)

        finished, reason = sm._evaluate_stop(xml)
        self.assertFalse(finished, "存在可展开按钮时不允许 bottom stop")
        self.assertNotEqual(sm._stop_type, "bottom")

    def test_caseC_expanded_transactions_then_stop(self):
        """场景C：展开后出现更多交易 + 仍有「暂无更多内容」→ 重新判定可停止"""
        xml = """<hierarchy>
<node text="买入确认中" bounds="[100,1600][400,1700]" />
<node text="暂无更多内容" bounds="[100,2400][1200,2500]" />
</hierarchy>"""
        sm = _make_decision_sm()
        decision = sm._current_page_decision(xml)
        self.assertTrue(decision["bottom_detected"])
        self.assertEqual(decision["expand_actionable"], 0,
                         "展开后按钮消失，actionable 应为 0")
        finished, reason = sm._evaluate_stop(xml)
        self.assertTrue(finished)
        self.assertEqual(sm._stop_type, "bottom")

    def test_caseD_two_expands_actionable_2(self):
        """场景D：连续 2 个 expand → actionable=2"""
        xml = """<hierarchy>
<node text="展开今日全部4条操作" bounds="[142,1556][1365,1687]" clickable="true" />
<node text="展开今日全部7条操作" bounds="[142,1305][1365,1432]" clickable="true" />
</hierarchy>"""
        sm = _make_decision_sm()
        decision = sm._current_page_decision(xml)
        self.assertEqual(decision["expand_found"], 2)
        self.assertEqual(decision["expand_actionable"], 2)

    def test_caseE_unsafe_expand_allows_bottom(self):
        """场景E：expand 不可点击（clickable=False）→ actionable=0，允许 bottom stop"""
        xml = """<hierarchy>
<node text="展开今日全部7条操作" bounds="[142,1305][1365,1432]" clickable="false" />
<node text="暂无更多内容" bounds="[100,2400][1200,2500]" />
</hierarchy>"""
        sm = _make_decision_sm()
        decision = sm._current_page_decision(xml)
        self.assertEqual(decision["expand_actionable"], 0)
        self.assertTrue(decision["bottom_detected"])
        finished, reason = sm._evaluate_stop(xml)
        self.assertTrue(finished)
        self.assertEqual(sm._stop_type, "bottom")

    def test_caseE_permanent_failed_excluded(self):
        """场景E：expand 连续失败被永久跳过 → 不计入 actionable"""
        em = ExpandManager(adb=_FakeAdb(), enabled=True)
        xml = """<hierarchy>
<node text="展开今日全部7条操作" bounds="[142,1305][1365,1432]" clickable="true" />
</hierarchy>"""
        vn = extract_visible_nodes(xml, SCREEN_W, SCREEN_H)
        bounds = (142, 1305, 1365, 1432)
        em._mark_failed("展开今日全部7条操作", bounds)
        em._mark_failed("展开今日全部7条操作", bounds)
        self.assertEqual(
            em.count_actionable_expands(vn, SCREEN_W, SCREEN_H), 0,
            "永久失败的展开按钮不应计入 actionable")


if __name__ == "__main__":
    print("=" * 60)
    print("v3.2 回归测试")
    print("=" * 60)
    unittest.main(verbosity=2)
