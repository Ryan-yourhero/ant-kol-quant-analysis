"""
环境检测模块 —— 供 `python main.py --check-env` 调用。

覆盖 6 项检查：
  1. [✓] Python环境     版本 / 项目根 / venv 激活状态
  2. [✓] ADB路径        settings.ADB_PATH → env ADB_PATH → shutil.which
  3. [✓] ADB版本        执行 adb version 能否拿到版本号
  4. [✓] 手机连接状态    adb devices -l（单台 device 可用、unauthorized/offline 提示）
  5. [✓] 应用状态       pm list packages + dumpsys activity（已安装 / 前台运行）
  6. [✓] Excel依赖      openpyxl 可导入 + 版本

用法：
    python -m core.env_checker
    # 或
    from core.env_checker import run_all_checks
    ok = run_all_checks(adb_path=..., verbose=True)
"""

from __future__ import annotations

import importlib
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# ------------------------------------------------------------------
# 导入项目模块（允许单独运行 `python core/env_checker.py`）
# ------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(_HERE)
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

try:
    from core import adb_controller as _adb_ctrl
except Exception:  # pragma: no cover - 单独运行时兜底
    _adb_ctrl = None  # type: ignore


# ==================================================================
#  工具：彩色输出（Windows 下不依赖 colorama，用 ANSI 字符串即可）
# ==================================================================
_OK = "\033[32m✓\033[0m"
_FAIL = "\033[31m✗\033[0m"
_WARN = "\033[33m!\033[0m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _msg(title: str, ok: bool, detail: str = "", fixes: Optional[List[str]] = None) -> str:
    mark = _OK if ok else _FAIL
    line = f"[{mark}] {title}"
    if detail:
        line += f"  {detail}"
    if not ok and fixes:
        line += "\n" + "\n".join(f"      👉 {f}" for f in fixes)
    return line


@dataclass
class CheckResult:
    title: str
    ok: bool
    detail: str = ""
    fixes: List[str] = field(default_factory=list)

    def print(self) -> None:
        print(_msg(self.title, self.ok, self.detail, self.fixes))


# ==================================================================
#  1. Python 环境
# ==================================================================
def check_python() -> CheckResult:
    v = sys.version_info
    ok = (v.major, v.minor) >= (3, 9)
    detail = (
        f"{sys.executable}  "
        f"Python {v.major}.{v.minor}.{v.micro}  "
        f"{platform.system()} {platform.release()}"
    )
    fixes: List[str] = []
    if not ok:
        fixes.append("升级 Python 到 3.9+（推荐 3.10 / 3.11）：https://www.python.org/downloads/")
    venv_ok = bool(getattr(sys, "real_prefix", None)) or "VIRTUAL_ENV" in os.environ or sys.prefix != sys.base_prefix
    if not venv_ok:
        fixes.append("建议先激活虚拟环境：.venv\\Scripts\\activate  (或 source .venv/bin/activate)")
    else:
        detail += "  (venv已激活)"
    return CheckResult("Python环境", ok, detail, fixes)


