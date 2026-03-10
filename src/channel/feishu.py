# -*- coding: utf-8 -*-
"""
飞书渠道实现 — 长连接（WebSocket）模式。

使用 lark-oapi SDK 的长连接方式接收消息，无需公网回调地址。

职责：
1. 通过 Open API 发送消息
2. 解析 lark-oapi 事件为统一消息格式
3. 通过 Open API 下载媒体文件
4. 启动 WebSocket 长连接
"""
from __future__ import annotations

import json
import threading
from typing import Any, Callable

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest, CreateMessageRequestBody,
    CreateMessageReactionRequest, CreateMessageReactionRequestBody,
    DeleteMessageReactionRequest, Emoji,
    GetMessageResourceRequest,
)

from channel.base import IMChannel
from config import FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_ADMIN_OPEN_ID
from log_utils import get_logger
from models import MediaResult, ParseResult

logger = get_logger(__name__)


class FeishuChannel(IMChannel):
    """飞书渠道"""

    def __init__(self) -> None:
        self._app_id = FEISHU_APP_ID
        self._app_secret = FEISHU_APP_SECRET
        self._admin_open_id = FEISHU_ADMIN_OPEN_ID
        self._message_handler: Callable | None = None

        # lark SDK 客户端（懒加载）
        self._client: lark.Client | None = None
        self._client_lock = threading.Lock()

    # ---- 属性 ----

    @property
    def channel_name(self) -> str:
        return "feishu"

    @property
    def user_id_prefix(self) -> str:
        return "fs_"

    # ---- 内部方法 ----

    def _get_client(self) -> lark.Client:
        """获取 lark SDK 客户端（懒加载单例）"""
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is not None:
                return self._client
            self._client = lark.Client.builder() \
                .app_id(self._app_id) \
                .app_secret(self._app_secret) \
                .log_level(lark.LogLevel.WARNING) \
                .build()
            logger.info("[飞书] SDK Client 已初始化")
            return self._client

    # ---- IMChannel 接口实现 ----

    def send_message(self, user_id: str, text: str) -> bool:
        """发送文本消息到飞书"""
        open_id = self.strip_prefix(user_id)
        client = self._get_client()
        body = CreateMessageRequestBody.builder() \
            .receive_id(open_id) \
            .msg_type("text") \
            .content(json.dumps({"text": text})) \
            .build()
        request = CreateMessageRequest.builder() \
            .receive_id_type("open_id") \
            .request_body(body) \
            .build()
        try:
            response = client.im.v1.message.create(request)
            if response.success():
                return True
            else:
                logger.warning("[飞书] 发送失败: code=%s, msg=%s", response.code, response.msg)
                return False
        except Exception as e:
            logger.error("[飞书] 发送异常: %s", e)
            return False

    def parse_message(self, raw_data: Any) -> ParseResult:
        """解析 lark-oapi 长连接推送的 im.message.receive_v1 事件"""
        data = raw_data
        event = data.event
        if not event:
            return None, None

        sender = event.sender
        open_id = sender.sender_id.open_id if sender and sender.sender_id else ""
        if not open_id:
            return None, None

        user_id = f"fs_{open_id}"

        message = event.message
        if not message:
            return None, None

        msg_id = message.message_id or ""
        msg_type = message.message_type or ""
        content_str = message.content or "{}"

        try:
            content = json.loads(content_str)
        except (json.JSONDecodeError, TypeError):
            content = {}

        # 文本消息
        if msg_type == "text":
            text = content.get("text", "")
            return {
                "msg_type": "text",
                "content": text,
                "from_user": user_id,
                "msg_id": msg_id,
                "_fs_open_id": open_id,
            }, user_id

        # 图片消息
        if msg_type == "image":
            image_key = content.get("image_key", "")
            return {
                "msg_type": "image",
                "media_id": f"{msg_id}:{image_key}",
                "from_user": user_id,
                "msg_id": msg_id,
                "content": "",
                "_fs_open_id": open_id,
            }, user_id

        # 语音消息
        if msg_type == "audio":
            file_key = content.get("file_key", "")
            return {
                "msg_type": "voice",
                "media_id": f"{msg_id}:{file_key}",
                "format": "opus",
                "from_user": user_id,
                "msg_id": msg_id,
                "_fs_open_id": open_id,
            }, user_id

        # 文件消息
        if msg_type == "file":
            file_key = content.get("file_key", "")
            file_name = content.get("file_name", "")
            return {
                "msg_type": "file",
                "media_id": f"{msg_id}:{file_key}",
                "file_name": file_name,
                "from_user": user_id,
                "msg_id": msg_id,
                "_fs_open_id": open_id,
            }, user_id

        logger.info("[飞书] 不支持的消息类型: %s", msg_type)
        return None, None

    def download_media(self, media_id: str) -> MediaResult:
        """通过飞书 Open API 下载媒体文件"""
        try:
            parts = media_id.split(":", 1)
            if len(parts) != 2:
                logger.error("[飞书] media_id 格式错误: %s", media_id)
                return None, None

            message_id, file_key = parts
            client = self._get_client()

            request = GetMessageResourceRequest.builder() \
                .message_id(message_id) \
                .file_key(file_key) \
                .type("image") \
                .build()

            response = client.im.v1.message_resource.get(request)
            if response.success():
                data = response.file.read()
                # 获取 Content-Type: 优先从 raw response headers 获取，兼容不同 SDK 版本
                ct = "application/octet-stream"
                try:
                    if hasattr(response, 'raw') and hasattr(response.raw, 'headers'):
                        ct = response.raw.headers.get("Content-Type", ct)
                    elif hasattr(response, 'header') and isinstance(response.header, dict):
                        ct = response.header.get("Content-Type", ct)
                except Exception:
                    pass
                # 如果仍是默认值，尝试根据 file_key 推断
                if ct == "application/octet-stream":
                    _ext_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                                ".gif": "image/gif", ".mp3": "audio/mpeg", ".amr": "audio/amr",
                                ".mp4": "video/mp4", ".pdf": "application/pdf"}
                    for ext, mime in _ext_map.items():
                        if file_key.lower().endswith(ext):
                            ct = mime
                            break
                logger.info("[飞书] 媒体下载成功: %d bytes, type=%s", len(data), ct)
                return data, ct
            else:
                logger.error("[飞书] 媒体下载失败: code=%s, msg=%s", response.code, response.msg)
                return None, None
        except Exception as e:
            logger.error("[飞书] 媒体下载异常: %s", e)
            return None, None

    # ---- 消息处理反馈 ----

    def on_message_received(self, msg: dict) -> Any:
        """收到消息后添加 Typing 表情，返回 reaction_id"""
        msg_id = msg.get("msg_id")
        if not msg_id:
            return None
        try:
            client = self._get_client()
            request = (
                CreateMessageReactionRequest.builder()
                .message_id(msg_id)
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(
                        Emoji.builder().emoji_type("Typing").build()
                    )
                    .build()
                )
                .build()
            )
            response = client.im.v1.message_reaction.create(request)
            if response.success() and response.data:
                reaction_id = response.data.reaction_id
                logger.debug("[飞书] Typing 表情已添加: msg_id=%s, reaction_id=%s", msg_id, reaction_id)
                return reaction_id
            else:
                logger.warning("[飞书] Typing 表情添加失败（可能缺少权限）: code=%s, msg=%s", response.code, response.msg)
                return None
        except Exception as e:
            logger.warning("[飞书] Typing 表情添加异常: %s", e)
            return None

    def on_message_done(self, msg: dict, context: Any) -> None:
        """消息处理完成后移除 Typing 表情"""
        if context is None:
            return
        msg_id = msg.get("msg_id")
        if not msg_id:
            return
        try:
            client = self._get_client()
            request = (
                DeleteMessageReactionRequest.builder()
                .message_id(msg_id)
                .reaction_id(context)
                .build()
            )
            response = client.im.v1.message_reaction.delete(request)
            if response.success():
                logger.debug("[飞书] Typing 表情已移除: msg_id=%s", msg_id)
            else:
                logger.warning("[飞书] Typing 表情移除失败: code=%s, msg=%s", response.code, response.msg)
        except Exception as e:
            logger.warning("[飞书] Typing 表情移除异常: %s", e)

    def get_admin_user_id(self) -> str | None:
        """返回飞书管理员用户 ID（含 fs_ 前缀）"""
        if self._admin_open_id:
            return f"fs_{self._admin_open_id}"
        return None

    def start(self, app: Any | None, message_handler: Callable) -> None:
        """启动飞书 WebSocket 长连接"""
        self._message_handler = message_handler
        channel = self

        def _on_im_message_receive(data):
            """长连接收到 im.message.receive_v1 事件的回调"""
            msg, user_id = channel.parse_message(data)
            if not msg or not user_id:
                return

            # 去重
            from gateway import is_duplicate_msg
            msg_key = f"fs_{msg.get('msg_id', '')}"
            if is_duplicate_msg(msg_key):
                return

            logger.info("[飞书] 收到消息: user=%s, type=%s", user_id, msg.get("msg_type"))

            # 异步处理，避免阻塞 SDK 事件循环
            def _process():
                try:
                    channel._message_handler(msg, user_id)
                except Exception as e:
                    logger.exception("[飞书] 处理消息异常")

            threading.Thread(target=_process, daemon=True).start()

        event_handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(_on_im_message_receive) \
            .build()

        ws_client = lark.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.WARNING,
        )

        logger.info("[飞书] WebSocket 长连接启动中...")

        # ws_client.start() 是阻塞的，在独立线程中运行
        ws_thread = threading.Thread(target=ws_client.start, daemon=True)
        ws_thread.start()
        logger.info("[飞书] WebSocket 长连接线程已启动")
