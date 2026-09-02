"""
UIHierarchyProvider — UI XML 获取抽象层
=======================================

v3.4.1 引入，解耦业务层与 XML 获取方式。

架构：
    ScrollManager / ExpandManager / VisibleNodeExtractor
                     ↓
              UIHierarchyProvider          ← 抽象基类
              ├── ShellUiAutomatorProvider ← 默认（adb shell uiautomator dump）
              └── UiAutomator2Provider     ← 可选（通过 ATX agent HTTP，需 UI_PROVIDER=uiautomator2）

配置：
    UI_PROVIDER          = 环境变量，默认 shell；显式 UI_PROVIDER=uiautomator2 才启用 uiautomator2
    U2_WAIT_IDLE_TIMEOUT = 环境变量或直接赋值，默认 500ms
"""

from __future__ import annotations

import os
import sys
import re
import time
import json
import urllib.request
import urllib.error
import subprocess
import datetime
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, Tuple

_THIS_FILE = os.path.abspath(__file__)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_THIS_FILE))
_VENDOR_DIR = os.path.join(_PROJECT_ROOT, "vendor")

# 环境变量配置
U2_WAIT_IDLE_TIMEOUT = int(os.environ.get("U2_WAIT_IDLE_TIMEOUT", "500"))

# Provider 选择：默认 shell；仅显式 UI_PROVIDER=uiautomator2 才启用 uiautomator2
UI_PROVIDER = (os.environ.get("UI_PROVIDER") or "shell").strip().lower()


# ============================================================
#  抽象基类
# ============================================================
class UIHierarchyProvider(ABC):
    """UI XML 获取抽象层。

    业务代码只依赖 provider.get_xml()，不关心 XML 来源。
    """

    @abstractmethod
    def get_xml(self) -> str:
        """获取当前 UI hierarchy XML。

        Returns:
            XML 字符串

        Raises:
            UIDumpError: 获取失败（含 idle timeout 等子类型）
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider 名称，用于日志。"""
        ...

    @property
    def config_info(self) -> Dict[str, Any]:
        """Provider 配置信息，启动时打印。"""
        return {"provider": self.name}


class UIDumpError(Exception):
    """UI XML 获取失败异常。"""

    def __init__(self, message: str, error_type: str = "unknown"):
        super().__init__(message)
        self.error_type = error_type


# ============================================================
#  ShellUiAutomatorProvider — adb shell uiautomator dump（fallback）
# ============================================================
class ShellUiAutomatorProvider(UIHierarchyProvider):
    """通过 adb shell uiautomator dump 获取 XML。

    限制：
      - 最多尝试 2 次
      - 一旦出现 "could not get idle state" 立即返回 UIDumpError
      - 不再无限重试
    """

    _MAX_RETRIES = 2

    def __init__(self, adb_controller):
        self._adb = adb_controller

    @property
    def name(self) -> str:
        return "ShellUiAutomatorProvider"

    def get_xml(self) -> str:
        last_error = None
        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                xml = self._adb.dump_and_pull_xml(skip_check=True)
                return xml
            except Exception as e:
                err_msg = str(e)
                last_error = err_msg
                # 检查是否 idle timeout
                if "could not get idle state" in err_msg.lower() or \
                   "idle" in err_msg.lower():
                    raise UIDumpError(
                        f"UI_DUMP_IDLE_TIMEOUT (attempt {attempt}/{self._MAX_RETRIES}): "
                        f"页面持续产生 AccessibilityEvent，shell dump 不可用",
                        error_type="idle_timeout",
                    )
                if attempt < self._MAX_RETRIES:
                    print(f"[ShellUiAutomator] 第{attempt}次尝试失败: {err_msg}")
                    time.sleep(0.5)
        raise UIDumpError(
            f"Shell dump 失败 ({self._MAX_RETRIES} 次): {last_error}",
            error_type="shell_dump_failed",
        )