# ==================================================================
#  2. ADB 路径
# ==================================================================
def check_adb_path(adb_path_arg: Optional[str]) -> Tuple[CheckResult, Optional[str]]:
    """
    返回 (结果, 解析到的 adb 绝对路径或None)
    """
    # 优先用公开函数 resolve_adb_path，没有就走 _settings 兜底
    resolved: Optional[str] = None
    source = ""
    attempts: List[Tuple[str, str]] = []

    if _adb_ctrl is not None and hasattr(_adb_ctrl, "resolve_adb_path"):
        try:
            info = _adb_ctrl.resolve_adb_path(
                custom_path=adb_path_arg,
                raise_on_missing=False,
                return_info=True,
            )
            resolved = info.get("resolved")
            source = info.get("source", "")
            attempts = info.get("attempts", [])
        except Exception as e:  # pragma: no cover
            attempts = [("resolve_adb_path 抛异常", str(e))]

    if resolved is None:
        # 兜底：手动 3 级查找，保证 env_checker 独立可用
        if adb_path_arg and os.path.isfile(os.path.expandvars(os.path.expanduser(adb_path_arg))):
            resolved = os.path.abspath(adb_path_arg)
            source = "命令行 --adb-path"
        else:
            if adb_path_arg:
                attempts.append(("--adb-path 参数", f"文件不存在: {adb_path_arg}"))
            try:
                from config import settings as _s  # type: ignore

                sv = getattr(_s, "ADB_PATH", None)
                if sv and os.path.isfile(os.path.expandvars(os.path.expanduser(sv))):
                    resolved = os.path.abspath(os.path.expandvars(os.path.expanduser(sv)))
                    source = "config/settings.py ADB_PATH"
                elif sv:
                    attempts.append(("settings.ADB_PATH", f"文件不存在: {sv}"))
            except Exception as e:  # pragma: no cover
                attempts.append(("导入 settings.py", str(e)))

            if not resolved:
                ev = os.environ.get("ADB_PATH")
                if ev and os.path.isfile(os.path.expandvars(os.path.expanduser(ev))):
                    resolved = os.path.abspath(os.path.expandvars(os.path.expanduser(ev)))
                    source = "环境变量 ADB_PATH"
                elif ev:
                    attempts.append(("环境变量 ADB_PATH", f"文件不存在: {ev}"))

            if not resolved:
                for name in ("adb.exe", "adb") if os.name == "nt" else ("adb",):
                    w = shutil.which(name)
                    if w:
                        resolved = w
                        source = f"系统 PATH ({name})"
                        break
                else:
                    attempts.append(("shutil.which(adb)", "不在 PATH 中"))

    detail = f"{resolved or '未找到'}  (来源: {source or '-'})"
    fixes: List[str] = []
    if not resolved:
        fixes += [
            "下载 Android SDK Platform Tools: https://developer.android.com/tools/releases/platform-tools",
            "在 config/settings.py 配置:  ADB_PATH = r'C:\\Android\\platform-tools\\adb.exe'",
            "或设置环境变量:  set ADB_PATH=C:\\Android\\platform-tools\\adb.exe",
            "或把 platform-tools 目录加入系统 PATH 后重启终端",
        ]
    r = CheckResult("ADB路径", bool(resolved), detail, fixes)
    if attempts and not resolved:
        r.detail += f"  (已尝试 {len(attempts)} 条候选)"
    return r, resolved


# ==================================================================
#  3. ADB 版本
# ==================================================================
def _run(args: List[str], timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
    )


def check_adb_version(adb_exe: Optional[str]) -> CheckResult:
    if not adb_exe:
        return CheckResult("ADB版本", False, "ADB路径未解析，跳过", ["先解决 [ADB路径] 项"])
    try:
        r = _run([adb_exe, "version"])
    except Exception as e:
        return CheckResult(
            "ADB版本",
            False,
            f"执行异常: {e}",
            ["杀毒软件/防火墙拦截 adb.exe → 将 platform-tools 加入白名单"],
        )
    if r.returncode != 0:
        return CheckResult(
            "ADB版本",
            False,
            f"exit={r.returncode}  {r.stderr.strip() or r.stdout.strip()[:200]}",
            ["执行 adb kill-server && adb start-server 重启 ADB 服务"],
        )
    m = re.search(r"Android Debug Bridge version\s+([\d\.]+)", r.stdout)
    ver = m.group(1) if m else (r.stdout.splitlines()[0] if r.stdout.strip() else "解析失败")
    return CheckResult("ADB版本", True, f"版本 {ver}")


