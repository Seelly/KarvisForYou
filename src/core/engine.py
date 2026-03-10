# -*- coding: utf-8 -*-
"""
Karvis 核心处理引擎 — process() 入口 + Skill 执行 + Agent Loop。

brain.py 的核心逻辑抽离到此模块，brain.py 变为精简兼容层。
"""
from __future__ import annotations

import json
import time as _time
from datetime import datetime, timezone, timedelta

from channel import router as channel_router
from config import ADMIN_USER_ID
from infra.logging import get_logger, get_request_id
from infra.shared import executor as _executor
from memory import (
    load_memory, add_message_to_state,
    read_state_cached, write_state_and_update_cache,
    apply_memory_updates,
)
from skill_loader import load_skill_registry, get_skill_metadata
from core.llm import (
    call_llm, call_qwen_vl, set_current_user,
    select_model_tier, rotate_jsonl,
)
from core.monitoring import check_and_alert
from core.rhythm import update_nudge_state, check_checkin_timeout, update_user_rhythm
from prompt.builder import build_system_prompt, build_user_message
import prompt.templates as prompts

logger = get_logger(__name__)


# ============ Skill 注册表 ============

def _get_skill_registry():
    """通过 skill_loader 自动发现并加载所有 skill"""
    return load_skill_registry()


# ============ 常量集合 ============

# V4: 不需要 Flash 加工的简单 skill
_SIMPLE_SKILLS = frozenset({
    "note.save", "classify.archive", "todo.add", "todo.done",
    "checkin.start", "checkin.answer", "checkin.skip", "checkin.cancel",
    "book.create", "book.excerpt", "book.thought", "book.summary", "book.quotes",
    "media.create", "media.thought",
    "mood.generate", "voice.journal",
    "settings.nickname", "settings.ai_name", "settings.soul", "settings.info",
    "web.token",
    "habit.propose", "habit.nudge", "habit.status", "habit.complete",
    "decision.record", "dynamic",
    "reflect.push", "reflect.answer", "reflect.skip", "reflect.history",
})

# 速记智能过滤：规则预筛跳过集合
_SKIP_NOTE_SKILLS = frozenset({
    "todo.add", "todo.done", "todo.list",
    "habit.propose", "habit.nudge", "habit.status", "habit.complete",
    "decision.record", "decision.review", "decision.list",
    "book.create", "book.excerpt", "book.thought", "book.summary", "book.quotes",
    "media.create", "media.thought",
    "web.token",
    "settings.nickname", "settings.ai_name", "settings.soul", "settings.info",
    "deep.dive",
})

_REFLECT_SKILLS = ("reflect.answer", "reflect.skip", "reflect.history", "reflect.push")
_CHECKIN_SKILLS = ("checkin.answer", "checkin.skip", "checkin.cancel", "checkin.start")


# ============ 核心处理流程 ============

