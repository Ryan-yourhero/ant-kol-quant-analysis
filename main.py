"""
蚂蚁财富大V操作数据采集工具 - 主入口

功能：
1. 连接ADB设备
2. 采集蚂蚁财富理财盘友圈页面的UI XML
3. 解析XML并提取大V操作记录（名称/操作/基金/金额/时间）
4. 输出为JSON或CSV格式

使用方式：
    # 查看帮助
    python main.py --help

    # 基础用法（自动连接设备并采集当前页面）
    python main.py

    # 指定设备序列号
    python main.py --device <serial>

    # 从本地XML文件离线解析（无需连接手机）
    python main.py --xml-file path/to/dump.xml

    # 输出到CSV
    python main.py --format csv -o output.csv

    # 启动App后再采集 + 滑动加载更多
    python main.py --launch --scroll 3
"""

import os
import sys
import json
import csv
import argparse
import logging
import datetime
from typing import List

# 确保项目根目录在PYTHONPATH中
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from config import settings
from core.adb_controller import ADBController, ADBError
from core.xml_parser import UIXmlParser
# v2: data_extractor 只保留兼容（爬虫阶段不再做交易判断）
from core.data_extractor import OperationDataExtractor, KolOperation  # noqa: F401
from core.scroll_manager import ScrollManager
from core.raw_text_extractor import extract_texts, TextAccumulator
# v2 主 MD 文件：严格屏幕镜像 MD（AI 直接投喂首选）
from core.screen_dump_exporter import export_screen_dump_md
from core.env_checker import run_all_checks as env_check_run_all


# ============================================================
#  日志初始化
# ============================================================

def setup_logging(level_str: str = None):
    """初始化日志系统（控制台+文件）"""
    log_level = getattr(logging, (level_str or settings.LOG_LEVEL).upper(), logging.INFO)

    os.makedirs(settings.LOG_DIR, exist_ok=True)
    log_file = os.path.join(
        settings.LOG_DIR,
        f"collect_{datetime.datetime.now().strftime('%Y%m%d')}.log",
    )

    # 根logger配置
    root = logging.getLogger()
    root.setLevel(log_level)
    # 避免重复添加handle
    if root.handlers:
        return

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    # 控制台Handler
    ch = logging.StreamHandler()
    ch.setLevel(log_level)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # 文件Handler
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)


logger = logging.getLogger("main")


# ============================================================
#  数据输出
# ============================================================

def save_results(operations, output_path: str, fmt: str):
    """
    v2: 保存结果，支持两种 payload：
      - List[KolOperation]（旧数据，兼容 --xml-file 走旧链路时的保留）
      - dict：{"page": 1, "texts": [...]} 或 accumulate 后的完整 raw_page dict

    新架构默认保存 raw_text dict。
    """
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    # 识别 payload
    if isinstance(operations, dict):
        payload = operations
    elif isinstance(operations, list):
        # 旧 list 兼容：构造成 records 结构
        records = [
            op.to_dict() if isinstance(op, KolOperation) else op
            for op in operations
        ]
        payload = {
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "total": len(records),
            "records": records,
        }
    else:
        raise TypeError(f"save_results 不支持的 payload 类型: {type(operations)}")

    if fmt.lower() == "json":
        payload.setdefault("generated_at", datetime.datetime.now().isoformat(timespec="seconds"))
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    elif fmt.lower() == "csv":
        # CSV 仅对 records 结构有意义
        records = payload.get("records") if isinstance(payload.get("records"), list) else []
        if not records:
            texts = payload.get("texts") if isinstance(payload, dict) else []
            if isinstance(texts, list) and texts:
                # raw_text 的 CSV 简化视图：两列 page_idx, text
                page_no = int(payload.get("page") or 0)
                with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["page", "text"])
                    for i, t in enumerate(texts, 1):
                        writer.writerow([page_no if page_no else 1, t])
            else:
                fields = list(KolOperation().to_dict().keys())
                with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=fields)
                    writer.writeheader()
        else:
            fields = list(records[0].keys())
            with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                writer.writerows(records)
    else:
        raise ValueError(f"不支持的输出格式: {fmt} (支持: json, csv)")

    total_texts = (
        len(payload.get("texts") or [])
        if "texts" in payload and isinstance(payload.get("records"), list) is False
        else len(payload.get("records") or [])
    )
    logger.info(f"采集结果已保存: {output_path} ({total_texts} 行, 格式: {fmt})")