# ==================================================================
#  4. 手机连接状态
# ==================================================================
def check_device(adb_exe: Optional[str], device_serial: Optional[str]) -> Tuple[CheckResult, Optional[str]]:
    """返回 (结果, 选中的serial 或 None)"""
    if not adb_exe:
        return CheckResult("手机连接状态", False, "ADB路径未解析，跳过", ["先解决 [ADB路径] 项"]), None
    try:
        r = _run([adb_exe, "devices", "-l"])
    except Exception as e:
        return CheckResult("手机连接状态", False, f"执行异常: {e}", []), None
    if r.returncode != 0:
        return (
            CheckResult(
                "手机连接状态",
                False,
                f"exit={r.returncode}  {r.stderr.strip()[:200]}",
                ["执行 adb kill-server / adb start-server 重启 ADB"],
            ),
            None,
        )
    devices: List[dict] = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        model = ""
        for p in parts[2:]:
            if p.startswith("model:"):
                model = p[len("model:") :]
                break
        devices.append({"serial": serial, "state": state, "model": model})

    if not devices:
        return (
            CheckResult(
                "手机连接状态",
                False,
                "未检测到任何设备",
                [
                    "USB 连接手机，确认通知栏有 USB 调试提示",
                    "手机 → 设置 → 开发者选项 → 打开 USB 调试 / 无线调试",
                    "首次连接时手机弹窗点击 [允许 USB 调试]（勾选始终允许）",
                    "数据线换一条 / 换 USB 口（部分仅充电线不支持调试）",
                ],
            ),
            None,
        )

    # 问题状态提示
    bad = [d for d in devices if d["state"] != "device"]
    fixes: List[str] = []
    for d in bad:
        if d["state"] == "unauthorized":
            fixes.append(f"设备 {d['serial']} unauthorized: 手机端 [允许 USB 调试]，或执行 adb kill-server")
        elif d["state"] == "offline":
            fixes.append(f"设备 {d['serial']} offline: 拔掉 USB 重插，或执行 adb reconnect offline")
        else:
            fixes.append(f"设备 {d['serial']} 状态 {d['state']}：重启手机开发者选项或 adb 服务")

    available = [d for d in devices if d["state"] == "device"]
    if not available:
        return CheckResult("手机连接状态", False, f"{len(devices)}台设备但无可用状态 {[(d['serial'], d['state']) for d in devices]}", fixes), None

    chosen: Optional[str] = None
    detail = f"可用 {len(available)}/{len(devices)} 台: " + "; ".join(
        f"{d['serial']}({d['state']}{', ' + d['model'] if d['model'] else ''})" for d in available
    )
    if device_serial:
        if any(d["serial"] == device_serial and d["state"] == "device" for d in devices):
            chosen = device_serial
            detail += f"  已指定 -s {device_serial}"
        else:
            fixes.append(f"指定序列号 {device_serial} 不在可用列表中，改列表自动选第一台或修正 --device")
            chosen = available[0]["serial"]
    else:
        if len(available) == 1:
            chosen = available[0]["serial"]
        else:
            fixes.append("多设备在线：在 settings.py 配置 TARGET_DEVICE_SERIAL，或传参 --device SERIAL")
            chosen = available[0]["serial"]
            detail += f"  自动选: {chosen}"

    return CheckResult("手机连接状态", True, detail, fixes), chosen


