# -*- coding: utf-8 -*-
"""
State 缓存读写模块。

提供按 user_id 分区的 State 三级缓存（内存 → /tmp → 存储后端回源）。
"""
import os
import copy
import time
import threading
import json as _json

from config import STATE_CACHE_TTL
from infra.logging import get_logger

logger = get_logger(__name__)

_TMP_CACHE_DIR = "/tmp/karvis_prompts"

# {user_id: {"data": dict, "expire_time": float}}
_state_cache = {}
_state_lock = threading.Lock()


def read_state_cached(ctx):
    """读取某个用户的 state（带缓存）。"""
    uid = ctx.user_id
    now = time.time()

    # 1. 内存缓存
    with _state_lock:
        cached = _state_cache.get(uid)
        if cached and cached["data"] is not None and cached["expire_time"] > now:
            logger.debug("[State] 命中内存缓存 (%s)", uid)
            return copy.deepcopy(cached["data"])

    # 2. /tmp 磁盘缓存
    tmp_file = os.path.join(_TMP_CACHE_DIR, f"_state_{uid}.json")
    try:
        if os.path.exists(tmp_file):
            mtime = os.path.getmtime(tmp_file)
            if now - mtime < STATE_CACHE_TTL:
                with open(tmp_file, "r", encoding="utf-8") as f:
                    data = _json.load(f)
                with _state_lock:
                    _state_cache[uid] = {"data": data, "expire_time": now + STATE_CACHE_TTL}
                logger.debug("[State] 命中 /tmp 缓存 (%s)", uid)
                return copy.deepcopy(data)
    except Exception as e:
        logger.debug("读取 /tmp state 缓存失败: %s", e)
        pass

    # 3. 通过 IO 回源读取
    data = ctx.IO.read_json(ctx.state_file) or {}
    _update_state_cache(uid, data)
    logger.debug("[State] 从文件读取 (%s)", uid)
    return copy.deepcopy(data)


def _update_state_cache(uid, state):
    """更新某用户 state 的内存和 /tmp 缓存"""
    now = time.time()
    with _state_lock:
        _state_cache[uid] = {"data": state, "expire_time": now + STATE_CACHE_TTL}
    try:
        tmp_file = os.path.join(_TMP_CACHE_DIR, f"_state_{uid}.json")
        os.makedirs(_TMP_CACHE_DIR, exist_ok=True)
        with open(tmp_file, "w", encoding="utf-8") as f:
            _json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        logger.debug("写入 /tmp state 缓存失败: %s", e)
        pass


def write_state_and_update_cache(state, ctx):
    """写入某用户的 state 并更新缓存"""
    ctx.IO.write_json(ctx.state_file, state)
    _update_state_cache(ctx.user_id, state)


def invalidate_state_cache():
    """清除所有 state 缓存"""
    with _state_lock:
        _state_cache.clear()
    logger.info("[State] 所有 state 缓存已清除")