def process(payload, send_fn=None, ctx=None):
    """
    Karvis 大脑的核心入口（多用户版）。

    参数:
        payload: dict, 结构化消息
        send_fn: 回复回调
        ctx: UserContext, 当前用户上下文
    """
    t_start = _time.time()
    logger.info("[Brain] received: %s", json.dumps(payload, ensure_ascii=False)[:200])

    user_id = payload.get("user_id", "unknown")
    set_current_user(user_id)

    # 0. 预热存储连接
    if ctx and hasattr(ctx.IO, 'get_token'):
        ctx.IO.get_token()
    t_token = _time.time()
    logger.debug("[Brain][timing] storage warmup: %.1fs", t_token - t_start)

    # 1. 读取 state 和 memory（并发）
    state_future = _executor.submit(read_state_cached, ctx)
    prompt_futs = {
        "mem": _executor.submit(load_memory, ctx),
    }

    # 2. 图片 VL 处理
    if payload.get("type") == "image" and payload.get("image_base64"):
        if ctx and ctx.is_admin:
            logger.info("[Brain] image detected (admin), calling Qwen VL...")
            vl_desc = call_qwen_vl(payload["image_base64"])
            if vl_desc:
                payload["image_description"] = vl_desc
                logger.info("[Brain] image understanding done: %s", vl_desc[:100])
            else:
                logger.warning("[Brain] image understanding failed, fallback to plain image")
        else:
            logger.info("[Brain] non-admin image, skip VL call, user=%s", user_id)
        del payload["image_base64"]
        if not (ctx and ctx.is_admin):
            state = state_future.result() or {}
            _save_to_quick_notes(payload, state, ctx)
            return {"reply": "图片已保存~ 图片理解功能即将在订阅版上线，敬请期待~"}

    user_text = _extract_user_text(payload)

    state = state_future.result() or {}
    t_state = _time.time()
    logger.debug("[Brain][timing] state read: %.1fs", t_state - t_token)

    # 3. 检查打卡超时
    check_checkin_timeout(state)

    # 4. 记录用户消息到短期记忆 + 更新 nudge_state
    if user_text and payload.get("type") != "system":
        add_message_to_state(state, "user", user_text)
        update_nudge_state(state)

    # 5. 构建 prompt 并调用 LLM
    system_prompt = build_system_prompt(state, ctx, prompt_futs=prompt_futs, payload=payload)
    t_prompt = _time.time()
    logger.debug("[Brain][timing] prompt build: %.1fs (prompt_len=%s)", t_prompt - t_state, len(system_prompt))

    user_message = build_user_message(payload)

    is_system = payload.get("type") == "system"
    action = payload.get("action", "") if is_system else None
    model_tier = select_model_tier(payload, is_system_action=is_system, action=action)
    logger.info("[Brain] model routing: tier=%s, is_system=%s, action=%s", model_tier, is_system, action)

    llm_response = call_llm([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message}
    ], model_tier=model_tier)
    t_llm = _time.time()
    logger.debug("[Brain][timing] LLM call (%s): %.1fs", model_tier, t_llm - t_prompt)

    if not llm_response:
        logger.warning("[Brain] LLM returned empty, fallback")
        if payload.get("type") != "system":
            _save_to_quick_notes(payload, state, ctx)
        _sn = prompts.get_storage_display_name(getattr(ctx, 'storage_mode', 'local'))
        return {"reply": f"已记录到{_sn}（AI 暂时不可用）"}

    # 6. 解析 LLM 输出
    decision = _parse_llm_output(llm_response)
    if not decision:
        logger.warning("[Brain] JSON parse failed, raw: %s", llm_response[:300])
        if payload.get("type") != "system":
            _save_to_quick_notes(payload, state, ctx)
        _sn = prompts.get_storage_display_name(getattr(ctx, 'storage_mode', 'local'))
        return {"reply": f"已记录到{_sn}"}

    logger.info("[Brain] decision: skill=%s, thinking=%s", decision.get("skill"), decision.get("thinking", "")[:80])
    if decision.get("memory_updates"):
        logger.info("[Brain] memory updates: %s", json.dumps(decision["memory_updates"], ensure_ascii=False)[:200])

    registry = _get_skill_registry()

    # 7. Quick-Notes 两阶段过滤
    primary_skill = _get_primary_skill(decision)

    # Reflect 防护
    if (state.get("reflect_pending")
            and not state.get("checkin_pending")
            and payload.get("type") != "system"
            and primary_skill not in _REFLECT_SKILLS
            and primary_skill not in _CHECKIN_SKILLS):
        logger.info("[Brain] reflect guard: %s -> reflect.answer", primary_skill)
        decision["skill"] = "reflect.answer"
        decision["params"] = {"answer": user_text}
        decision.pop("steps", None)
        primary_skill = "reflect.answer"

    _pending_note_filter = False
    if payload.get("type") != "system" and primary_skill not in _CHECKIN_SKILLS + _REFLECT_SKILLS[:2]:
        if primary_skill in _SKIP_NOTE_SKILLS:
            logger.debug("[Brain][NoteFilter] rule skip: skill=%s", primary_skill)
        elif primary_skill == "note.save":
            _save_to_quick_notes(payload, state, ctx)
        else:
            _pending_note_filter = True

    # 8. 执行 Steps
    steps, step_results = _execute_steps(decision, state, registry, ctx)
    t_skill = _time.time()
    logger.debug("[Brain][timing] skill exec: %.1fs", t_skill - t_llm)

    # Agent Loop
    if len(steps) == 1 and decision.get("continue"):
        first_result = step_results[0]["result"] if step_results else {}
        agent_context = first_result.get("agent_context") if isinstance(first_result, dict) else None
        first_skill = steps[0].get("skill", "")
        if agent_context and first_skill.startswith("internal."):
            decision, last_skill_result = _run_agent_loop(
                system_prompt, user_message, decision, agent_context, state, registry, ctx
            )
            steps = [{"skill": decision.get("skill", "ignore"), "params": decision.get("params", {})}]
            step_results = [{"skill": decision.get("skill", "ignore"), "result": last_skill_result or {"success": True}}]
            t_agent = _time.time()
            logger.debug("[Brain][timing] agent loop: %.1fs", t_agent - t_skill)
            t_skill = t_agent

    # 9. 合并状态更新
    for sr in step_results:
        r = sr.get("result", {})
        if isinstance(r, dict):
            if r.get("state_updates"):
                logger.debug("[Brain] merging state_updates from %s: %s", sr.get("skill"), list(r["state_updates"].keys()))
                state.update(r["state_updates"])
            if r.get("memory_updates"):
                existing = decision.get("memory_updates", [])
                decision["memory_updates"] = existing + r["memory_updates"]
                logger.debug("[Brain] merging memory_updates from %s: +%s, total=%s",
                         sr.get("skill"), len(r["memory_updates"]), len(decision["memory_updates"]))
    llm_state_updates = decision.get("state_updates", {})
    if llm_state_updates:
        state.update(llm_state_updates)

    # 10. 智能回复路由
    reply = _resolve_reply(user_text, decision, steps, step_results)
    logger.info("[Brain] reply routing: reply=%s(%s chars)", "yes" if reply else "no", len(reply) if reply else 0)

    if not reply and payload.get("type") != "system":
        if decision.get("memory_updates"):
            reply = "记住啦~"
        elif primary_skill == "note.save":
            reply = "已记录 ✅"
        elif primary_skill == "ignore":
            reply = "收到~"
        else:
            reply = "好的~"
            logger.debug("[Brain] fallback reply: skill=%s -> %s", primary_skill, reply)

    if reply:
        add_message_to_state(state, "karvis", reply)

    # 先发回复，再保存 state/memory
    if send_fn and reply:
        try:
            send_fn(reply)
            logger.info("[Brain] reply sent first, starting background save")
        except Exception as e:
            logger.error("[Brain] early send failed: %s", e)

    # Flash 过滤 Quick-Notes
    if _pending_note_filter:
        _executor.submit(_flash_filter_and_save, payload, state, ctx, primary_skill)

    # V8: 更新用户节奏画像
    try:
        update_user_rhythm(state)
    except Exception as e:
        logger.error("[Brain][V8] rhythm update failed (non-blocking): %s", e)

    t_save_start = _time.time()
    _save_state_and_memory(state, decision, payload=payload, reply=reply, elapsed=t_save_start - t_start, ctx=ctx)
    t_end = _time.time()
    logger.info("[Brain][timing] save state: %.1fs | total: %.1fs", t_end - t_save_start, t_end - t_start)

    # 异步告警检测
    total_elapsed = t_end - t_start
    _executor.submit(check_and_alert, total_elapsed, user_id,
                     _get_primary_skill(decision) if decision else "unknown",
                     user_text, None)

    return {"reply": reply, "already_sent": bool(send_fn and reply)}