def print_summary(result):
    """
    v2: 在终端打印结果摘要。
    result 支持：
      - dict(raw_text 结果): {"page":..., "texts":[...], ...}  / {..., "pages":[], "all_unique_texts":[]}
      - List[KolOperation]: 兼容旧链路
    """
    print("\n" + "=" * 80)
    if isinstance(result, list):
        operations = result
        print(f"  采集完成，共提取 {len(operations)} 条大V操作记录 [兼容旧模式]")
        print("=" * 80)
        if not operations:
            print("  (未识别到任何操作记录，可尝试：)")
            print("  1. 确认蚂蚁财富App已打开到盘友圈页面")
            print("  2. 使用 --xml-file 保存XML以便离线调试")
            print("  3. 调整 --scroll 参数加载更多内容")
            print()
            return
        print(f"{'#':>3}  {'置信度':>6}  {'大V':<14}  {'操作':<6}  "
              f"{'基金名称':<26}  {'金额':<12}  {'时间'}")
        print("-" * 80)
        for i, op in enumerate(operations, 1):
            print(f"{i:>3}  {op.confidence:>6.2f}  {op.kol_name or '(空)':<14}  "
                  f"{op.operation_text or op.operation:<6}  "
                  f"{(op.fund_name or '(空)')[:26]:<26}  "
                  f"{op.amount or '(空)':<12}  "
                  f"{op.timestamp or '(空)'}")
        print()
        return

    # ---- v2 纯文本摘要 ----
    pages = result.get("pages") if isinstance(result.get("pages"), list) else None
    texts = result.get("texts") if isinstance(result.get("texts"), list) else None
    all_unique = result.get("all_unique_texts")
    print("  采集完成（v2 纯文本）：未在爬虫阶段判断买卖/交易类型")
    print("  (结构化判断将留给后续 AIParser 模块：raw_text -> structured_trade JSON)")
    print("=" * 80)

    if pages is not None:
        print(f"  总页数        : {len(pages)}")
    if texts is not None:
        print(f"  本页文本数    : {len(texts)}")
    if all_unique is not None:
        print(f"  累计唯一文本  : {len(all_unique)} 行")

    # 文本预览（优先 all_unique，其次 texts，其次各页拼接）
    preview: List[str] = []
    if isinstance(all_unique, list):
        preview = list(all_unique)[:30]
        label = "累计唯一文本预览（Top 30）"
    elif isinstance(texts, list):
        preview = list(texts)[:50]
        label = "本页文本预览（Top 50）"
    elif isinstance(pages, list):
        for p in pages:
            for t in (p.get("texts") or []):
                preview.append(t)
                if len(preview) >= 80:
                    break
            if len(preview) >= 80:
                break
        label = "分页文本预览（Top 80）"
    else:
        label = "(无预览)"

    if preview:
        print(f"\n  {label}：")
        for i, s in enumerate(preview, 1):
            print(f"  {i:>3}. {s}")
    else:
        print(f"\n  {label}：(空)")
    print()


# ============================================================
#  核心采集流程
# ============================================================

def collect_from_device(args) -> str:
    """从ADB设备采集XML，返回XML内容"""
    logger.info("===== 开始连接ADB设备 =====")
    adb = ADBController(
        adb_path=args.adb_path,
        device_serial=args.device,
    )

    try:
        device = adb.connect()
        logger.info(f"设备连接成功: {device.serial}" +
                    (f" [{device.model}]" if device.model else ""))
    except ADBError as e:
        logger.error(f"设备连接失败: {e}")
        sys.exit(1)

    # 可选：启动App
    if args.launch:
        try:
            adb.launch_app()
            logger.info("已启动蚂蚁财富App，请等待页面加载...")
            import time
            time.sleep(3)
        except ADBError as e:
            logger.warning(f"启动App失败（可能已在前台）: {e}")

    # 可选：滑动加载更多
    if args.scroll and args.scroll > 0:
        logger.info(f"将向上滑动 {args.scroll} 次以加载更多内容")
        import time
        for i in range(args.scroll):
            adb.swipe_up()
            time.sleep(0.8)
        # 滑动后回到顶部再采集（可选，这里直接采集当前位置）

    # 可选：截图调试
    if args.screenshot:
        try:
            adb.screenshot()
        except ADBError as e:
            logger.warning(f"截图失败: {e}")

    # 采集XML
    logger.info("===== 开始采集页面XML =====")
    try:
        xml_content = adb.dump_ui_xml(save_local=not args.no_save_dump)
    except ADBError as e:
        logger.error(f"采集页面XML失败: {e}")
        sys.exit(2)

    # 如果需要，dump一份文本预览到日志
    if args.verbose:
        parser = UIXmlParser()
        root = parser.parse(xml_content)
        preview = parser.dump_tree_text(root, max_depth=8)
        logger.debug("页面文本预览（前100行）:\n" + "\n".join(preview.splitlines()[:100]))

    return xml_content


