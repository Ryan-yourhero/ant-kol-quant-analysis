"""
滚动采集管理器 — 仅采集页面原始文本，不做交易规则判断
================================================================

架构调整（v2）：
  ScrollManager 现在只负责：
    1. 连接 ADB
    2. dump XML → 抽页面 texts （raw_text_extractor）
    3. 页面去重 / 判断到底
    4. 滑动
    5. 保存原始文本 JSON （raw_pages_YYYYMMDD_HHMMSS.json）

  ⚠️  不再调用 data_extractor 做 BUY/SELL/TRANSFER/CANCEL 结构化判断。
  ⚠️  爬虫阶段不判断买卖，后续由 AIParser 模块完成： raw_text -> structured_trade JSON。

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

# ---- 兼容"直接脚本运行"与"作为包导入" ----
_THIS_FILE = os.path.abspath(__file__)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_THIS_FILE))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

try:
    from .adb_controller import ADBController, ADBError
    from .raw_text_extractor import extract_texts, TextAccumulator
except ImportError:
    from core.adb_controller import ADBController, ADBError  # type: ignore
    from core.raw_text_extractor import extract_texts, TextAccumulator  # type: ignore

try:
    from .screen_dump_exporter import export_screen_dump_md
except ImportError:  # pragma: no cover - 展示层可选
    export_screen_dump_md = None  # type: ignore


# ============================================================
#  常量
# ============================================================
DEFAULT_SWIPE_COORDS = (500, 1600, 500, 500, 500)   # x1, y1, x2, y2, duration_ms（旧版兼容，默认已改用比例滑动）

# v2.3: 盘友圈是 WebView，会"吞掉"前几次滑动（慢速大幅度滑动基本无效）。
#   实测有效方式：短促、快速的接力上滑 → 滑几次后页面开始真正滚动。
#   x1_ratio, y1_ratio, x2_ratio, y2_ratio, duration_ms
DEFAULT_SWIPE_RATIOS = (0.5, 0.70, 0.5, 0.35, 400)
DEFAULT_SWIPES_PER_STEP = 3      # 每一轮滑动 = 连续 N 次快速接力滑动（burst）

DEFAULT_WAIT_AFTER_SWIPE_SEC = 1.2
DEFAULT_MAX_SWIPES = 100
# 页面可能连续吞掉几轮滑动才动，所以"相同"要连续很多次才能判到底
DEFAULT_STABLE_TO_STOP = 4

# 到底判定策略（v2.3）：
#   不依赖任何标志文本；只认「滑不动了」：
#   连续 N 次页面内容签名相同（签名对数字做容差，防止点赞/评论数跳动干扰）→ 停止。

OUTPUT_DIR = os.path.join(_PROJECT_ROOT, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)


# ============================================================
#  工具：快速哈希页面 texts（比 XML hash 更贴近"页面展示是否变化"）
# ============================================================
# 数字容差：点赞/评论/浏览数会实时跳动，把数字串替换成占位符，
# 避免"只是数字变了"被误判成"翻页了"，导致到底后停不下来。
# 真正翻出新帖子时，帖子标题/正文/基金名等文本会变化，仍能正确识别。
_NUM_RE = re.compile(r"\d[\d,\.]*")


def _texts_sig(texts: List[str]) -> str:
    masked = [_NUM_RE.sub("N", t) for t in texts]
    s = "\n".join(masked)
    return hashlib.md5(s.encode("utf-8")).hexdigest()


# ============================================================
#  ScrollManager v2 — 纯文本采集
# ============================================================
class ScrollManager:
    """
    用法：
        sm = ScrollManager(max_swipes=10)
        result = sm.run()
        print(result.keys())
    """

    def __init__(
        self,
        adb_path: Optional[str] = None,
        device_serial: Optional[str] = None,
        max_swipes: int = DEFAULT_MAX_SWIPES,
        stable_to_stop: int = DEFAULT_STABLE_TO_STOP,
        wait_after_swipe_sec: float = DEFAULT_WAIT_AFTER_SWIPE_SEC,
        swipe_coords: Tuple[int, int, int, int, int] = DEFAULT_SWIPE_COORDS,
        swipe_ratios: Optional[Tuple[float, float, float, float, int]] = DEFAULT_SWIPE_RATIOS,
        swipes_per_step: int = DEFAULT_SWIPES_PER_STEP,
    ):
        self.adb = ADBController(adb_path=adb_path, device_serial=device_serial)
        self.accumulator = TextAccumulator()

        self.max_swipes = max_swipes
        self.stable_to_stop = max(1, int(stable_to_stop))
        self.wait_after_swipe_sec = wait_after_swipe_sec
        self.swipe_coords = swipe_coords
        # swipe_ratios=None 时退回旧的硬编码坐标滑动
        self.swipe_ratios = swipe_ratios
        self.swipes_per_step = max(1, int(swipes_per_step))

        self.page_read_count: int = 0
        self.swipe_count: int = 0

        # 到底判定：连续 N 轮「页面内容签名相同」→ 停止
        self.last_texts_sig: Optional[str] = None
        self.texts_same_in_a_row: int = 0

        # 每轮摘要
        self.history: List[Dict[str, Any]] = []

    # ============================================================
    #  滑动
    # ============================================================
    def _one_swipe(self) -> None:
        """单次滑动（比例优先，退回硬编码坐标）。"""
        if self.swipe_ratios is not None:
            x1r, y1r, x2r, y2r, dur = self.swipe_ratios
            self.adb.swipe_by_ratio(x1r, y1r, x2r, y2r, duration_ms=dur)
        else:
            x1, y1, x2, y2, duration_ms = self.swipe_coords
            self.adb.swipe(x1, y1, x2, y2, duration_ms=duration_ms)

    def scroll_up(self) -> None:
        """
        一轮滚动 = 连续多次快速接力滑动（burst）。

        蚂蚁财富盘友圈是 WebView：单次 input swipe 经常被"吞掉"，
        实测连续多次短促快速上滑才能让页面真正滚动，故每一轮做接力滑动。
        """
        self.swipe_count += 1
        print(f"\n执行第{self.swipe_count}轮滑动（接力 ×{self.swipes_per_step}）")
        try:
            for _ in range(self.swipes_per_step):
                self._one_swipe()
                time.sleep(0.3)
        except ADBError as e:
            print(f"  ⚠ 滑动异常（继续下一流程）：{e}")
        if self.wait_after_swipe_sec > 0:
            print(f"  等待页面加载 {self.wait_after_swipe_sec}s …")
            time.sleep(self.wait_after_swipe_sec)

    # ============================================================
    #  单页采集：dump XML → extract_texts → 加入 accumulator
    # ============================================================
    def collect_page(self) -> Dict[str, Any]:
        """
        返回：
          {
            "page": int,
            "texts": [...],           # 本页所有文本（文档顺序）
            "new_texts": [...],       # 全局首次出现的文本
            "texts_sig": str,         # 用于到底判定
            "same_as_last": bool,     # 与上一页 texts 完全一致
          }
        """
        self.page_read_count += 1
        page_no = self.page_read_count
        print(f"\n第{page_no}次页面读取")

        # Step 1: dump XML
        try:
            xml_content = self.adb.dump_and_pull_xml(skip_check=True)
        except ADBError as e:
            print(f"  ❌ 获取 XML 失败：{e}")
            xml_content = ""

        # Step 2: 抽 texts （即使 xml 为空也要生成空页，保证到底判定正常）
        if xml_content:
            try:
                page_dict = extract_texts(xml_content, page=page_no)
            except Exception as e:
                print(f"  ❌ 文本提取异常：{e}")
                page_dict = {"page": page_no, "texts": []}
        else:
            page_dict = {"page": page_no, "texts": []}

        texts: List[str] = list(page_dict.get("texts") or [])
        texts_sig = _texts_sig(texts)

        # Step 3: 到底判定计数器（只认"滑不动"：页面内容签名是否变化）
        same_as_last = bool(texts_sig and self.last_texts_sig == texts_sig)
        if same_as_last:
            self.texts_same_in_a_row += 1
            print(f"  页面无变化（连续{self.texts_same_in_a_row}轮），本页 {len(texts)} 行")
        else:
            self.texts_same_in_a_row = 0
            print(f"  本页 texts：{len(texts)} 行  (新)")
        self.last_texts_sig = texts_sig if texts_sig else self.last_texts_sig

        # Step 4: 累加 & 全局去重
        added = self.accumulator.add_page(page_dict)
        new_texts = list(added.get("new_texts") or [])

        if self.page_read_count == 1:
            if texts:
                preview = texts[:10]
                print(f"  页面文本预览（Top {len(preview)}）：")
                for s in preview:
                    print(f"    · {s}")
            else:
                print("  本页未提取到任何文本")
        else:
            if new_texts:
                print(f"  全局新增文本（{len(new_texts)} 行）：")
                for s in new_texts[:8]:
                    print(f"    · {s}")
                if len(new_texts) > 8:
                    print(f"    … 余下 {len(new_texts)-8} 行见 JSON")
            else:
                print("  全局无新增文本")

        info = {
            "page": page_no,
            "texts": texts,
            "new_texts": new_texts,
            "texts_sig": texts_sig,
            "same_as_last": same_as_last,
        }
        self.history.append({
            "page": page_no,
            "swipe_before": self.swipe_count,
            "texts_n": len(texts),
            "new_texts_n": len(new_texts),
            "texts_sig_8": texts_sig[:8] + "…" if texts_sig else "",
            "texts_same_in_a_row": self.texts_same_in_a_row,
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        })
        return info

    # ============================================================
    #  停止条件
    # ============================================================
    def is_finished(self) -> Tuple[bool, str]:
        # 到底判定（v2.3）：不依赖任何标志文本，只认「滑不动了」
        # 优先级 1：连续 N 轮页面内容相同（数字已做容差，排除点赞/评论跳动）
        if self.texts_same_in_a_row >= self.stable_to_stop:
            return True, (f"连续{self.texts_same_in_a_row}轮页面内容无变化"
                          f"（滑动已到底），停止。")
        # 优先级 2：兜底
        if self.swipe_count >= self.max_swipes:
            return True, f"达到最大滑动轮数 {self.max_swipes}，停止（兜底）。"
        return False, ""

    # ============================================================
    #  主循环
    # ============================================================
    def run(self) -> Dict[str, Any]:
        print("\n" + "=" * 70)
        print("开始采集：蚂蚁财富盘友圈页面原始文本采集（v2 纯文本，不判断买卖）")
        print(f"  max_swipes       = {self.max_swipes}  (兜底防死循环)")
        print(f"  到底判定         = 连续{self.stable_to_stop}轮页面内容无变化 → 滑不动即停（不看标志文本）")
        if self.swipe_ratios is not None:
            x1r, y1r, x2r, y2r, dur = self.swipe_ratios
            print(f"  swipe_command    = adb shell input swipe (按屏幕比例 "
                  f"{x1r},{y1r} → {x2r},{y2r}, {dur}ms)")
        else:
            print(f"  swipe_command    = adb shell input swipe {' '.join(map(str, self.swipe_coords))}")
        print("=" * 70)

        stop_reason = ""

        try:
            self.adb.check_device_status()
        except ADBError as e:
            print(f"\n❌ 设备连接失败：{e}")
            return {
                "mode": "raw_pages",
                "total_pages": 0,
                "total_unique_texts": 0,
                "swipe_count": 0,
                "pages_read": 0,
                "stop_reason": f"设备连接失败: {e}",
                "pages": [],
                "all_unique_texts": [],
                "history": [],
                "output_file": "",
            }

        while True:
            self.collect_page()
            finished, reason = self.is_finished()
            if finished:
                stop_reason = reason
                print(f"\n{stop_reason}")
                break
            if self.swipe_count >= self.max_swipes:
                stop_reason = f"达到最大滑动轮数 {self.max_swipes}，停止。"
                print(f"\n{stop_reason}")
                break
            # 一直滑，直到页面内容不再变化（滑不动）为止
            self.scroll_up()

        # ---------- 保存结果 ----------
        ts_tag = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = os.path.join(OUTPUT_DIR, f"raw_pages_{ts_tag}.json")
        all_unique_texts = sorted(self.accumulator.seen)  # 按字典序，方便浏览
        result_payload: Dict[str, Any] = {
            "mode": "raw_pages",
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "total_pages": self.accumulator.summary()["total_pages"],
            "total_unique_texts": self.accumulator.summary()["total_unique_texts"],
            "pages_read": self.page_read_count,
            "swipe_count": self.swipe_count,
            "stop_reason": stop_reason or "已完成",
            "pages": [
                {"page": p["page"], "texts": p["texts"]}
                for p in self.accumulator.pages
            ],
            "all_unique_texts": all_unique_texts,
            "history": self.history,
        }
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(result_payload, f, ensure_ascii=False, indent=2)

        # ---------- 同步导出 screen_dump md（v2 屏幕文字镜像 - 给 AI/人工阅读的主文件） ----------
        #   严格按顺序：# 页面N → 每个 text 一行，空行分隔，不做任何识别/重组
        output_screen_md = ""
        try:
            if export_screen_dump_md is not None:
                output_screen_md = export_screen_dump_md(result_payload)
        except Exception as exc:  # pragma: no cover - 展示层失败不影响主流程
                    print(f"  ⚠ screen_dump MD 导出失败（不影响 JSON 保存）：{exc}")

        # ---------- 最终打印 ----------
        print("\n" + "=" * 70)
        print("采集结束 - 最终结果（v2：屏幕文字镜像，不做任何业务识别）")
        print(f"  总页数           : {len(self.accumulator.pages)}")
        print(f"  读取页面次数   : {self.page_read_count}")
        print(f"  累计唯一文本 : {len(all_unique_texts)} 行")
        print(f"  滑动次数     : {self.swipe_count}/{self.max_swipes}")
        print(f"  停止原因     : {stop_reason}")
        print(f"  JSON 结果    : {output_file}")
        if output_screen_md:
            print(f"  镜像 MD 文件 : {output_screen_md} (AI直接投喂首选，严格屏幕镜像)")
        print("=" * 70)
        # 去掉预览："累计唯一文本预览（Top 30） → 改按原页序预览第 1 页前 30 行
        preview_page = self.accumulator.pages[0]["texts"] if self.accumulator.pages else []
        if preview_page:
            print(f"第 1 页文本预览（Top 30，屏幕顺序）：")
            for i, s in enumerate(preview_page[:30], 1):
                print(f"  {i:>3}. {s}")
            if len(preview_page) > 30:
                print(f"  … 余下 {len(preview_page)-30} 行，详见镜像 MD 文件：{output_screen_md or output_file}")
        print()

        result_payload["output_file"] = output_file
        result_payload["output_screen_md"] = output_screen_md
        return result_payload


# ============================================================
#  命令行自测入口
# ============================================================
def _build_cli() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="ScrollManager v2 - 仅采集页面原始文本（不再判断买卖）")
    p.add_argument("--max", type=int, default=DEFAULT_MAX_SWIPES,
                   help=f"最大滑动次数（默认{DEFAULT_MAX_SWIPES}）")
    p.add_argument("--stable", type=int, default=DEFAULT_STABLE_TO_STOP,
                   help=f"连续几次 texts 相同即判定到底停止（默认{DEFAULT_STABLE_TO_STOP}）")
    p.add_argument("--wait", type=float, default=DEFAULT_WAIT_AFTER_SWIPE_SEC,
                   help=f"滑动后等待秒数（默认{DEFAULT_WAIT_AFTER_SWIPE_SEC}）")
    p.add_argument("-s", "--serial", help="设备序列号")
    return p


def _main():
    args = _build_cli().parse_args()
    sm = ScrollManager(
        device_serial=args.serial,
        max_swipes=args.max,
        stable_to_stop=args.stable,
        wait_after_swipe_sec=args.wait,
    )
    try:
        sm.run()
    except KeyboardInterrupt:
        print("\n\n⏹ 用户中断")
        summary = sm.accumulator.summary()
        print(f"  已积累 {summary['total_pages']} 页 / {summary['total_unique_texts']} 行唯一文本，"
              f"已在每轮保存到 output/raw_pages_*.json")
        sys.exit(130)


if __name__ == "__main__":
    _main()
