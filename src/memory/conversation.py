# -*- coding: utf-8 -*-
"""
对话窗口管理 + 长期记忆更新模块。

包含：
- 对话消息的格式化、追加和压缩
- memory.md 的增量更新（apply_memory_updates）
"""
from datetime import datetime

from config import RECENT_MESSAGES_LIMIT
from infra.logging import BEIJING_TZ, get_logger
from memory.prompt_cache import get_prompt_cache

logger = get_logger(__name__)


# ============ 对话窗口管理 ============

def format_recent_messages(state):
    """从 state 中提取最近 N 条消息，格式化为文本。"""
    recent = state.get("recent_messages", [])[-RECENT_MESSAGES_LIMIT:]
    if not recent:
        return "（暂无最近对话）"

    lines = []
    for m in recent:
        role_val = m.get("role", "")
        if role_val == "system":
            lines.append(m.get("content", ""))
            continue
        role = "用户" if role_val == "user" else "Karvis"
        t = m.get("time", "")
        content = m.get("content", "")
        if len(content) > 150:
            content = content[:150] + "..."
        lines.append(f"[{t}] {role}: {content}")
    return "\n".join(lines)


def add_message_to_state(state, role, content):
    """往 state 的对话窗口中追加一条消息。"""
    now_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")

    messages = state.setdefault("recent_messages", [])
    messages.append({
        "role": role,
        "content": content[:300],
        "time": now_str
    })

    if len(messages) > RECENT_MESSAGES_LIMIT:
        state["recent_messages"] = maybe_compress_messages(messages)


def maybe_compress_messages(messages):
    """对话压缩：保留最近 6 条原始消息，旧消息压缩为摘要（每条 100 字，总上限 800 字）。"""
    COMPRESS_KEEP_RECENT = 6  # 保留最近 6 条原始消息

    if len(messages) <= RECENT_MESSAGES_LIMIT:
        return messages

    to_compress = messages[:-COMPRESS_KEEP_RECENT]
    to_keep = messages[-COMPRESS_KEEP_RECENT:]

    summary_parts = []
    for m in to_compress:
        if m.get("role") == "system" and m.get("content", "").startswith("[对话摘要]"):
            summary_parts.append(m["content"])
            continue
        role = "用户" if m.get("role") == "user" else "Karvis"
        content = m.get("content", "")
        # 截取关键部分（保留足够语义）
        if len(content) > 100:
            content = content[:100] + "..."
        summary_parts.append(f"{role}: {content}")

    time_range = ""
    if to_compress:
        first_time = to_compress[0].get("time", "")
        last_time = to_compress[-1].get("time", "")
        if first_time and last_time:
            time_range = f"({first_time} ~ {last_time})"

    summary_text = f"[对话摘要] {time_range} " + " | ".join(summary_parts)
    if len(summary_text) > 800:
        summary_text = summary_text[:800] + "..."

    summary_msg = {
        "role": "system",
        "content": summary_text,
        "time": to_compress[-1].get("time", "") if to_compress else ""
    }

    result = [summary_msg] + to_keep
    logger.info("[记忆] 对话压缩: %d 条 → 1 条摘要, 保留 %d 条原始", len(to_compress), len(to_keep))
    return result


# ============ 长期记忆更新 ============

def apply_memory_updates(updates, ctx):
    """将 LLM 返回的 memory_updates 应用到该用户的 memory.md。"""
    if not updates:
        return

    memory_file = ctx.memory_file
    memory_text = ctx.IO.read_text(memory_file)
    if memory_text is None:
        logger.warning("[记忆] 无法读取 memory.md (%s)，跳过记忆更新", ctx.user_id)
        return

    changed = False
    for item in updates:
        if isinstance(item, str):
            logger.warning("[记忆] 跳过非法格式的 memory_update: %s", item[:50])
            continue
        if not isinstance(item, dict):
            continue
        section = item.get("section", "")
        action = item.get("action", "add")
        content = item.get("content", "")
        if not section or not content:
            continue

        section_header = f"## {section}"

        if action == "delete":
            if section_header not in memory_text:
                continue
            parts = memory_text.split(section_header, 1)
            before = parts[0]
            after = parts[1]
            next_idx = after.find("\n## ")
            section_body = after[:next_idx] if next_idx >= 0 else after
            rest = after[next_idx:] if next_idx >= 0 else ""
            keyword = content.lower()
            lines = section_body.split("\n")
            new_lines = [l for l in lines if keyword not in l.lower()]
            if len(new_lines) != len(lines):
                memory_text = before + section_header + "\n".join(new_lines) + rest
                changed = True
                logger.info("[记忆] 删除: section=%s, keyword=%s", section, content)
            continue

        if section_header in memory_text:
            if action == "add":
                dedup_key = content.split(":")[0].strip().lower() if ":" in content else content[:10].lower()
                parts = memory_text.split(section_header, 1)
                before = parts[0]
                after = parts[1]
                next_idx = after.find("\n## ")
                section_body = after[:next_idx] if next_idx >= 0 else after
                rest = after[next_idx:] if next_idx >= 0 else ""

                existing_lines = section_body.lower()
                if dedup_key in existing_lines:
                    logger.debug("[记忆] 去重跳过: section=%s, key=%s", section, dedup_key)
                    continue

                memory_text = before + section_header + section_body.rstrip() + f"\n- {content}\n" + rest
                changed = True
            elif action == "update":
                parts = memory_text.split(section_header, 1)
                before = parts[0]
                after = parts[1]
                next_idx = after.find("\n## ")
                if next_idx >= 0:
                    rest = after[next_idx:]
                    memory_text = before + section_header + f"\n- {content}\n" + rest
                else:
                    memory_text = before + section_header + f"\n- {content}\n"
                changed = True
        else:
            memory_text = memory_text.rstrip() + f"\n\n{section_header}\n- {content}\n"
            changed = True

    if changed:
        ok = ctx.IO.write_text(memory_file, memory_text)
        if ok:
            logger.info("[记忆] memory.md 已更新 (%s): %d 条", ctx.user_id, len(updates))
            get_prompt_cache().invalidate(memory_file)
        else:
            logger.error("[记忆] memory.md 写入失败 (%s)", ctx.user_id)