def parse_and_extract(xml_content: str):
    """
    v2: 解析 XML 并输出页面原始 texts（dict），不再判断买卖/交易类型。
    保留与旧函数同名的入口，仅返回类型改为 dict: {page, texts:[...]}。
    """
    logger.info("===== 解析XML并提取页面 texts（v2，不判断买卖） =====")
    parser = UIXmlParser()
    try:
        root = parser.parse(xml_content)
    except ValueError as e:
        logger.error(f"XML解析失败: {e}")
        sys.exit(3)

    all_text_nodes = parser.get_all_text_nodes(root)
    logger.info(f"页面共包含 {len(all_text_nodes)} 个含文本的UI节点（仅做文本提取）")

    # v2：复用 raw_text_extractor 的标准清洗/去重/系统按钮过滤
    page_dict = extract_texts(xml_content, page=1)
    return page_dict


def process_xml_file(xml_path: str) -> str:
    """从本地XML文件读取内容"""
    if not os.path.exists(xml_path):
        logger.error(f"XML文件不存在: {xml_path}")
        sys.exit(4)
    logger.info(f"读取本地XML文件: {xml_path}")
    with open(xml_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


# ============================================================
#  参数解析
# ============================================================

def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="蚂蚁财富App盘友圈大V操作数据采集工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 基本采集（自动连接唯一设备）
  python main.py

  # 指定设备 + 启动App + 滑动3次加载更多 + 输出CSV
  python main.py --device ABC123 --launch --scroll 3 --format csv

  # 离线解析本地XML
  python main.py --xml-file dumps/dump_XXX.xml
""",
    )

    # ===== ADB / 设备相关 =====
    group_adb = parser.add_argument_group("ADB与设备")
    group_adb.add_argument(
        "--adb-path",
        help="ADB可执行文件路径（默认自动查找）",
    )
    group_adb.add_argument(
        "--device", "-d",
        help="目标设备序列号（多设备时必填），也可用环境变量TARGET_DEVICE_SERIAL",
    )
    group_adb.add_argument(
        "--launch",
        action="store_true",
        help="采集前启动蚂蚁财富App",
    )
    group_adb.add_argument(
        "--scroll",
        type=int,
        default=0,
        metavar="N",
        help="采集前向上滑动N次以加载更多内容（默认0）",
    )
    group_adb.add_argument(
        "--screenshot",
        action="store_true",
        help="采集XML同时保存截图（用于对照调试）",
    )
    group_adb.add_argument(
        "--no-save-dump",
        action="store_true",
        help="不把XML dump保存到本地dumps目录（默认保存）",
    )

    # ===== 输入源 =====
    group_input = parser.add_argument_group("输入源（二选一）")
    group_input.add_argument(
        "--xml-file", "-x",
        metavar="PATH",
        help="使用本地XML文件作为输入（无需连接手机，离线调试用）",
    )

    # ===== 输出相关 =====
    group_out = parser.add_argument_group("输出")
    group_out.add_argument(
        "--output", "-o",
        metavar="PATH",
        help="输出文件路径（默认自动生成到output目录）",
    )
    group_out.add_argument(
        "--format", "-f",
        choices=["json", "csv"],
        default=settings.OUTPUT_FORMAT,
        help=f"输出文件格式（默认: {settings.OUTPUT_FORMAT}）",
    )
    group_out.add_argument(
        "--no-save",
        action="store_true",
        help="只打印结果，不保存JSON/CSV文件",
    )

    # ===== 通用 =====
    parser.add_argument(
        "--check-env",
        action="store_true",
        help="运行环境自检（Python/ADB/设备/应用/openpyxl），通过后再执行其它操作",
    )
    parser.add_argument(
        "--check-env-only",
        action="store_true",
        help="仅运行环境自检，不做其它操作（等价于独立检测命令）",
    )
    # ===== 调试入口 =====
    group_debug = parser.add_argument_group("调试（不启动完整采集）")
    group_debug.add_argument(
        "--dump",
        action="store_true",
        help="仅调试：对当前页 dump XML → adb pull 保存到 debug/xml/window_YYYYMMDD_HHMMSS.xml",
    )
    group_debug.add_argument(
        "--show-keywords",
        action="store_true",
        help="配合 --dump 使用：读取刚保存的 XML，检测 6 个交易关键词是否出现",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="详细日志模式（含页面文本预览）",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help=f"日志级别（默认: {settings.LOG_LEVEL}）",
    )

    return parser


# ============================================================
#  Main
# ============================================================

def run_scroll_manager_mode():
    """
    测试入口：直接 python main.py 运行 ScrollManager
    1. 检查手机连接
    2. 开始当前页面自动滚动采集（v2：只采集页面 texts，不判断买卖）
    3. 输出最终结果（JSON + 镜像 MD）
    """
    print("\n" + "#" * 70)
    print("# 测试入口：python main.py  →  ScrollManager 自动滚动采集（v2 纯文本）")
    print("#  架构：爬虫阶段只采集 texts，交易判断留给后续 AIParser(raw_text -> structured_trade)")
    print("#  如需单页采集/离线解析等其他模式，请加参数运行：python main.py --help")
    print("#" * 70)

    # 初始化日志（默认INFO级别）
    setup_logging()

    # 确保目录存在
    for d in (settings.LOCAL_DUMP_DIR, settings.OUTPUT_DIR, settings.LOG_DIR):
        os.makedirs(d, exist_ok=True)

    sm = ScrollManager()
    try:
        result = sm.run()
    except KeyboardInterrupt:
        print("\n\n⏹ 用户中断")
        summary = sm.accumulator.summary()
        print(f"  已积累 {summary['total_pages']} 页 / {summary['total_unique_texts']} 行唯一文本，"
              f"已保存到 output/raw_pages_*.json")
        sys.exit(130)

    # 最终摘要
    print("\n✅ 测试入口执行完成")
    json_output_file = result.get("output_file", "")
    # v2 主 MD：严格屏幕镜像（AI 直接投喂首选）
    screen_md = result.get("output_screen_md", "") or ""

    pages_read = result.get("pages_read", 0)
    total_unique_texts = result.get("total_unique_texts", 0)
    print(f"   JSON 结果           : {json_output_file or '(未保存)'}")
    if screen_md:
        print(f"   屏幕文字镜像 MD     : {screen_md}（严格屏幕顺序、不识别、不重组、AI直接投喂首选）")
    print(f"   读取页面次数        : {pages_read}")
    print(f"   累计唯一文本        : {total_unique_texts} 行")
    print(f"   停止原因            : {result.get('stop_reason', '')}")

    # ====== 最终摘要（仅 JSON + MD） ======
    print("\n" + "=" * 60)
    print("📂 数据输出摘要")
    print(f"   原始文本 JSON: {json_output_file}")
    if screen_md:
        print(f"   镜像 MD 文件 : {screen_md}")
    print("=" * 60)
    return result


def main():
    # 测试入口：无任何 CLI 参数时，直接跑 ScrollManager
    if len(sys.argv) == 1:
        run_scroll_manager_mode()
        return

    # 其他带参数场景：保留原有 argparse 流程
    parser = build_argparser()
    args = parser.parse_args()

    # ----------------------------------------------------------
    #  --check-env-only  /  --check-env：优先做环境检测
    # ----------------------------------------------------------
    if args.check_env_only or args.check_env:
        setup_logging(args.log_level or ("DEBUG" if args.verbose else None))
        ok = env_check_run_all(
            adb_path=args.adb_path,
            device_serial=args.device,
            verbose=True,
        )
        if args.check_env_only:
            sys.exit(0 if ok else 1)
        if not ok:
            print("\n[!] 环境检测未通过，已按提示修复后再试，或去掉 --check-env 继续执行。")
            sys.exit(2)

    # ----------------------------------------------------------
    #  --dump：调试入口（仅 dump + pull XML 到 debug/xml/，不做完整采集）
    # ----------------------------------------------------------
    if args.dump:
        setup_logging(args.log_level or ("DEBUG" if args.verbose else None))

        # 1) 确保目录
        DEBUG_XML_DIR = os.path.join(BASE_DIR, "debug", "xml")
        os.makedirs(DEBUG_XML_DIR, exist_ok=True)

        # 2) 复用现有 ADBController（走 settings.ADB_PATH → env ADB_PATH → PATH 同一套优先级）
        device_serial = args.device or settings.TARGET_DEVICE_SERIAL or None
        try:
            adb = ADBController(adb_path=args.adb_path, device_serial=device_serial)
        except ADBError as e:
            print(f"\n[ADB] 初始化失败:\n{e}")
            sys.exit(2)

        # 3) 连接状态
        try:
            dev_list = adb.check_device_status()
            ok_online = any(d.get("available") for d in dev_list) if isinstance(dev_list, list) else True
        except ADBError as e:
            print(f"\n[ADB] device NOT connected: {e}")
            sys.exit(3)
        print("[ADB] device connected")

        # 4) dump 到手机 /sdcard/window.xml （复用 dump_ui_xml，内部就是 adb shell uiautomator dump）
        try:
            adb.dump_ui_xml()
        except ADBError as e:
            print(f"\n[XML] dump FAILED: {e}")
            sys.exit(4)
        print("[XML] dump success")

        # 5) pull 到本地 debug/xml/window_YYYYMMDD_HHMMSS.xml
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        local_path = os.path.join(DEBUG_XML_DIR, f"window_{ts}.xml")
        try:
            saved_path = adb.pull_xml(local_path)
        except ADBError as e:
            print(f"\n[XML] pull FAILED: {e}")
            sys.exit(5)
        saved_path = saved_path or local_path
        saved_path = os.path.abspath(saved_path)
        print(f"[XML] saved: {saved_path}")
        print(f"      相对路径: {os.path.relpath(saved_path, BASE_DIR)}")

        # 6) 可选：关键词检测
        if args.show_keywords:
            print()
            if not os.path.isfile(saved_path):
                print("[关键词] 跳过：保存的 XML 文件不存在")
            else:
                try:
                    with open(saved_path, "r", encoding="utf-8", errors="replace") as f:
                        xml_text = f.read()
                except Exception as e:
                    print(f"[关键词] 读取 XML 失败: {e}")
                    xml_text = ""

                KEYWORDS = [
                    "买入确认中",
                    "卖出确认中",
                    "转换确认中",
                    "撤销",
                    "买入金额(元)",
                    "卖出份额(份)",
                ]
                print("关键词检测结果：")
                for kw in KEYWORDS:
                    hit = (kw in xml_text)
                    print(f"  {kw}: {'yes' if hit else 'no'}")
        sys.exit(0)

    # 日志
    setup_logging(args.log_level or ("DEBUG" if args.verbose else None))

    logger.info("蚂蚁财富大V操作数据采集工具启动")
    logger.info(f"项目目录: {BASE_DIR}")

    # 确保运行时目录存在
    for d in (settings.LOCAL_DUMP_DIR, settings.OUTPUT_DIR, settings.LOG_DIR):
        os.makedirs(d, exist_ok=True)

    # Step 1: 获取XML内容
    if args.xml_file:
        xml_content = process_xml_file(args.xml_file)
    else:
        xml_content = collect_from_device(args)

    # Step 2: 解析 + 提取 (v2: page_texts dict，不再判断买卖)
    page_result = parse_and_extract(xml_content)

    # Step 3: 打印摘要
    print_summary(page_result)

    # Step 4: 保存文件（JSON + screen_dump 镜像 MD；不做识别/不总结/不重组）
    output_path = None
    output_screen_md_path = None
    if not args.no_save:
        if args.output:
            output_path = args.output
        else:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            # v2: argparse 单页模式也输出 raw_page 前缀；若用户想继续用旧前缀可改 settings
            prefix = getattr(settings, "RAW_PAGE_FILENAME_PREFIX", settings.OUTPUT_FILENAME_PREFIX)
            fname = f"{prefix}_{ts}.{args.format.lower()}"
            output_path = os.path.join(settings.OUTPUT_DIR, fname)
        save_results(page_result, output_path, args.format)

        # Step 4b: 同步生成 screen_dump 镜像 MD（严格顺序、每行空行、AI 直接投喂首选）
        if isinstance(page_result, dict):
            try:
                # 路径约定：raw_page_20260805_144000.json -> screen_dump_20260805_144000.md
                md_target = os.path.join(
                    os.path.dirname(output_path),
                    os.path.splitext(os.path.basename(output_path))[0]
                    .replace(getattr(settings, "RAW_PAGE_FILENAME_PREFIX", "raw_page"),
                             "screen_dump")
                    .replace(getattr(settings, "OUTPUT_FILENAME_PREFIX", "kol_operations"),
                             "screen_dump")
                    + ".md",
                )
                output_screen_md_path = export_screen_dump_md(page_result, md_path=md_target)
            except Exception as exc:
                logger.warning(f"screen_dump 镜像 MD 导出失败（JSON/CSV 已保存，不影响主流程）: {exc}")

    # ====== 最终摘要（仅 JSON + MD） ======
    if (not args.no_save) and output_path:
        print("\n" + "=" * 60)
        print("📂 数据输出摘要")
        print(f"   原始文本 JSON: {output_path}")
        if output_screen_md_path:
            print(f"   镜像 MD 文件 : {output_screen_md_path}（严格屏幕顺序，AI 直接投喂首选）")
        print("=" * 60)

    logger.info("全部流程完成，退出。")


if __name__ == "__main__":
    main()
