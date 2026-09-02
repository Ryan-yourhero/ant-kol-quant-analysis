"""
ExpandManager v3.4 — 自动展开"展开今日全部X条操作"折叠按钮
============================================================
v3.4 重构：
  - **只使用 visible_nodes**，不再遍历完整 XML DOM
  - 彻底删除 zero-bounds 父节点猜坐标逻辑
  - 展开按钮只有同时满足五条件才允许点击：
      1. 自身文本匹配
      2. 自身 bounds != [0,0][0,0]
      3. 当前 visible=True
      4. 自身 clickable=true
      5. 点击中心处于屏幕安全区域
  - 点击前必须 double-dump 重新确认（防止旧坐标误点）
  - 展开成功三条件验证：按钮消失 / 新操作节点 / 状态变化
  - 循环保护：同一按钮连续失败2次 → 标记failed并跳过
"""

from __future__ import annotations

import re
import sys
import time
import os
from typing import Any, Dict, List, Optional, Tuple, Set

_THIS_FILE = os.path.abspath(__file__)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_THIS_FILE))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

try:
    from .raw_text_extractor import extract_visible_nodes
except ImportError:
    try:
        from core.raw_text_extractor import extract_visible_nodes  # type: ignore
    except ImportError:
        extract_visible_nodes = None  # type: ignore


# ============================================================
#  常量
# ============================================================
_EXPAND_RE = re.compile(r"^展开今日全部(\d+)条操作$")
_DEFAULT_CLICK_WAIT_SEC = 1.2
_DEFAULT_MAX_ROUNDS = 10

# 同一按钮连续失败上限（超过则标记为 permanently_failed）
_MAX_CONSECUTIVE_FAILS = 2

# 展开成功验证：新增操作节点关键词
_EXPAND_OPERATION_KEYWORDS = ["买入确认中", "卖出确认中", "定投确认中", "撤销"]

# double-dump 位置一致性容差（px）
_CONFIRM_POSITION_TOLERANCE = 20

# 安全区域边距（px），防止点击屏幕边缘
_SAFE_EDGE_MARGIN_X = 10
_SAFE_EDGE_MARGIN_Y = 10


# ============================================================
#  工具函数（v3.4：仅保留 bounds 解析和坐标计算）
# ============================================================
def _normalize(text: Optional[str]) -> str:
    """与 raw_text_extractor._normalize 一致：全角空格→半角，压缩空白，去前后空白。"""
    if not text:
        return ""
    t = text.replace("\u3000", " ")
    parts = t.split()
    return " ".join(parts)


def parse_bounds(bounds_str: str) -> Optional[Tuple[int, int, int, int]]:
    """解析 bounds 属性值（如 "[0,0][1440,3120]"）。公共函数，同时给 scroll_manager 使用。"""
    if not bounds_str:
        return None
    m = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))


