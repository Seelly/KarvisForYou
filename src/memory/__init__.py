# -*- coding: utf-8 -*-
"""
记忆管理子包 — 提供对话窗口管理、长期记忆更新、缓存等功能。

向后兼容：`from memory import XXX` 等价于原 `from memory import XXX`。
"""

# prompt_cache 模块
from memory.prompt_cache import (
    PromptCache,
    get_prompt_cache,
    load_memory,
)

# state 缓存模块
from memory.state import (
    read_state_cached,
    write_state_and_update_cache,
    invalidate_state_cache,
)

# 对话窗口 + 长期记忆模块
from memory.conversation import (
    format_recent_messages,
    add_message_to_state,
    maybe_compress_messages,
    apply_memory_updates,
)


def invalidate_all_caches():
    """清除所有缓存（定时任务用）"""
    from infra.logging import get_logger
    get_prompt_cache().invalidate()
    invalidate_state_cache()
    get_logger(__name__).info("[Memory] 全部缓存已清除")


__all__ = [
    "PromptCache",
    "get_prompt_cache",
    "load_memory",
    "read_state_cached",
    "write_state_and_update_cache",
    "invalidate_state_cache",
    "invalidate_all_caches",
    "format_recent_messages",
    "add_message_to_state",
    "maybe_compress_messages",
    "apply_memory_updates",
]
