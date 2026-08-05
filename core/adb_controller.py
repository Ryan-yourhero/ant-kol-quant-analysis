"""
ADB控制模块 — 可直接运行
==========================

提供5个核心功能：
1. check_device_status()         检查adb设备连接状态
2. dump_ui_xml()                 获取当前页面XML:  adb shell uiautomator dump /sdcard/window.xml
3. pull_xml()                    拉取XML:           adb pull /sdcard/window.xml <本地路径>
4. swipe()                       滑动:              adb shell input swipe x1 y1 x2 y2 [duration]
5. tap()                         点击:              adb shell input tap x y

直接运行（命令行自测）：
    python core/adb_controller.py status                    # 1. 查看设备状态
    python core/adb_controller.py dump                      # 2. dump XML 到手机 /sdcard/window.xml
    python core/adb_controller.py pull                      # 3. 把 window.xml 拉到当前目录
    python core/adb_controller.py dump_and_pull             # 2+3 一步完成，返回XML内容字符串
    python core/adb_controller.py swipe 540 1800 540 600    # 4. 滑动 (x1 y1 x2 y2)
    python core/adb_controller.py swipe 540 1800 540 600 500   # 滑动+时长(ms)
    python core/adb_controller.py tap 540 1600              # 5. 点击 (x y)
    python core/adb_controller.py -s ABC123 status          # 指定设备序列号
"""

import os
import re
import sys
import time
import shutil
import subprocess
import argparse
from typing import Optional, List, Tuple, Dict, Any

# 项目配置：加载 settings.ADB_PATH（推荐在此处配置adb绝对路径，避免每次传参）
try:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from config import settings as _settings
except Exception:
    _settings = None


# ============================================================
#  常量：严格按照用户指定的路径
# ============================================================

DEVICE_XML_PATH = "/sdcard/window.xml"   # 用户明确指定的dump路径
DEFAULT_TIMEOUT = 30                      # ADB命令超时(秒)


# ============================================================
#  异常类
# ============================================================

class ADBError(Exception):
    """ADB操作异常"""
    pass


class ADBDeviceNotFoundError(ADBError):
    """无可用设备"""
    pass


class ADBMultipleDevicesError(ADBError):
    """多设备但未指定序列号"""
    pass


# ============================================================
#  ADB控制器
# ============================================================

