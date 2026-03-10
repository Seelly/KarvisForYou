# -*- coding: utf-8 -*-
"""
新用户引导流程模块。

职责：
- 新用户欢迎消息
- 引导阶段处理（昵称设置、第一条笔记、第一个待办）
- 引导完成后的追加提示
"""
from __future__ import annotations

import os
import time

from core import engine as brain
from channel import router as channel_router
from infra.logging import get_logger
from user.registry import get_all_active_users, update_user_nickname
from services.token_service import generate_token

logger = get_logger(__name__)


def handle_new_user(user_id: str, ctx) -> None:
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


def handle_onboarding_text(user_id: str, ctx, config: dict, onboarding: int, msg: dict) -> bool:
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


def handle_onboarding_non_text(user_id: str, ctx, config: dict, onboarding: int) -> bool:
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


def handle_onboarding_followup(user_id: str, ctx, original_onboarding: int) -> None:
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