# ==================================================================
#  5. 应用状态
# ==================================================================
def check_app(adb_exe: Optional[str], serial: Optional[str]) -> CheckResult:
    if not adb_exe:
        return CheckResult("应用状态", False, "ADB路径未解析", ["先解决 [ADB路径]"])
    if not serial:
        return CheckResult("应用状态", False, "无可用于检测的设备序列号", ["先解决 [手机连接状态]"])

    try:
        from config import settings as _s  # type: ignore

        pkg = getattr(_s, "ANT_WEALTH_PACKAGE", "com.antfortune.wealth")
    except Exception:  # pragma: no cover
        pkg = "com.antfortune.wealth"

    # 5.1 是否已安装
    cmd_base: List[str] = [adb_exe]
    if serial:
        cmd_base += ["-s", serial]
    try:
        r = _run(cmd_base + ["shell", "pm", "list", "packages", pkg])
    except Exception as e:
        return CheckResult("应用状态", False, f"pm list packages 异常: {e}", [])
    installed = f"package:{pkg}" in (r.stdout or "")

    # 5.2 是否正在前台
    foreground = False
    detail_parts = []
    if installed:
        detail_parts.append(f"{pkg} 已安装")
        try:
            r2 = _run(cmd_base + ["shell", "dumpsys", "activity", "activities"])
            out = r2.stdout or ""
            if pkg in out and (
                "mResumedActivity" in out or "mResumed" in out or "topResumedActivity" in out
            ):
                # 精确判断：最近的 resumed 行是否含 pkg
                for line in out.splitlines():
                    if ("mResumed" in line or "topResumedActivity" in line) and pkg in line:
                        foreground = True
                        break
        except Exception:
            foreground = False
        if foreground:
            detail_parts.append("前台运行中 ✅")
        else:
            detail_parts.append("未在前台")
    else:
        detail_parts.append(f"{pkg} 未安装")

    fixes: List[str] = []
    if not installed:
        fixes += [
            "打开手机应用商店搜索 [蚂蚁财富] 安装",
            "或下载 APK 后执行:  adb -s " + (serial or "") + f" install ant_wealth.apk",
        ]
    if installed and not foreground:
        fixes += [
            "手动打开 蚂蚁财富 APP → 登录账号 → 停留在要采集的页面",
            "或命令行启动:  adb -s " + (serial or "") + f" shell monkey -p {pkg} -c android.intent.category.LAUNCHER 1",
        ]
    return CheckResult("应用状态", installed, "  ".join(detail_parts), fixes)


# ==================================================================
#  6. Excel 依赖
# ==================================================================
def check_excel_dep() -> CheckResult:
    try:
        mod = importlib.import_module("openpyxl")
    except Exception as e:
        return CheckResult(
            "Excel依赖",
            False,
            f"openpyxl 导入失败: {e}",
            ["执行:  pip install openpyxl", "或:  pip install -r requirements.txt"],
        )
    ver = getattr(mod, "__version__", "(无__version__)")
    return CheckResult("Excel依赖", True, f"openpyxl {ver}")


# ==================================================================
#  统一入口
# ==================================================================
def run_all_checks(
    adb_path: Optional[str] = None,
    device_serial: Optional[str] = None,
    verbose: bool = True,
) -> bool:
    """
    返回整体是否通过（关键项 pass 才算通过：1-5，6 若仅用于 Excel 可允许 warning 级别的提示）
    """
    if verbose:
        print()
        print(f"{_BOLD}========== 环境检测 check-env =========={_RESET}")
        print(f"  项目根目录 : {_PROJ}")
        print(f"  命令执行   : {' '.join(sys.argv)}")
        print()

    results: List[CheckResult] = []
    results.append(check_python())

    r2, adb_exe = check_adb_path(adb_path)
    results.append(r2)
    results.append(check_adb_version(adb_exe))

    r4, serial = check_device(adb_exe, device_serial)
    results.append(r4)
    results.append(check_app(adb_exe, serial))
    results.append(check_excel_dep())

    for r in results:
        r.print()

    overall_ok = all(r.ok for r in results)
    if verbose:
        print()
        if overall_ok:
            print(f"[{_OK}] 环境检测全部通过 → 可以直接执行  python main.py")
        else:
            failed = [r.title for r in results if not r.ok]
            print(f"[{_FAIL}] 有 {len(failed)} 项未通过：{', '.join(failed)}")
            print("      按每一项的 👉 提示处理后，再运行  python main.py --check-env  验证。")
        print()
    return overall_ok


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="KOL-RICH 环境自检")
    p.add_argument("--adb-path", default=None, help="adb.exe 绝对路径（可选，不传走 settings/env/PATH）")
    p.add_argument("--device", "-s", default=None, help="设备序列号（多设备时指定）")
    args = p.parse_args()
    ok = run_all_checks(adb_path=args.adb_path, device_serial=args.device, verbose=True)
    sys.exit(0 if ok else 1)
