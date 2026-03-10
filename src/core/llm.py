# -*- coding: utf-8 -*-
"""
LLM 调用层 — 多模型路由、降级、用量日志。

提供统一的 call_llm() 入口，支持三层模型路由：
- flash: Qwen Flash（极快极便宜）
- main:  DeepSeek V3.2
- think: DeepSeek V3.2 + thinking 模式
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import threading
import time as _time
from datetime import datetime, timezone, timedelta

import requests

from config import (
    DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL,
    QWEN_API_KEY, QWEN_BASE_URL, QWEN_MODEL, QWEN_VL_MODEL,
)
from infra.logging import get_logger
from infra.paths import USAGE_LOG_FILE
from infra.shared import executor as _executor
import prompt.templates as prompts

logger = get_logger(__name__)

# 线程本地变量：暂存当前请求的 user_id，供 LLM 调用层记录用量
_thread_local = threading.local()


def set_current_user(user_id: str) -> None:
    """设置当前线程的 user_id（在 process() 入口调用）"""
    _thread_local.user_id = user_id


# ============ 模型选择 ============

def select_model_tier(payload, is_system_action=False, action=None) -> str:
    """
    根据请求类型选择模型层级。
    Returns: "flash" | "main" | "think"
    """
    if is_system_action:
        if action in ("morning_report", "evening_checkin",
                       "daily_report", "weekly_review", "monthly_review"):
            return "main"
        if action == "companion_check":
            return "flash"
        return "main"
    return "main"


def select_skill_model_tier(skill_name: str) -> str:
    """Skill 执行时的模型选择（Agent Loop 中）"""
    if skill_name in ("deep_dive", "decision_track"):
        return "think"
    return "main"


# ============ 统一调用入口 ============

def call_llm(messages, model_tier="main", max_tokens=3000,
             temperature=0.3, enable_thinking=None):
    """
    统一 LLM 调用入口，支持三层模型路由 + 自动降级。

    Args:
        model_tier: "flash" | "main" | "think"
        enable_thinking: 覆盖 thinking 设置。None = 按 tier 自动决定
    Returns:
        str: LLM 回复文本，失败返回 None
    """
    try:
        if model_tier == "flash":
            return _call_qwen_flash(messages, max_tokens, temperature)

        thinking = enable_thinking
        if thinking is None:
            thinking = (model_tier == "think")

        return _call_deepseek(messages, max_tokens, temperature,
                              enable_thinking=thinking)
    except Exception as e:
        if model_tier == "flash":
            logger.warning("[Brain] Qwen Flash failed: %s, fallback to DeepSeek", e)
            try:
                return _call_deepseek(messages, max_tokens, temperature,
                                      enable_thinking=False)
            except Exception as e2:
                logger.error("[Brain] DeepSeek fallback also failed: %s", e2)
                return None
        logger.error("[Brain] LLM call failed (tier=%s): %s", model_tier, e)
        return None


def _call_deepseek(messages, max_tokens=3000, temperature=0.3,
                   enable_thinking=False):
    """调用 DeepSeek V3.2，支持 thinking 模式控制"""
    url = f"{DEEPSEEK_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature
    }
    if "v3.2" in DEEPSEEK_MODEL:
        data["enable_thinking"] = enable_thinking

    total_chars = sum(len(m.get("content", "")) for m in messages)
    tier_label = "Think" if enable_thinking else "Main"
    logger.info("[Brain][%s] DeepSeek request: model=%s, thinking=%s, prompt_chars=%s, max_tokens=%s",
                tier_label, DEEPSEEK_MODEL, enable_thinking, total_chars, max_tokens)

    t0 = _time.time()
    resp = requests.post(url, headers=headers, json=data, timeout=60)
    t1 = _time.time()

    if resp.status_code == 200:
        result = resp.json()
        usage = result.get("usage", {})
        logger.info("[Brain][%s] DeepSeek response: %.1fs, prompt_tokens=%s, completion_tokens=%s",
                    tier_label, t1-t0, usage.get("prompt_tokens"), usage.get("completion_tokens"))
        _log_llm_usage("think" if enable_thinking else "main",
                       DEEPSEEK_MODEL, usage, t1 - t0)
        return result["choices"][0]["message"]["content"]

    logger.error("[Brain][%s] DeepSeek API error: %s - %s", tier_label, resp.status_code, resp.text[:200])
    raise RuntimeError(f"DeepSeek API {resp.status_code}")


def _call_qwen_flash(messages, max_tokens=500, temperature=0.3):
    """调用 Qwen Flash（阿里云百炼），极快极便宜"""
    url = f"{QWEN_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {QWEN_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": QWEN_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature
    }

    total_chars = sum(len(m.get("content", "")) for m in messages)
    logger.info("[Brain][Flash] Qwen request: model=%s, prompt_chars=%s, max_tokens=%s",
                QWEN_MODEL, total_chars, max_tokens)

    t0 = _time.time()
    resp = requests.post(url, headers=headers, json=data, timeout=30)
    t1 = _time.time()

    if resp.status_code == 200:
        result = resp.json()
        usage = result.get("usage", {})
        logger.info("[Brain][Flash] Qwen response: %.1fs, prompt_tokens=%s, completion_tokens=%s",
                    t1-t0, usage.get("prompt_tokens"), usage.get("completion_tokens"))
        _log_llm_usage("flash", QWEN_MODEL, usage, t1 - t0)
        return result["choices"][0]["message"]["content"]

    logger.error("[Brain][Flash] Qwen API error: %s - %s", resp.status_code, resp.text[:200])
    raise RuntimeError(f"Qwen API {resp.status_code}")


def call_qwen_vl(image_base64, prompt=None):
    """
    调用千问 VL（视觉语言模型）理解图片内容。

    Args:
        image_base64: 图片的 base64 编码字符串
        prompt: 图片理解的提示语
    Returns:
        str: 图片描述文本，失败返回 None
    """
    if prompt is None:
        prompt = prompts.VL_DEFAULT
    url = f"{QWEN_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {QWEN_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": QWEN_VL_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_base64}"
                        }
                    },
                    {
                        "type": "text",
                        "text": prompt
                    }
                ]
            }
        ],
        "max_tokens": 500
    }

    logger.info("[Brain][VL] Qwen VL request: model=%s, image_size=%sKB, prompt=%s",
                QWEN_VL_MODEL, len(image_base64)//1024, prompt[:50])

    t0 = _time.time()
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=60)
        t1 = _time.time()

        if resp.status_code == 200:
            result = resp.json()
            usage = result.get("usage", {})
            description = result["choices"][0]["message"]["content"]
            logger.info("[Brain][VL] Qwen VL response: %.1fs, prompt_tokens=%s, completion_tokens=%s, desc=%s",
                    t1-t0, usage.get("prompt_tokens"), usage.get("completion_tokens"), description[:80])
            _log_llm_usage("vl", QWEN_VL_MODEL, usage, t1 - t0)
            return description

        logger.error("[Brain][VL] Qwen VL API error: %s - %s", resp.status_code, resp.text[:200])
        return None
    except Exception as e:
        logger.error("[Brain][VL] Qwen VL call exception: %s", e)
        return None


# 向后兼容别名
def call_deepseek(messages, max_tokens=500, temperature=0.3):
    """向后兼容：等同于 call_llm(tier='main', thinking=off)"""
    return call_llm(messages, model_tier="main", max_tokens=max_tokens,
                    temperature=temperature)


# ============ 用量日志 ============

_JSONL_ROTATE_MAX_MB = 10


def _log_llm_usage(model_tier, model_name, usage_dict, latency_s):
    """记录一次 LLM 调用的用量到 usage_log.jsonl，支持自动轮转"""
    try:
        user_id = getattr(_thread_local, "user_id", "unknown")
        now = datetime.now(timezone(timedelta(hours=8)))

        entry = {
            "ts": now.isoformat(timespec="seconds"),
            "user_id": user_id,
            "model_tier": model_tier,
            "model": model_name,
            "prompt_tokens": usage_dict.get("prompt_tokens", 0),
            "completion_tokens": usage_dict.get("completion_tokens", 0),
            "total_tokens": usage_dict.get("total_tokens", 0),
            "latency_s": round(latency_s, 1),
        }

        os.makedirs(os.path.dirname(USAGE_LOG_FILE), exist_ok=True)
        rotate_jsonl(USAGE_LOG_FILE, max_size_mb=10)

        with open(USAGE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error("[UsageLog] record failed: %s", e)


def rotate_jsonl(filepath, max_size_mb=None):
    """JSONL 文件轮转：超过阈值时重命名为 .bak 并压缩"""
    if max_size_mb is None:
        max_size_mb = _JSONL_ROTATE_MAX_MB
    try:
        if not os.path.exists(filepath):
            return
        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        if size_mb < max_size_mb:
            return

        now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
        bak_path = f"{filepath}.{now_str}.bak"
        os.rename(filepath, bak_path)
        logger.info("[Rotate] %s (%.1fMB) -> %s", filepath, size_mb, bak_path)

        def _compress():
            try:
                with open(bak_path, 'rb') as f_in:
                    with gzip.open(f"{bak_path}.gz", 'wb') as f_out:
                        shutil.copyfileobj(f_in, f_out)
                os.remove(bak_path)
                logger.info("[Rotate] compressed: %s.gz", bak_path)
            except Exception as e:
                logger.error("[Rotate] compress failed: %s", e)

        _executor.submit(_compress)
    except Exception as e:
        logger.error("[Rotate] rotation failed: %s", e)