# ============================================================
#  UiAutomator2Provider — uiautomator2 ATX agent HTTP（优先）
# ============================================================
class UiAutomator2Provider(UIHierarchyProvider):
    """通过 uiautomator2 ATX agent HTTP 服务获取 XML。

    不走 adb shell uiautomator dump，而是通过 HTTP 请求 ATX agent 的
    /dump/hierarchy 接口。不调用 waitForIdle()，可配置 waitForIdleTimeout。

    依赖：
      - vendor/uiautomator2 （pip install --target=vendor）
      - 设备上有 ATX agent（首次连接自动推送）
    """

    _DEFAULT_WAIT_IDLE_TIMEOUT = U2_WAIT_IDLE_TIMEOUT  # ms

    def __init__(self, device_serial: str, wait_idle_timeout: int = None):
        """
        Args:
            device_serial:   设备序列号
            wait_idle_timeout: waitForIdle 超时(ms)，默认 U2_WAIT_IDLE_TIMEOUT
        """
        self._serial = device_serial
        self._wait_idle_timeout = (
            wait_idle_timeout if wait_idle_timeout is not None
            else self._DEFAULT_WAIT_IDLE_TIMEOUT
        )
        self._d: Any = None  # uiautomator2.Device, lazy init
        self._init_ok = False

    @property
    def name(self) -> str:
        return "UiAutomator2Provider"

    @property
    def config_info(self) -> Dict[str, Any]:
        return {
            "provider": self.name,
            "waitForIdleTimeout": f"{self._wait_idle_timeout}ms",
            "method": "ATX agent HTTP /dump/hierarchy",
        }

    def _ensure_device(self) -> Any:
        """延迟初始化 uiautomator2 connection，避免启动时阻塞。"""
        if self._d is not None:
            return self._d

        # 添加 vendor 到 sys.path
        if _VENDOR_DIR not in sys.path:
            sys.path.insert(0, _VENDOR_DIR)

        try:
            import uiautomator2 as u2  # type: ignore
        except ImportError:
            raise UIDumpError(
                "uiautomator2 未安装。请运行: pip install --target=vendor uiautomator2",
                error_type="import_error",
            )

        try:
            self._d = u2.connect(self._serial)
            # 设置 wait_timeout
            self._d.wait_timeout = self._wait_idle_timeout / 1000.0
            self._init_ok = True
            return self._d
        except Exception as e:
            raise UIDumpError(
                f"uiautomator2 连接失败: {e}",
                error_type="connection_error",
            )

    def get_xml(self) -> str:
        d = self._ensure_device()
        try:
            t0 = time.time()
            xml = d.dump_hierarchy()
            elapsed = (time.time() - t0) * 1000
            if elapsed > 2000:
                print(f"[U2Provider] ⚠ 获取 XML 耗时 {elapsed:.0f}ms > 2s")
            return xml
        except Exception as e:
            err_msg = str(e)
            if "timeout" in err_msg.lower() or "idle" in err_msg.lower():
                raise UIDumpError(
                    f"UI_DUMP_IDLE_TIMEOUT (uiautomator2, "
                    f"waitForIdleTimeout={self._wait_idle_timeout}ms): {err_msg}",
                    error_type="idle_timeout",
                )
            raise UIDumpError(
                f"uiautomator2 dump_hierarchy 失败: {err_msg}",
                error_type="dump_error",
            )


# ============================================================
#  Provider 工厂
# ============================================================
def create_provider(
    adb_controller,
    prefer: str = "shell",
    device_serial: str = "",
    wait_idle_timeout: int = None,
) -> Tuple[UIHierarchyProvider, str]:
    """
    创建 UIHierarchyProvider。

    选择规则（仅由环境变量 UI_PROVIDER 决定，默认 shell）：
      - UI_PROVIDER=shell（默认）      → ShellUiAutomatorProvider
      - UI_PROVIDER=uiautomator2 / u2 → UiAutomator2Provider（需已安装）

    prefer 参数保留以兼容旧调用方（如 ScrollManager 会传入 "u2"），
    但不再作为选择依据，避免每次启动都误触发 uiautomator2。

    Returns:
        (provider, source_note) — source_note 描述实际使用的 provider 来源
    """
    if not device_serial:
        device_serial = adb_controller.device_serial or ""

    # 归一化 provider 选择：仅 shell / uiautomator2 两种
    choice = (os.environ.get("UI_PROVIDER") or "shell").strip().lower()
    if choice in ("u2", "uiautomator2"):
        choice = "uiautomator2"
    else:
        choice = "shell"

    if choice == "uiautomator2":
        # 仅显式配置 UI_PROVIDER=uiautomator2 时才会走到这里
        try:
            provider = UiAutomator2Provider(
                device_serial=device_serial,
                wait_idle_timeout=wait_idle_timeout,
            )
            # 快速验证连接性
            provider._ensure_device()
            print("[PROVIDER] 使用 UiAutomator2Provider (ATX agent)")
            return provider, "UiAutomator2Provider (ATX agent)"
        except UIDumpError as e:
            print(f"[PROVIDER] ⚠ UiAutomator2Provider 初始化失败: {e}")
            print("[PROVIDER] 请先安装 uiautomator2：pip install --target=vendor uiautomator2")
            raise
        except Exception as e:
            print(f"[PROVIDER] ⚠ UiAutomator2Provider 初始化异常: {e}")
            raise

    # 默认 shell
    print("[PROVIDER] 使用 ShellUiAutomatorProvider")
    provider = ShellUiAutomatorProvider(adb_controller)
    return provider, "ShellUiAutomatorProvider"


