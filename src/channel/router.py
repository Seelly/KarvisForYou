# -*- coding: utf-8 -*-
"""
渠道路由器 — 管理 IMChannel 实例，根据用户 ID 自动路由。

设计：
- ChannelRouter 类管理 {channel_name: IMChannel} 注册表
- send_message(user_id, text) 通过用户 ID 前缀自动路由到对应渠道
- send_alert(text) 遍历所有已注册渠道的管理员 ID 推送告警
- get_channel(user_id) 返回渠道实例，供下载媒体等场景使用
- 兼容老用户（无前缀企微用户通过 user_config.json 判断）
"""
from __future__ import annotations

import json
import os
from typing import Optional

from channel.base import IMChannel
from infra.logging import get_logger
from infra.paths import DATA_DIR

logger = get_logger(__name__)


class ChannelRouter:
    """渠道路由器 — 统一管理 IM 渠道实例，根据 user_id 自动路由消息。"""

    def __init__(self):
        self._channels: dict[str, IMChannel] = {}
        self._user_channel_cache: dict[str, str] = {}

    # ============ 渠道注册 ============

    def register(self, channel: IMChannel) -> None:
        """注册一个 IMChannel 渠道实例"""
        self._channels[channel.channel_name] = channel
        logger.info("[路由器] 渠道已注册: %s (前缀=%s)", channel.channel_name, channel.user_id_prefix)

    def get_registered_channels(self) -> dict[str, IMChannel]:
        """返回所有已注册渠道的字典 {name: IMChannel}"""
        return dict(self._channels)

    # ============ 用户渠道查询 ============

    def get_user_channel(self, user_id: str) -> Optional[str]:
        """
        获取用户所属渠道名称（带内存缓存）。

        通过 user_id 前缀匹配已注册渠道，匹配不到则查 user_config.json 兼容老用户。
        返回渠道名称（如 "wework"、"telegram"、"feishu"）或 None。
        """
        if user_id in self._user_channel_cache:
            return self._user_channel_cache[user_id]

        # 通过前缀匹配已注册渠道
        for ch in self._channels.values():
            if user_id.startswith(ch.user_id_prefix):
                self._user_channel_cache[user_id] = ch.channel_name
                return ch.channel_name

        # 兼容层：无前缀用户（老企微用户），读取 user_config.json 中的 channel 字段
        try:
            config_file = os.path.join(DATA_DIR, "users", user_id, "_Karvis", "user_config.json")
            if os.path.exists(config_file):
                with open(config_file, "r", encoding="utf-8") as f:
                    config = json.load(f)
                channel = config.get("channel")
                if channel:
                    self._user_channel_cache[user_id] = channel
                    return channel
        except Exception as e:
            logger.debug("读取 user_config.json 失败: %s", e)

        logger.warning("[路由器] 无法确定 user_id=%s 的渠道", user_id)
        return None

    def get_channel(self, user_id: str) -> Optional[IMChannel]:
        """根据 user_id 获取对应的 IMChannel 实例。"""
        channel_name = self.get_user_channel(user_id)
        if channel_name:
            return self._channels.get(channel_name)
        return None

    def get_channel_by_name(self, name: str) -> Optional[IMChannel]:
        """根据渠道名获取 IMChannel 实例"""
        return self._channels.get(name)

    def set_user_channel(self, user_id: str, channel: str) -> None:
        """设置用户渠道（缓存 + 写入 config 由调用方负责）"""
        self._user_channel_cache[user_id] = channel

    def clear_user_channel_cache(self, user_id: str | None = None) -> None:
        """清除渠道缓存"""
        if user_id:
            self._user_channel_cache.pop(user_id, None)
        else:
            self._user_channel_cache.clear()

    # ============ 统一发送 ============

    def send_message(self, user_id: str, text: str) -> bool:
        """统一发送入口 — 根据用户 ID 自动路由到对应渠道"""
        ch = self.get_channel(user_id)
        if ch:
            return ch.send_message(user_id, text)
        logger.warning("[路由器] 未找到渠道 for user %s, 已注册渠道: %s",
                       user_id, list(self._channels.keys()))
        return False

    def send_alert(self, text: str) -> list:
        """
        管理员告警 — 遍历所有已注册渠道，推送到各渠道管理员。

        每个渠道通过自身的 get_admin_user_id() 获取管理员 ID，
        新增渠道无需修改此函数。
        """
        results = []
        for name, ch in self._channels.items():
            try:
                admin_uid = ch.get_admin_user_id()
                if admin_uid:
                    ok = ch.send_message(admin_uid, text)
                    results.append((name, ok))
            except Exception as e:
                logger.error("[路由器] %s 告警发送失败: %s", name, e)
                results.append((name, False))
        return results
