"""
ADB 手动调试工具  (python tools/adb_test.py)
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from typing import List, Optional

# ------------------------------------------------------------------
# 项目根注册到 sys.path（允许 直接 python tools/adb_test.py）
# ------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(_HERE)
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from core import adb_controller as _adb  # type: ignore  # noqa: E402


# ==================================================================
#  公共辅助：执行 adb + 打印结果
# ==================================================================
def _run(adb_args: List[str], adb_path: Optional[str], serial: Optional[str], timeout: int, shell: bool = False) -> int:
    """执行 adb 命令并把 stdout/stderr 透传到当前终端。"""
    resolved = _adb.resolve_adb_path(adb_path, raise_on_missing=False, log=False)
    if resolved is None:
        # 拿不到就走 raise_on_missing=True 看详细错误
        _ = _adb.resolve_adb_path(adb_path, raise_on_missing=True, log=False)
        return 2  # 不会到这里

    cmd = [resolved]
    if serial:
        cmd += ["-s", serial]
    if shell:
        cmd.append("shell")
    cmd += list(adb_args)

    if os.name == "nt":
        cmd_input = " ".join(cmd)
        use_shell = True
    else:
        cmd_input = cmd
        use_shell = False

    # 用实时输出（不用 capture）
    try:
        p = subprocess.run(
            cmd_input,
            shell=use_shell,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"\n[!] 命令超时({timeout}s): {' '.join(cmd)}", file=sys.stderr)
        return 124
    except KeyboardInterrupt:
        print("\n[!] 用户中断", file=sys.stderr)
        return 130
    return p.returncode


def _p(args: argparse.Namespace, adb_args: List[str], shell: bool = False) -> int:
    return _run(adb_args, args.adb_path, args.device, args.timeout, shell=shell)


# ==================================================================
#  子命令实现
# ==================================================================
def _cmd_version(args) -> int:
    return _p(args, ["version"])


def _cmd_devices(args) -> int:
    # 先跑 adb devices -l，再补一个 get-state / get-serialno（方便调试 unauthorized/offline）
    rc = _p(args, ["devices", "-l"])
    print()
    # 仅在有设备时补一些有用信息
    try:
        r = _adb.run_adb_raw(
            ["devices", "-l"],
            adb_path=args.adb_path,
            serial=None,
            timeout=args.timeout,
        )
    except Exception as e:
        print(f"[!] 无法解析 devices 列表: {e}")
        return rc
    available = 0
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if state == "device":
            available += 1
            # 对每台 device 打 型号 + android 版本
            try:
                r1 = _adb.run_adb_raw(
                    ["shell", "getprop", "ro.product.model", ";", "getprop", "ro.build.version.release"],
                    adb_path=args.adb_path,
                    serial=serial,
                    timeout=args.timeout,
                )
                lines = [x.strip() for x in (r1.stdout or "").splitlines() if x.strip()]
                model, ver = (lines + ["", ""])[:2]
                print(f"   {serial}  型号={model or '-'}  Android={ver or '-'}")
            except Exception as e:
                print(f"   {serial}  查询型号失败: {e}")
    print(f"可用 device={available}")
    return rc


def _cmd_dump(args) -> int:
    """adb shell uiautomator dump [REMOTE_PATH]，然后可选 pull 到本地"""
    remote = args.remote_path or "/sdcard/window.xml"
    rc = _p(args, ["shell", "uiautomator", "dump", "--compressed", remote])
    if rc != 0:
        # 有些手机不支持 --compressed，回退
        rc = _p(args, ["shell", "uiautomator", "dump", remote])
    if rc != 0:
        return rc
    # 如果传了 --local-path，自动 pull
    if args.local_path:
        print(f"\n>>> 自动 pull: {remote} -> {args.local_path}")
        return _p(args, ["pull", remote, args.local_path])
    return rc


def _cmd_pull(args) -> int:
    return _p(args, ["pull", args.remote, args.local])


def _cmd_push(args) -> int:
    return _p(args, ["push", args.local, args.remote])


def _cmd_install(args) -> int:
    a = ["install", "-r"]
    if args.user is not None:
        a += ["--user", str(args.user)]
    if args.replace:
        a.append("-t")
    a.append(args.apk)
    return _p(args, a)


def _cmd_uninstall(args) -> int:
    return _p(args, ["uninstall", args.package])


def _cmd_packages(args) -> int:
    a = ["shell", "pm", "list", "packages"]
    if args.three:
        a.append("-3")  # 第三方
    if args.system:
        a.append("-s")
    if args.filter:
        # 先列全，再在 Python 侧过滤（跨平台兼容）
        pass
    rc = _p(args, a) if not args.filter else 0
    if args.filter:
        # 走 run_adb_raw 再自行过滤打印
        try:
            r = _adb.run_adb_raw(
                a, adb_path=args.adb_path, serial=args.device, timeout=args.timeout
            )
        except Exception as e:
            print(f"[!] 执行失败: {e}")
            return 1
        lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
        hits = [ln for ln in lines if args.filter.lower() in ln.lower()]
        for ln in hits:
            print(ln)
        print(f"\n匹配 {len(hits)}/{len(lines)} 条，filter={args.filter!r}")
        return r.returncode
    return rc


def _cmd_pkgpath(args) -> int:
    return _p(args, ["shell", "pm", "path", args.package])


def _cmd_activity_top(args) -> int:
    # dumpsys activity top → 抽出含 package/Activity 的行
    try:
        r = _adb.run_adb_raw(
            ["shell", "dumpsys", "activity", "top"],
            adb_path=args.adb_path,
            serial=args.device,
            timeout=args.timeout,
        )
    except Exception as e:
        print(f"[!] 执行失败: {e}")
        return 1
    out = r.stdout or ""
    print(out[:8000])
    # 再做一轮提炼：找 ACTIVITY / mResumed / topResumed
    print("\n========== 提炼（可能的前台页面） ==========")
    found = False
    m_resumed = re.compile(r"mResumedActivity.*u0\s+([\w\./]+)", re.IGNORECASE)
    top_resumed = re.compile(r"topResumedActivity\s*=\s*\{?[^\s]*\s*([\w\./]+)", re.IGNORECASE)
    for pat in (m_resumed, top_resumed):
        m = pat.search(out)
        if m:
            print("  前台:", m.group(1))
            found = True
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("ACTIVITY") and ("realActivity" in line or line.count("/") >= 1):
            print(" ", line[:200])
            found = True
    if not found:
        print("  (未解析到前台 Activity，可看上方原始输出)")
    return r.returncode


def _cmd_shell(args) -> int:
    return _p(args, list(args.shell_args), shell=False)  # shell_args 已经自带 "shell" 不需要再加


def _cmd_raw(args) -> int:
    """python tools/adb_test.py raw -- <任何 adb 参数>"""
    return _p(args, list(args.adb_args), shell=False)


def _cmd_server(args) -> int:
    if args.action == "kill":
        return _p(args, ["kill-server"])
    if args.action == "start":
        return _p(args, ["start-server"])
    if args.action == "restart":
        rc = _p(args, ["kill-server"])
        if rc != 0:
            return rc
        return _p(args, ["start-server"])
    if args.action == "reconnect":
        return _p(args, ["reconnect", args.state or "device"])
    return _p(args, ["devices", "-l"])


# ==================================================================
#  CLI 构造
# ==================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="adb_test.py",
        description="ADB 手动调试工具（统一走 settings.ADB_PATH → env ADB_PATH → PATH 查找 adb）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=r"""
