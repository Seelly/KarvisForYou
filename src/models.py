# -*- coding: utf-8 -*-
"""
核心数据结构类型定义。

所有渠道和模块共享的统一消息格式、payload 结构等。
使用 TypedDict 提供结构化类型标注，方便 IDE 自动补全和 mypy 检查。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple, TypedDict, Union


# ============================================================
# RawMessage — 渠道解析后的统一消息格式
# ============================================================

class RawMessage(TypedDict, total=False):
    """
    各渠道 parse_message() 返回的统一消息 dict。

    必选字段：
        msg_type: 消息类型，可选值 "text" | "image" | "voice" | "video" | "link" | "file" | "event"
        from_user: 发送者用户 ID（含渠道前缀，如 "tg_123"、"fs_xxx"、"ww_xxx"）
    可选字段：
        msg_id: 消息 ID（用于去重）
        content: 文本内容 或 图片/视频的 caption
        media_id: 媒体文件 ID（图片、语音、视频、文件）
        file_name: 文件名（file 类型消息）
        format: 语音格式（如 "amr"、"ogg"、"opus"）
        title: 链接标题（link 类型消息）
        url: 链接 URL（link 类型消息）
        description: 链接描述（link 类型消息）
    渠道私有字段（以 _<prefix>_ 开头）：
        _tg_sender_name: Telegram 发送者姓名
        _tg_chat_id: Telegram chat ID
        _fs_open_id: 飞书 open_id
    """
    msg_type: str
    from_user: str
    msg_id: str
    content: str
    media_id: str
    file_name: str
    format: str
    title: str
    url: str
    description: str
    # 渠道私有字段
    _tg_sender_name: str
    _tg_chat_id: str
    _fs_open_id: str


# ============================================================
# Payload — brain.process() 的输入格式
# ============================================================

class Payload(TypedDict, total=False):
    """
    build_payload() 构造的结构化消息，传给 brain.process()。

    必选字段：
        user_id: 用户 ID（含渠道前缀）
        type: 消息类型，可选值 "text" | "image" | "voice" | "video" | "link" | "file" | "system"
    文本消息：
        text: 文本内容
        page_content: URL 抓取到的网页正文（F1）
        detected_url: 检测到的 URL
    媒体消息：
        attachment: 上传后的附件路径
        image_base64: 图片 base64 编码（用于千问 VL）
        image_description: VL 图像理解描述
    链接消息：
        title: 链接标题
        url: 链接 URL
        description: 链接描述
        content: 链接抓取的网页正文
    系统消息：
        action: 系统动作名称（如 "morning_report"、"evening_checkin"）
        context: 系统动作的上下文数据
    """
    user_id: str
    type: str
    text: str
    page_content: str
    detected_url: str
    attachment: str
    image_base64: str
    image_description: str
    title: str
    url: str
    description: str
    content: str
    action: str
    context: Dict[str, Any]


# ============================================================
# 通用类型别名
# ============================================================

# 媒体下载结果：(文件字节数据, Content-Type) 或 (None, None)
MediaResult = Tuple[Optional[bytes], Optional[str]]

# 消息解析结果：(消息 dict, 用户 ID) 或 (None, None)
ParseResult = Tuple[Optional[RawMessage], Optional[str]]

# build_payload 结果：(payload dict, 快速回复) — 二者互斥
BuildPayloadResult = Tuple[Optional[Payload], Optional[str]]
