# -*- coding: utf-8 -*-
"""
Telegram 渠道实现。

职责：
1. 发送文本消息到 Telegram
2. 解析 Telegram Update 为统一消息格式
3. 下载 Telegram 媒体文件
4. 注册 Flask Webhook 路由
5. 管理 Webhook 注册
"""
from __future__ import annotations

import threading
from typing import Any, Callable

import requests

from channel.base import IMChannel
from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_API_BASE,
    TELEGRAM_ADMIN_CHAT_ID, TELEGRAM_WEBHOOK_SECRET,
)
from log_utils import get_logger
from models import MediaResult, ParseResult

logger = get_logger(__name__)


class TelegramChannel(IMChannel):
    """Telegram 渠道"""

    def __init__(self) -> None:
        self._bot_token = TELEGRAM_BOT_TOKEN
        self._api_base = TELEGRAM_API_BASE
        self._admin_chat_id = TELEGRAM_ADMIN_CHAT_ID
        self._webhook_secret = TELEGRAM_WEBHOOK_SECRET
        self._message_handler: Callable | None = None

    # ---- 属性 ----

    @property
    def channel_name(self) -> str:
        return "telegram"

    @property
    def user_id_prefix(self) -> str:
        return "tg_"

    # ---- 内部方法 ----

    def _get_bot_api(self) -> str:
        return f"{self._api_base}/bot{self._bot_token}"

    def _get_file_api(self) -> str:
        return f"{self._api_base}/file/bot{self._bot_token}"

    # ---- IMChannel 接口实现 ----

    def send_message(self, user_id: str, text: str) -> bool:
        """发送文本消息到 Telegram"""
        chat_id = self.strip_prefix(user_id)
        url = f"{self._get_bot_api()}/sendMessage"
        data = {"chat_id": chat_id, "text": text}
        try:
            resp = requests.post(url, json=data, timeout=15)
            result = resp.json()
            ok = result.get("ok", False)
            if not ok:
                logger.warning("[TG] 发送失败: %s", result.get("description", result))
            return ok
        except Exception as e:
            logger.error("[TG] 发送异常: %s", e)
            return False

    def parse_message(self, raw_data: Any) -> ParseResult:
        """解析 Telegram Update 为统一消息格式"""
        update: dict = raw_data
        message = update.get("message") or update.get("edited_message")
        if not message:
            return None, None

        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))
        if not chat_id:
            return None, None

        user_id = f"tg_{chat_id}"
        msg_id = str(message.get("message_id", ""))

        # 提取发送者信息（用于新用户注册时获取名字）
        from_user = message.get("from", {})
        sender_name = from_user.get("first_name", "")
        if from_user.get("last_name"):
            sender_name += " " + from_user["last_name"]

        # /start 命令 — 当作普通文本处理（触发注册/欢迎）
        if "text" in message and message["text"].strip().startswith("/start"):
            return {
                "msg_type": "text",
                "content": "你好",
                "from_user": user_id,
                "msg_id": msg_id,
                "_tg_sender_name": sender_name,
                "_tg_chat_id": chat_id,
            }, user_id

        # 文本消息
        if "text" in message:
            return {
                "msg_type": "text",
                "content": message["text"],
                "from_user": user_id,
                "msg_id": msg_id,
                "_tg_sender_name": sender_name,
                "_tg_chat_id": chat_id,
            }, user_id

        # 图片（取最大尺寸）
        if "photo" in message:
            photos = message["photo"]
            photo = photos[-1]
            return {
                "msg_type": "image",
                "media_id": photo["file_id"],
                "from_user": user_id,
                "msg_id": msg_id,
                "content": message.get("caption", ""),
                "_tg_sender_name": sender_name,
                "_tg_chat_id": chat_id,
            }, user_id

        # 语音
        if "voice" in message:
            return {
                "msg_type": "voice",
                "media_id": message["voice"]["file_id"],
                "format": "ogg",
                "from_user": user_id,
                "msg_id": msg_id,
                "_tg_sender_name": sender_name,
                "_tg_chat_id": chat_id,
            }, user_id

        # 视频
        if "video" in message:
            return {
                "msg_type": "video",
                "media_id": message["video"]["file_id"],
                "from_user": user_id,
                "msg_id": msg_id,
                "content": message.get("caption", ""),
                "_tg_sender_name": sender_name,
                "_tg_chat_id": chat_id,
            }, user_id

        # 文档
        if "document" in message:
            doc = message["document"]
            return {
                "msg_type": "file",
                "media_id": doc["file_id"],
                "file_name": doc.get("file_name", ""),
                "from_user": user_id,
                "msg_id": msg_id,
                "_tg_sender_name": sender_name,
                "_tg_chat_id": chat_id,
            }, user_id

        # 贴纸 — 当作图片处理
        if "sticker" in message:
            sticker = message["sticker"]
            if sticker.get("is_animated") or sticker.get("is_video"):
                return None, None
            return {
                "msg_type": "image",
                "media_id": sticker.get("file_id", ""),
                "from_user": user_id,
                "msg_id": msg_id,
                "content": sticker.get("emoji", ""),
                "_tg_sender_name": sender_name,
                "_tg_chat_id": chat_id,
            }, user_id

        logger.info("[TG] 不支持的消息类型: %s", list(message.keys()))
        return None, None

    def download_media(self, media_id: str) -> MediaResult:
        """通过 Telegram Bot API 下载媒体文件"""
        try:
            bot_api = self._get_bot_api()
            # 1. 获取 file_path
            resp = requests.get(f"{bot_api}/getFile", params={"file_id": media_id}, timeout=10)
            result = resp.json()
            if not result.get("ok"):
                logger.error("[TG] getFile 失败: %s", result)
                return None, None
            file_path = result["result"]["file_path"]

            # 2. 下载文件
            download_url = f"{self._get_file_api()}/{file_path}"
            resp = requests.get(download_url, timeout=30)
            if resp.status_code == 200:
                ct = resp.headers.get("Content-Type", "")
                logger.info("[TG] 媒体下载成功: %d bytes, type=%s", len(resp.content), ct)
                return resp.content, ct
            else:
                logger.error("[TG] 媒体下载失败: HTTP %s", resp.status_code)
                return None, None
        except Exception as e:
            logger.error("[TG] 媒体下载异常: %s", e)
            return None, None

    # ---- 消息处理反馈 ----

    def on_message_received(self, msg: dict) -> Any:
        """收到消息后发送 typing 状态，让用户知道 Bot 正在处理"""
        from_user = msg.get("from_user", "")
        if not from_user:
            return None
        chat_id = self.strip_prefix(from_user)
        try:
            url = f"{self._get_bot_api()}/sendChatAction"
            resp = requests.post(url, json={"chat_id": chat_id, "action": "typing"}, timeout=5)
            result = resp.json()
            if not result.get("ok"):
                logger.warning("[TG] sendChatAction 失败: %s", result.get("description", result))
        except Exception as e:
            logger.warning("[TG] sendChatAction 异常: %s", e)
        return None

    def get_admin_user_id(self) -> str | None:
        """返回 Telegram 管理员用户 ID（含 tg_ 前缀）"""
        if self._admin_chat_id:
            return f"tg_{self._admin_chat_id}"
        return None

    def start(self, app: Any | None, message_handler: Callable) -> None:
        """注册 Telegram Webhook 路由到 Flask app"""
        if app is None:
            logger.warning("[TG] Flask app 为空，无法注册路由")
            return

        self._message_handler = message_handler
        channel = self

        from flask import Blueprint, request as flask_request, jsonify
        tg_bp = Blueprint("telegram", __name__)

        @tg_bp.route("/telegram", methods=["POST"])
        def telegram_webhook():
            """接收 Telegram Update"""
            # 可选：验证 Webhook secret
            if channel._webhook_secret:
                header_secret = flask_request.headers.get(
                    "X-Telegram-Bot-Api-Secret-Token", ""
                )
                if header_secret != channel._webhook_secret:
                    logger.warning("[TG] Webhook secret 验证失败")
                    return jsonify({"ok": False}), 403

            update = flask_request.get_json(silent=True)
            if not update:
                return jsonify({"ok": True})

            msg, user_id = channel.parse_message(update)
            if not msg or not user_id:
                return jsonify({"ok": True})

            # 去重
            from gateway import is_duplicate_msg
            msg_key = f"tg_{msg.get('msg_id', '')}"
            if is_duplicate_msg(msg_key):
                return jsonify({"ok": True})

            # 异步处理（快速响应 Telegram，必须 <10s）
            def _process():
                try:
                    channel._message_handler(msg, user_id)
                except Exception as e:
                    logger.exception("[TG] 处理消息异常")

            threading.Thread(target=_process, daemon=True).start()
            return jsonify({"ok": True})

        app.register_blueprint(tg_bp)
        logger.info("[TG] Webhook 路由已注册")

    # ---- Webhook 管理 ----

    def setup_webhook(self, base_url: str) -> bool:
        """向 Telegram 注册 Webhook URL（服务启动时调用）"""
        webhook_url = f"{base_url}/telegram"
        url = f"{self._get_bot_api()}/setWebhook"
        data = {
            "url": webhook_url,
            "allowed_updates": ["message"],
            "drop_pending_updates": True,
        }
        if self._webhook_secret:
            data["secret_token"] = self._webhook_secret
        try:
            resp = requests.post(url, json=data, timeout=10)
            result = resp.json()
            ok = result.get("ok", False)
            logger.info("[TG] setWebhook %s: %s", "成功" if ok else "失败",
                        result.get("description", ""))
            return ok
        except Exception as e:
            logger.error("[TG] setWebhook 异常: %s", e)
            return False

    def get_webhook_info(self) -> dict:
        """查询当前 Webhook 状态"""
        try:
            resp = requests.get(f"{self._get_bot_api()}/getWebhookInfo", timeout=10)
            return resp.json().get("result", {})
        except Exception as e:
            logger.warning("获取 Webhook 状态失败: %s", e)
            return {}
