# -*- coding: utf-8 -*-
"""
IM 渠道抽象层。

所有 IM 渠道（企微、Telegram、飞书等）的统一接口定义。
"""
from channel.base import IMChannel
from channel.router import ChannelRouter

# 全局单例 — 所有模块通过此实例路由消息
router = ChannelRouter()

__all__ = ["IMChannel", "ChannelRouter", "router"]
