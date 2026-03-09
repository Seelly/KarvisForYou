# -*- coding: utf-8 -*-
"""Web 令牌管理服务 — 从 user_context.py 拆分"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timedelta

from log_utils import BEIJING_TZ, get_logger

logger = get_logger(__name__)

# 复用 user_context 中的系统路径
from user_context import SYSTEM_DIR

TOKENS_FILE = os.path.join(SYSTEM_DIR, "tokens.json")

_tokens_lock = threading.Lock()


def _read_tokens() -> dict:
    """读取令牌表"""
    try:
        if os.path.exists(TOKENS_FILE):
            with open(TOKENS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "tokens" not in data:
                    data["tokens"] = {}
                return data
    except Exception as e:
        logger.error("[Tokens] 读取令牌表失败: %s", e)
    return {"tokens": {}}


def _write_tokens(data: dict):
    """写入令牌表"""
    try:
        os.makedirs(os.path.dirname(TOKENS_FILE), exist_ok=True)
        with open(TOKENS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("[Tokens] 写入令牌表失败: %s", e)


def generate_token(user_id: str, expire_hours: int = 24) -> str:
    """为用户生成 Web 访问令牌。返回 token 字符串。"""
    from config import WEB_TOKEN_EXPIRE_HOURS
    if expire_hours == 24:
        expire_hours = WEB_TOKEN_EXPIRE_HOURS

    token = str(uuid.uuid4())
    now = datetime.now(BEIJING_TZ)
    expire_at = now + timedelta(hours=expire_hours)

    with _tokens_lock:
        data = _read_tokens()
        data["tokens"][token] = {
            "user_id": user_id,
            "created_at": now.isoformat(timespec="seconds"),
            "expire_at": expire_at.isoformat(timespec="seconds"),
        }
        _write_tokens(data)

    logger.info("[Tokens] 生成令牌: user=%s, token=%s..., expire=%s",
                user_id, token[:8], expire_at.isoformat(timespec="seconds"))
    return token


def verify_token(token: str) -> dict:
    """验证令牌。返回 {"valid": True, "user_id": "xxx"} 或 {"valid": False}"""
    if not token:
        return {"valid": False}

    with _tokens_lock:
        data = _read_tokens()
        token_data = data.get("tokens", {}).get(token)

    if not token_data:
        logger.warning("[Tokens] 令牌不存在: %s...", token[:8])
        return {"valid": False}

    try:
        expire_at = datetime.fromisoformat(token_data["expire_at"])
        now = datetime.now(BEIJING_TZ)
        if now > expire_at:
            logger.warning("[Tokens] 令牌已过期: %s..., expire_at=%s",
                           token[:8], token_data["expire_at"])
            return {"valid": False, "expired": True}
    except (ValueError, KeyError):
        return {"valid": False}

    user_id = token_data.get("user_id", "")
    return {"valid": True, "user_id": user_id}


def cleanup_expired_tokens() -> int:
    """清理过期令牌，返回清理数量"""
    now = datetime.now(BEIJING_TZ)
    removed = 0

    with _tokens_lock:
        data = _read_tokens()
        tokens = data.get("tokens", {})
        to_remove = []

        for token, info in tokens.items():
            try:
                expire_at = datetime.fromisoformat(info["expire_at"])
                if now > expire_at:
                    to_remove.append(token)
            except (ValueError, KeyError):
                to_remove.append(token)

        for token in to_remove:
            del tokens[token]
            removed += 1

        if removed > 0:
            _write_tokens(data)

    if removed > 0:
        logger.info("[Tokens] 清理过期令牌: %s 个", removed)
    return removed
