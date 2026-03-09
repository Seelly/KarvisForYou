# -*- coding: utf-8 -*-
"""
渠道管理器 — 管理 IMChannel 实例，根据用户 ID 自动路由。

设计：
- 维护 {channel_name: IMChannel} 注册表
- send_message(user_id, text) 通过用户 ID 前缀自动路由到对应渠道
- send_alert(text) 遍历所有已注册渠道的管理员 ID 推送告警
- get_channel(user_id) 返回渠道实例，供下载媒体等场景使用
- 兼容老用户（无前缀企微用户通过 user_config.json 判断）
"""
from __future__ import annotations

import json
import os
from typing import Optional

from log_utils import get_logger

logger = get_logger(__name__)

# 延迟导入，避免循环依赖
# from channel.base import IMChannel  # 仅用于类型标注

# ============ 渠道注册表 ============
_channels: dict = {}  # {"wework": IMChannel, "telegram": IMChannel, ...}


def register_channel(channel) -> None:
    """注册一个 IMChannel 渠道实例"""
    _channels[channel.channel_name] = channel
    logger.info("[路由器] 渠道已注册: %s (前缀=%s)", channel.channel_name, channel.user_id_prefix)


def get_registered_channels() -> dict:
    """返回所有已注册渠道的字典 {name: IMChannel}"""
    return dict(_channels)


# ============ 用户渠道缓存 ============
_user_channel_cache: dict[str, str] = {}


def get_user_channel(user_id: str) -> Optional[str]:
    """
    获取用户所属渠道名称（带内存缓存）。

    通过 user_id 前缀匹配已注册渠道，匹配不到则查 user_config.json 兼容老用户。
    返回渠道名称（如 "wework"、"telegram"、"feishu"）或 None。
    """
    if user_id in _user_channel_cache:
        return _user_channel_cache[user_id]

    # 通过前缀匹配已注册渠道
    for ch in _channels.values():
        if user_id.startswith(ch.user_id_prefix):
            _user_channel_cache[user_id] = ch.channel_name
            return ch.channel_name

    # 兼容层：无前缀用户（老企微用户），读取 user_config.json 中的 channel 字段
    try:
        from user_context import DATA_DIR
        config_file = os.path.join(DATA_DIR, "users", user_id, "_Karvis", "user_config.json")
        if os.path.exists(config_file):
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)
            channel = config.get("channel")
            if channel:
                _user_channel_cache[user_id] = channel
                return channel
    except Exception as e:
        logger.debug("读取 user_config.json 失败: %s", e)
        pass

    # 无法确定渠道，返回 None（不再默认兜底企微）
    logger.warning("[路由器] 无法确定 user_id=%s 的渠道", user_id)
    return None


def get_channel(user_id: str):
    """
    根据 user_id 获取对应的 IMChannel 实例。

    返回 IMChannel 实例或 None。
    """
    channel_name = get_user_channel(user_id)
    if channel_name:
        return _channels.get(channel_name)
    return None


def get_channel_by_name(name: str):
    """根据渠道名获取 IMChannel 实例"""
    return _channels.get(name)


def set_user_channel(user_id: str, channel: str) -> None:
    """设置用户渠道（缓存 + 写入 config 由调用方负责）"""
    _user_channel_cache[user_id] = channel


def clear_user_channel_cache(user_id: str | None = None) -> None:
    """清除渠道缓存"""
    if user_id:
        _user_channel_cache.pop(user_id, None)
    else:
        _user_channel_cache.clear()


# ============ 统一发送 ============

def send_message(user_id: str, text: str) -> bool:
    """统一发送入口 — 根据用户 ID 自动路由到对应渠道"""
    ch = get_channel(user_id)
    if ch:
        return ch.send_message(user_id, text)
    logger.warning("[路由器] 未找到渠道 for user %s, 已注册渠道: %s",
                   user_id, list(_channels.keys()))
    return False


def send_alert(text: str) -> list:
    """
    管理员告警 — 遍历所有已注册渠道，推送到各渠道管理员。

    每个渠道通过自身的 get_admin_user_id() 获取管理员 ID，
    新增渠道无需修改此函数。
    """
    results = []
    for name, ch in _channels.items():
        try:
            admin_uid = ch.get_admin_user_id()
            if admin_uid:
                ok = ch.send_message(admin_uid, text)
                results.append((name, ok))
        except Exception as e:
            logger.error("[路由器] %s 告警发送失败: %s", name, e)
            results.append((name, False))
    return results