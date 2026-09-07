"""
滚动采集管理器 v3.4 — 仅采集页面原始文本，不做交易规则判断
================================================================

v3.4「可视屏幕层」重构：
  - 所有业务识别只使用 visible_nodes，不再直接使用完整 XML DOM
  - 完整 XML 保存到 debug/ 目录作为备份
  - ExpandManager 只接收 visible_nodes
  - 到底判定只检查 visible_nodes
  - MD 导出只写 visible_nodes（一屏一页）
  - scroll_signature 基于 visible_nodes
  - DEBUG_VISIBLE 模式对比 full_dom vs visible_nodes

v3.2 架构：
  ScrollManager 负责：
    1. 前置检查（ADB连接 + 锁屏检测 + 目标页面确认）
    2. 委托 ExpandManager 处理所有展开按钮（唯一展开入口，传入 visible_nodes）
    3. dump XML → 抽页面 texts + 提取 visible_nodes → 保存 full XML 到 debug/
    4. 安全滑动（固定 x=0.90，纯纵向）
    5. 滑动检测基于 scroll_signature（排除点赞/评论/时间噪声）
    6. 页面跑偏检测基于 visible_nodes + 自动恢复（最多2次）
    7. 到底判定基于 visible_nodes 中的「暂无更多内容」
    8. 正式采集条件（目标页面 + 展开完成 + 非中间态）
    9. 统一轮次日志
    10. 保存原始文本 JSON + 镜像 MD（只写 visible_nodes）

直接运行：
    python core/scroll_manager.py --max 10
"""

from __future__ import annotations

import os
import sys
import time
import json
import hashlib
import argparse
import datetime
import re
from typing import List, Tuple, Optional, Set, Dict, Any

_THIS_FILE = os.path.abspath(__file__)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_THIS_FILE))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

try:
    from .adb_controller import ADBController, ADBError
    from .raw_text_extractor import extract_texts, TextAccumulator, normalize_text
except ImportError:
    from core.adb_controller import ADBController, ADBError  # type: ignore
    from core.raw_text_extractor import extract_texts, TextAccumulator, normalize_text  # type: ignore

try:
    from .raw_text_extractor import (
        extract_visible_nodes, visible_signature, build_visible_signature,
        scroll_signature, build_scroll_signature,
        scroll_top_signature, build_scroll_top_signature,
    )
except ImportError:
    try:
        from core.raw_text_extractor import (  # type: ignore
            extract_visible_nodes, visible_signature, build_visible_signature,
            scroll_signature, build_scroll_signature,
            scroll_top_signature, build_scroll_top_signature,
        )
    except ImportError:
        extract_visible_nodes = None  # type: ignore
        visible_signature = None  # type: ignore
        build_visible_signature = None  # type: ignore
        scroll_signature = None  # type: ignore
        build_scroll_signature = None  # type: ignore
        scroll_top_signature = None  # type: ignore
        build_scroll_top_signature = None  # type: ignore

try:
    from .screen_dump_exporter import export_screen_dump_md
except ImportError:
    export_screen_dump_md = None  # type: ignore

try:
    from .ui_provider import (
        UIHierarchyProvider, UiAutomator2Provider, ShellUiAutomatorProvider,
        UIDumpError, create_provider, validate_provider,
    )
except ImportError:
    try:
        from core.ui_provider import (  # type: ignore
            UIHierarchyProvider, UiAutomator2Provider, ShellUiAutomatorProvider,
            UIDumpError, create_provider, validate_provider,
        )
    except ImportError:
        UIHierarchyProvider = None  # type: ignore
        UiAutomator2Provider = None  # type: ignore
        ShellUiAutomatorProvider = None  # type: ignore
        UIDumpError = None  # type: ignore
        create_provider = None  # type: ignore
        validate_provider = None  # type: ignore

try:
    from .expand_manager import ExpandManager, parse_bounds
except ImportError:
    try:
        from core.expand_manager import ExpandManager, parse_bounds  # type: ignore
    except ImportError:
        ExpandManager = None  # type: ignore
        parse_bounds = None  # type: ignore


# ============================================================
#  常量
# ============================================================
# 安全滑动参数（v3.2：固定x=0.90，纯纵向）
SAFE_SWIPE_XR = 0.90
SAFE_SWIPE_Y1R = 0.75
SAFE_SWIPE_Y2R = 0.25
SAFE_SWIPE_DURATION = 650  # ms，足够明显不会被识别为 tap

# 滑动重试策略（改变Y距离或duration，不改变X）
SWIPE_STRATEGIES = [
    (SAFE_SWIPE_XR, 0.75, 0.25, 650, "safe_x=0.90 50% 650ms"),
    (SAFE_SWIPE_XR, 0.78, 0.22, 800, "safe_x=0.90 56% 800ms"),
    (SAFE_SWIPE_XR, 0.72, 0.28, 1000, "safe_x=0.90 44% 1000ms"),
]
MAX_SCROLL_RETRIES = len(SWIPE_STRATEGIES) - 1

DEFAULT_WAIT_AFTER_SWIPE_SEC = 1.0
DEFAULT_MAX_SWIPES = 100
DEFAULT_MAX_SCROLL_RETRIES = MAX_SCROLL_RETRIES
MAX_DRIFT_RECOVERIES = 2

# v3.5 时序稳定参数（统一从配置读取，不硬编码 sleep）
FIRST_PAGE_STABILIZE_WAIT = 2.0   # preflight 通过后、首次 dump 前等待（秒）
POST_SWIPE_WAIT = 1.2             # 滑动后等待页面稳定（秒）
POST_EXPAND_WAIT = 1.3            # 展开按钮点击后、正式采集前等待（秒）
STABLE_DUMP_MAX_ATTEMPTS = 3      # 稳定 dump 最大尝试次数
STABLE_DUMP_INTERVAL = 0.5        # 稳定 dump 两次尝试间隔（秒）

# Tab 坐标
TAB_TODAY_OPERATION = (325, 538)

# 到底判定
_BOTTOM_TEXT = "暂无更多内容"

# 目标页面指示器（visible_nodes 中）
_TARGET_PAGE_INDICATORS = [
    "今日操作", "全部", "最新", "收益率", "展开今日全部",
    "买入确认中", "卖出确认中", "定投确认中", "转换确认中",
]
# 非目标页面指示器
_NON_TARGET_INDICATORS = [
    "基金详情", "产品详情", "基金档案", "个人主页", "他的主页",
    "达人主页", "基金名称",
]
# 锁屏检测关键词
_LOCKSCREEN_KEYWORDS = [
    "设备已锁定", "数字密码编辑框", "锁屏", "keyguard",
    "正在充电，已完成百分之", "近期的闹钟", "录音机",
    "当前温度和天气",
]
_LOCKSCREEN_MAX_KEYWORD_HITS = 2  # 命中 >= 2 个锁屏关键词即判定锁屏