# ============ State / Memory 持久化 ============

def _save_state_and_memory(state, decision, payload=None, reply=None, elapsed=None, ctx=None):
    """保存 state、更新记忆、写决策日志（并发写）"""
    futs = []
    futs.append(_executor.submit(_write_state, state, ctx))

    memory_updates = decision.get("memory_updates", [])
    if memory_updates:
        logger.info("[Brain] async saving memory_updates: %s items", len(memory_updates))
        futs.append(_executor.submit(_write_memory, memory_updates, ctx))

    futs.append(_executor.submit(_write_decision_log, payload, decision, reply, elapsed, ctx))

    for f in futs:
        try:
            f.result(timeout=30)
        except Exception as e:
            logger.error("[Brain] write exception: %s", e)


def _write_state(state, ctx):
    try:
        write_state_and_update_cache(state, ctx)
    except Exception as e:
        logger.error("[Brain] state save failed: %s", e)


def _write_memory(memory_updates, ctx):
    try:
        apply_memory_updates(memory_updates, ctx)
    except Exception as e:
        logger.error("[Brain] memory update failed: %s", e)


def _write_decision_log(payload, decision, reply, elapsed, ctx):
    """将每次决策写入 JSONL 日志"""
    try:
        beijing_tz = timezone(timedelta(hours=8))
        now_str = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")

        input_type = payload.get("type", "") if payload else ""
        action = payload.get("action", "") if input_type == "system" else ""

        entry = {
            "ts": now_str,
            "user_id": ctx.user_id if ctx else "",
            "input_type": input_type,
            "action": action,
            "skill": decision.get("skill", "") if decision else "",
            "has_memory_updates": bool(decision.get("memory_updates")) if decision else False,
            "has_reply": bool(reply),
            "elapsed_s": round(elapsed, 1) if elapsed else None,
        }
        rid = get_request_id()
        if rid:
            entry["request_id"] = rid
        line = json.dumps(entry, ensure_ascii=False)

        log_file = ctx.decision_log_file if ctx else ""
        if log_file and ctx:
            if ctx.storage_mode == "local":
                rotate_jsonl(log_file, max_size_mb=5)
            existing = ctx.IO.read_text(log_file) or ""
            new_content = existing + line + "\n"
            ctx.IO.write_text(log_file, new_content)
        logger.debug("[Brain] decision log written: skill=%s", entry["skill"])
    except Exception as e:
        logger.error("[Brain] decision log write failed (non-blocking): %s", e)


