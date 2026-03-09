# -*- coding: utf-8 -*-
"""
IM 渠道抽象层。

所有 IM 渠道（企微、Telegram、飞书等）的统一接口定义。
"""
from channel.base import IMChannel

__all__ = ["IMChannel"]
