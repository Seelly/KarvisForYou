# -*- coding: utf-8 -*-
"""
Web 查看令牌 Skill
用户说"给我查看链接"时生成带 token 的 Web 访问 URL。
"""
import os

from infra.logging import get_logger

logger = get_logger(__name__)


def generate_web_token(params, state, ctx):
    """
    生成 Web 查看令牌并返回带 token 的 URL。
    用户在企微中说"给我查看链接"、"我要看我的数据"时触发。
    """
    from services.token_service import generate_token

    user_id = ctx.user_id
    logger.info("为用户 %s 生成 Web 访问令牌", user_id)

    token = generate_token(user_id)

    # 域名从环境变量读取；如果没配，尝试从 PROCESS_ENDPOINT_URL 推断
    domain = os.environ.get("WEB_DOMAIN", "")
    if not domain:
        # 从 PROCESS_ENDPOINT_URL 推断（如 http://xxx.trycloudflare.com/process）
        process_url = os.environ.get("PROCESS_ENDPOINT_URL", "")
        if "trycloudflare.com" in process_url or ("://" in process_url and "127.0.0.1" not in process_url and "localhost" not in process_url):
            # 提取域名部分
            import re
            match = re.search(r'https?://([^/]+)', process_url)
            if match:
                domain = match.group(1)
    if not domain:
        domain = "127.0.0.1:9000"
    # IP 地址用 http，有域名才用 https
    _is_ip = all(part.isdigit() for part in domain.split(":")[0].split("."))
    scheme = "http" if _is_ip or "127.0.0.1" in domain or "localhost" in domain else "https"
    url = f"{scheme}://{domain}/web/login?token={token}"

    logger.info("令牌已生成: user=%s, url=%s...", user_id, url[:80])

    nickname = ctx.get_nickname() or "你"
    reply = (
        f"🔗 {nickname}的数据查看链接：\n\n"
        f"{url}\n\n"
        f"有效期 24 小时，过期后再跟我说「给我查看链接」就好~"
    )

    return {
        "success": True,
        "reply": reply,
    }


# ============ Skill 注册 ============

SKILL_REGISTRY = {
    "web.token": generate_web_token,
}
