# -*- coding: utf-8 -*-
"""
用户管理子包 — 提供用户上下文、注册表管理、新用户引导等功能。
"""
from user.context import UserContext
from user.registry import (
    get_or_create_user,
    increment_message_count,
    get_all_active_users,
    get_all_users,
    update_user_status,
    update_user_nickname,
    is_user_suspended,
    DAILY_MESSAGE_LIMIT,
    INACTIVE_DAYS_THRESHOLD,
)
from user.onboarding import (
    handle_new_user,
    handle_onboarding_text,
    handle_onboarding_non_text,
    handle_onboarding_followup,
)

__all__ = [
    "UserContext",
    "get_or_create_user",
    "increment_message_count",
    "get_all_active_users",
    "get_all_users",
    "update_user_status",
    "update_user_nickname",
    "is_user_suspended",
    "DAILY_MESSAGE_LIMIT",
    "INACTIVE_DAYS_THRESHOLD",
    "handle_new_user",
    "handle_onboarding_text",
    "handle_onboarding_non_text",
    "handle_onboarding_followup",
]