OUTPUT_DIR = os.path.join(_PROJECT_ROOT, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# v3.4: 完整 XML 备份目录
DEBUG_DIR = os.path.join(_PROJECT_ROOT, "debug")
os.makedirs(DEBUG_DIR, exist_ok=True)

# v3.4: DEBUG_VISIBLE 模式（环境变量控制）
DEBUG_VISIBLE = os.environ.get("DEBUG_VISIBLE", "").lower() in ("1", "true", "yes")


# ============================================================
#  工具
# ============================================================
_NUM_RE = re.compile(r"\d[\d,\.]*")


def _texts_sig(texts: List[str]) -> str:
    """完整 DOM text signature（调试用）。"""
    masked = [_NUM_RE.sub("N", t) for t in texts]
    s = "\n".join(masked)
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _build_result_page_payload(p: Dict[str, Any]) -> Dict[str, Any]:
    """把 accumulator 的一页 dict 转成 raw_pages payload 的页 dict。

    visible_texts 缺失或非 list 时返回空 list，绝不回退为完整 texts。
    """
    page_no = p.get("page")
    visible_texts = p.get("visible_texts")
    if not isinstance(visible_texts, list):
        print(f"[VISIBLE_TEXTS] missing on page={page_no}, "
              f"NOT falling back to full texts")
        visible_texts = []
    return {
        "page": page_no,
        "texts": list(p.get("texts") or []),
        "visible_texts": visible_texts,
    }


# ============================================================
#  ScrollManager v3.2
# ============================================================
class ScrollManager:
    _legacy_expand_methods_removed = True

    def __init__(
        self,
        adb_path: Optional[str] = None,
        device_serial: Optional[str] = None,
        max_swipes: int = DEFAULT_MAX_SWIPES,
        wait_after_swipe_sec: float = DEFAULT_WAIT_AFTER_SWIPE_SEC,
        max_scroll_retries: int = DEFAULT_MAX_SCROLL_RETRIES,
        enable_expand: bool = True,
        expand_max_rounds: int = 10,
        provider: Optional[Any] = None,
        provider_prefer: str = "u2",
        u2_wait_idle_timeout: int = 500,
    ):
        self.adb = ADBController(adb_path=adb_path, device_serial=device_serial)

        # v3.4.1: UIHierarchyProvider
        if provider is not None:
            self._provider = provider
            self._provider_note = "外部注入"
        elif create_provider is not None:
            self._provider, self._provider_note = create_provider(
                self.adb,
                prefer=provider_prefer,
                device_serial=self.adb.device_serial,
                wait_idle_timeout=u2_wait_idle_timeout,
            )
        else:
            self._provider = None
            self._provider_note = "不可用"
        self.accumulator = TextAccumulator()

        self.max_swipes = max_swipes
        self.max_scroll_retries = max(0, int(max_scroll_retries))
        self.wait_after_swipe_sec = wait_after_swipe_sec

        self._screen_w: int = 1440
        self._screen_h: int = 3120

        self._expand: Optional[ExpandManager] = None
        if enable_expand and ExpandManager is not None:
            self._expand = ExpandManager(
                adb=self.adb,
                max_rounds=expand_max_rounds,
                enabled=True,
                provider=self._provider,
            )
        elif enable_expand and ExpandManager is None:
            print("[EXPAND] ExpandManager 不可用，自动展开已禁用")

        self.page_read_count: int = 0
        self.swipe_count: int = 0

        self._bottom_marker_detected: bool = False
        self._stop_type: str = ""
        self._scroll_retry_total: int = 0
        self._last_bottom_check: Dict[str, Any] = {}
        self._last_retry_count: int = 0

        # 展开统计
        self._expand_dom_found: int = 0
        self._expand_visible_found: int = 0
        self._expand_clickable_found: int = 0
        self._expand_unsafe_skipped: int = 0
        self._expand_permanently_failed: int = 0
        self._expand_click_attempts: int = 0
        self._expand_clicks_success: int = 0
        self._expand_clicks_failed: int = 0
        self._expand_rounds: int = 0
        self._expand_remaining_visible: int = 0
        self._expand_sources: Dict[str, int] = {}

        # 当前轮次统计（用于统一日志）
        self._current_expand_stats: Dict[str, Any] = {}
        self._current_round_info: Dict[str, Any] = {}

        self.history: List[Dict[str, Any]] = []

    # ============================================================
    #  架构自检
    # ============================================================
    @staticmethod
    def _print_arch_check():
        legacy_methods = ["_find_expand_buttons", "_get_visible_click_point",
                          "_ensure_all_expanded_before_slide"]
        legacy_found = [m for m in legacy_methods if hasattr(ScrollManager, m)]

        print("\n" + "-" * 50)
        print("[ARCH] Expand implementation:")
        if ExpandManager is not None:
            print("  core.expand_manager.ExpandManager  (唯一展开入口)")
        else:
            print("  ExpandManager 不可用")
        print("[ARCH] ScrollManager legacy expand methods:")
        if legacy_found:
            for m in legacy_found:
                print(f"  {m} 仍然存在！应删除")
        else:
            print("  disabled/removed")
        print("[ARCH] Scroll detection:")
        if scroll_signature is not None:
            print("  scroll_signature (排除噪声，Y分桶)")
        elif visible_signature is not None:
            print("  visible_signature")
        else:
            print("  不可用")
        print("[ARCH] Bottom detection:")
        print("  visible_nodes (text+content-desc, only visible_nodes)")
        print("[ARCH] Swipe mode:")
        print(f"  safe_x={SAFE_SWIPE_XR}  pure_vertical  min_dur={SAFE_SWIPE_DURATION}ms")
        print("[ARCH] Preflight:")
        print("  lock screen detection  enabled")
        print("[ARCH] v3.4 Visible layer:")
        print(f"  DEBUG_VISIBLE={DEBUG_VISIBLE}")
        print("  Full XML → debug/*.xml")
        print("  MD → visible_nodes only")
        # v3.4.1: Provider info
        print(f"[ARCH] v3.4.1 UI Hierarchy Source:")
        if hasattr(ScrollManager, '_instance_provider'):
            inst = ScrollManager._instance_provider
            if inst and hasattr(inst, 'config_info'):
                for k, v in inst.config_info.items():
                    print(f"  {k} = {v}")
            else:
                print("  provider = (not initialized)")
        else:
            print("  provider = (will be initialized)")
        print("-" * 50)

    # ============================================================
    #  前置检查（v3.3：纯只读，不操作手机）
    # ============================================================
    def _get_xml(self) -> str:
        """v3.4.1: 通过 UIHierarchyProvider 获取 XML。

        Returns:
            XML 字符串；失败时返回空字符串
        """
        if self._provider is None:
            try:
                return self.adb.dump_and_pull_xml(skip_check=True)
            except Exception:
                return ""
        try:
            return self._provider.get_xml()
        except UIDumpError as e:
            print(f"[PROVIDER] ⚠ XML 获取失败: {e}")
            if e.error_type == "idle_timeout":
                self._stop_type = "ui_dump_failed"
            return ""
        except Exception as e:
            print(f"[PROVIDER] ⚠ XML 获取异常: {e}")
            return ""

    def _get_stable_xml(self, max_attempts: int = STABLE_DUMP_MAX_ATTEMPTS) -> str:
        """v3.5: 稳定 dump — 连续两次交易结构节点数量基本一致才返回。

        不要求整个 DOM 完全一致（点赞数/网速等会变化），
        只比较交易结构关键节点数量。

        Returns:
            最稳定的一次 XML；全部失败返回空字符串
        """
        if max_attempts < 2:
            return self._get_xml()

        sw, sh = self._get_screen_size_cached()
        prev_q: Optional[Dict[str, int]] = None
        prev_xml: str = ""

        for attempt in range(1, max_attempts + 1):
            xml = self._get_xml()
            if not xml:
                return prev_xml  # 返回上一次成功的

            q = self._page_quality(xml, sw, sh)
            print(f"[STABLE_DUMP] attempt={attempt}  "
                  f"visible_nodes={q.get('visible_nodes', 0)}  "
                  f"op_status={q.get('op_status', 0)}  "
                  f"fund_like={q.get('fund_like', 0)}  "
                  f"amount_labels={q.get('amount_labels', 0)}  "
                  f"amount_values={q.get('amount_values', 0)}")

            if prev_q is not None:
                # 比较交易结构节点：允许 ±1 的浮动（WebView 异步渲染）
                stable = True
                for key in ("op_status", "fund_like", "amount_labels", "amount_values"):
                    if abs(q.get(key, 0) - prev_q.get(key, 0)) > 1:
                        stable = False
                        break
                if stable:
                    print(f"[STABLE_DUMP] ✓ 交易结构稳定（attempt={attempt}），采用本次 XML")
                    return xml

            prev_q = q
            prev_xml = xml
            if attempt < max_attempts:
                time.sleep(STABLE_DUMP_INTERVAL)

        print(f"[STABLE_DUMP] ⚠ {max_attempts} 次尝试后交易结构仍未稳定，采用最后一次")
        return prev_xml

    def _page_quality(self, xml: str, sw: int, sh: int) -> Dict[str, int]:
        """v3.5: 统计当前页面交易结构完整度指标。"""
        q: Dict[str, int] = {
            "visible_nodes": 0,
            "op_status": 0,
            "fund_like": 0,
            "amount_labels": 0,
            "amount_values": 0,
        }
        if not xml or extract_visible_nodes is None:
            return q
        try:
            nodes = extract_visible_nodes(xml, sw, sh)
            q["visible_nodes"] = len(nodes)
            for n in nodes:
                t = n.get("text", "")
                if t in ("买入确认中", "卖出确认中", "定投确认中", "转换确认中"):
                    q["op_status"] += 1
                # 基金名候选：含基金后缀或"混合/ETF/联接"等
                if any(kw in t for kw in ("混合", "ETF", "联接", "股票", "债券", "指数", "货币")):
                    if len(t) >= 4 and len(t) <= 30:
                        q["fund_like"] += 1
                if t in ("买入金额(元)", "卖出份额(份)"):
                    q["amount_labels"] += 1
                # 金额数值：纯数字+逗号+小数点，长度合理
                if re.fullmatch(r"[\d,]+\.?\d*", t) and len(t) >= 3:
                    q["amount_values"] += 1
        except Exception:
            pass
        return q

    def _preflight_check(self) -> bool:
        """
        前置检查（纯被动/只读）：
          1. ADB 连接
          2. 屏幕状态（通过 wm size / dumpsys）
          3. 是否锁屏（通过 dump XML 检测关键词）
          4. 是否为目标页面（通过 visible_nodes 判断）

        任何条件不满足 → 打印提示 → 立即退出。
        整个阶段：0 tap / 0 swipe / 0 keyevent / 0 monkey。
        """
        print("\n" + "=" * 50)
        print("[PRECHECK]")
        print("=" * 50)

        adb_ok = False
        screen_ok = False
        lock_ok = False
        page_ok = False

        # 1. ADB 连接
        try:
            self.adb.check_device_status()
            adb_ok = True
            print("  ADB连接          : ✅")
        except ADBError as e:
            print(f"  ADB连接          : ❌ {e}")

        if not adb_ok:
            self._stop_type = "preflight_device_error"
            print("\n  程序未对手机执行任何操作。")
            print(f"{'=' * 50}")
            return False

        # 2. 获取屏幕尺寸（只读）
        try:
            sw, sh = self.adb.get_screen_size()
            self._screen_w = sw
            self._screen_h = sh
            print(f"  屏幕尺寸          : {sw} x {sh}")
        except Exception:
            print(f"  屏幕尺寸          : 使用默认值 {self._screen_w}x{self._screen_h}")

        # 3. 检查屏幕是否点亮（通过 dumpsys power，只读）
        try:
            result = self.adb._run(["dumpsys", "power"], shell=True)
            stdout = result.stdout if hasattr(result, "stdout") else str(result)
            if "mWakefulness=Awake" in stdout:
                screen_ok = True
                print("  屏幕状态         : ✅ 已点亮")
            else:
                print("  屏幕状态         : ⚠ 可能已熄灭（将尝试 dump 确认）")
        except Exception:
            print("  屏幕状态         : ⚠ 无法检测（将尝试 dump 确认）")

        # 4. dump 当前 XML（只读）
        try:
            xml = self._get_xml()
            if not xml:
                raise ADBError("XML 为空")
        except ADBError as e:
            print(f"  XML dump         : ❌ {e}")
            self._stop_type = "preflight_dump_error"
            print("\n  程序未对手机执行任何操作。")
            print(f"{'=' * 50}")
            return False

        # 5. 检查锁屏
        try:
            page_dict = extract_texts(xml, page=0)
            all_texts = "\n".join(page_dict.get("texts", []))
        except Exception:
            all_texts = ""

        lock_hits = sum(1 for kw in _LOCKSCREEN_KEYWORDS if kw in all_texts)
        is_locked = lock_hits >= _LOCKSCREEN_MAX_KEYWORD_HITS

        if is_locked:
            print(f"  锁屏状态         : ❌ 手机已锁定")
            print(f"  锁屏特征命中: {lock_hits} 个 (阈值 {_LOCKSCREEN_MAX_KEYWORD_HITS})")
            for kw in _LOCKSCREEN_KEYWORDS:
                if kw in all_texts:
                    print(f"    - {kw}")
            print()
            print("  程序未对手机执行任何操作。")
            print("  请手动解锁并进入「今日操作」页面后重新运行。")
            print(f"{'=' * 50}")
            self._stop_type = "preflight_locked"
            return False

        lock_ok = True
        print("  锁屏状态         : ✅ 已解锁")

        # 6. 检查目标页面
        target_page = self._check_target_page_from_xml(xml, self._screen_w, self._screen_h)
        if target_page:
            page_ok = True
            print("  目标页面         : ✅ 今日操作")
        else:
            print("  目标页面         : ❌ 不是蚂蚁财富「今日操作」页面")
            # 列出当前可见文本帮助用户定位
            if extract_visible_nodes is not None:
                vis_nodes = extract_visible_nodes(xml, self._screen_w, self._screen_h)
                vis_texts = [n["text"] for n in vis_nodes][:15]
                if vis_texts:
                    print(f"  当前可见文本: {vis_texts}")
            print()
            print("  程序未对手机执行任何操作。")
            print("  请手动打开蚂蚁财富「今日操作」页面后重新运行 python main.py")
            print(f"{'=' * 50}")
            self._stop_type = "preflight_wrong_page"
            return False

        # 7. 屏幕已确认点亮（通过 dump 成功反向确认）
        if not screen_ok:
            print("  屏幕状态         : ✅ 已点亮（通过 XML dump 确认）")
            screen_ok = True

        print("\n  说明：启动检查不会主动操作手机。")
        print(f"{'=' * 50}")
        return True

    def _check_target_page_from_xml(self, xml: str, sw: int, sh: int) -> bool:
        """从 XML 检查当前是否为目标页面（基于 visible_nodes）。"""
        if extract_visible_nodes is None:
            return True  # 降级
        nodes = extract_visible_nodes(xml, sw, sh)
        texts = [n["text"] for n in nodes]
        return self._is_target_page(texts)

    def _is_target_page(self, visible_texts: List[str]) -> bool:
        """检查可见节点是否属于「今日操作」列表页。"""
        combined = "\n".join(visible_texts)
        has_non_target = any(ind in combined for ind in _NON_TARGET_INDICATORS)
        has_target = any(ind in combined for ind in _TARGET_PAGE_INDICATORS)
        if has_non_target and not has_target:
            return False
        return True

    # ============================================================
    #  安全滑动（目标14）
    # ============================================================
    def _safe_swipe(self, xr: float = SAFE_SWIPE_XR, y1r: float = SAFE_SWIPE_Y1R,
                    y2r: float = SAFE_SWIPE_Y2R, dur: int = SAFE_SWIPE_DURATION) -> None:
        """安全滑动：固定 X 坐标，纯纵向，duration 足够明显。"""
        sw, sh = self._get_screen_size_cached()
        x = int(sw * xr)
        y1 = int(sh * y1r)
        y2 = int(sh * y2r)
        print(f"[SCROLL] safe_x={x}  start=({x},{y1})  end=({x},{y2})  duration={dur}ms")
        try:
            self.adb._run(["shell", "input", "swipe",
                           str(x), str(y1), str(x), str(y2), str(dur)])
        except ADBError as e:
            print(f"  swipe 异常: {e}")

    def _one_swipe_strategy(self, strategy_idx: int) -> str:
        """执行指定滑动策略。"""
        if strategy_idx >= len(SWIPE_STRATEGIES):
            strategy_idx = len(SWIPE_STRATEGIES) - 1
        xr, y1r, y2r, dur, label = SWIPE_STRATEGIES[strategy_idx]
        self._safe_swipe(xr, y1r, y2r, dur)
        return label

    def scroll_up(self, retry_n: int = 0, strategy_idx: int = 0) -> str:
        """单次向上滑动（v3.5：使用 POST_SWIPE_WAIT 统一等待）。"""
        self.swipe_count += 1
        tag = f"（重试{retry_n}/{self.max_scroll_retries}）" if retry_n > 0 else ""
        label = self._one_swipe_strategy(strategy_idx)
        print(f"\n  执行第{self.swipe_count}次滑动{tag}")
        print(f"  策略: {label}")
        # v3.5: 使用 POST_SWIPE_WAIT 统一等待（优先于旧的 wait_after_swipe_sec）
        wait_sec = POST_SWIPE_WAIT if POST_SWIPE_WAIT > 0 else self.wait_after_swipe_sec
        if wait_sec > 0:
            time.sleep(wait_sec)
        return label

    def _tap_today_operation(self) -> bool:
        tx, ty = TAB_TODAY_OPERATION
        print(f"  → 点击「今日操作」tab ({tx},{ty})")
        try:
            self.adb.tap(tx, ty)
            time.sleep(1.0)
            return True
        except ADBError as e:
            print(f"  click tab 失败：{e}")
            return False

    def _check_screen_state(self) -> bool:
        """v3.3: 纯只读屏幕状态检查。不执行任何设备操作。
        仅通过 dumpsys power 检查屏幕是否点亮。
        注意：preflight 中已做此检查，此函数供外部调用。"""
        try:
            result = self.adb._run(["dumpsys", "power"], shell=True)
            stdout = result.stdout if hasattr(result, "stdout") else str(result)
            if "mWakefulness=Awake" in stdout:
                return True
        except Exception:
            pass
        return False

    def _get_screen_size_cached(self) -> Tuple[int, int]:
        if self._screen_w == 1440 and self._screen_h == 3120:
            try:
                self._screen_w, self._screen_h = self.adb.get_screen_size()
            except Exception:
                pass
        return self._screen_w, self._screen_h

    # ============================================================
    #  页面跑偏检测与自动恢复（目标15）
    # ============================================================
    def _guard_check(self, xml_content: str) -> bool:
        """
        检查当前页面是否仍为目标页面。
        使用 visible_nodes（不是全 DOM）。
        如果跑偏，自动 back() 恢复，最多2次。
        """
        sw, sh = self._get_screen_size_cached()
        if extract_visible_nodes is None:
            return True
        nodes = extract_visible_nodes(xml_content, sw, sh)
        texts = [n["text"] for n in nodes]
        is_target = self._is_target_page(texts)
        return is_target

    def _guard_recover(self) -> bool:
        """页面跑偏恢复：back → 等待 → dump → 验证。"""
        for attempt in range(MAX_DRIFT_RECOVERIES):
            print(f"\n[GUARD] 检测到页面跑偏，执行 back() 恢复 ({attempt+1}/{MAX_DRIFT_RECOVERIES})")
            try:
                self.adb._run(["shell", "input", "keyevent", "4"])
            except Exception:
                pass
            time.sleep(1.0)
            try:
                xml_after = self._get_xml()
                sw, sh = self._get_screen_size_cached()
                if extract_visible_nodes is not None:
                    nodes = extract_visible_nodes(xml_after, sw, sh)
                    vt = [n["text"] for n in nodes]
                    if self._is_target_page(vt):
                        print(f"[GUARD] 恢复成功=True")
                        return True
            except Exception:
                pass
        print(f"[GUARD] 恢复失败 (已达上限 {MAX_DRIFT_RECOVERIES} 次)")
        return False

    def _get_visible_texts(self, xml_content: str) -> List[str]:
        if not xml_content or extract_visible_nodes is None:
            return []
        sw, sh = self._get_screen_size_cached()
        nodes = extract_visible_nodes(xml_content, sw, sh)
        return [n["text"] for n in nodes]

    # ============================================================
    #  展开按钮（委托 ExpandManager，v3.4: 传入 visible_nodes）
    # ============================================================
    def _expand_and_get_stats(self) -> Dict[str, Any]:
        if self._expand is None:
            return {"dom_found": 0, "visible_found": 0, "clickable_found": 0,
                    "unsafe_skipped": 0, "permanently_failed": 0,
                    "click_attempts": 0, "success": 0, "failed": 0,
                    "rounds": 0, "remaining_visible": 0,
                    "sources": {}, "final_xml": ""}
        try:
            xml = self._get_xml()
        except ADBError:
            return {"dom_found": 0, "visible_found": 0, "clickable_found": 0,
                    "unsafe_skipped": 0, "permanently_failed": 0,
                    "click_attempts": 0, "success": 0, "failed": 0,
                    "rounds": 0, "remaining_visible": 0,
                    "sources": {}, "final_xml": ""}

        # v3.4: 提取 visible_nodes，传入 ExpandManager
        sw, sh = self._get_screen_size_cached()
        if extract_visible_nodes is not None:
            visible_nodes = extract_visible_nodes(xml, sw, sh)
        else:
            visible_nodes = []

        stats = self._expand.expand_current_screen(visible_nodes)
        return stats

    def _accumulate_expand_stats(self, stats: Dict[str, Any]):
        """累加 ExpandManager 返回的统计到全局计数器。"""
        self._expand_dom_found += stats.get("dom_found", 0)
        self._expand_visible_found += stats.get("visible_found", 0)
        self._expand_clickable_found += stats.get("clickable_found", 0)
        self._expand_unsafe_skipped += stats.get("unsafe_skipped", 0)
        self._expand_permanently_failed += stats.get("permanently_failed", 0)
        self._expand_click_attempts += stats.get("click_attempts", 0)
        self._expand_clicks_success += stats.get("success", 0)
        self._expand_clicks_failed += stats.get("failed", 0)
        self._expand_rounds += stats.get("rounds", 0)
        self._expand_remaining_visible = stats.get("remaining_visible", 0)
        for k, v in stats.get("sources", {}).items():
            self._expand_sources[k] = self._expand_sources.get(k, 0) + v

    # ============================================================
    #  单页采集（v3.4: 保存 full XML 到 debug/，提取 visible_texts）
    # ============================================================
    def _save_debug_xml(self, xml_content: str, index: int):
        """保存完整 XML 到 debug/window_NNN.xml。"""
        if not xml_content:
            return
        try:
            fname = os.path.join(DEBUG_DIR, f"window_{index:03d}.xml")
            # 确保 XML 是字符串
            if isinstance(xml_content, bytes):
                xml_str = xml_content.decode("utf-8", errors="replace")
            else:
                xml_str = str(xml_content)
            with open(fname, "w", encoding="utf-8") as f:
                f.write(xml_str)
        except Exception:
            pass  # debug 保存失败不影响主流程

    def collect_page(self, xml_content: str = "", formal: bool = True) -> Dict[str, Any]:
        """
        采集当前页的文本。

        v3.4 改动：
          - 保存完整 XML 到 debug/window_NNN.xml
          - 提取 visible_texts 供 MD 导出使用
          - scroll_signature 只基于 visible_nodes

        Args:
            xml_content: 如果非空，直接使用此 XML，不重新 dump。
            formal:      是否为正式采集（True=保存到accumulator/MD，False=仅提取信息）。
        """
        self.page_read_count += 1
        page_no = self.page_read_count

        if not xml_content:
            try:
                xml_content = self._get_xml()
            except ADBError as e:
                print(f"  dump 失败: {e}")
                xml_content = ""

        # v3.4: 保存完整 XML 到 debug/
        self._save_debug_xml(xml_content, page_no)

        if xml_content:
            try:
                page_dict = extract_texts(xml_content, page=page_no)
            except Exception as e:
                page_dict = {"page": page_no, "texts": []}
        else:
            page_dict = {"page": page_no, "texts": []}

        texts: List[str] = list(page_dict.get("texts") or [])
        texts_sig = _texts_sig(texts)

        sw, sh = self._get_screen_size_cached()
        visible_nodes: List[Dict[str, Any]] = []
        visible_texts: List[str] = []
        visible_texts_source = "unavailable"
        visible_sig = ""
        scroll_sig = ""
        scroll_top_sig = ""
        full_dom_text_nodes = len(texts)

        if extract_visible_nodes is not None and xml_content:
            try:
                visible_nodes = extract_visible_nodes(xml_content, sw, sh)
                visible_texts = [n["text"] for n in visible_nodes]
                visible_texts_source = "extract_visible_nodes"
                if build_visible_signature is not None or visible_signature is not None:
                    sig_fn = build_visible_signature or visible_signature
                    visible_sig = sig_fn(visible_nodes) if sig_fn else ""
                if build_scroll_signature is not None or scroll_signature is not None:
                    sig_fn = build_scroll_signature or scroll_signature
                    scroll_sig = sig_fn(visible_nodes) if sig_fn else ""
                if build_scroll_top_signature is not None or scroll_top_signature is not None:
                    sig_fn = build_scroll_top_signature or scroll_top_signature
                    scroll_top_sig = sig_fn(visible_nodes) if sig_fn else ""
            except Exception:
                pass

        # v3.4: DEBUG_VISIBLE 模式打印对比
        if DEBUG_VISIBLE and visible_nodes:
            # 统计 full DOM 中的展开按钮数（仅用于 DEBUG）
            full_dom_expand = len(re.findall(r"展开今日全部\d+条操作", xml_content)) if xml_content else 0
            visible_expand = len([
                n for n in visible_nodes
                if re.match(r"^展开今日全部\d+条操作$", n["text"])
            ])
            print(f"\n[DEBUG_VISIBLE]")
            print(f"  full_dom_text_nodes = {full_dom_text_nodes}")
            print(f"  visible_nodes       = {len(visible_nodes)}")
            print(f"  full_dom_expand     = {full_dom_expand}")
            print(f"  visible_expand      = {visible_expand}")

        bottom_check = self._check_bottom_marker_visible(xml_content)
        self._last_bottom_check = bottom_check
        bottom_marker_found = bottom_check.get("found", False)
        bottom_marker_visible = bottom_check.get("visible", False)
        if bottom_marker_visible:
            self._bottom_marker_detected = True

        # 只在正式采集时才加入 accumulator
        if formal:
            # v3.4: accumulator 也存 visible_texts（真实可见文本，不回退为 texts）
            page_with_visible = dict(page_dict)
            page_with_visible["visible_texts"] = visible_texts
            added = self.accumulator.add_page(page_with_visible)
            new_texts = list(added.get("new_texts") or [])

            # 一致性日志：texts vs visible_texts
            texts_n = len(texts)
            visible_n = len(visible_texts)
            ratio = (visible_n / texts_n) if texts_n else 0.0
            print(
                "[PAGE_VISIBLE]\n"
                f"  page={page_no}\n"
                f"  texts_n={texts_n}\n"
                f"  visible_texts_n={visible_n}\n"
                f"  ratio={ratio:.3f}\n"
                f"  visible_texts_source={visible_texts_source}"
            )
            if visible_n == texts_n and texts_n > 0:
                print(
                    "[PAGE_VISIBLE] WARNING visible_texts_n == texts_n "
                    "(页面可能整屏可见，或 visible_texts 被误当成 full texts)"
                )
        else:
            new_texts = []

        expand_stats = self._current_expand_stats or {}

        info = {
            "page": page_no,
            "texts": texts,
            "visible_texts": visible_texts,
            "new_texts": new_texts,
            "texts_sig": texts_sig,
            "visible_sig": visible_sig,
            "scroll_sig": scroll_sig,
            "scroll_top_sig": scroll_top_sig,
            "visible_nodes_n": len(visible_nodes),
            "visible_texts_source": visible_texts_source,
            "full_dom_text_nodes": full_dom_text_nodes,
            "bottom_marker_found": bottom_marker_found,
            "bottom_marker_visible": bottom_marker_visible,
            "formal": formal,
        }
        if formal:
            self.history.append({
                "page": page_no,
                "swipe_before": self.swipe_count,
                "texts_n": len(texts),
                "visible_nodes_n": len(visible_nodes),
                "visible_texts_n": len(visible_texts),
                "visible_texts_source": visible_texts_source,
                "full_dom_text_nodes": full_dom_text_nodes,
                "new_texts_n": len(new_texts),
                "texts_sig": texts_sig,
                "visible_sig": visible_sig,
                "scroll_sig": scroll_sig,
                "scroll_top_sig": scroll_top_sig,
                "texts_sig_8": texts_sig[:8] + "…" if texts_sig else "",
                "visible_sig_8": visible_sig[:8] + "…" if visible_sig else "",
                "scroll_sig_8": scroll_sig[:8] + "…" if scroll_sig else "",
                "scroll_top_sig_8": scroll_top_sig[:8] + "…" if scroll_top_sig else "",
                "bottom_marker_found": bottom_marker_found,
                "bottom_marker_visible": bottom_marker_visible,
                "expand_dom_found": expand_stats.get("dom_found", 0),
                "expand_visible_found": expand_stats.get("visible_found", 0),
                "expand_clickable_found": expand_stats.get("clickable_found", 0),
                "expand_unsafe_skipped": expand_stats.get("unsafe_skipped", 0),
                "expand_permanently_failed": expand_stats.get("permanently_failed", 0),
                "expand_click_attempts": expand_stats.get("click_attempts", 0),
                "expand_success": expand_stats.get("success", 0),
                "expand_failed": expand_stats.get("failed", 0),
                "expand_remaining_visible": expand_stats.get("remaining_visible", 0),
                "expand_rounds": expand_stats.get("rounds", 0),
                "formal": True,
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            })
        return info

    # ============================================================
    #  到底判定（v3.4: 只检查 visible_nodes）
    # ============================================================
    def _check_bottom_marker_visible(self, xml_content: str) -> Dict[str, Any]:
        """
        v3.4: 只检查 visible_nodes 中是否存在「暂无更多内容」。
        完整 XML 有但 visible_nodes 没有 → 不算到底。
        """
        result: Dict[str, Any] = {
            "found": False, "visible": False, "bounds": "",
            "source": "", "center_y": 0, "screen_height": 0,
        }
        if not xml_content:
            return result
        try:
            sw, screen_h = self._get_screen_size_cached()
        except Exception:
            return result
        result["screen_height"] = screen_h

        # v3.4: 只从 visible_nodes 搜索
        if extract_visible_nodes is None:
            # 降级：检查 XML 字符串
            if _BOTTOM_TEXT in xml_content:
                result["found"] = True
                result["source"] = "regex_fallback"
                print(f"\n[BOTTOM] found=True（正则降级）")
            return result

        visible_nodes = extract_visible_nodes(xml_content, sw, screen_h)

        for vn in visible_nodes:
            if _BOTTOM_TEXT not in vn["text"]:
                continue
            result["found"] = True
            result["source"] = vn.get("source", "unknown")

            b = vn["bounds"]
            x1, y1, x2, y2 = b
            center_y = (y1 + y2) // 2
            result["bounds"] = f"[{x1},{y1}][{x2},{y2}]"
            result["center_y"] = center_y

            # 检查是否在屏幕下半部分
            in_bottom_half = center_y >= screen_h * 0.5
            result["visible"] = in_bottom_half

            print(f"\n[BOTTOM] found={result['found']}  visible={result['visible']}")
            print(f"  source={result['source']}  bounds={result['bounds']}")
            print(f"  center_y={center_y}  screen_h={screen_h}  in_bottom_half={in_bottom_half}")
            return result

        # 在 visible_nodes 中没找到 → 即使完整 XML 有也不认
        return result

    def _current_page_decision(self, xml_content: str) -> Dict[str, Any]:
        """基于当前 XML 计算「展开 vs 到底」决策所需字段。

        返回:
          {
            "bottom_detected": bool,    # 当前页可见「暂无更多内容」
            "expand_found": int,        # 当前页匹配到的展开按钮总数
            "expand_actionable": int,   # 可安全点击且未永久失败的按钮数
            "expand_unsafe": int,       # unsafe / 不可点击按钮数
            "expand_pending": bool,     # 是否还有待展开按钮
          }
        """
        sw, sh = self._get_screen_size_cached()

        visible_nodes: List[Dict[str, Any]] = []
        if xml_content and extract_visible_nodes is not None:
            try:
                visible_nodes = extract_visible_nodes(xml_content, sw, sh)
            except Exception:
                visible_nodes = []

        expand_found = 0
        expand_actionable = 0
        expand_unsafe = 0
        if self._expand is not None:
            try:
                nodes = self._expand.find_expand_nodes(visible_nodes, sw, sh)
                expand_found = len(nodes)
                expand_actionable = self._expand.count_actionable_expands(
                    visible_nodes, sw, sh)
                expand_unsafe = sum(1 for n in nodes if n.get("unsafe_reason"))
            except Exception:
                pass

        bottom_check = self._check_bottom_marker_visible(xml_content)
        bottom_detected = bool(bottom_check.get("visible", False))
        expand_pending = expand_actionable > 0

        print(f"\n[BOTTOM CHECK]")
        print(f"  bottom_detected={bottom_detected}")
        print(f"  expand_pending={expand_pending}")
        if bottom_detected and expand_pending:
            print(f"  decision=DEFER_BOTTOM_STOP")

        return {
            "bottom_detected": bottom_detected,
            "expand_found": expand_found,
            "expand_actionable": expand_actionable,
            "expand_unsafe": expand_unsafe,
            "expand_pending": expand_pending,
        }

    def _evaluate_stop(self, xml_content: str) -> Tuple[bool, str]:
        """展开优先的停止决策。

        只有同时满足 bottom_detected == True 且 actionable_expand_count == 0
        才允许 bottom stop。
        """
        decision = self._current_page_decision(xml_content)
        bottom_detected = decision["bottom_detected"]
        actionable = decision["expand_actionable"]

        print(f"\n[STOP DECISION]")
        print(f"  bottom_detected={bottom_detected}")
        print(f"  expand_found={decision['expand_found']}")
        print(f"  expand_actionable={actionable}")
        print(f"  expand_unsafe={decision['expand_unsafe']}")

        if bottom_detected and actionable == 0:
            self._stop_type = "bottom"
            print(f"  decision=stop_bottom")
            return True, "检测到暂无更多内容，且当前页无待展开内容"

        if self.swipe_count >= self.max_swipes:
            self._stop_type = "max_swipes"
            print(f"  decision=stop_max_swipes")
            return True, f"达到最大滑动轮数 {self.max_swipes}"

        if bottom_detected and actionable > 0:
            print(f"  decision=continue_expand")
        else:
            print(f"  decision=continue")
        return False, ""

    def is_finished(self, actionable_expand_count: int = 0) -> Tuple[bool, str]:
        """兼容保留：底部停止必须同时满足 bottom_detected 且无待展开内容。"""
        if self._bottom_marker_detected and actionable_expand_count == 0:
            self._stop_type = "bottom"
            return True, "检测到暂无更多内容，且当前页无待展开内容"
        if self.swipe_count >= self.max_swipes:
            self._stop_type = "max_swipes"
            return True, f"达到最大滑动轮数 {self.max_swipes}"
        return False, ""

    # ============================================================
    #  统一轮次日志（目标20）
    # ============================================================
    def _print_round_header(self, round_n: int):
        print(f"\n{'=' * 60}")
        print(f"[ROUND {round_n}]")
        print(f"{'=' * 60}")

    def _print_round_summary(self, round_n: int, target_page: bool,
                              visible_nodes_n: int, scroll_sig: str,
                              expand_stats: Dict[str, Any],
                              formal: bool, bottom_found: bool,
                              bottom_visible: bool,
                              scroll_before: str, scroll_after: str,
                              scroll_changed: bool, still_target: bool,
                              full_dom_n: int = 0):
        print(f"\n[PAGE]")
        print(f"  target_page={target_page}")
        print(f"  visible_nodes={visible_nodes_n}")
        if DEBUG_VISIBLE:
            print(f"  full_dom_text_nodes={full_dom_n}")
        print(f"  scroll_signature={scroll_sig[:20] if scroll_sig else '(n/a)'}…")

        exp = expand_stats or {}
        print(f"\n[EXPAND]")
        print(f"  dom_found={exp.get('dom_found', 0)}")
        print(f"  visible_safe={exp.get('clickable_found', 0)}")
        print(f"  unsafe={exp.get('unsafe_skipped', 0)}")
        print(f"  clicked={exp.get('click_attempts', 0)}")
        print(f"  success={exp.get('success', 0)}")
        print(f"  failed={exp.get('failed', 0)}")
        print(f"  permanently_failed={exp.get('permanently_failed', 0)}")
        print(f"  remaining_visible={exp.get('remaining_visible', 0)}")

        print(f"\n[COLLECT]")
        print(f"  正式保存页面={formal}")

        print(f"\n[BOTTOM]")
        print(f"  found={bottom_found}")
        print(f"  visible={bottom_visible}")

        if scroll_before:
            print(f"\n[SCROLL]")
            print(f"  before={scroll_before[:20]}…")
            print(f"  after={scroll_after[:20] if scroll_after else '(n/a)'}…")
            print(f"  changed={scroll_changed}")

        print(f"\n[GUARD]")
        print(f"  still_target_page={still_target}")
        print(f"{'=' * 60}")

    # ============================================================
    #  主循环（v3.2）
    # ============================================================
    def run(self) -> Dict[str, Any]:
        self._print_arch_check()

        print("\n" + "=" * 70)
        print("开始采集：蚂蚁财富盘友圈页面原始文本采集（v3.5 时序稳定）")
        print(f"  滑动区域         = safe_x={SAFE_SWIPE_XR:.0%}  纯纵向")
        print(f"  滑动检测         = scroll_signature（排除点赞/评论/时间噪声）")
        print(f"  到底判定         = visible_nodes（text+content-desc, 中心Y>=50%）")
        print(f"  展开逻辑         = ExpandManager v3.4（只使用 visible_nodes + double-dump确认）")
        print(f"  前置检查         = 锁屏检测+目标页面确认")
        print(f"  UI数据源         = {self._provider.name if self._provider else 'ADB shell dump (fallback)'}")
        if self._provider and hasattr(self._provider, 'config_info'):
            for k, v in self._provider.config_info.items():
                if k != "provider":
                    print(f"    {k} = {v}")
        # v3.5: 时序参数诊断
        print(f"\n[TIMING] 时序参数:")
        print(f"  FIRST_PAGE_STABILIZE_WAIT = {FIRST_PAGE_STABILIZE_WAIT}s")
        print(f"  POST_SWIPE_WAIT           = {POST_SWIPE_WAIT}s")
        print(f"  POST_EXPAND_WAIT          = {POST_EXPAND_WAIT}s")
        print(f"  STABLE_DUMP_MAX_ATTEMPTS  = {STABLE_DUMP_MAX_ATTEMPTS}")
        print(f"  STABLE_DUMP_INTERVAL      = {STABLE_DUMP_INTERVAL}s")
        if DEBUG_VISIBLE:
            print(f"  DEBUG_VISIBLE             = ✅ 已启用（将对比 full_dom vs visible_nodes）")
        print("=" * 70)

        # ---- 前置检查 ----
        if not self._preflight_check():
            if self._stop_type == "preflight_locked":
                return self._empty_result("锁屏状态 — 请手动解锁后重新运行")
            elif self._stop_type == "preflight_wrong_page":
                return self._empty_result("非目标页面 — 请手动打开「今日操作」后重新运行")
            else:
                return self._empty_result(f"前置检查失败: {self._stop_type}")

        print("\n[PRECHECK] 环境检查通过 ✅")
        print("[CRAWLER] 开始采集...\n")

        # v3.5: 首屏稳定等待（避免页面 WebView/Accessibility 异步渲染未完成就 dump）
        if FIRST_PAGE_STABILIZE_WAIT > 0:
            print(f"[TIMING] 首屏稳定等待 {FIRST_PAGE_STABILIZE_WAIT}s ...")
            time.sleep(FIRST_PAGE_STABILIZE_WAIT)

        # 初始轮
        round_n = 0

        while True:
            # v3.4.1: 检查 UI dump 失败
            if self._stop_type == "ui_dump_failed":
                stop_reason = "UI XML 获取失败（idle timeout）"
                break

            self._print_round_header(round_n)

            # ---- 展开优先：先处理当前页所有可见且安全的 expand ----
            # ExpandManager.expand_current_screen 内部已实现连续展开（逐个点击 +
            # 每轮重新 dump + 防无限点击），此处调用一次即可把当前页展开干净。
            self._current_expand_stats = self._expand_and_get_stats()
            self._accumulate_expand_stats(self._current_expand_stats)
            final_xml = self._current_expand_stats.get("final_xml", "")

            # v3.5: 展开后稳定等待 + 重新 dump（确保拿到展开后的最新 XML）
            if POST_EXPAND_WAIT > 0:
                print(f"[TIMING] 展开后稳定等待 {POST_EXPAND_WAIT}s ...")
                time.sleep(POST_EXPAND_WAIT)
                final_xml = self._get_stable_xml()

            # 验证目标页面
            target_page = self._guard_check(final_xml)
            if not target_page:
                recovered = self._guard_recover()
                if not recovered:
                    self._stop_type = "navigation_error"
                    stop_reason = "页面跑偏且无法恢复"
                    break

            # v3.5: 页面质量检查（正式采集前）
            sw_q, sh_q = self._get_screen_size_cached()
            quality = self._page_quality(final_xml, sw_q, sh_q)
            print(f"[PAGE QUALITY] round={round_n}  "
                  f"op_status={quality['op_status']}  "
                  f"fund_candidates={quality['fund_like']}  "
                  f"amount_labels={quality['amount_labels']}  "
                  f"amount_values={quality['amount_values']}")
            if quality["op_status"] > 0 and quality["amount_values"] < quality["op_status"]:
                print(f"[PAGE QUALITY]  交易结构可能未加载完整 "
                      f"(op_status={quality['op_status']} > amount_values={quality['amount_values']})，"
                      f"等待 {STABLE_DUMP_INTERVAL}s 后重新 dump...")
                time.sleep(STABLE_DUMP_INTERVAL)
                final_xml = self._get_xml()
                quality2 = self._page_quality(final_xml, sw_q, sh_q)
                print(f"[PAGE QUALITY] 补抓后: op_status={quality2['op_status']}  "
                      f"amount_values={quality2['amount_values']}")

            # ---- 展开后重新采集当前页（formal），确保展开新增的交易进入 visible_texts/raw_pages/MD ----
            page_info = self.collect_page(xml_content=final_xml, formal=target_page)
            scroll_sig = page_info.get("scroll_sig", "")

            self._print_round_summary(
                round_n, target_page, page_info.get("visible_nodes_n", 0), scroll_sig,
                self._current_expand_stats,
                target_page,
                page_info.get("bottom_marker_found", False),
                page_info.get("bottom_marker_visible", False),
                "", "", False, target_page,
                page_info.get("full_dom_text_nodes", 0),
            )

            # ---- 停止决策：展开优先（bottom_detected AND actionable==0 才 bottom stop）----
            finished, reason = self._evaluate_stop(final_xml)
            if finished:
                stop_reason = reason
                break

            # ---- 滑动到下一屏 ----
            scroll_sig_before = scroll_sig
            scroll_top_sig_before = page_info.get("scroll_top_sig", "")
            texts_sig_before = page_info.get("texts_sig", "")

            retry_count = 0
            scrolled = False
            after_info: Dict[str, Any] = {}

            for attempt in range(len(SWIPE_STRATEGIES)):
                self.scroll_up(retry_n=attempt, strategy_idx=attempt)
                # 滑动后采集（非正式：用于检测是否移动）
                after_info = self.collect_page(formal=False)

                # 跑偏检测与恢复
                try:
                    xml_after = self._get_xml()
                except Exception:
                    xml_after = ""
                still_target = self._guard_check(xml_after)
                if not still_target:
                    recovered = self._guard_recover()
                    if not recovered:
                        self._stop_type = "navigation_error"
                        stop_reason = "页面跑偏且无法恢复"
                        break
                    # 恢复后重新 dump
                    try:
                        xml_after = self._get_xml()
                        still_target = self._guard_check(xml_after)
                    except Exception:
                        still_target = False

                # 比较 scroll_signature。
                # 注意：这里不再用底部标志提前 break，底部判定统一交给下一轮循环
                # 顶部的 _evaluate_stop（展开优先），避免 bottom marker 抢占 expand。
                after_sig = after_info.get("scroll_sig", "")
                after_top_sig = after_info.get("scroll_top_sig", "")
                after_full_sig = after_info.get("texts_sig", "")
                changed_scroll = (scroll_sig_before and after_sig and
                                  scroll_sig_before != after_sig)
                top_changed = (scroll_top_sig_before and after_top_sig and
                              scroll_top_sig_before != after_top_sig)

                print(f"\n[SCROLL] 滑动后 full_text_signature={after_full_sig[:20]}…")
                print(f"[SCROLL] 滑动后 scroll_signature={after_sig[:20] if after_sig else '(n/a)'}…")
                print(f"[SCROLL] 滑动后 top_signature={after_top_sig[:20] if after_top_sig else '(n/a)'}…")
                print(f"[SCROLL] 页面 scroll 变化={'是' if changed_scroll else '否'}  "
                      f"顶部变化={'是' if top_changed else '否'}")

                # 只要 scroll_signature 变化即视为滑动成功。
                # 顶部不变是正常现象（固定导航 / 首个 KOL 尚未滚出），不能据此判定失败。
                if changed_scroll:
                    scrolled = True
                    break

                retry_count = attempt + 1
                if attempt + 1 < len(SWIPE_STRATEGIES):
                    next_label = SWIPE_STRATEGIES[attempt + 1][4]
                    print(f"  当前策略无效，重试 ({attempt+1}/{self.max_scroll_retries}): {next_label}")

            self._scroll_retry_total += retry_count
            self._last_retry_count = retry_count

            if self._stop_type == "navigation_error":
                stop_reason = "页面跑偏且无法恢复"
                break

            if not scrolled:
                # 滑动无变化：可能是真到底（底部标志刚露出）或页面卡住。
                # 用最新 XML 重新做一次停止决策（展开优先），避免误判为 stuck。
                try:
                    latest_xml = self._get_xml()
                except Exception:
                    latest_xml = ""
                finished2, reason2 = self._evaluate_stop(latest_xml)
                if finished2:
                    stop_reason = reason2
                else:
                    stop_reason = f"页面疑似卡住/滑动失败（重试{retry_count}次后页面仍无变化）"
                    self._stop_type = "stuck"
                break

            # 滑动成功，进入下一屏：round_n+1 后回到循环顶部，重新展开 + 采集 + 底部判定
            round_n += 1

        # ---------- 保存结果 ----------
        ts_tag = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = os.path.join(OUTPUT_DIR, f"raw_pages_{ts_tag}.json")
        all_unique_texts = sorted(self.accumulator.seen)

        # 组装每页 payload：visible_texts 缺失时不得静默回退为完整 texts
        result_pages = [_build_result_page_payload(p) for p in self.accumulator.pages]

        result_payload: Dict[str, Any] = {
            "mode": "raw_pages",
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "total_pages": self.accumulator.summary()["total_pages"],
            "total_unique_texts": self.accumulator.summary()["total_unique_texts"],
            "pages_read": self.page_read_count,
            "swipe_count": self.swipe_count,
            "stop_reason": stop_reason or "已完成",
            "stop_type": self._stop_type or "unknown",
            "bottom_marker_detected": self._bottom_marker_detected,
            "bottom_marker_visible": self._last_bottom_check.get("visible", False),
            "bottom_bounds": self._last_bottom_check.get("bounds", ""),
            "bottom_center_y": self._last_bottom_check.get("center_y", 0),
            "bottom_screen_height": self._last_bottom_check.get("screen_height", 0),
            "bottom_source": self._last_bottom_check.get("source", ""),
            "scroll_retry_total": self._scroll_retry_total,
            "expand_dom_found": self._expand_dom_found,
            "expand_visible_found": self._expand_visible_found,
            "expand_clickable_found": self._expand_clickable_found,
            "expand_unsafe_skipped": self._expand_unsafe_skipped,
            "expand_permanently_failed": self._expand_permanently_failed,
            "expand_click_attempts": self._expand_click_attempts,
            "expand_clicks_success": self._expand_clicks_success,
            "expand_clicks_failed": self._expand_clicks_failed,
            "expand_remaining_visible": self._expand_remaining_visible,
            "expand_rounds": self._expand_rounds,
            "expand_sources": self._expand_sources,
            "pages": result_pages,
            "all_unique_texts": all_unique_texts,
            "history": self.history,
        }
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(result_payload, f, ensure_ascii=False, indent=2)

        output_screen_md = ""
        try:
            if export_screen_dump_md is not None:
                output_screen_md = export_screen_dump_md(result_payload)
        except Exception as exc:
            print(f"  screen_dump MD 导出失败: {exc}")

        print("\n" + "=" * 70)
        print("采集结束 (v3.2)")
        print(f"  总页数           : {len(self.accumulator.pages)}")
        print(f"  读取页面次数     : {self.page_read_count}")
        print(f"  累计唯一文本     : {len(all_unique_texts)} 行")
        print(f"  滑动次数         : {self.swipe_count}/{self.max_swipes}")
        print(f"  滑动重试         : {self._scroll_retry_total}")
        print(f"  展开 DOM发现     : {self._expand_dom_found}")
        print(f"  展开 可见        : {self._expand_visible_found}")
        print(f"  展开 可点击      : {self._expand_clickable_found}")
        print(f"  展开 unsafe跳过  : {self._expand_unsafe_skipped}")
        print(f"  展开 永久失败    : {self._expand_permanently_failed}")
        print(f"  展开 点击尝试    : {self._expand_click_attempts}")
        print(f"  展开 成功        : {self._expand_clicks_success}")
        print(f"  展开 失败        : {self._expand_clicks_failed}")
        print(f"  检测到底标志     : {'是' if self._bottom_marker_detected else '否'}")
        print(f"  停止类型         : {self._stop_type or 'unknown'}")
        print(f"  停止原因         : {stop_reason}")
        print(f"  JSON 结果        : {output_file}")
        if output_screen_md:
            print(f"  镜像 MD 文件     : {output_screen_md}")
        print("=" * 70)

        result_payload["output_file"] = output_file
        result_payload["output_screen_md"] = output_screen_md
        return result_payload

    def _empty_result(self, reason: str) -> Dict[str, Any]:
        return {
            "mode": "raw_pages", "total_pages": 0, "total_unique_texts": 0,
            "swipe_count": 0, "pages_read": 0, "stop_reason": reason,
            "stop_type": self._stop_type,
            "bottom_marker_detected": False, "bottom_marker_visible": False,
            "bottom_bounds": "", "bottom_center_y": 0, "bottom_screen_height": 0,
            "scroll_retry_total": 0,
            "expand_dom_found": 0, "expand_visible_found": 0,
            "expand_clickable_found": 0, "expand_unsafe_skipped": 0,
            "expand_permanently_failed": 0,
            "expand_click_attempts": 0, "expand_clicks_success": 0,
            "expand_clicks_failed": 0, "expand_remaining_visible": 0,
            "expand_rounds": 0, "expand_sources": {},
            "pages": [], "all_unique_texts": [], "history": [],
            "output_file": "",
        }


# ============================================================
#  命令行自测入口
# ============================================================
def _build_cli() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="ScrollManager v3.2")
    p.add_argument("--max", type=int, default=DEFAULT_MAX_SWIPES)
    p.add_argument("--max-retries", type=int, default=DEFAULT_MAX_SCROLL_RETRIES)
    p.add_argument("--wait", type=float, default=DEFAULT_WAIT_AFTER_SWIPE_SEC)
    p.add_argument("-s", "--serial", help="设备序列号")
    p.add_argument("--no-expand", action="store_true")
    return p


def _main():
    args = _build_cli().parse_args()
    sm = ScrollManager(
        device_serial=args.serial,
        max_swipes=args.max,
        wait_after_swipe_sec=args.wait,
        max_scroll_retries=args.max_retries,
        enable_expand=not args.no_expand,
    )
    try:
        sm.run()
    except KeyboardInterrupt:
        print("\n\n 用户中断")
        sys.exit(130)


if __name__ == "__main__":
    _main()
