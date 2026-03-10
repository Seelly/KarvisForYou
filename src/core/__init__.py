# -*- coding: utf-8 -*-
"""
核心子包 — Karvis 大脑的核心处理逻辑。

子模块：
- engine:     核心处理引擎 (process)
- llm:        LLM 调用层 (call_llm, call_qwen_vl)
- monitoring: 告警和预算监控
- rhythm:     用户节奏学习
- interfaces: Protocol 接口定义
"""
from core.engine import process
from core.llm import call_llm, call_deepseek, call_qwen_vl
from core.interfaces import LLMProvider, SkillHandler, MessageSender, PromptBuilder

__all__ = [
    "process",
    "call_llm",
    "call_deepseek",
    "call_qwen_vl",
    # 接口
    "LLMProvider",
    "SkillHandler",
    "MessageSender",
    "PromptBuilder",
]