# ============ Agent Loop ============

def _run_agent_loop(system_prompt, user_message, first_decision, first_context, state, registry, ctx):
    """多轮 Agent Loop"""
    MAX_ROUNDS = 5

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
        {"role": "assistant", "content": json.dumps(first_decision, ensure_ascii=False)},
        {"role": "user", "content": json.dumps({
            "type": "agent_step", "step": 1, "skill_result": first_context
        }, ensure_ascii=False)}
    ]

    last_decision = first_decision
    last_skill_result = {"success": True, "agent_context": first_context}

    for step in range(2, MAX_ROUNDS + 1):
        logger.info("[Brain][AgentLoop] round %s", step)

        llm_response = call_llm(messages, model_tier="main", max_tokens=500, temperature=0.3)
        if not llm_response:
            logger.warning("[Brain][AgentLoop] LLM returned empty, breaking")
            break

        decision = _parse_llm_output(llm_response)
        if not decision:
            logger.warning("[Brain][AgentLoop] JSON parse failed, breaking")
            break

        last_decision = decision
        skill_name = decision.get("skill", "ignore")

        logger.info("[Brain][AgentLoop] step=%s, skill=%s, continue=%s", step, skill_name, decision.get("continue"))

        if not decision.get("continue"):
            if skill_name and not skill_name.startswith("internal.") and skill_name != "ignore":
                handler = registry.get(skill_name)
                if handler:
                    try:
                        last_skill_result = handler(decision.get("params", {}), state, ctx)
                    except Exception as e:
                        logger.error("[Brain][AgentLoop] final skill %s exec failed: %s", skill_name, e)
                        last_skill_result = {"success": False}
            break

        handler = registry.get(skill_name)
        if not handler:
            logger.warning("[Brain][AgentLoop] unknown skill: %s, breaking", skill_name)
            break

        try:
            skill_result = handler(decision.get("params", {}), state, ctx)
            agent_context = skill_result.get("agent_context") if isinstance(skill_result, dict) else None
        except Exception as e:
            logger.error("[Brain][AgentLoop] skill %s exception: %s", skill_name, e)
            agent_context = {"error": str(e)}

        last_skill_result = skill_result or {"success": True}

        messages.append({"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)})
        messages.append({"role": "user", "content": json.dumps({
            "type": "agent_step", "step": step, "skill_result": agent_context or {}
        }, ensure_ascii=False)})

    logger.info("[Brain][AgentLoop] loop done, final skill=%s", last_decision.get("skill"))
    return last_decision, last_skill_result


# ============ Skill 执行 ============

def _get_primary_skill(decision):
    """从 decision 中提取主 skill 名称"""
    steps = decision.get("steps")
    if steps and len(steps) > 0:
        return steps[0].get("skill", "ignore")
    return decision.get("skill", "ignore")


def _execute_steps(decision, state, registry, ctx):
    """执行 steps 数组中的所有 skill，收集结果"""
    steps = decision.get("steps")
    if not steps:
        skill = decision.get("skill", "ignore")
        params = decision.get("params", {})
        steps = [{"skill": skill, "params": params}]

    all_metadata = get_skill_metadata()

    results = []
    for i, step in enumerate(steps):
        skill_name = step.get("skill", "ignore")
        params = step.get("params", {})

        if skill_name == "note.save":
            logger.debug("[Brain] Step %s: note.save handled by unified write, skip", i)
            results.append({"skill": skill_name, "result": {"success": True}})
            continue
        if skill_name == "ignore":
            results.append({"skill": skill_name, "result": {"success": True}})
            continue

        # V12: 执行层权限检查
        meta = all_metadata.get(skill_name, {})
        vis = meta.get("visibility", "public")

        if vis == "private" and not ctx.is_admin:
            logger.warning("[Brain] Step %s: %s is private, user %s denied", i, skill_name, ctx.user_id)
            results.append({"skill": skill_name, "result": {
                "success": False,
                "reply_override": "我目前没有这个功能哦~ 如果你想管理待办或记笔记，随时告诉我~"
            }})
            continue

        if vis == "preview" and not ctx.is_admin:
            preview_msg = meta.get("preview_message",
                                   "该功能即将在订阅版上线，敬请期待~ 目前你可以用文字描述给我，我一样能帮到你~")
            logger.info("[Brain] Step %s: %s is preview, returning teaser", i, skill_name)
            results.append({"skill": skill_name, "result": {
                "success": False, "reply_override": preview_msg
            }})
            continue

        if not ctx.is_skill_allowed(skill_name):
            logger.info("[Brain] Step %s: %s disabled by user %s", i, skill_name, ctx.user_id)
            results.append({"skill": skill_name, "result": {
                "success": False,
                "reply_override": f"「{skill_name.split('.')[0]}」功能未开启，你可以说「开启{skill_name.split('.')[0]}」来启用~"
            }})
            continue

        handler = registry.get(skill_name)
        if not handler:
            logger.warning("[Brain] Step %s: unknown skill %s", i, skill_name)
            results.append({"skill": skill_name, "result": {"success": False, "error": f"未知 skill: {skill_name}"}})
            continue

        try:
            result = handler(params, state, ctx)
            results.append({"skill": skill_name, "result": result or {"success": True}})
            logger.info("[Brain] Step %s: %s -> success=%s", i, skill_name,
                        result.get("success") if isinstance(result, dict) else True)
        except Exception as e:
            logger.exception("[Brain] Step %s: %s exception", i, skill_name)
            results.append({"skill": skill_name, "result": {"success": False, "error": str(e)}})

    return steps, results


# ============ 回复路由 ============

def _resolve_reply(user_text, decision, steps, step_results):
    """智能回复路由"""
    all_skills = [s.get("skill", "ignore") for s in steps]
    llm_reply = decision.get("reply")

    for sr in step_results:
        r = sr.get("result", {})
        if isinstance(r, dict) and r.get("reply_override"):
            return r["reply_override"]

    if all_skills == ["ignore"] and llm_reply:
        return llm_reply

    if all(s in _SIMPLE_SKILLS for s in all_skills):
        for sr in step_results:
            r = sr.get("result", {})
            if isinstance(r, dict) and r.get("reply"):
                return r["reply"]
        return llm_reply

    if len(step_results) == 1 and all_skills[0] in _SIMPLE_SKILLS:
        r = step_results[0].get("result", {})
        return r.get("reply") if isinstance(r, dict) else llm_reply

    logger.info("[Brain][V4] triggering Flash reply layer: skills=%s", all_skills)
    t0 = _time.time()
    flash_reply = _call_flash_for_reply(user_text, decision, steps, step_results)
    t1 = _time.time()
    logger.debug("[Brain][V4][timing] Flash reply generation: %.1fs", t1-t0)
    return flash_reply or llm_reply


def _call_flash_for_reply(user_text, decision, steps, step_results):
    """调用 Flash 模型生成最终回复"""
    context_parts = []
    context_parts.append(f"用户消息: {user_text}")
    context_parts.append(f"AI 判断: {decision.get('thinking', '')}")

    for i, (step, sr) in enumerate(zip(steps, step_results)):
        skill_name = step.get("skill", "")
        r = sr.get("result", {})
        if not isinstance(r, dict):
            r = {"success": True}
        success = r.get("success", False)
        reply_data = r.get("reply", "")
        error = r.get("error", "")

        if success and reply_data:
            context_parts.append(f"操作{i+1} [{skill_name}] 成功，数据:\n{reply_data}")
        elif success:
            context_parts.append(f"操作{i+1} [{skill_name}] 成功")
        else:
            context_parts.append(f"操作{i+1} [{skill_name}] 失败: {error or reply_data}")

    llm_reply = decision.get("reply", "")
    if llm_reply:
        context_parts.append(f"AI 预生成回复（仅供参考）: {llm_reply}")

    context = "\n".join(context_parts)

    try:
        reply = call_llm([
            {"role": "system", "content": prompts.FLASH_REPLY},
            {"role": "user", "content": context}
        ], model_tier="flash", max_tokens=300, temperature=0.5)
        return reply
    except Exception as e:
        logger.error("[Brain][V4] Flash reply generation failed: %s", e)
        return None


# ============ Quick-Notes ============

def _save_to_quick_notes(payload, state, ctx):
    """所有用户消息统一写入 Quick-Notes"""
    try:
        from skills import note_save
        content = ""
        attachment = ""
        msg_type = payload.get("type", "")

        if msg_type == "text":
            content = payload.get("text", "")
        elif msg_type == "voice":
            content = payload.get("text", "")
            attachment = payload.get("attachment", "")
        elif msg_type == "image":
            attachment = payload.get("attachment", "")
        elif msg_type == "video":
            attachment = payload.get("attachment", "")
        elif msg_type == "link":
            title = payload.get("title", "")
            url = payload.get("url", "")
            desc = payload.get("description", "")
            content = f"[{title}]({url})" if url else title
            if desc:
                content += f"\n\n> {desc}"

        if content or attachment:
            note_save.execute({"content": content, "attachment": attachment}, state, ctx)
    except Exception as e:
        logger.error("[Brain] Quick-Notes write failed (non-blocking): %s", e)


def _flash_filter_and_save(payload, state, ctx, primary_skill):
    """回复后异步执行：用 Flash 判断消息是否值得写入 Quick-Notes"""
    text = _extract_user_text(payload)
    if not text or not text.strip():
        return
    try:
        result = call_llm([
            {"role": "system", "content": prompts.FLASH_NOTE_FILTER},
            {"role": "user", "content": text}
        ], model_tier="flash", max_tokens=5, temperature=0)
        should_save = result and result.strip().upper().startswith("YES")
        if should_save:
            _save_to_quick_notes(payload, state, ctx)
            logger.info("[Brain][NoteFilter] Flash decided save: skill=%s, text=%s...", primary_skill, text[:40])
        else:
            logger.info("[Brain][NoteFilter] Flash decided skip: skill=%s, text=%s...", primary_skill, text[:40])
    except Exception as e:
        logger.warning("[Brain][NoteFilter] Flash filter failed, fallback save: %s", e)
        _save_to_quick_notes(payload, state, ctx)


# ============ 辅助函数 ============

def _extract_user_text(payload):
    """从 payload 中提取用户文本"""
    msg_type = payload.get("type", "")
    if msg_type == "text":
        return payload.get("text", "")
    elif msg_type == "voice":
        return f"[语音] {payload.get('text', '')}"
    elif msg_type == "image":
        desc = payload.get("image_description", "")
        return f"[图片] {desc}" if desc else "[图片]"
    elif msg_type == "video":
        return "[视频]"
    elif msg_type == "link":
        return f"[链接] {payload.get('title', '')}"
    return ""


def _parse_llm_output(text):
    """解析 LLM 输出的 JSON（容错处理）"""
    text = text.strip()

    if "<think>" in text:
        think_end = text.find("</think>")
        if think_end >= 0:
            text = text[think_end + len("</think>"):].strip()
        else:
            text = text.replace("<think>", "").strip()

    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    logger.warning("[Brain] cannot parse JSON: %s", text[:200])
    return None