def _bounds_center(bounds: Tuple[int, int, int, int]) -> Tuple[int, int]:
    return (bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2


def _is_visible_bounds(bounds: Tuple[int, int, int, int],
                        screen_w: int, screen_h: int) -> bool:
    """bounds 是否与屏幕可视区域有交集。"""
    x1, y1, x2, y2 = bounds
    if (x1, y1, x2, y2) == (0, 0, 0, 0):
        return False
    if x2 <= 0 or y2 <= 0 or x1 >= screen_w or y1 >= screen_h:
        return False
    return x2 > 0 and y2 > 0 and x1 < screen_w and y1 < screen_h


def _click_point_in_safe_area(bounds: Tuple[int, int, int, int],
                               screen_w: int, screen_h: int) -> Optional[Tuple[int, int]]:
    """计算可见区域中心点击坐标，并检查是否在安全区域内。"""
    x1, y1, x2, y2 = bounds
    if not _is_visible_bounds(bounds, screen_w, screen_h):
        return None
    cx = (max(0, x1) + min(screen_w, x2)) // 2
    cy = (max(0, y1) + min(screen_h, y2)) // 2
    # 安全检查：不在屏幕边缘
    if cx < _SAFE_EDGE_MARGIN_X or cx > screen_w - _SAFE_EDGE_MARGIN_X:
        return None
    if cy < _SAFE_EDGE_MARGIN_Y or cy > screen_h - _SAFE_EDGE_MARGIN_Y:
        return None
    return (cx, cy)


# ============================================================
#  ExpandManager v3.4 — 只使用 visible_nodes
# ============================================================
class ExpandManager:
    """自动展开当前屏幕中的「展开今日全部X条操作」按钮。

    v3.4 流程：
      1. 接收预提取的 visible_nodes（不是完整 XML）
      2. 在 visible_nodes 中查找匹配展开按钮
      3. 五条件过滤：文本匹配 + bounds非零 + visible + clickable + 安全区域
      4. double-dump 重新确认（防止旧坐标误点）
      5. 按 center_y 从上到下排序，逐个点击
      6. 每点击一次重新 dump → 三条件验证展开成功
      7. 重复直到当前屏无可安全点击的展开按钮
    """

    def __init__(
        self,
        adb,
        click_wait_sec: float = _DEFAULT_CLICK_WAIT_SEC,
        max_rounds: int = _DEFAULT_MAX_ROUNDS,
        enabled: bool = True,
        provider=None,
    ):
        self.adb = adb
        self.click_wait_sec = max(0.8, float(click_wait_sec))
        self.max_rounds = max(1, int(max_rounds))
        self.enabled = bool(enabled)
        # v3.4.1: UI XML provider（用于 double-dump 等内部 dump）
        self._provider = provider

        # 循环保护
        self._attempted: Dict[Tuple[str, Tuple], int] = {}  # (text, bounds) → fail_count
        self._permanently_failed: Set[Tuple[str, Tuple]] = set()

    def _is_permanently_failed(self, text: str, bounds: Tuple) -> bool:
        return (text, bounds) in self._permanently_failed

    def _mark_failed(self, text: str, bounds: Tuple):
        key = (text, bounds)
        self._attempted[key] = self._attempted.get(key, 0) + 1
        if self._attempted[key] >= _MAX_CONSECUTIVE_FAILS:
            self._permanently_failed.add(key)
            print(f"[EXPAND] ⚠ {text} 连续失败{self._attempted[key]}次，本轮永久跳过")

    def _reset_protection(self):
        """每屏重新开始，重置保护状态。"""
        self._attempted.clear()
        self._permanently_failed.clear()

    # ── v3.4.1: XML 获取（通过 provider 或 adb fallback） ──
    def _get_xml(self) -> str:
        if self._provider is not None:
            return self._provider.get_xml()
        return self.adb.dump_and_pull_xml(skip_check=True)

    # ── v3.4: 从 visible_nodes 查找展开按钮 ──
    @staticmethod
    def find_expand_nodes(
        visible_nodes: List[Dict[str, Any]],
        screen_w: int = 9999,
        screen_h: int = 9999,
    ) -> List[Dict[str, Any]]:
        """
        v3.4: 从 visible_nodes 列表中查找展开按钮。
        不再遍历完整 XML DOM。

        每个节点：
          {
            "text": str,              # 匹配文本
            "count": int,             # N（几条操作）
            "source": str,            # "text"|"content-desc"
            "bounds": (x1,y1,x2,y2),  # 有效 bounds
            "center": (cx,cy),        # 中心坐标
            "visible": bool,          # 是否可见
            "clickable": bool,        # 是否可安全点击（五条件全满足）
            "unsafe_reason": str,     # 不可点击原因
            "click_point": (cx,cy)|None,
          }
        """
        results: List[Dict[str, Any]] = []
        if not visible_nodes:
            return results

        for vn in visible_nodes:
            m = _EXPAND_RE.match(vn.get("text", ""))
            if not m:
                continue

            bounds = vn.get("bounds")
            if bounds is None or bounds == (0, 0, 0, 0):
                # v3.4: zero-bounds 直接跳过，不做父节点猜坐标
                results.append({
                    "text": vn["text"],
                    "count": int(m.group(1)),
                    "source": vn.get("source", "unknown"),
                    "bounds": bounds or (0, 0, 0, 0),
                    "center": (0, 0),
                    "visible": False,
                    "clickable": False,
                    "unsafe_reason": "zero-bounds=[0,0][0,0]，直接排除（不猜父节点）",
                    "click_point": None,
                })
                continue

            # 可见性判断
            vtu = vn.get("visible_to_user")
            visible = vtu is not False  # None 和 True 都算可见

            # 五条件检查
            unsafe_reason = ""

            if not visible:
                unsafe_reason = "visible_to_user=False"
            elif not vn.get("clickable", False):
                unsafe_reason = "clickable=False"
            else:
                click_point = _click_point_in_safe_area(bounds, screen_w, screen_h)
                if click_point is None:
                    unsafe_reason = "点击中心不在屏幕安全区域"

            is_clickable = visible and vn.get("clickable", False)
            if is_clickable:
                click_point = _click_point_in_safe_area(bounds, screen_w, screen_h)
                if click_point is None:
                    is_clickable = False
                    unsafe_reason = "点击中心不在屏幕安全区域"
            else:
                click_point = None

            center = _bounds_center(bounds)

            results.append({
                "text": vn["text"],
                "count": int(m.group(1)),
                "source": vn.get("source", "unknown"),
                "bounds": bounds,
                "center": center,
                "visible": visible,
                "clickable": is_clickable,
                "unsafe_reason": unsafe_reason,
                "click_point": click_point,
            })

        results.sort(key=lambda n: n["center"][1])
        return results

    def count_actionable_expands(
        self,
        visible_nodes: List[Dict[str, Any]],
        screen_w: int = 9999,
        screen_h: int = 9999,
    ) -> int:
        """统计当前屏中「可安全点击且未被永久跳过」的展开按钮数量。

        clickable=True 表示五条件全部满足（文本匹配 / 非零 bounds / 可见 /
        clickable=true / 点击中心在安全区域）。已永久失败的按钮不计入，
        因为其已被判定为不可继续处理。
        """
        if not self.enabled:
            return 0
        nodes = self.find_expand_nodes(visible_nodes, screen_w, screen_h)
        actionable = 0
        for n in nodes:
            if not n.get("clickable"):
                continue
            if self._is_permanently_failed(n["text"], n["bounds"]):
                continue
            actionable += 1
        return actionable

    # ── v3.4: double-dump 重新确认按钮 ──
    def _confirm_button(
        self,
        target: Dict[str, Any],
        screen_w: int,
        screen_h: int,
    ) -> Optional[Dict[str, Any]]:
        """
        点击前 double-dump 重新确认按钮仍然存在且位置一致。

        步骤：
          1. 重新 dump XML
          2. 提取 visible_nodes
          3. 查找相同的按钮（text 匹配 + bounds 有效 + visible + 位置一致 ±20px）
          4. 确认通过 → 返回确认信息；否则 → None
        """
        print(f"[EXPAND] double-dump 重新确认: {target['text']} ...")

        # 重新 dump
        try:
            xml2 = self._get_xml()
        except Exception as exc:
            print(f"[EXPAND] ⚠ double-dump 失败: {exc}")
            return None

        if not xml2:
            print("[EXPAND] ⚠ double-dump 返回空 XML")
            return None

        # 重新提取 visible_nodes
        if extract_visible_nodes is None:
            print("[EXPAND] ⚠ extract_visible_nodes 不可用，跳过确认")
            return None

        vn2 = extract_visible_nodes(xml2, screen_w, screen_h)

        # 查找匹配按钮
        target_text = target["text"]
        target_center = target["center"]

        for vn in vn2:
            if vn["text"] != target_text:
                continue
            if vn["bounds"] == (0, 0, 0, 0):
                continue
            if vn.get("visible_to_user") is False:
                continue
            if not vn.get("clickable", False):
                continue

            vn_center = _bounds_center(vn["bounds"])

            # 位置一致性检查
            dx = abs(vn_center[0] - target_center[0])
            dy = abs(vn_center[1] - target_center[1])
            if dx <= _CONFIRM_POSITION_TOLERANCE and dy <= _CONFIRM_POSITION_TOLERANCE:
                # 检查安全区域
                safe_pt = _click_point_in_safe_area(vn["bounds"], screen_w, screen_h)
                if safe_pt is None:
                    print(f"[EXPAND] ⚠ 按钮位置一致但不在安全区域，取消点击")
                    return None

                print(f"[EXPAND] ✓ double-dump 确认成功 "
                      f"bounds=[{vn['bounds'][0]},{vn['bounds'][1]}]"
                      f"[{vn['bounds'][2]},{vn['bounds'][3]}]  "
                      f"pos_delta=({dx},{dy})")
                return {
                    "text": vn["text"],
                    "bounds": vn["bounds"],
                    "click_point": safe_pt,
                }

        print(f"[EXPAND] ⚠ double-dump 未找到匹配按钮（可能已消失/位置变化），取消点击")
        return None

    # ── 点击 ──
    def _click_one(self, confirmed: Dict[str, Any]) -> bool:
        pt = confirmed.get("click_point")
        if pt is None:
            print(f"[EXPAND] ⚠ 无可点击坐标: {confirmed['text']}")
            return False
        cx, cy = pt
        print(f"[EXPAND] 点击: {confirmed['text']}  coord=({cx},{cy})")
        try:
            self.adb.tap(cx, cy)
            return True
        except Exception as exc:
            print(f"[EXPAND] ❌ tap 失败: {exc}")
            return False

    # ── 三条件展开验证（v3.4：使用 visible_nodes） ──
    def _verify_expand(
        self,
        visible_before: List[Dict[str, Any]],
        xml_after: str,
        target_text: str,
        target_bounds: Tuple[int, int, int, int],
        screen_w: int,
        screen_h: int,
    ) -> Dict[str, Any]:
        """
        验证展开是否成功，三条件（满足任一即成功）：

        条件A: 点击后在 visible_nodes 中找不到相同按钮
        条件B: 按钮对应区域新增了操作节点（买入确认中/卖出确认中/定投确认中/撤销）
        条件C: 可见区域文本/状态明显改变
        """
        result = {
            "success": False,
            "reason": "",
            "button_disappeared": False,
            "new_operation_nodes": 0,
            "button_state_changed": False,
            "tap_ok": True,
        }

        # 提取点击后的 visible_nodes
        if extract_visible_nodes is None or not xml_after:
            return result

        visible_after = extract_visible_nodes(xml_after, screen_w, screen_h)

        # 条件A: 按钮是否消失（在 visible_nodes 中找不到）
        after_expand = self.find_expand_nodes(visible_after, screen_w, screen_h)
        button_still_there = any(
            n["text"] == target_text and n["clickable"]
            for n in after_expand
        )
        result["button_disappeared"] = not button_still_there

        # 条件B: 目标区域附近是否新增操作节点
        tx1, ty1, tx2, ty2 = target_bounds
        search_y1 = ty1
        search_y2 = ty2 + 500

        op_count = 0
        for vn in visible_after:
            candidate = vn["text"]
            is_op = any(kw in candidate for kw in _EXPAND_OPERATION_KEYWORDS)
            if not is_op:
                continue
            b = vn["bounds"]
            by1, by2 = b[1], b[3]
            if search_y1 <= by1 <= search_y2 or search_y1 <= by2 <= search_y2:
                op_count += 1
        result["new_operation_nodes"] = op_count

        # 判断成功
        if result["button_disappeared"]:
            result["success"] = True
            result["reason"] = "A: 按钮已消失"
        elif result["new_operation_nodes"] > 0:
            result["success"] = True
            result["reason"] = f"B: 新增{result['new_operation_nodes']}个操作节点"
        else:
            # 条件C: 检查可见文本是否变化
            before_texts = {n["text"] for n in visible_before}
            after_texts = {vn["text"] for vn in visible_after}
            changed = before_texts != after_texts
            diff = after_texts - before_texts
            business_new = [
                d for d in diff
                if any(kw in d for kw in
                       ["买入确认中", "卖出确认中", "定投确认中", "撤销",
                        "买入", "卖出", "定投", "转换", "金额", "份额"])
            ]
            if changed and business_new:
                result["success"] = True
                result["button_state_changed"] = True
                result["reason"] = f"C: 新增业务内容 {business_new}"

        print(f"[EXPAND] 验证: tap=True  "
              f"button_disappeared={result['button_disappeared']}  "
              f"new_operation_nodes={result['new_operation_nodes']}  "
              f"state_changed={result['button_state_changed']}")
        if result["success"]:
            print(f"  → expand_success=True  ({result['reason']})")
        else:
            print(f"  → expand_success=False")

        return result

    # ── v3.4 主流程：接收 visible_nodes，逐个确认并点击 ──
    def expand_current_screen(
        self,
        visible_nodes: List[Dict[str, Any]],
        max_rounds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        v3.4: 逐个点击当前屏幕可见展开按钮。

        Args:
            visible_nodes: 预提取的 visible_nodes（不是完整 XML）
            max_rounds:   最大轮数

        Returns:
          {
            "dom_found": int,          # visible_nodes 中匹配到的展开按钮总数
            "visible_found": int,      # 其中可见的按钮数
            "clickable_found": int,    # 其中可安全点击的按钮数
            "unsafe_skipped": int,     # 因 unsafe 跳过的按钮数
            "permanently_failed": int, # 连续失败被永久跳过的按钮数
            "click_attempts": int,     # 尝试点击次数（含 double-dump 确认）
            "success": int,            # 成功展开的次数
            "failed": int,             # 失败的次数
            "rounds": int,             # 执行的轮数
            "sources": {str: int},     # 各来源的数量
            "remaining_visible": int,  # 剩余未能处理的可见按钮数
            "final_xml": str,          # 展开完成后的最终 XML
          }
        """
        result = {
            "dom_found": 0,
            "visible_found": 0,
            "clickable_found": 0,
            "unsafe_skipped": 0,
            "permanently_failed": 0,
            "click_attempts": 0,
            "success": 0,
            "failed": 0,
            "rounds": 0,
            "sources": {},
            "remaining_visible": 0,
            "final_xml": "",
        }

        if not self.enabled:
            return result

        # 重置循环保护
        self._reset_protection()

        rounds_limit = self.max_rounds if max_rounds is None else max(1, int(max_rounds))

        try:
            screen_w, screen_h = self.adb.get_screen_size()
        except Exception:
            screen_w, screen_h = 1440, 3120

        # v3.4: 从 visible_nodes 查找展开按钮
        all_nodes = self.find_expand_nodes(visible_nodes, screen_w, screen_h)
        result["dom_found"] = len(all_nodes)

        for n in all_nodes:
            src = n.get("source", "unknown")
            result["sources"][src] = result["sources"].get(src, 0) + 1

        clickable_nodes = [n for n in all_nodes if n["clickable"]]
        visible_non_clickable = [n for n in all_nodes if n.get("visible") and not n.get("clickable")]
        unsafe_nodes = [n for n in all_nodes if n.get("unsafe_reason")]
        result["visible_found"] = len([n for n in all_nodes if n.get("visible")])
        result["clickable_found"] = len(clickable_nodes)
        result["unsafe_skipped"] = len(unsafe_nodes)

        # 打印扫描结果
        print(f"\n[EXPAND] visible_nodes 中匹配展开按钮: {len(all_nodes)} 个")
        print(f"[EXPAND] 可见展开按钮: {result['visible_found']} 个")
        print(f"[EXPAND] 可安全点击按钮: {len(clickable_nodes)} 个")
        if unsafe_nodes:
            print(f"[EXPAND] 因 unsafe 跳过: {len(unsafe_nodes)} 个")
            for n in unsafe_nodes:
                print(f"  - {n['text']}  reason={n.get('unsafe_reason', '?')}")

        for i, n in enumerate(clickable_nodes, 1):
            print(f"  {i}. {n['text']}  source={n['source']}  "
                  f"bounds=[{n['bounds'][0]},{n['bounds'][1]}]"
                  f"[{n['bounds'][2]},{n['bounds'][3]}]  "
                  f"visible=True  clickable=True")
        if visible_non_clickable:
            for i, n in enumerate(visible_non_clickable, 1):
                print(f"  (不可点击) {n['text']}  source={n['source']}  "
                      f"reason={n.get('unsafe_reason', '?')}")
        hidden = [n for n in all_nodes if not n.get("visible")]
        if hidden:
            for i, n in enumerate(hidden, 1):
                print(f"  (不可见) {i}. {n['text']}  source={n['source']}")

        if not clickable_nodes:
            if all_nodes:
                if result["visible_found"] > 0 and not clickable_nodes:
                    print("[EXPAND] 可见按钮均不可安全点击，跳过")
                else:
                    print("[EXPAND] visible_nodes 中无展开按钮，跳过")
            return result

        # 保存初始 visible_nodes（用于验证）
        visible_before = visible_nodes

        # 逐个点击安全可见按钮
        for _ in range(rounds_limit):
            # 重新 dump 并提取 visible_nodes
            try:
                current_xml = self._get_xml()
                result["final_xml"] = current_xml
            except Exception as exc:
                print(f"[EXPAND] ⚠ dump 失败: {exc}")
                break

            if extract_visible_nodes is None:
                print("[EXPAND] ⚠ extract_visible_nodes 不可用")
                break

            current_visible = extract_visible_nodes(current_xml, screen_w, screen_h)

            # 重新查找可点击按钮
            re_nodes = self.find_expand_nodes(current_visible, screen_w, screen_h)
            re_clickable = [
                n for n in re_nodes
                if n["clickable"] and not self._is_permanently_failed(
                    n["text"], n["bounds"])
            ]

            if not re_clickable:
                still_failed = [
                    n for n in re_nodes
                    if n["clickable"] and self._is_permanently_failed(n["text"], n["bounds"])
                ]
                result["remaining_visible"] = len(still_failed)
                if still_failed:
                    print(f"⚠ [EXPAND] 本屏仍存在{len(still_failed)}个无法安全展开的按钮:")
                    for n in still_failed:
                        print(f"  - {n['text']}  bounds=[{n['bounds'][0]},"
                              f"{n['bounds'][1]}][{n['bounds'][2]},{n['bounds'][3]}]  (连续失败已跳过)")
                    result["permanently_failed"] = len(still_failed)
                else:
                    print("[EXPAND] 当前屏已无可安全点击的展开按钮")
                break

            result["rounds"] += 1

            target = re_clickable[0]

            # v3.4: double-dump 重新确认
            confirmed = self._confirm_button(target, screen_w, screen_h)
            if confirmed is None:
                # 确认失败，标记失败但不计入 click_attempts（没有真正点击）
                self._mark_failed(target["text"], target["bounds"])
                result["failed"] += 1
                continue

            result["click_attempts"] += 1

            xml_before = current_xml
            clicked = self._click_one(confirmed)

            if not clicked:
                result["failed"] += 1
                self._mark_failed(target["text"], target["bounds"])
                if self.click_wait_sec > 0:
                    time.sleep(self.click_wait_sec)
                continue

            if self.click_wait_sec > 0:
                time.sleep(self.click_wait_sec)

            # 验证点击效果
            try:
                verify_xml = self._get_xml()
                result["final_xml"] = verify_xml
            except Exception:
                verify_xml = current_xml

            verify_result = self._verify_expand(
                visible_before, verify_xml,
                target["text"], target["bounds"],
                screen_w, screen_h,
            )

            if verify_result["success"]:
                result["success"] += 1
            else:
                result["failed"] += 1
                self._mark_failed(target["text"], target["bounds"])

            # 更新 visible_before 为当前
            if extract_visible_nodes is not None and verify_xml:
                try:
                    visible_before = extract_visible_nodes(verify_xml, screen_w, screen_h)
                except Exception:
                    pass

        # 最终检查
        try:
            final_xml = self._get_xml()
            result["final_xml"] = final_xml
            if extract_visible_nodes is not None:
                final_visible = extract_visible_nodes(final_xml, screen_w, screen_h)
                final_nodes = self.find_expand_nodes(final_visible, screen_w, screen_h)
                final_clickable = [n for n in final_nodes if n["clickable"]]
                final_perm_failed = [
                    n for n in final_clickable
                    if self._is_permanently_failed(n["text"], n["bounds"])
                ]
            else:
                final_clickable = []
                final_perm_failed = []
        except Exception:
            final_clickable = []
            final_perm_failed = []

        if final_perm_failed:
            result["remaining_visible"] = len(final_perm_failed)
            print(f"⚠ [EXPAND] 本屏仍存在{len(final_perm_failed)}个无法安全展开的按钮（已连续失败跳过）")
        elif final_clickable:
            print(f"[EXPAND] 当前屏剩余可点击展开按钮: {len(final_clickable)} 个")
        else:
            print("[EXPAND] 当前屏展开按钮已全部处理")

        print(
            f"[EXPAND] 本轮统计: 发现={result['dom_found']} 可见={result['visible_found']} "
            f"可点击={result['clickable_found']} 跳过unsafe={result['unsafe_skipped']} "
            f"永久跳过={result['permanently_failed']} "
            f"确认+点击={result['click_attempts']} 成功={result['success']} "
            f"失败={result['failed']} 轮数={result['rounds']}"
        )

        return result


# ============================================================
#  v3.4 离线测试 — 使用 visible_nodes
# ============================================================
def _offline_tests() -> bool:
    print("=" * 60)
    print("ExpandManager v3.4 离线单测 (visible_nodes)")
    print("=" * 60)

    # 构造模拟 visible_nodes（模拟 extract_visible_nodes 的输出）
    def _mk_vn(text, bounds, source="text", clickable=True, scrollable=False,
               visible_to_user=None):
        return {
            "text": text,
            "bounds": bounds,
            "center_y": (bounds[1] + bounds[3]) // 2,
            "source": source,
            "clickable": clickable,
            "scrollable": scrollable,
            "visible_to_user": visible_to_user,
            "xml_index": 0,
        }

    screen_w, screen_h = 1440, 3120
    passed = 0
    failed = 0

    # ── Test 1: text + content-desc 双检查 ──
    print("\n[Test 1] text + content-desc 双检查（visible_nodes）")
    vn_list = [
        _mk_vn("展开今日全部8条操作", (100, 1800, 500, 1900)),
        _mk_vn("展开今日全部4条操作", (142, 1556, 1365, 1687)),
        _mk_vn("展开今日全部7条操作", (30, 1758, 1413, 1890), source="content-desc"),
        _mk_vn("展开今日全部15条操作", (65, 2400, 610, 2500)),
        _mk_vn("查看详情", (800, 1200, 1000, 1280)),
    ]
    nodes = ExpandManager.find_expand_nodes(vn_list, screen_w, screen_h)
    texts = [(n["text"], n["source"]) for n in nodes]
    if len(texts) == 4:
        print(f"  ✓ 找到 {len(nodes)} 个按钮: {texts}")
        passed += 1
    else:
        print(f"  ✗ 期望 4 个，实际 {len(nodes)}: {texts}")
        failed += 1

    # ── Test 2: 可见性 + clickable 过滤 ──
    print("\n[Test 2] 可见性 + clickable 过滤")
    visible = [n for n in nodes if n["visible"]]
    clickable = [n for n in nodes if n["clickable"]]
    unsafe_count = sum(1 for n in nodes if n["unsafe_reason"])
    print(f"  可见: {len(visible)}, 可点击: {len(clickable)}, unsafe: {unsafe_count}")
    if len(clickable) == 4 and unsafe_count == 0:
        print(f"  ✓ 4个可点击 + 0个unsafe")
        passed += 1
    else:
        print(f"  ✗ 期望4个可点击、0个unsafe")
        failed += 1

    # ── Test 3: zero-bounds 直接跳过，不猜父节点 ──
    print("\n[Test 3] zero-bounds 直接跳过（v3.4：不猜父节点）")
    vn_zero = [
        _mk_vn("展开今日全部8条操作", (0, 0, 0, 0), clickable=False),
        _mk_vn("展开今日全部4条操作", (142, 1556, 1365, 1687)),
    ]
    nodes3 = ExpandManager.find_expand_nodes(vn_zero, screen_w, screen_h)
    unsafe = [n for n in nodes3 if n["unsafe_reason"]]
    safe = [n for n in nodes3 if n["clickable"]]
    print(f"  找到 {len(nodes3)} 个: unsafe={len(unsafe)}, safe={len(safe)}")
    if len(unsafe) == 1 and len(safe) == 1:
        zero_reason = unsafe[0].get("unsafe_reason", "")
        print(f"  ✓ zero-bounds 直接跳过，原因: {zero_reason}")
        passed += 1
    else:
        print(f"  ✗ unsafe={len(unsafe)} safe={len(safe)}")
        failed += 1

    # ── Test 4: Y 从上到下排序 ──
    print("\n[Test 4] Y 从上到下排序")
    y_order = [n["center"][1] for n in nodes]
    if y_order == sorted(y_order):
        print(f"  ✓ Y 顺序正确: {y_order}")
        passed += 1
    else:
        print(f"  ✗ Y 顺序: {y_order}")
        failed += 1

    # ── Test 5: 不误匹配 ──
    print("\n[Test 5] 不误匹配")
    vn_noise = [
        _mk_vn("查看详情", (800, 1200, 1000, 1280)),
        _mk_vn("展开全文", (100, 900, 500, 950)),
        _mk_vn("催一下", (800, 1000, 900, 1050)),
        _mk_vn("展开今日全部", (65, 800, 300, 850)),
    ]
    noise_nodes = ExpandManager.find_expand_nodes(vn_noise, screen_w, screen_h)
    if len(noise_nodes) == 0:
        print("  ✓ 无误匹配")
        passed += 1
    else:
        print(f"  ✗ 误匹配: {[n['text'] for n in noise_nodes]}")
        failed += 1

    # ── Test 6: 同屏两个可见展开按钮排序 ──
    print("\n[Test 6] 同屏两个可见展开按钮排序")
    vn_two = [
        _mk_vn("展开今日全部4条操作", (142, 1556, 1365, 1687)),
        _mk_vn("展开今日全部7条操作", (142, 1305, 1365, 1432), source="content-desc"),
    ]
    two_nodes = ExpandManager.find_expand_nodes(vn_two, screen_w, screen_h)
    if len(two_nodes) == 2:
        if "7条" in two_nodes[0]["text"] and "4条" in two_nodes[1]["text"]:
            print("  ✓ 两个按钮正确按Y排序（7条在上，4条在下）")
            passed += 1
        else:
            print(f"  ✗ 排序错误: {[n['text'] for n in two_nodes]}")
            failed += 1
    else:
        print(f"  ✗ 期望2个: {len(two_nodes)}")
        failed += 1

    # ── Test 7: 屏幕外按钮 → visible=False ──
    print("\n[Test 7] visible_nodes 中屏幕外按钮不存在（已由 extract_visible_nodes 过滤）")
    # visible_nodes 本身就只有屏幕内节点 → expand 如果看到就是可见
    vn_onscreen = [
        _mk_vn("展开今日全部4条操作", (142, 1556, 1365, 1687)),
    ]
    on_nodes = ExpandManager.find_expand_nodes(vn_onscreen, screen_w, screen_h)
    visible_on = [n for n in on_nodes if n["visible"]]
    if len(visible_on) == 1:
        print("  ✓ 屏幕内按钮正确识别为可见")
        passed += 1
    else:
        print(f"  ✗ 期望1可见: {len(visible_on)}")
        failed += 1

    # ── Test 8: visible_to_user=False 排除 ──
    print("\n[Test 8] visible_to_user=False 排除")
    vn_hidden = [
        _mk_vn("展开今日全部4条操作", (142, 1556, 1365, 1687), visible_to_user=False, clickable=False),
    ]
    hidden_nodes = ExpandManager.find_expand_nodes(vn_hidden, screen_w, screen_h)
    if len(hidden_nodes) == 1 and not hidden_nodes[0]["clickable"]:
        print(f"  ✓ visible_to_user=False 被排除（不可点击）")
        passed += 1
    else:
        print(f"  ✗ 期望不可点击")
        failed += 1

    # ── Test 9: clickable=False 排除 ──
    print("\n[Test 9] clickable=False 排除")
    vn_nc = [
        _mk_vn("展开今日全部4条操作", (142, 1556, 1365, 1687), clickable=False),
    ]
    nc_nodes = ExpandManager.find_expand_nodes(vn_nc, screen_w, screen_h)
    if len(nc_nodes) == 1 and not nc_nodes[0]["clickable"]:
        print(f"  ✓ clickable=False 被排除")
        passed += 1
    else:
        print(f"  ✗ 期望不可点击")
        failed += 1

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"结果: {passed}/{total} 通过" + (", 全部通过 ✓" if failed == 0 else f", {failed} 失败"))
    return failed == 0


if __name__ == "__main__":
    _offline_tests()
