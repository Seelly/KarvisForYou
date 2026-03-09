# -*- coding: utf-8 -*-
"""
IMChannel 抽象基类。

所有 IM 渠道必须实现此接口，以便网关和路由器统一调度。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable

from models import MediaResult, ParseResult


class IMChannel(ABC):
    """
    IM 渠道的统一接口。

    每个渠道实现类（如 WeWorkChannel、TelegramChannel、FeishuChannel）
    必须继承此类并实现所有抽象方法。

    属性:
        channel_name: 渠道标识名（如 "wework"、"telegram"、"feishu"）
        user_id_prefix: 用户 ID 前缀（如 "ww_"、"tg_"、"fs_"）
    """

    @property
    @abstractmethod
    def channel_name(self) -> str:
        """渠道标识名，用于配置和日志（如 "wework"、"telegram"、"feishu"）"""
        ...

    @property
    @abstractmethod
    def user_id_prefix(self) -> str:
        """用户 ID 前缀（如 "ww_"、"tg_"、"fs_"），所有渠道必须有前缀"""
        ...

    @abstractmethod
    def send_message(self, user_id: str, text: str) -> bool:
        """
        发送文本消息到指定用户。

        Args:
            user_id: 用户 ID（含渠道前缀）
            text: 消息文本

        Returns:
            是否发送成功
        """
        ...

    @abstractmethod
    def parse_message(self, raw_data: Any) -> ParseResult:
        """
        解析渠道原始消息为统一格式。

        Args:
            raw_data: 渠道原始数据（XML 字符串、JSON dict、lark 事件对象等）

        Returns:
            (RawMessage dict, user_id) 或 (None, None)
        """
        ...

    @abstractmethod
    def download_media(self, media_id: str) -> MediaResult:
        """
        下载媒体文件。

        Args:
            media_id: 媒体文件 ID（格式因渠道而异）

        Returns:
            (文件字节数据, Content-Type) 或 (None, None)
        """
        ...

    @abstractmethod
    def start(self, app: Any | None, message_handler: Callable) -> None:
        """
        启动渠道。

        对于 Webhook 类渠道（企微、Telegram）：注册 Flask 路由。
        对于长连接类渠道（飞书）：启动 WebSocket 连接。

        Args:
            app: Flask app 实例（长连接渠道可为 None）
            message_handler: 消息处理回调函数，签名为 (msg: dict, user_id: str) -> None
        """
        ...

    def get_admin_user_id(self) -> str | None:
        """
        获取该渠道的管理员用户 ID（含前缀）。

        用于告警推送等场景。默认返回 None（无管理员配置）。
        子类按需覆写。

        Returns:
            管理员 user_id（含前缀）或 None
        """
        return None

    def strip_prefix(self, user_id: str) -> str:
        """去除 user_id 的渠道前缀，返回原始 ID"""
        if user_id.startswith(self.user_id_prefix):
            return user_id[len(self.user_id_prefix):]
        return user_id

    # ---- 消息处理反馈接口（可选覆写） ----

    def on_message_received(self, msg: dict) -> Any:
        """
        收到消息后立即调用，用于展示"处理中"状态。

        典型用法：飞书添加 Typing 表情、Telegram 发送 typing 状态等。
        不支持此能力的渠道无需覆写，基类默认返回 None。

        Args:
            msg: 渠道解析后的统一消息 dict（包含 msg_id、from_user 等字段）

        Returns:
            反馈上下文对象（如飞书的 reaction_id），供 on_message_done 使用。
            基类默认返回 None。
        """
        return None

    def on_message_done(self, msg: dict, context: Any) -> None:
        """
        消息处理完成（成功或失败）后调用，用于清理"处理中"状态。

        典型用法：飞书移除 Typing 表情等。
        不支持此能力的渠道无需覆写，基类默认空操作。

        Args:
            msg: 渠道解析后的统一消息 dict（同 on_message_received）
            context: on_message_received 的返回值（如 reaction_id）
        """
        pass
