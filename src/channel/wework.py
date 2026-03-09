# -*- coding: utf-8 -*-
"""
企业微信渠道实现。

职责：
1. 管理企微 access_token
2. 发送文本消息
3. 解析企微 XML 消息
4. 下载临时素材（媒体文件）
5. 注册 Flask Webhook 路由（GET 验证 + POST 消息接收）
"""
from __future__ import annotations

import json
import os
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable

import requests

from channel.base import IMChannel
from config import (
    CORP_ID, CORP_SECRET, AGENT_ID,
    WEWORK_TOKEN, ENCODING_AES_KEY,
    ADMIN_USER_ID,
)
from log_utils import get_logger
from models import MediaResult, ParseResult
from wework_crypto import WXBizMsgCrypt

logger = get_logger(__name__)


class WeWorkChannel(IMChannel):
    """企业微信渠道"""

    def __init__(self) -> None:
        self._corp_id = CORP_ID
        self._corp_secret = CORP_SECRET
        self._agent_id = AGENT_ID
        self._admin_user_id = ADMIN_USER_ID
        # 企微消息加解密器
        self._crypto = WXBizMsgCrypt(WEWORK_TOKEN, ENCODING_AES_KEY, CORP_ID)
        # access_token 缓存
        self._token_cache: dict[str, Any] = {"token": None, "expire_time": 0}
        # 消息处理回调（由 start() 注入）
        self._message_handler: Callable | None = None

    # ---- 属性 ----

    @property
    def channel_name(self) -> str:
        return "wework"

    @property
    def user_id_prefix(self) -> str:
        return "ww_"

    # ---- access_token 管理 ----

    def _get_access_token(self) -> str | None:
        """获取企微 access_token（带缓存）"""
        now = time.time()
        if self._token_cache["token"] and self._token_cache["expire_time"] > now:
            return self._token_cache["token"]
        url = (
            f"https://qyapi.weixin.qq.com/cgi-bin/gettoken"
            f"?corpid={self._corp_id}&corpsecret={self._corp_secret}"
        )
        try:
            resp = requests.get(url, timeout=10)
            result = resp.json()
            if result.get("errcode") == 0:
                self._token_cache["token"] = result["access_token"]
                self._token_cache["expire_time"] = now + result["expires_in"] - 200
                return result["access_token"]
            logger.error("[企微] token 获取失败: %s", result)
        except Exception as e:
            logger.error("[企微] token 获取异常: %s", e)
        return None

    # ---- IMChannel 接口实现 ----

    def send_message(self, user_id: str, text: str) -> bool:
        """发送企业微信文本消息"""
        token = self._get_access_token()
        if not token:
            return False
        # 去除 ww_ 前缀，得到企微原始 user_id
        raw_id = self.strip_prefix(user_id)
        url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
        data = {
            "touser": raw_id,
            "msgtype": "text",
            "agentid": self._agent_id,
            "text": {"content": text}
        }
        try:
            resp = requests.post(url, json=data, timeout=10)
            result = resp.json()
            ok = result.get("errcode") == 0
            if not ok:
                logger.error("[企微回复] 发送失败: %s", result)
            return ok
        except Exception as e:
            logger.error("[企微回复] 发送异常: %s", e)
            return False

    def parse_message(self, raw_data: Any) -> ParseResult:
        """解析企微 XML 消息为统一格式"""
        xml_data: str = raw_data
        root = ET.fromstring(xml_data)
        msg_type_node = root.find("MsgType")
        from_user_node = root.find("FromUserName")
        if msg_type_node is None or from_user_node is None:
            return None, None

        msg_type = msg_type_node.text
        raw_from_user = from_user_node.text or ""
        user_id = f"ww_{raw_from_user}"

        result: dict[str, Any] = {"msg_type": msg_type, "from_user": user_id}

        msg_id_node = root.find("MsgId")
        if msg_id_node is not None:
            result["msg_id"] = msg_id_node.text

        if msg_type == "text":
            content_node = root.find("Content")
            result["content"] = content_node.text if content_node is not None else ""
        elif msg_type == "image":
            media_id_node = root.find("MediaId")
            if media_id_node is not None:
                result["media_id"] = media_id_node.text
        elif msg_type == "voice":
            media_id_node = root.find("MediaId")
            fmt_node = root.find("Format")
            if media_id_node is not None:
                result["media_id"] = media_id_node.text
            if fmt_node is not None:
                result["format"] = fmt_node.text
        elif msg_type == "video":
            media_id_node = root.find("MediaId")
            if media_id_node is not None:
                result["media_id"] = media_id_node.text
        elif msg_type == "link":
            for tag in ("Title", "Description", "Url"):
                node = root.find(tag)
                if node is not None:
                    result[tag.lower()] = node.text

        return result, user_id

    def download_media(self, media_id: str) -> MediaResult:
        """从企微下载临时素材"""
        token = self._get_access_token()
        if not token:
            return None, None
        url = (
            f"https://qyapi.weixin.qq.com/cgi-bin/media/get"
            f"?access_token={token}&media_id={media_id}"
        )
        try:
            resp = requests.get(url, timeout=30)
            content_type = resp.headers.get("Content-Type", "")
            if "application/json" in content_type or "text/plain" in content_type:
                logger.error("[企微素材] 下载失败: %s", resp.text[:200])
                return None, None
            logger.info("[企微素材] 下载成功: size=%s, type=%s", len(resp.content), content_type)
            return resp.content, content_type
        except Exception as e:
            logger.error("[企微素材] 下载异常: %s", e)
            return None, None

    def get_admin_user_id(self) -> str | None:
        """返回企微管理员用户 ID（含 ww_ 前缀）"""
        if self._admin_user_id:
            return f"ww_{self._admin_user_id}"
        return None

    def start(self, app: Any | None, message_handler: Callable) -> None:
        """注册企微 Webhook 路由到 Flask app"""
        if app is None:
            logger.warning("[企微] Flask app 为空，无法注册路由")
            return

        self._message_handler = message_handler
        process_endpoint_url = os.environ.get(
            "PROCESS_ENDPOINT_URL", f"http://127.0.0.1:{app.config.get('SERVER_PORT', 9000)}/process"
        )

        channel = self  # 闭包引用

        from flask import Blueprint, request as flask_request
        wework_bp = Blueprint("wework", __name__)

        @wework_bp.route("/wework", methods=["GET", "POST"])
        def wework_webhook():
            """企业微信入口"""
            if flask_request.method == "GET":
                msg_signature = flask_request.args.get("msg_signature", "")
                timestamp = flask_request.args.get("timestamp", "")
                nonce = flask_request.args.get("nonce", "")
                echostr = flask_request.args.get("echostr", "")
                reply = channel._crypto.verify_url(msg_signature, timestamp, nonce, echostr)
                return reply if reply else "verify failed"

            if flask_request.method == "POST":
                try:
                    xml_data = flask_request.data.decode("utf-8")
                    msg_signature = flask_request.args.get("msg_signature", "")
                    timestamp = flask_request.args.get("timestamp", "")
                    nonce = flask_request.args.get("nonce", "")

                    # 解密
                    root = ET.fromstring(xml_data)
                    encrypt_node = root.find("Encrypt")
                    if encrypt_node is not None:
                        decrypted_xml = channel._crypto.decrypt_msg(
                            msg_signature, timestamp, nonce, encrypt_node.text
                        )
                        if not decrypted_xml:
                            logger.error("[企微] 解密失败")
                            return "success"
                        msg, user_id = channel.parse_message(decrypted_xml)
                    else:
                        msg, user_id = channel.parse_message(xml_data)

                    if not msg or not user_id:
                        return "success"

                    msg_id = msg.get("msg_id", "")
                    logger.info("[企微] user=%s, type=%s, id=%s", user_id, msg["msg_type"], msg_id)

                    # 消息去重（通过 gateway 模块的 is_duplicate_msg）
                    from gateway import is_duplicate_msg
                    if msg_id and is_duplicate_msg(msg_id):
                        return "success"

                    # 异步处理：通过公网 URL 调用自己的 /process 端点
                    payload_data = json.dumps({
                        "msg": msg,
                        "user_id": user_id,
                    }, ensure_ascii=False)

                    def fire_and_forget():
                        try:
                            resp = requests.post(
                                process_endpoint_url,
                                data=payload_data.encode("utf-8"),
                                headers={"Content-Type": "application/json"},
                                timeout=300,
                            )
                            logger.info("[企微触发] /process 返回: %s", resp.status_code)
                        except Exception as e:
                            logger.error("[企微触发] /process 调用异常: %s", e)

                    t = threading.Thread(target=fire_and_forget)
                    t.start()
                    time.sleep(0.3)

                    logger.info("[企微] 已触发 /process，立即返回 success")
                    return "success"

                except Exception as e:
                    logger.exception("[企微] 错误: %s", e)
                    return "success"

            return "success"

        app.register_blueprint(wework_bp)
        logger.info("[企微] Webhook 路由已注册")
