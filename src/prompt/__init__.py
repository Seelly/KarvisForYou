# -*- coding: utf-8 -*-
"""
Prompt 子包 — 管理所有 Prompt 模板和组装逻辑。

子模块：
- templates: 全部 Prompt 常量定义（原 prompts.py）
- builder:   Prompt 组装器（从 brain.py 抽离的组装函数）
"""
from prompt.templates import (
    SOUL,
    SKILLS,
    SKILL_PROMPT_LINES,
    RULES,
    RULES_CORE,
    RULES_SYSTEM_TASKS,
    RULES_BOOKS_MEDIA,
    RULES_HABITS,
    RULES_ADVANCED,
    RULES_FINANCE,
    RULES_SKILLS_MGMT,
    OUTPUT_FORMAT,
    LONG_TASKS,
    CONFIRM_TEMPLATES,
    build_skills_prompt,
    get_storage_display_name,
    get_confirm_message,
    get,
)
from prompt.builder import (
    build_time_string,
    select_rules,
    build_system_prompt,
    build_state_summary,
    build_user_message,
)

__all__ = [
    # 模板常量
    "SOUL", "SKILLS", "SKILL_PROMPT_LINES",
    "RULES", "RULES_CORE", "RULES_SYSTEM_TASKS",
    "RULES_BOOKS_MEDIA", "RULES_HABITS", "RULES_ADVANCED",
    "RULES_FINANCE", "RULES_SKILLS_MGMT", "OUTPUT_FORMAT",
    "LONG_TASKS", "CONFIRM_TEMPLATES",
    # 模板函数
    "build_skills_prompt", "get_storage_display_name",
    "get_confirm_message", "get",
    # 组装函数
    "build_time_string", "select_rules",
    "build_system_prompt", "build_state_summary",
    "build_user_message",
]
