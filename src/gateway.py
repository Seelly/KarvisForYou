# -*- coding: utf-8 -*-
"""
消息网关模块。

职责：
1. 消息去重
2. 消息 → Payload 转换（build_payload）
3. 主处理流程（handle_message）
4. 附件上传
"""
from __future__ import annotations

import base64
import os
import time
from datetime import datetime

import brain
import channel_router
from config import MSG_CACHE_EXPIRE_SECONDS
from log_utils import get_logger, BEIJING_TZ
from media import extract_url, fetch_link_content, recognize_voice
from user_context import (
    get_or_create_user, get_all_active_users,
    increment_message_count, is_user_suspended,
    update_user_nickname, generate_token,
    DAILY_MESSAGE_LIMIT,
)

logger = get_logger(__name__)


# ============ 消息去重（带大小限制） ============
_MSG_CACHE_MAX_SIZE = 2000
_processed_msg_cache: dict[str, float] = {}


def is_duplicate_msg(msg_id: str) -> bool:
    """检查消息是否已处理过（防止重复处理）"""
    if not msg_id:
        return False
    now = time.time()
    # 清理过期
    expired = [k for k, v in _processed_msg_cache.items() if v < now]
    for k in expired:
        del _processed_msg_cache[k]
    # 防止内存泄漏：超过上限时清除最早的一批
    if len(_processed_msg_cache) >= _MSG_CACHE_MAX_SIZE:
        oldest = sorted(_processed_msg_cache.items(), key=lambda x: x[1])[:_MSG_CACHE_MAX_SIZE // 4]
        for k, _ in oldest:
            del _processed_msg_cache[k]
        logger.info("[去重] 缓存超限，清理 %s 条旧记录", len(oldest))
    if msg_id in _processed_msg_cache:
        logger.info("[去重] 跳过: %s", msg_id)
        return True
    _processed_msg_cache[msg_id] = now + MSG_CACHE_EXPIRE_SECONDS
    return False


# ============ 附件上传 ============

def generate_attachment_name(msg_type: str, ext: str) -> str:
    """生成附件文件名"""
    ts = datetime.now(BEIJING_TZ).strftime("%Y%m%d_%H%M%S")
    return f"{ts}_{msg_type}.{ext}"


def upload_attachment(data: bytes, msg_type: str, ext: str, ctx, content_type: str = "application/octet-stream") -> str | None:
    """上传附件到用户 attachments 目录，返回完整路径或 None"""
    filename = generate_attachment_name(msg_type, ext)
    file_path = f"{ctx.attachments_path}/{filename}"
    ok = ctx.IO.upload_binary(file_path, data, content_type)
    return file_path if ok else None


# ============ 消息 → Payload 转换（网关核心） ============

def build_payload(msg: dict, ctx) -> tuple:
    """
    将渠道原始消息转换为 Karvis payload。
    处理媒体下载、附件上传、ASR，但不做任何业务判断。

    返回 (payload_dict, None) 或 (None, quick_reply_str)
    """
    msg_type = msg["msg_type"]
    user_id = msg.get("from_user", "")
    payload: dict = {"user_id": user_id}

    # 获取用户所属渠道实例（用于媒体下载等）
    ch = channel_router.get_channel(user_id)

    if msg_type == "text":
        content = msg.get("content", "")
        if content.startswith("/help") or content.startswith("帮助"):
            return None, "Karvis 🤖\n\n发送任何内容，我会帮你记录下来。\n支持：文字、图片、语音、视频、链接\n\n打卡相关：说\"打卡\"开始每日复盘"
        payload["type"] = "text"
        payload["text"] = content
        # F1: 检测纯 URL 文本，自动抓取网页正文
        url = extract_url(content)
        if url:
            page_content = fetch_link_content(url)
            if page_content:
                payload["page_content"] = page_content
                payload["detected_url"] = url
        return payload, None

    elif msg_type == "image":
        media_id = msg.get("media_id", "")
        if not media_id:
            return None, "无法获取图片"
        if not ch:
            return None, "无法确定渠道"
        data, content_type = ch.download_media(media_id)
        if not data:
            return None, "图片下载失败"
        ext = "jpg"
        if "png" in (content_type or ""):
            ext = "png"
        elif "gif" in (content_type or ""):
            ext = "gif"
        attachment = upload_attachment(data, "img", ext, ctx, content_type or "image/jpeg")
        if not attachment:
            return None, "图片上传失败"
        payload["type"] = "image"
        payload["attachment"] = attachment
        payload["image_base64"] = base64.b64encode(data).decode("utf-8")
        return payload, None

    elif msg_type == "voice":
        media_id = msg.get("media_id", "")
        audio_format = msg.get("format", "amr")
        if not media_id:
            return None, "无法获取语音"
        if not ch:
            return None, "无法确定渠道"
        data, content_type = ch.download_media(media_id)
        # 根据渠道确定语音格式/扩展名
        channel_name = channel_router.get_user_channel(user_id)
        if channel_name == "telegram":
            ext = "ogg"
        elif channel_name == "feishu":
            ext = "opus"
        else:
            ext = audio_format.lower() if audio_format else "amr"
        if not data:
            return None, "语音下载失败"
        attachment = upload_attachment(data, "voice", ext, ctx, content_type or f"audio/{ext}")
        # ASR 语音识别
        recognized_text = recognize_voice(data, voice_format=ext) or ""
        payload["type"] = "voice"
        payload["text"] = recognized_text
        payload["attachment"] = attachment or ""
        return payload, None

    elif msg_type == "video":
        media_id = msg.get("media_id", "")
        if not media_id:
            return None, "无法获取视频"
        if not ch:
            return None, "无法确定渠道"
        data, content_type = ch.download_media(media_id)
        if not data:
            return None, "视频下载失败"
        size_mb = len(data) / (1024 * 1024)
        logger.info("[视频] 大小=%.1fMB", size_mb)
        attachment = upload_attachment(data, "video", "mp4", ctx, content_type or "video/mp4")
        if not attachment:
            return None, "视频上传失败"
        payload["type"] = "video"
        payload["attachment"] = attachment
        return payload, None

    elif msg_type == "link":
        payload["type"] = "link"
        payload["title"] = msg.get("title", "链接")
        payload["url"] = msg.get("url", "")
        payload["description"] = msg.get("description", "")[:200]
        if payload["url"]:
            payload["content"] = fetch_link_content(payload["url"])
        return payload, None

    else:
        return None, f"暂不支持该消息类型: {msg_type}"


# ============ 消息处理主流程 ============

def handle_message(msg: dict, user_id: str) -> None:
    """
    网关主处理流程：
    1. 获取 UserContext（自动注册新用户）
    2. 消息限额检查
    3. 构造 payload（含媒体处理）
    4. 交给 brain.process()
    5. 发送回复
    """
    t0 = time.time()
    msg_type = msg.get("msg_type", "")
    logger.info("[handle_message] === 开始处理 user=%s, msg_type=%s ===", user_id, msg_type)

    # event 类型（关注、进入应用等）静默忽略
    if msg_type == "event":
        logger.info("[handle_message] 忽略 event 类型消息, user=%s", user_id)
        return

    try:
        # 检查用户是否被挂起
        if is_user_suspended(user_id):
            logger.info("[handle_message] 用户 %s 已被挂起，拒绝处理", user_id)
            channel_router.send_message(user_id, "你的账号已被暂停使用，如有疑问请联系管理员。")
            return

        # 获取/创建用户上下文
        ctx, is_new = get_or_create_user(user_id)
        logger.info("[handle_message] 用户上下文已获取: is_new=%s, base_dir=%s", is_new, ctx.base_dir)

        # 新用户欢迎消息
        if is_new:
            _handle_new_user(user_id, ctx)
            return

        # 新用户引导流程（onboarding）
        config = ctx.get_user_config()
        onboarding = config.get("onboarding_step", 0)

        if onboarding > 0 and msg_type == "text":
            should_continue = _handle_onboarding_text(user_id, ctx, config, onboarding, msg)
            if not should_continue:
                return

        elif onboarding > 0 and msg_type != "text":
            should_continue = _handle_onboarding_non_text(user_id, ctx, config, onboarding)
            if not should_continue:
                return

        # 消息计数 + 限额检查
        count, over_limit = increment_message_count(user_id)
        logger.info("[handle_message] 消息计数: count=%s, limit=%s, over=%s",
                    count, DAILY_MESSAGE_LIMIT, over_limit)
        if over_limit:
            channel_router.send_message(user_id, f"今日消息已达上限（{DAILY_MESSAGE_LIMIT} 条），明天再来吧~")
            return

        # 消息处理反馈：通知渠道"正在处理"（如飞书 Typing 表情、Telegram typing 状态）
        ch = channel_router.get_channel(user_id)
        reaction_ctx = None
        if ch:
            try:
                reaction_ctx = ch.on_message_received(msg)
            except Exception as e:
                logger.warning("[handle_message] on_message_received 异常: %s", e)

        try:
            payload, quick_reply = build_payload(msg, ctx)
            logger.info("[handle_message] payload构建完成: type=%s, quick_reply=%s",
                        payload.get("type") if payload else "None",
                        "有" if quick_reply else "无")

            # 帮助命令或媒体处理失败
            if payload is None:
                if quick_reply and user_id:
                    channel_router.send_message(user_id, quick_reply)
                return

            # 交给大脑

            def _send_reply(text):
                if user_id:
                    channel_router.send_message(user_id, text)

            logger.info("[handle_message] 交给 brain.process(), payload_type=%s", payload.get("type"))
            result = brain.process(payload, send_fn=_send_reply, ctx=ctx)
            reply = result.get("reply") if result else None
            already_sent = result.get("already_sent", False) if result else False
            logger.info("[handle_message] brain 返回: reply=%s(%s字), already_sent=%s",
                        "有" if reply else "无", len(reply) if reply else 0, already_sent)

            if reply and user_id and not already_sent:
                channel_router.send_message(user_id, reply)

            # 引导阶段追加提示
            _handle_onboarding_followup(user_id, ctx, onboarding)

            logger.info("[handle_message] === 处理完成 user=%s, 耗时=%.1fs ===", user_id, time.time() - t0)

        finally:
            # 消息处理反馈：清理"处理中"状态（如飞书移除 Typing 表情）
            if ch:
                try:
                    ch.on_message_done(msg, reaction_ctx)
                except Exception as e:
                    logger.warning("[handle_message] on_message_done 异常: %s", e)

    except Exception as e:
        logger.exception("[handle_message] === 处理异常 user=%s, 耗时=%.1fs ===", user_id, time.time() - t0)
        if user_id:
            channel_router.send_message(user_id, "处理消息时出错了，请稍后重试")


# ============ 辅助函数 ============

def _handle_new_user(user_id: str, ctx) -> None:
    """处理新用户欢迎消息"""
    logger.info("[handle_message] 新用户 %s，发送欢迎消息", user_id)

    # 通过渠道名称动态生成欢迎语
    channel_name = channel_router.get_user_channel(user_id)
    if channel_name == "telegram":
        welcome = (
            "Hi~ I'm Karvis 🤖\n"
            "Your AI life assistant.\n\n"
            "Let's get to know each other! What should I call you?\n"
            "(Just say \"Call me XX\")"
        )
    else:
        # 中文欢迎语（飞书、企微等）
        channel_display = {
            "feishu": "飞书",
            "wework": "企业微信",
        }.get(channel_name, "这里")
        welcome = (
            f"嗨～我是 Karvis 🤖\n"
            f"你的 AI 生活助手，住在{channel_display}里。\n\n"
            f"先认识一下吧，你希望我怎么称呼你？\n"
            f"（直接说「叫我XX」就好~）"
        )

    channel_router.send_message(user_id, welcome)

    # 通知管理员有新用户注册
    try:
        total = len(get_all_active_users())
        channel_router.send_alert(
            f"📢 新用户注册\n\nuser_id: {user_id}\n当前活跃用户数: {total}"
        )
    except Exception as e:
        logger.error("[handle_message] 新用户通知管理员失败: %s", e)


def _handle_onboarding_text(user_id: str, ctx, config: dict, onboarding: int, msg: dict) -> bool:
    """
    处理引导阶段的文本消息。
    返回 True 表示继续后续处理，False 表示已处理完毕。
    """
    content = msg.get("content", "").strip()
    logger.info("[onboarding] step=%s, user=%s, content=%s", onboarding, user_id, content[:50])

    # 任何阶段说"跳过"都结束引导
    if content in ("跳过", "算了", "skip"):
        config["onboarding_step"] = 0
        ctx.save_user_config(config)
        channel_router.send_message(user_id, "没问题！有什么想法随时发给我就好～")
        return False

    if onboarding == 1:
        # 等昵称 — 用模型提取
        extract_prompt = (
            "用户在设置昵称，请从下面这句话中提取出用户希望被称呼的昵称。\n"
            "只返回昵称本身，不要任何解释、引号或标点。\n"
            "如果无法识别，返回空。\n\n"
            f"用户说：{content}"
        )
        nickname = brain.call_llm(
            [{"role": "user", "content": extract_prompt}],
            model_tier="flash", max_tokens=20, temperature=0
        )
        nickname = (nickname or "").strip().strip("\"'""''")
        if not nickname:
            channel_router.send_message(user_id, "没听清名字呢，再说一次？直接打名字就行~")
            return False

        config["nickname"] = nickname
        config["onboarding_step"] = 2
        ctx.save_user_config(config)

        update_user_nickname(user_id, nickname)

        reply = (
            f"好的{nickname}！以后就这么叫你啦～\n\n"
            f"来试试我的核心功能吧 👇\n"
            f"随便发句话给我，比如：\n"
            f"「今天天气真好，心情不错」"
        )
        channel_router.send_message(user_id, reply)
        return False

    elif onboarding == 2:
        config["onboarding_step"] = 3
        ctx.save_user_config(config)
        return True  # 继续走正常 brain 流程

    elif onboarding == 3:
        config["onboarding_step"] = 0
        ctx.save_user_config(config)
        return True  # 继续正常流程

    return True


def _handle_onboarding_non_text(user_id: str, ctx, config: dict, onboarding: int) -> bool:
    """
    处理引导阶段的非文本消息。
    返回 True 表示继续后续处理，False 表示已处理完毕。
    """
    if onboarding == 1:
        channel_router.send_message(user_id, "先告诉我你的名字吧～直接打名字就行~")
        return False
    else:
        config["onboarding_step"] = 0
        ctx.save_user_config(config)
        return True


def _handle_onboarding_followup(user_id: str, ctx, original_onboarding: int) -> None:
    """处理引导完成后的追加提示"""
    config_now = ctx.get_user_config()
    ob_step = config_now.get("onboarding_step", 0)
    nickname = config_now.get("nickname") or ""

    if ob_step == 3:
        # 刚完成第一条笔记，追加待办引导
        time.sleep(0.5)
        guide = (
            "✨ 看，你的第一条记录已经保存好了！\n\n"
            "再试试待办功能？直接说：\n"
            "「帮我添加待办 明天买咖啡」"
        )
        channel_router.send_message(user_id, guide)

    elif ob_step == 0 and original_onboarding == 3:
        # 刚完成引导 — 生成 Web 链接一并发出
        time.sleep(0.5)

        token = generate_token(user_id)
        domain = os.environ.get("WEB_DOMAIN", "127.0.0.1:9000")
        _is_ip = all(part.isdigit() for part in domain.split(":")[0].split("."))
        scheme = "http" if _is_ip or "127.0.0.1" in domain or "localhost" in domain else "https"
        web_url = f"{scheme}://{domain}/web/login?token={token}"

        final = (
            f"🎉 太棒了{nickname}！你已经掌握了核心用法：\n\n"
            f"💬 发消息 → 自动记笔记\n"
            f"✅ 说「添加待办」→ 管理任务\n"
            f"📊 每晚自动生成日报\n"
            f"🌙 晚上 9 点会邀请你打卡复盘\n\n"
            f"📱 你还可以在浏览器里查看所有数据：\n"
            f"{web_url}\n\n"
            f"链接 24 小时有效，过期了跟我说「给我查看链接」就行～\n\n"
            f"还有更多玩法慢慢发现，有什么想法随时告诉我！"
        )
        channel_router.send_message(user_id, final)
