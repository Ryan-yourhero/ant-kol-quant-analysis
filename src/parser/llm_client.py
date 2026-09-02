"""
LLM 客户端 — 兼容 OpenAI / 类 OpenAI 接口
配置从 .env 读取：LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
"""

from __future__ import annotations

import os
import json
import time
import logging
from typing import Any, Dict, Optional

import requests  # type: ignore

logger = logging.getLogger("parser.llm_client")

# ---- 读取 .env ----
try:
    from dotenv import load_dotenv  # type: ignore

    _ENV_LOADED = load_dotenv()
except ImportError:
    _ENV_LOADED = False


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


LLM_API_KEY: str = _env("LLM_API_KEY", "")
LLM_BASE_URL: str = _env("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_MODEL: str = _env("LLM_MODEL", "gpt-4o-mini")


def is_configured() -> bool:
    """检查 AI 接口是否已配置"""
    return bool(LLM_API_KEY and LLM_BASE_URL)


def _do_request(
    messages: list,
    model: str,
    temperature: float,
    max_tokens: int,
    timeout: int,
    response_format: Optional[Dict[str, str]],
    extra_body: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """执行单次 API 请求，返回响应字典"""
    url = f"{LLM_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format:
        body["response_format"] = response_format
    if extra_body:
        body.update(extra_body)

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=timeout)
    except requests.RequestException as e:
        logger.error("LLM 请求异常: %s", e)
        return {"ok": False, "error": f"网络异常: {e}"}

    if resp.status_code != 200:
        logger.error("LLM 返回错误: HTTP %s, body=%s", resp.status_code, resp.text[:500])
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:300]}"}

    try:
        return {"ok": True, "data": resp.json()}
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"JSON 解析失败: {e}"}


def _extract_content(data: dict) -> Dict[str, Any]:
    """从 API 响应中提取 content 和诊断信息"""
    choices = data.get("choices", [])
    if not choices:
        return {"content": "", "finish_reason": "no_choices", "error": "LLM 返回空 choices"}

    choice = choices[0]
    finish_reason = choice.get("finish_reason", "unknown")
    msg = choice.get("message", {})
    content = msg.get("content", "") or ""
    reasoning_content = msg.get("reasoning_content", "") or ""

    return {
        "content": content,
        "finish_reason": finish_reason,
        "content_len": len(content),
        "reasoning_len": len(reasoning_content) if reasoning_content else 0,
    }


def chat_completion(
    messages: list,
    *,
    model: Optional[str] = None,
    temperature: float = 0.1,
    max_tokens: int = 4096,
    timeout: int = 120,
    response_format: Optional[Dict[str, str]] = None,
    extra_body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    调用 OpenAI 兼容的 chat/completions 接口。

    Args:
        messages: 消息列表
        model: 模型名称（默认读取 .env）
        temperature: 温度
        max_tokens: 最大输出 token
        timeout: 超时秒数
        response_format: 响应格式约束（如 {"type": "json_object"}）
        extra_body: 额外的请求体字段（如关闭 Thinking）

    Returns:
        {"ok": True, "content": str} 或 {"ok": False, "error": str}
    """
    if not is_configured():
        return {"ok": False, "error": "LLM 未配置（缺少 LLM_API_KEY / LLM_BASE_URL）"}

    actual_model = model or LLM_MODEL

    # ---- 自动关闭 Flash 模型的 Thinking ----
    if "flash" in actual_model.lower() and extra_body is None:
        extra_body = {"thinking": {"type": "disabled"}}

    logger.info("调用 LLM: model=%s, url=%s", actual_model, LLM_BASE_URL)

    t0 = time.time()

    # ---- 第一次请求 ----
    result = _do_request(
        messages, actual_model, temperature, max_tokens, timeout,
        response_format, extra_body,
    )

    if not result["ok"]:
        logger.info("LLM 耗时: %.1fs (失败)", time.time() - t0)
        return result

    info = _extract_content(result["data"])
    elapsed = time.time() - t0

    logger.info(
        "LLM 完成: finish_reason=%s, content=%d chars, reasoning=%d chars, 耗时=%.1fs",
        info["finish_reason"], info["content_len"], info["reasoning_len"], elapsed,
    )

    if info.get("error"):
        return {"ok": False, "error": info["error"]}

    content = info["content"]

    # ---- content 为空：自动重试一次 ----
    if not content:
        logger.warning(
            "LLM content 为空 (finish_reason=%s, reasoning=%d chars)，自动重试...",
            info["finish_reason"], info["reasoning_len"],
        )
        t1 = time.time()
        result2 = _do_request(
            messages, actual_model, temperature, max_tokens, timeout,
            response_format, extra_body,
        )
        if not result2["ok"]:
            logger.error("LLM 重试失败: %s", result2["error"])
            return {"ok": False, "error": f"重试失败: {result2['error']}"}

        info2 = _extract_content(result2["data"])
        elapsed2 = time.time() - t1
        logger.info(
            "LLM 重试: finish_reason=%s, content=%d chars, reasoning=%d chars, 耗时=%.1fs",
            info2["finish_reason"], info2["content_len"], info2["reasoning_len"], elapsed2,
        )

        content = info2["content"]
        if not content:
            logger.error(
                "LLM 重试后 content 仍为空 (finish_reason=%s)",
                info2["finish_reason"],
            )
            return {"ok": False, "error": f"content 为空 (finish_reason={info2['finish_reason']})"}

    return {"ok": True, "content": content}