# ============================================================
#  验证脚本 (目标7)
# ============================================================
def validate_provider(
    provider: UIHierarchyProvider,
    rounds: int = 10,
    interval_sec: float = 1.0,
    screen_w: int = 1440,
    screen_h: int = 3120,
    use_visible_extractor: bool = True,
) -> Dict[str, Any]:
    """
    最小验证：连续获取 N 次 XML，统计成功率和耗时。

    Args:
        provider:          UI XML provider
        rounds:            测试轮数
        interval_sec:      轮次间隔（秒）
        screen_w, screen_h: 屏幕尺寸
        use_visible_extractor: 是否使用 extract_visible_nodes 提取可见节点

    Returns:
        {
            "success": int,
            "fail": int,
            "times": [float, ...],       # 每次耗时(秒)
            "xml_chars": [int, ...],     # XML 字符数
            "visible_nodes_n": [int, ...],  # visible_nodes 数量
            "expand_clickable_n": [int, ...],  # 可点击展开按钮数
            "avg_time": float,
            "min_time": float,
            "max_time": float,
            "errors": [str, ...]         # 错误信息
        }
    """
    try:
        from .raw_text_extractor import extract_visible_nodes
    except ImportError:
        try:
            from core.raw_text_extractor import extract_visible_nodes
        except ImportError:
            extract_visible_nodes = None  # type: ignore
            use_visible_extractor = False

    times = []
    xml_chars = []
    visible_n = []
    expand_n = []
    errors = []
    success = 0
    fail = 0

    print(f"\n{'=' * 60}")
    print(f"UIHierarchyProvider 验证 ({provider.name})")
    print(f"  rounds={rounds}  interval={interval_sec}s")
    print(f"  use_visible_extractor={use_visible_extractor}")
    print(f"{'=' * 60}")

    for i in range(1, rounds + 1):
        try:
            t0 = time.time()
            xml = provider.get_xml()
            t1 = time.time()
            elapsed = t1 - t0
            times.append(elapsed)
            xml_chars.append(len(xml))
            success += 1

            vn_count = 0
            exp_count = 0
            if use_visible_extractor and extract_visible_nodes is not None:
                vn = extract_visible_nodes(xml, screen_w, screen_h)
                vn_count = len(vn)
                visible_n.append(vn_count)
                exp_btns = [
                    n for n in vn
                    if re.match(r"^展开今日全部\d+条操作$", n.get("text", ""))
                    and n.get("clickable")
                ]
                exp_count = len(exp_btns)
                expand_n.append(exp_count)

            print(
                f"  [{i:2d}] chars={len(xml):6d}  "
                f"visible_nodes={vn_count:3d}  "
                f"expand_clickable={exp_count:2d}  "
                f"time={elapsed:.3f}s"
            )

        except UIDumpError as e:
            fail += 1
            errors.append(str(e))
            print(f"  [{i:2d}] FAIL ({e.error_type}): {e}")
        except Exception as e:
            fail += 1
            errors.append(str(e))
            print(f"  [{i:2d}] FAIL: {e}")

        if i < rounds:
            time.sleep(interval_sec)

    avg_time = sum(times) / len(times) if times else 0

    print(f"\n{'=' * 60}")
    print(f"RESULTS: {success}/{rounds} success, {fail}/{rounds} fail")
    print(f"TIME: min={min(times) if times else 0:.3f}s  "
          f"max={max(times) if times else 0:.3f}s  "
          f"avg={avg_time:.3f}s")
    if visible_n:
        print(f"VISIBLE: min={min(visible_n)}  max={max(visible_n)}  "
              f"avg={sum(visible_n)/len(visible_n):.0f}")
        print(f"EXPAND_CLICKABLE: {expand_n}")
    if errors:
        print(f"ERRORS: {errors}")
    print(f"{'=' * 60}")

    return {
        "provider": provider.name,
        "success": success,
        "fail": fail,
        "times": times,
        "xml_chars": xml_chars,
        "visible_nodes_n": visible_n,
        "expand_clickable_n": expand_n,
        "avg_time": avg_time,
        "min_time": min(times) if times else 0,
        "max_time": max(times) if times else 0,
        "errors": errors,
    }


# ============================================================
#  独立验证入口
# ============================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="UIHierarchyProvider 验证")
    parser.add_argument("--serial", default="", help="设备序列号")
    parser.add_argument("--rounds", type=int, default=10, help="测试轮数")
    parser.add_argument("--interval", type=float, default=1.0, help="轮次间隔(秒)")
    _cli_prefer_default = "u2" if UI_PROVIDER in ("u2", "uiautomator2") else "shell"
    parser.add_argument("--prefer", default=_cli_prefer_default, choices=["u2", "shell"],
                        help="优先 provider（默认取环境变量 UI_PROVIDER，缺省 shell）")
    parser.add_argument("--timeout", type=int, default=500,
                        help="waitForIdleTimeout (ms)")
    args = parser.parse_args()

    # 需要 ADB controller
    try:
        from .adb_controller import ADBController
    except ImportError:
        from core.adb_controller import ADBController

    adb = ADBController()
    serial = args.serial or adb.device_serial or ""

    print(f"\n[UI SOURCE]")
    print(f"  prefer = {args.prefer}")
    print(f"  timeout = {args.timeout}ms")
    print(f"  serial = {serial}")

    provider, note = create_provider(
        adb, prefer=args.prefer, device_serial=serial,
        wait_idle_timeout=args.timeout,
    )

    print(f"  active = {provider.name}")
    print(f"  note = {note}")

    validate_provider(provider, rounds=args.rounds, interval_sec=args.interval)