class ADBController:
    """
    ADB 控制器

    示例：
        adb = ADBController()
        adb.check_device_status()          # 检查状态（打印+返回设备列表）
        xml = adb.dump_and_pull_xml()      # dump + pull 一步到位，返回XML内容
        adb.swipe(540, 1800, 540, 600)     # 滑动
        adb.tap(540, 1600)                 # 点击
    """

    def __init__(
        self,
        adb_path: Optional[str] = None,
        device_serial: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        """
        Args:
            adb_path:      ADB可执行文件路径；None则自动查找（环境变量ADB_PATH → 系统PATH）
            device_serial: 设备序列号；多设备时必须指定；单设备可留空自动选择
            timeout:       单条ADB命令超时秒数
        """
        self.adb_path = self._resolve_adb(adb_path)
        self.device_serial = device_serial
        self.timeout = timeout
        self._screen_size: Optional[Tuple[int, int]] = None

        print(f"[ADB] adb = {self.adb_path}")
        if self.device_serial:
            print(f"[ADB] device = {self.device_serial}")

    # ----------------------------------------------------------
    #  内部：ADB路径解析（按优先级从高到低尝试）
    # ----------------------------------------------------------
    @staticmethod
    def _resolve_adb(custom_path: Optional[str]) -> str:
        """
        ADB 路径查找优先级（从高到低，命中即返回）：
          1) 显式参数 custom_path（命令行 --adb-path 传入）
          2) config/settings.py 中的 ADB_PATH 配置项
          3) 系统环境变量 ADB_PATH
          4) 系统 PATH 中的 adb / adb.exe（通过 shutil.which 查找）
          5) Windows 常见安装目录枚举（Android SDK / 项目根目录）
        """
        attempts: List[Tuple[str, str]] = []   # [(候选路径, 失败原因)]
        adb_exe_name = "adb.exe" if os.name == "nt" else "adb"

        # ---------- 1. 显式参数 ----------
        if custom_path:
            ok, reason, resolved = ADBController._try_candidate(custom_path, adb_exe_name)
            if ok:
                ADBController._log_hit("① 命令行参数 --adb-path", resolved)
                return resolved
            attempts.append((custom_path, reason))

        # ---------- 2. config/settings.ADB_PATH ----------
        settings_val = None
        if _settings is not None:
            try:
                settings_val = getattr(_settings, "ADB_PATH", None)
            except Exception:
                settings_val = None
        if settings_val:
            ok, reason, resolved = ADBController._try_candidate(settings_val, adb_exe_name)
            if ok:
                ADBController._log_hit("② config/settings.py 中的 ADB_PATH", resolved)
                return resolved
            attempts.append((f"settings.ADB_PATH = {settings_val}", reason))

        # ---------- 3. 环境变量 ADB_PATH ----------
        env_adb = os.environ.get("ADB_PATH")
        if env_adb:
            ok, reason, resolved = ADBController._try_candidate(env_adb, adb_exe_name)
            if ok:
                ADBController._log_hit("③ 环境变量 ADB_PATH", resolved)
                return resolved
            attempts.append((f"环境变量 ADB_PATH = {env_adb}", reason))

        # ---------- 4. 系统 PATH 中的 adb / adb.exe ----------
        for name in ("adb", adb_exe_name):
            resolved = shutil.which(name)
            if resolved:
                ADBController._log_hit(f"④ 系统 PATH（查找 {name!r}）", resolved)
                return resolved
            attempts.append((f"shutil.which({name!r}) in PATH", "未找到"))

        # ---------- 5. Windows 常见目录枚举 ----------
        if os.name == "nt":
            candidates = ADBController._windows_common_candidates(adb_exe_name)
            for c in candidates:
                ok, reason, resolved = ADBController._try_candidate(c, adb_exe_name)
                if ok:
                    ADBController._log_hit("⑤ Windows 常见安装目录", resolved)
                    return resolved
                attempts.append((c, reason))

        # ---------- 全部失败，给出可操作提示 ----------
        raise ADBController._build_not_found_error(attempts)

    # ---------- 辅助：尝试一个候选路径 ----------
    @staticmethod
    def _try_candidate(candidate: str, adb_exe_name: str):
        """返回 (ok: bool, fail_reason: str, resolved_path: str)"""
        if not candidate:
            return False, "空值", ""
        c = os.path.expandvars(os.path.expanduser(candidate.strip()))

        # 如果是目录，自动拼接 adb(.exe)
        if os.path.isdir(c):
            c = os.path.join(c, adb_exe_name)

        # 先看是不是存在的文件
        if os.path.isfile(c):
            if os.access(c, os.X_OK) or c.lower().endswith(".exe"):
                return True, "", os.path.abspath(c)
            return False, "文件存在但无执行权限", c

        # 非绝对路径 → 再交给 shutil.which（可能是PATH中的短名）
        if not os.path.isabs(c):
            w = shutil.which(c)
            if w:
                return True, "", w
            return False, f"既不是有效文件路径，也不在 PATH 中", c

        return False, "路径不存在", c

    # ---------- 辅助：Windows 常见 SDK 目录 ----------
    @staticmethod
    def _windows_common_candidates(adb_exe_name: str) -> List[str]:
        out: List[str] = []
        base_dirs = []
        # ANDROID_HOME / ANDROID_SDK_ROOT
        for k in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
            v = os.environ.get(k)
            if v:
                base_dirs.append(v)
        # 用户目录下的默认位置
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            base_dirs.append(os.path.join(local_app, "Android", "Sdk"))
        # C:\Android
        base_dirs.append(r"C:\Android")
        # 项目根目录下的 platform-tools（方便打包分发）
        try:
            proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            base_dirs.append(os.path.join(proj_root, "platform-tools"))
            base_dirs.append(os.path.join(proj_root, "platform-tools-latest-windows", "platform-tools"))
        except Exception:
            pass

        for d in base_dirs:
            if d:
                out.append(os.path.join(d, "platform-tools", adb_exe_name))
                out.append(os.path.join(d, adb_exe_name))
        return out

    # ---------- 辅助：命中日志 ----------
    @staticmethod
    def _log_hit(channel: str, resolved: str) -> None:
        print(f"[ADB] 命中路径来源：{channel}  →  {resolved}")

    # ---------- 辅助：构建详细的找不到错误 ----------
    @staticmethod
    def _build_not_found_error(attempts: List[Tuple[str, str]]) -> ADBError:
        lines = []
        lines.append("未找到 adb 可执行文件。")
        lines.append("")
        lines.append("推荐做法（任选其一，按推荐度排序）：")
        lines.append("  1) 在 config/settings.py 中配置 ADB_PATH（一劳永逸，推荐）：")
        lines.append(r'       ADB_PATH = r"C:\Android\platform-tools\adb.exe"')
        lines.append("  2) 每次运行传参数：")
        lines.append(r'       python main.py --adb-path "C:\Android\platform-tools\adb.exe"')
        lines.append("  3) 设置环境变量 ADB_PATH 指向 adb 绝对路径")
        lines.append("  4) 把 adb 所在目录加入系统 PATH（通常是 platform-tools 目录）")
        lines.append("")
        lines.append("如果还没装 adb，先下载 Android SDK Platform Tools：")
        lines.append("  https://developer.android.com/tools/releases/platform-tools")
        lines.append("")
        lines.append(f"（已尝试过的候选路径共 {len(attempts)} 条，按查找顺序：）")
        for i, (path, reason) in enumerate(attempts, 1):
            # 避免路径太长
            disp = path if len(path) < 120 else path[:117] + "..."
            lines.append(f"  {i:>2}. {disp}")
            lines.append(f"       ↳ {reason}")
        return ADBError("\n".join(lines))

    # ----------------------------------------------------------
    #  内部：统一执行ADB命令（ADBController 实例方法）
    # ----------------------------------------------------------
    def _run(self, args: List[str], shell: bool = False) -> subprocess.CompletedProcess:
        """
        执行ADB命令
        shell=True  会在命令中插入 shell 子命令，对应 adb shell xxx
        """
        cmd = [self.adb_path]
        if self.device_serial:
            cmd += ["-s", self.device_serial]
        if shell:
            cmd.append("shell")
        cmd += args

        # Windows下用单字符串+shell=True，避免PATH/转义问题
        if os.name == "nt":
            cmd_input = " ".join(cmd)
            use_shell = True
        else:
            cmd_input = cmd
            use_shell = False

        try:
            result = subprocess.run(
                cmd_input,
                shell=use_shell,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise ADBError(f"命令超时({self.timeout}s): {' '.join(cmd)}") from e
        return result

    # ============================================================
    #  功能 1：检查设备连接状态
    # ============================================================
    def check_device_status(self) -> List[dict]:
        """
        检查adb设备连接状态
          - 执行 adb devices -l
          - 解析每台设备的 serial / state / model
          - 遇到 unauthorized/offline 会打印明确提示
          - 多台在线且未指定序列号时抛异常提示用户指定

        Returns:
            [{"serial": str, "state": str, "model": str, "available": bool}, ...]

        Raises:
            ADBDeviceNotFoundError:   没有任何设备
            ADBMultipleDevicesError:  多台可用(device)但没指定序列号
        """
        print("\n" + "=" * 60)
        print("[状态] 正在检查设备连接：adb devices -l")
        r = self._run(["devices", "-l"])
        if r.returncode != 0:
            raise ADBError(f"执行 adb devices 失败：{r.stderr.strip() or r.stdout.strip()}")

        devices: List[dict] = []
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            serial = parts[0]
            state = parts[1]
            model = ""
            for p in parts[2:]:
                if p.startswith("model:"):
                    model = p[len("model:"):]
                    break
            devices.append({
                "serial": serial,
                "state": state,
                "model": model,
                "available": state == "device",
            })

        # 打印状态
        if not devices:
            print("  ❌ 未检测到任何设备")
            print("     请：1) USB连接手机  2) 打开开发者选项→USB调试  3) 手机弹窗点允许")
            raise ADBDeviceNotFoundError("无设备连接")

        print(f"  共检测到 {len(devices)} 台设备：")
        for d in devices:
            tip = ""
            if d["state"] == "unauthorized":
                tip = "  ⚠ 未授权 → 请在手机上允许USB调试"
            elif d["state"] == "offline":
                tip = "  ⚠ 离线 → 请重新插拔USB或重启adb服务 (adb kill-server && adb start-server)"
            elif d["available"]:
                tip = "  ✅ 可用"
            model_str = f"[{d['model']}]" if d["model"] else ""
            print(f"    - {d['serial']}  state={d['state']}  {model_str} {tip}")

        available = [d for d in devices if d["available"]]
        if not available:
            raise ADBDeviceNotFoundError("没有处于 device 状态的可用设备")

        # 多设备时：未指定序列号就提示
        if len(available) > 1 and not self.device_serial:
            serials = [d["serial"] for d in available]
            raise ADBMultipleDevicesError(
                f"检测到多台可用设备：{serials}\n"
                f"请用参数 -s <serial> 或构造参数 device_serial= 指定目标设备"
            )

        # 自动选设备
        if len(available) == 1 and not self.device_serial:
            self.device_serial = available[0]["serial"]
            print(f"  → 自动选择唯一可用设备：{self.device_serial}")

        print("[状态] 设备检查通过 ✓\n" + "=" * 60)
        return devices

    # ============================================================
    #  功能 2：获取当前页面XML（dump到手机端）
    # ============================================================
    def dump_ui_xml(self) -> str:
        """
        执行：adb shell uiautomator dump /sdcard/window.xml
        把当前界面层级dump到手机的 /sdcard/window.xml

        Returns:
            设备端XML路径 DEVICE_XML_PATH = '/sdcard/window.xml'
        """
        print(f"\n[Dump] 执行：adb shell uiautomator dump {DEVICE_XML_PATH}")
        # 有些机型需要先删除旧文件避免提示"already exists"
        self._run(["rm", "-f", DEVICE_XML_PATH], shell=True)

        r = self._run(["uiautomator", "dump", DEVICE_XML_PATH], shell=True)
        if r.returncode != 0:
            raise ADBError(f"uiautomator dump 失败：{r.stderr.strip() or r.stdout.strip()}")
        # uiautomator dump 成功通常返回 stdout 包含 UI hierachy dumped to /sdcard/window.xml
        if r.stdout.strip():
            print(f"       {r.stdout.strip()}")
        print(f"[Dump] 已写入设备：{DEVICE_XML_PATH} ✓")
        return DEVICE_XML_PATH

    # ============================================================
    #  功能 3：拉取XML到本地
    # ============================================================
    def pull_xml(self, local_path: str = None) -> str:
        """
        执行：adb pull /sdcard/window.xml <local_path>
        把手机端XML拉到本地

        Args:
            local_path: 本地保存路径（文件名或目录均可）；
                        None → 保存到当前工作目录 window.xml

        Returns:
            本地XML文件的绝对路径
        """
        if local_path is None:
            local_path = os.path.join(os.getcwd(), "window.xml")
        # 如果传的是目录，自动补全文件名
        elif os.path.isdir(local_path):
            local_path = os.path.join(local_path, "window.xml")

        os.makedirs(os.path.dirname(os.path.abspath(local_path)) or ".", exist_ok=True)

        print(f"\n[Pull] 执行：adb pull {DEVICE_XML_PATH} {local_path}")
        r = self._run(["pull", DEVICE_XML_PATH, local_path])
        if r.returncode != 0:
            raise ADBError(f"adb pull 失败：{r.stderr.strip() or r.stdout.strip()}")

        if not os.path.exists(local_path):
            raise ADBError(f"adb pull 返回成功但本地文件不存在：{local_path}")

        size_kb = os.path.getsize(local_path) / 1024
        print(f"[Pull] 已拉取到本地：{os.path.abspath(local_path)}  ({size_kb:.1f} KB) ✓")
        return os.path.abspath(local_path)

    # ----------------------------------------------------------
    #  便捷：dump + pull + 返回内容 一步完成
    # ----------------------------------------------------------
    def dump_and_pull_xml(self, local_path: str = None, skip_check: bool = False) -> str:
        """
        dump_ui_xml() → pull_xml() → 读取内容返回

        Args:
            local_path:  本地拉取保存路径（None → 工作目录 window.xml）
            skip_check:  跳过 check_device_status。
                        当外层（如 ScrollManager.run）已经在循环开始前做过
                        check_device_status 时，后续每次循环不必再检查，避免
                        重复打印设备状态并节省时间。

        Returns:
            XML字符串（utf-8）
        """
        if not skip_check:
            self.check_device_status()
        self.dump_ui_xml()
        local = self.pull_xml(local_path)
        with open(local, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        print(f"\n[XML] 读取完成，共 {len(content)} 字符")
        return content

    # ============================================================
    #  功能 4：滑动
    # ============================================================
    def swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: Optional[int] = None,
    ) -> None:
        """
        执行：adb shell input swipe x1 y1 x2 y2 [duration_ms]

        Args:
            x1, y1:     起点坐标
            x2, y2:     终点坐标
            duration_ms:滑动时长（毫秒），None使用系统默认
        """
        args = ["input", "swipe", str(x1), str(y1), str(x2), str(y2)]
        if duration_ms is not None:
            args.append(str(duration_ms))
        cmd_display = "adb shell " + " ".join(args)
        print(f"\n[Swipe] 执行：{cmd_display}")

        r = self._run(args, shell=True)
        if r.returncode != 0:
            raise ADBError(f"input swipe 失败：{r.stderr.strip() or r.stdout.strip()}")
        # input 命令通常成功无输出
        duration_str = f"{duration_ms}ms" if duration_ms else "默认"
        print(f"[Swipe] ({x1},{y1}) → ({x2},{y2})  时长={duration_str} ✓")

    # ----------------------------------------------------------
    #  便捷：按屏幕百分比滑动（避免不同分辨率硬编码坐标）
    # ----------------------------------------------------------
    def swipe_by_ratio(
        self,
        x1_ratio: float,
        y1_ratio: float,
        x2_ratio: float,
        y2_ratio: float,
        duration_ms: int = 500,
    ) -> None:
        """
        按屏幕比例滑动（推荐，适配不同分辨率）

        示例：
            向上滑（下→中）：swipe_by_ratio(0.5, 0.8, 0.5, 0.3)
            向下滑（上→中）：swipe_by_ratio(0.5, 0.3, 0.5, 0.8)
            向左滑（右→左）：swipe_by_ratio(0.8, 0.5, 0.2, 0.5)
        """
        w, h = self.get_screen_size()
        x1 = int(w * x1_ratio)
        y1 = int(h * y1_ratio)
        x2 = int(w * x2_ratio)
        y2 = int(h * y2_ratio)
        self.swipe(x1, y1, x2, y2, duration_ms)

    # ============================================================
    #  功能 5：点击
    # ============================================================
    def tap(self, x: int, y: int) -> None:
        """
        执行：adb shell input tap x y

        Args:
            x, y: 点击坐标（像素）
        """
        args = ["input", "tap", str(x), str(y)]
        print(f"\n[Tap] 执行：adb shell input tap {x} {y}")
        r = self._run(args, shell=True)
        if r.returncode != 0:
            raise ADBError(f"input tap 失败：{r.stderr.strip() or r.stdout.strip()}")
        print(f"[Tap] 点击坐标 ({x}, {y}) ✓")

    # ----------------------------------------------------------
    #  便捷：按屏幕百分比点击
    # ----------------------------------------------------------
    def tap_by_ratio(self, x_ratio: float, y_ratio: float) -> None:
        w, h = self.get_screen_size()
        x = int(w * x_ratio)
        y = int(h * y_ratio)
        self.tap(x, y)

    # ============================================================
    #  辅助：获取屏幕分辨率
    # ============================================================
    def get_screen_size(self) -> Tuple[int, int]:
        """返回 (width, height)，带缓存"""
        if self._screen_size:
            return self._screen_size
        r = self._run(["wm", "size"], shell=True)
        m = re.search(r"(\d+)x(\d+)", r.stdout)
        if not m:
            raise ADBError(f"无法解析屏幕尺寸，输出：{r.stdout.strip()!r}")
        w, h = int(m.group(1)), int(m.group(2))
        self._screen_size = (w, h)
        print(f"[Info] 屏幕尺寸：{w} x {h}")
        return (w, h)


# ============================================================
#  公开：ADB 路径解析（独立于 ADBController，不抛异常也能拿到 info）
# ============================================================
def resolve_adb_path(
    custom_path: Optional[str] = None,
    *,
    raise_on_missing: bool = False,
    return_info: bool = False,
    log: bool = False,
):
    """
    独立入口：按优先级解析 adb 绝对路径。

    优先级：
      1) custom_path（命令行 --adb-path）
      2) config/settings.py  ADB_PATH
      3) 环境变量 ADB_PATH
      4) 系统 PATH shutil.which(adb / adb.exe)
      5) Windows 常见 SDK 目录（settings 中第 5 级）

    Args:
        custom_path:       命令行传入的 --adb-path
        raise_on_missing:  找不到时抛 ADBError，否则返回 None
        return_info:       True 时返回 dict {resolved, source, attempts}，否则只返回 str/None
        log:               命中时打印 [ADB] 命中来源
    """
    from typing import Dict, Any  # 兼容旧类型已经顶部导入

    attempts: List[Tuple[str, str]] = []
    adb_exe_name = "adb.exe" if os.name == "nt" else "adb"

    # 复用到 ADBController._try_candidate / _windows_common_candidates
    _try = ADBController._try_candidate
    _win_cand = ADBController._windows_common_candidates
    _hit = ADBController._log_hit

    def _stage(cand, stage_name: str):
        if not cand:
            return None
        ok, reason, resolved = _try(cand if isinstance(cand, str) else str(cand), adb_exe_name)
        if ok:
            if log:
                _hit(stage_name, resolved)
            if return_info:
                info["resolved"] = resolved
                info["source"] = stage_name
            return resolved
        attempts.append((
            cand if isinstance(cand, str) else str(cand),
            reason or "",
        ))
        return None

    info: Dict[str, Any] = {"resolved": None, "source": "", "attempts": attempts}

    # 1) custom_path
    if _stage(custom_path, "① 命令行 --adb-path") is not None:
        return (info if return_info else info["resolved"])

    # 2) settings.ADB_PATH
    settings_val = None
    if _settings is not None:
        try:
            settings_val = getattr(_settings, "ADB_PATH", None)
        except Exception:
            settings_val = None
    if settings_val:
        if _stage(settings_val, "② config/settings.py ADB_PATH") is not None:
            return (info if return_info else info["resolved"])
    else:
        attempts.append(("settings.ADB_PATH", "未配置（为空/None）"))

    # 3) env ADB_PATH
    env_adb = os.environ.get("ADB_PATH")
    if env_adb:
        if _stage(env_adb, "③ 环境变量 ADB_PATH") is not None:
            return (info if return_info else info["resolved"])
    else:
        attempts.append(("环境变量 ADB_PATH", "未设置"))

    # 4) shutil.which
    which_hit = False
    for name in ("adb", adb_exe_name):
        resolved = shutil.which(name)
        if resolved:
            if log:
                _hit(f"④ 系统 PATH ({name!r})", resolved)
            info["resolved"] = resolved
            info["source"] = f"④ 系统 PATH（查找 {name!r}）"
            which_hit = True
            break
        attempts.append((f"shutil.which({name!r}) in PATH", "未找到"))
    if which_hit:
        return (info if return_info else info["resolved"])

    # 5) Windows 常见目录
    if os.name == "nt":
        for c in _win_cand(adb_exe_name):
            ok, reason, resolved = _try(c, adb_exe_name)
            if ok:
                if log:
                    _hit("⑤ Windows 常见安装目录", resolved)
                info["resolved"] = resolved
                info["source"] = "⑤ Windows 常见安装目录"
                return (info if return_info else info["resolved"])
            attempts.append((c, reason or ""))

    # 全部失败
    if raise_on_missing:
        raise ADBController._build_not_found_error(attempts)
    info["attempts"] = attempts
    return (info if return_info else None)


# ============================================================
#  公开：轻量级 run_adb（不依赖 ADBController 构造）
# ============================================================
def run_adb_raw(
    args: List[str],
    *,
    adb_path: Optional[str] = None,
    serial: Optional[str] = None,
    timeout: int = 30,
    shell: bool = False,
) -> subprocess.CompletedProcess:
    """
    直接执行一条 adb 命令。不依赖 ADBController 实例，方便 adb_test/env_checker 手动调试。

    - args:    跟在 `adb [-s SERIAL] [shell]` 之后的参数列表，例如 ["devices", "-l"]
    - shell:   True 会在 args 前插入 "shell"，等价于 adb shell ...
    """
    resolved = resolve_adb_path(adb_path, raise_on_missing=True, log=False)
    cmd = [resolved]
    if serial:
        cmd += ["-s", serial]
    if shell:
        cmd.append("shell")
    cmd += list(args)

    if os.name == "nt":
        cmd_input = " ".join(cmd)
        use_shell = True
    else:
        cmd_input = cmd
        use_shell = False

    try:
        return subprocess.run(
            cmd_input,
            shell=use_shell,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise ADBError(f"ADB命令超时({timeout}s): {' '.join(cmd)}") from e


# ============================================================
#  命令行自测入口（代码可直接运行）
# ============================================================

def _build_cli():
    p = argparse.ArgumentParser(
        description="ADB控制器 - 直接运行版",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python core/adb_controller.py status
  python core/adb_controller.py dump
  python core/adb_controller.py pull
  python core/adb_controller.py dump_and_pull
  python core/adb_controller.py swipe 540 1800 540 600
  python core/adb_controller.py swipe 540 1800 540 600 500
  python core/adb_controller.py tap 540 1600
  python core/adb_controller.py -s ABC123 status
""",
    )
    p.add_argument("-s", "--serial", help="设备序列号（多设备时必填）")
    p.add_argument("--adb-path", help="ADB可执行文件路径")
    p.add_argument(
        "action",
        choices=["status", "dump", "pull", "dump_and_pull", "swipe", "tap"],
        help="动作：status=检查状态；dump=手机端dump XML；pull=拉到本地；dump_and_pull=两者合一；swipe=滑动；tap=点击",
    )
    p.add_argument(
        "args",
        nargs="*",
        help="动作参数：swipe需要x1 y1 x2 y2 [duration]；tap需要x y",
    )
    p.add_argument("-o", "--output", help="pull / dump_and_pull 的本地输出路径")
    return p


def _main():
    args = _build_cli().parse_args()
    adb = ADBController(adb_path=args.adb_path, device_serial=args.serial)

    try:
        if args.action == "status":
            adb.check_device_status()

        elif args.action == "dump":
            adb.check_device_status()
            adb.dump_ui_xml()

        elif args.action == "pull":
            adb.check_device_status()
            adb.pull_xml(args.output)

        elif args.action == "dump_and_pull":
            xml = adb.dump_and_pull_xml(args.output)
            # 前500字符预览
            preview = xml[:500].replace("\r", " ")
            print("\n[XML 预览前500字符]\n" + preview + "\n...")

        elif args.action == "swipe":
            if len(args.args) not in (4, 5):
                print("❌ swipe 参数错误：需要 x1 y1 x2 y2 [duration_ms]")
                sys.exit(2)
            nums = list(map(int, args.args))
            adb.check_device_status()
            if len(nums) == 4:
                adb.swipe(*nums)
            else:
                adb.swipe(*nums[:4], duration_ms=nums[4])
            time.sleep(0.3)

        elif args.action == "tap":
            if len(args.args) != 2:
                print("❌ tap 参数错误：需要 x y")
                sys.exit(2)
            x, y = map(int, args.args)
            adb.check_device_status()
            adb.tap(x, y)

    except (ADBError, ADBDeviceNotFoundError, ADBMultipleDevicesError) as e:
        print(f"\n❌ 错误：{e}")
        sys.exit(1)

    print("\n✅ 执行完成")


if __name__ == "__main__":
    _main()
