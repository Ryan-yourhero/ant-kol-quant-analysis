"""
联网搜索工具 — 基金方向补全（v2 身份校验 + 严格 source_type）
============================================================

设计：
  1. 优先 Tavily API（.env TAVILY_API_KEY）；无 key 时降级到 DuckDuckGo HTML 抓取
  2. 强制超时 / 重试限制 / 单基金日上限
  3. 每条结果预提取 fund_code（6 位数字） / fund_company（关键词）
  4. source_type 严格分类：official / announcement / platform / media / other

  信任度（产品要求）：
    official ≈ announcement > platform > media > other

参考 .env：
  TAVILY_API_KEY=...           # 可选
  WEB_SEARCH_PROVIDER=tavily   # tavily | duckduckgo | none
  WEB_SEARCH_TIMEOUT=10
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("backend.web_search")

# ---- 读取 .env ----
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "").strip()
WEB_SEARCH_PROVIDER = os.environ.get(
    "WEB_SEARCH_PROVIDER", "tavily" if TAVILY_API_KEY else "duckduckgo"
).strip()
WEB_SEARCH_TIMEOUT = int(os.environ.get("WEB_SEARCH_TIMEOUT", "10"))

# 同一进程内的简单内存缓存
_CACHE: Dict[str, tuple] = {}
_CACHE_TTL_SECONDS = 3600

_DAILY_LIMIT_PER_FUND = 3
_DAILY_COUNTER: Dict[str, List[datetime]] = {}

# ============================================================
#  预识别：基金代码 / 基金公司 / 链接来源
# ============================================================

# 6 位基金代码（A 类 / 场内 ETF 等）
_FUND_CODE_RE = re.compile(r"\b(\d{6})\b")
# 常见基金公司关键词（用于从 URL/title 推断 fund_company）
_FUND_COMPANY_KEYWORDS = (
    "易方达", "南方", "华夏", "广发", "招商", "博时", "嘉实", "工银", "工银瑞信",
    "汇添富", "富国", "兴全", "国泰", "中欧", "景顺长城", "长城", "华安", "银华",
    "万家", "鹏华", "东吴", "国金", "财通", "国寿安保", "天弘", "鑫元", "前海开源",
    "创金合信", "信澳", "摩根", "华泰柏瑞", "金鹰", "上投摩根", "汇丰晋信", "中银",
)


def _extract_fund_code_from_text(*texts: str) -> Optional[str]:
    """从多段文本中提取最可能的 6 位基金代码。"""
    candidates: List[str] = []
    for t in texts:
        if not t:
            continue
        for m in _FUND_CODE_RE.findall(t):
            if not m.startswith(("000", "001", "002", "003", "110", "160", "161", "162",
                                  "163", "164", "165", "166", "167", "168", "501", "502",
                                  "510", "511", "512", "513", "518", "159", "150", "588")):
                # 弱过滤：基金代码首位 0/1/5 常见
                pass
            candidates.append(m)
    if not candidates:
        return None
    # 取出现次数最多的
    from collections import Counter
    c = Counter(candidates)
    return c.most_common(1)[0][0]


def _extract_fund_company_from_text(*texts: str) -> Optional[str]:
    """从文本中识别基金公司名。"""
    joined = " ".join(t or "" for t in texts)
    for kw in _FUND_COMPANY_KEYWORDS:
        if kw in joined:
            return kw
    return None


# ============================================================
#  入口
# ============================================================

def search_fund_direction(
    fund_name: str,
    fund_code: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """联网搜索基金方向信息（带身份预识别）。"""
    if WEB_SEARCH_PROVIDER == "none":
        logger.info("WEB_SEARCH_PROVIDER=none，跳过联网搜索")
        return []

    if not _within_daily_limit(fund_name):
        logger.warning("基金 %s 今日联网次数已达上限，跳过", fund_name)
        return []

    query = _build_query(fund_name, fund_code)
    cache_key = _hash_query(query)
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.info("命中联网缓存: %s", query)
        return cached

    if WEB_SEARCH_PROVIDER == "tavily" and TAVILY_API_KEY:
        results = _search_tavily(query)
    else:
        results = _search_duckduckgo(query)

    # 每条结果预提取身份信息
    for r in results:
        _prefill_identity(r, fund_name, fund_code)

    results = _rerank_by_source(results)
    _cache_set(cache_key, results)
    return results


def _build_query(fund_name: str, fund_code: Optional[str]) -> str:
    name = fund_name.rstrip(".").rstrip("…").strip()
    parts = [name]
    if fund_code:
        parts.append(f"基金代码 {fund_code}")
    parts.append("投资方向 重仓股 投资范围 基金合同")
    return " ".join(parts)


def _hash_query(q: str) -> str:
    import hashlib
    return hashlib.md5(q.encode("utf-8")).hexdigest()


# ============================================================
#  Tavily
# ============================================================

def _search_tavily(query: str) -> List[Dict[str, Any]]:
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "max_results": 10,
                "search_depth": "advanced",
                "include_answer": False,
                "include_raw_content": False,
            },
            timeout=WEB_SEARCH_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning("Tavily HTTP %s: %s", resp.status_code, resp.text[:200])
            return []
        data = resp.json()
        out = []
        for r in data.get("results", []):
            out.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
                "source_type": _classify_source(r.get("url", ""), r.get("title", "")),
            })
        return out
    except requests.RequestException as e:
        logger.error("Tavily 请求失败: %s", e)
        return []


# ============================================================
#  DuckDuckGo HTML（无 key 时的降级方案）
# ============================================================

_DDG_URL = "https://html.duckduckgo.com/html/"


def _search_duckduckgo(query: str) -> List[Dict[str, Any]]:
    try:
        resp = requests.post(
            _DDG_URL,
            data={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=WEB_SEARCH_TIMEOUT,
        )
        if resp.status_code != 200:
            return []
        results = []
        for m in re.finditer(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
            r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
            resp.text,
            flags=re.DOTALL,
        ):
            url = m.group(1)
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            snippet = re.sub(r"<[^>]+>", "", m.group(3)).strip()
            results.append({
                "title": title,
                "url": url,
                "snippet": snippet,
                "source_type": _classify_source(url, title),
            })
        return results[:10]
    except requests.RequestException as e:
        logger.error("DuckDuckGo 请求失败: %s", e)
        return []


# ============================================================
#  Source 分类（严格版）
# ============================================================

# official：基金公司官网
_OFFICIAL_DOMAINS = (
    "fund.eastmoney.com",  # 这是数据平台，下方已转 platform
    "fund123.com",
    # 常见基金公司官网（保留域名关键词）
    "e-fund.com.cn", "cs.com.cn", "cmfchina.com", "ssga.com",
    "gffunds.com.cn", "cmbchina.com", "bocim.com", "fullgoal.com",
    "eamc.com.cn", "gtjas.com", "zofund.com", "zhangfund.com",
    "thfund.com.cn", "eamc.com.cn", "wanjiafunds.com", "eamc.com.cn",
    "dongwufund.com", "dongfangfunds.com", "e-fund.com.cn",
)
# 移除 fund.eastmoney.com / fund.10jqka.com.cn 误归到 official
_OFFICIAL_DOMAINS = tuple(d for d in _OFFICIAL_DOMAINS if d not in ("fund.eastmoney.com",))

# announcement：基金季报/招股书/官方公告
_ANNOUNCEMENT_KEYWORDS = (
    "季度报告", "年度报告", "招募说明书", "基金合同", "基金公告",
    "基金产品资料概要", "law", "cninfo.com.cn", "sse.com.cn", "szse.cn",
    "shclearing.com.cn",
    ".pdf",  # 大多数官方文件是 PDF
)

# platform：东方财富/天天基金/同花顺/蚂蚁财富等
_PLATFORM_DOMAINS = (
    "eastmoney.com", "fund.eastmoney.com", "10jqka.com.cn", "fund.10jqka.com.cn",
    "xueqiu.com", "licaike.com", "1234567.com.cn",  # 蚂蚁财富
    "howbuy.com", "goodfund.com", "qimao.com", "51fund.com",
    "antfortune.com", "alipay.com",
)

# media：新闻媒体
_MEDIA_DOMAINS = (
    "sohu.com", "sina.com.cn", "qq.com", "163.com", "ifeng.com",
    "wallstreetcn.com", "21jingji.com", "yicai.com", "stcn.com",
    "jiemian.com", "cls.cn", "cnstock.com", "futunn.com",
    "hexun.com", "cs.com.cn", "people.com.cn", "xinhuanet.com",
)


def _classify_source(url: str, title: str = "") -> str:
    """严格 source_type 分类（v2）：
      official / announcement / platform / media / other
    """
    u = (url or "").lower()
    t = (title or "").lower()
    blob = u + " " + t

    # 1. announcement 优先（PDF/季报等）
    if any(kw.lower() in blob for kw in _ANNOUNCEMENT_KEYWORDS):
        return "announcement"

    # 2. official：基金公司官网
    for kw in _OFFICIAL_DOMAINS:
        if kw in u:
            return "official"

    # 3. platform
    for kw in _PLATFORM_DOMAINS:
        if kw in u:
            return "platform"

    # 4. media
    for kw in _MEDIA_DOMAINS:
        if kw in u:
            return "media"

    return "other"


# ============================================================
#  身份预识别
# ============================================================

def _prefill_identity(
    result: Dict[str, Any],
    target_fund_name: str,
    target_fund_code: Optional[str],
) -> None:
    """对单条搜索结果做轻量预识别：fund_code / fund_company。

    完整 identity_confidence 由 LLM 抽取阶段判定（结合语义）。
    """
    code = _extract_fund_code_from_text(result.get("url", ""), result.get("title", ""),
                                         result.get("snippet", ""))
    company = _extract_fund_company_from_text(result.get("title", ""), result.get("snippet", ""))

    result["matched_fund_code"] = code
    result["matched_fund_company"] = company
    # 启发式 identity_confidence（占位，LLM 阶段会重判）
    if target_fund_code and code == target_fund_code:
        result["identity_confidence"] = "high"
    elif code is None and company is None:
        result["identity_confidence"] = "low"
    else:
        result["identity_confidence"] = "medium"


def _rerank_by_source(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 source_type 优先级排序：announcement > official > platform > media > other。"""
    RANK = {"announcement": 1, "official": 2, "platform": 3, "media": 4, "other": 5}
    return sorted(results, key=lambda r: RANK.get(r.get("source_type", "other"), 99))


# ============================================================
#  缓存 / 限额
# ============================================================

def _cache_get(key: str) -> Optional[List[Dict[str, Any]]]:
    if key not in _CACHE:
        return None
    ts, data = _CACHE[key]
    if time.time() - ts > _CACHE_TTL_SECONDS:
        _CACHE.pop(key, None)
        return None
    return data


def _cache_set(key: str, data: List[Dict[str, Any]]) -> None:
    _CACHE[key] = (time.time(), data)


def _within_daily_limit(fund_name: str) -> bool:
    today = datetime.now().date()
    history = _DAILY_COUNTER.get(fund_name, [])
    history = [t for t in history if t.date() == today]
    if len(history) >= _DAILY_LIMIT_PER_FUND:
        _DAILY_COUNTER[fund_name] = history
        return False
    history.append(datetime.now())
    _DAILY_COUNTER[fund_name] = history
    return True