快速使用:
  python tools/adb_test.py devices                           # 列设备
  python tools/adb_test.py version                           # adb 版本
  python tools/adb_test.py dump -r /sdcard/window.xml -l dumps\w.xml   # dump + 拉到本地
  python tools/adb_test.py pull /sdcard/window.xml           # 拉文件
  python tools/adb_test.py packages ant                      # 过滤包
  python tools/adb_test.py activity-top                      # 当前前台 Activity
  python tools/adb_test.py raw -- shell pm list packages -3  # 透传任意 adb 参数
""",
    )

    # 全局参数（放在各子命令之前解析）
    p.add_argument("--adb-path", default=None, help="adb 可执行文件路径（也可用 settings/env/PATH）")
    p.add_argument("--device", "-s", default=None, help="设备序列号")
    p.add_argument("--timeout", type=int, default=30, help="单条命令超时秒数，默认 30")

    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    # version
    s = sub.add_parser("version", help="adb version")
    s.set_defaults(func=_cmd_version)

    # devices
    s = sub.add_parser("devices", help="adb devices -l + 每台设备型号/Android版本")
    s.set_defaults(func=_cmd_devices)

    # dump
    s = sub.add_parser("dump", help="adb shell uiautomator dump [+ 可选自动 pull]")
    s.add_argument("-r", "--remote-path", default=None, help="手机侧 XML 路径（默认 /sdcard/window.xml）")
    s.add_argument("-l", "--local-path", default=None, help="指定后，dump 成功后自动 adb pull 到此本地路径")
    s.set_defaults(func=_cmd_dump)

    # pull
    s = sub.add_parser("pull", help="adb pull <remote> [local]")
    s.add_argument("remote", help="手机侧路径，如 /sdcard/window.xml")
    s.add_argument("local", nargs="?", default=".", help="本地路径，默认当前目录")
    s.set_defaults(func=_cmd_pull)

    # push
    s = sub.add_parser("push", help="adb push <local> <remote>")
    s.add_argument("local", help="本地文件路径")
    s.add_argument("remote", help="手机侧路径")
    s.set_defaults(func=_cmd_push)

    # install
    s = sub.add_parser("install", help="adb install -r APK.apk")
    s.add_argument("apk", help="APK 文件路径")
    s.add_argument("--replace", action="store_true", help="追加 -t（允许测试 APK）")
    s.add_argument("--user", default=None, help="--user <id>，多用户场景指定用户")
    s.set_defaults(func=_cmd_install)

    # uninstall
    s = sub.add_parser("uninstall", help="adb uninstall <pkg>")
    s.add_argument("package", help="包名，如 com.antfortune.wealth")
    s.set_defaults(func=_cmd_uninstall)

    # packages
    s = sub.add_parser("packages", help="pm list packages [-3] [关键字过滤]")
    s.add_argument("filter", nargs="?", default=None, help="关键字过滤（大小写不敏感）")
    s.add_argument("--three", action="store_true", help="仅列第三方包（-3）")
    s.add_argument("--system", action="store_true", help="仅列系统包（-s）")
    s.set_defaults(func=_cmd_packages)

    # pkgpath
    s = sub.add_parser("pkgpath", help="pm path <pkg>")
    s.add_argument("package", help="包名")
    s.set_defaults(func=_cmd_pkgpath)

    # activity-top
    s = sub.add_parser("activity-top", help="dumpsys activity top + 提炼前台 Activity")
    s.set_defaults(func=_cmd_activity_top)

    # shell（透传 shell 命令）
    s = sub.add_parser("shell", help="adb shell <任意 shell args...>")
    s.add_argument("shell_args", nargs=argparse.REMAINDER, help="shell 命令，例如：getprop ro.build.version.release")
    s.set_defaults(func=_cmd_shell)

    # raw（最通用透传）
    s = sub.add_parser("raw", help="adb <任意 adb 参数...>，完全透传")
    s.add_argument("adb_args", nargs=argparse.REMAINDER, help="参数放在 -- 之后，例如：raw -- shell input keyevent HOME")
    s.set_defaults(func=_cmd_raw)

    # server
    s = sub.add_parser("server", help="ADB server 控制: kill/start/restart/reconnect")
    s.add_argument("action", choices=["kill", "start", "restart", "reconnect"], help="server 操作")
    s.add_argument("state", nargs="?", default=None, help="reconnect 时可选：device / offline")
    s.set_defaults(func=_cmd_server)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # 先对 adb 路径做一次解析并打印（让用户直观看到来源）—— 解析但不重复 log
    info = _adb.resolve_adb_path(args.adb_path, return_info=True, log=False)
    resolved = info.get("resolved")
    source = info.get("source") or "-"
    if resolved:
        print(f"[adb] 可执行  : {resolved}")
        print(f"[adb] 命中来源: {source}")
        print()
    else:
        print("[adb] ❌ 未解析到 adb 可执行文件。")
        # 走 raise_on_missing=True 打印详细 attempts
        try:
            _adb.resolve_adb_path(args.adb_path, raise_on_missing=True, log=False)
        except Exception as e:
            print(e)
        return 2

    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
