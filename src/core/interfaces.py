# -*- coding: utf-8 -*-
"""
核心接口定义 — 使用 Protocol 定义模块间契约。

提供类型安全的接口定义，降低模块间耦合。
各模块面向接口编程，而非面向具体实现。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    """LLM 服务提供者接口。

    所有 LLM 调用层（DeepSeek、Qwen 等）的统一契约。
    支持运行时类型检查（runtime_checkable）。
    """

    def call(self, messages: list[dict], *,
             max_tokens: int = 3000,
             temperature: float = 0.3) -> str | None:
        """调用 LLM 并返回文本回复。失败返回 None。"""
        ...


@runtime_checkable
class SkillHandler(Protocol):
    """Skill 处理器接口。

    每个 Skill 模块通过 @skill 装饰器注册时，
    其 execute 函数需符合此签名。
    """

    def __call__(self, params: dict, state: dict, ctx: Any) -> dict:
        """
        执行 Skill 并返回结果。

        Args:
            params: LLM 提取的技能参数
            state:  用户当前状态
            ctx:    UserContext 实例

        Returns:
            结果 dict，至少含 {"success": bool}，
            可选 {"reply": str, "state_updates": dict, "memory_updates": list}
        """
        ...


@runtime_checkable
class MessageSender(Protocol):
    """消息发送者接口。

    用于 process() 的 send_fn 参数类型标注。
    """

    def __call__(self, text: str) -> None:
        """发送消息给用户。"""
        ...


@runtime_checkable
class PromptBuilder(Protocol):
    """Prompt 组装器接口。"""

    def build_system_prompt(self, state: dict, ctx: Any, *,
                            prompt_futs: dict | None = None,
                            payload: dict | None = None) -> str:
        """组装完整的 System Prompt。"""
        ...

    def build_user_message(self, payload: dict) -> str:
        """构建发给 LLM 的 user message。"""
        ...
