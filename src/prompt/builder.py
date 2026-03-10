# -*- coding: utf-8 -*-
"""
Prompt 组装器 — 负责将模板、上下文、规则拼装成最终的 System/User Prompt。

从 brain.py 抽离的函数：
- _build_time_string   → build_time_string
- _select_rules        → select_rules
- build_system_prompt  → build_system_prompt
- _build_state_summary → build_state_summary
- _build_user_message  → build_user_message
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

try:
    import cnlunar
    _HAS_CNLUNAR = True
except ImportError:
    _HAS_CNLUNAR = False

from infra.logging import get_logger
from memory import load_memory, format_recent_messages
from skill_loader import get_skills_for_prompt
import prompt.templates as prompts

logger = get_logger(__name__)

_WEEKDAY_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


# ============ 时间字符串 ============

def build_time_string(now_bj) -> str:
    """构建包含公历+农历的时间字符串，注入到 System Prompt。

    示例输出:
      2026-02-19 20:30 星期四 | 农历正月初三 | 春节假期 | 雨水
    """
    base = now_bj.strftime("%Y-%m-%d %H:%M")
    weekday = _WEEKDAY_CN[now_bj.weekday()]
    parts = [f"{base} {weekday}"]

    if _HAS_CNLUNAR:
        try:
            lunar = cnlunar.Lunar(now_bj.replace(tzinfo=None), godType='8char')
            # 农历日期
            parts.append(f"农历{lunar.lunarMonthCn}{lunar.lunarDayCn}")
            # 农历节日（如 春节、元宵、端午 等）
            festivals = []
            if lunar.lunarFestival:
                festivals.append(lunar.lunarFestival)
            if lunar.solarFestival:
                festivals.append(lunar.solarFestival)
            if festivals:
                parts.append("、".join(festivals))
            # 节气（当天恰逢节气才有值）
            st = lunar.todaySolarTerms
            if st and st != "无":
                parts.append(f"节气：{st}")
        except Exception as e:
            logger.debug("cnlunar 解析异常: %s", e)

    return " | ".join(parts)


# ============ 规则选择 ============

def select_rules(state, payload=None, ctx=None) -> list[str]:
    """根据 payload.type / state / 用户文本，选择需要注入的 RULES 分段。

    方案 C: system 类型请求只注入 RULES_SYSTEM_TASKS（不含 SKILLS 和用户交互 RULES）
    方案 A: 用户消息根据 state 和关键词动态注入分段，RULES_CORE 始终注入
    V12: 管理员额外注入 RULES_FINANCE，所有用户注入 RULES_SKILLS_MGMT
    """
    # 方案 C: 定时任务走精简 prompt
    if payload and payload.get("type") == "system":
        return [prompts.RULES_SYSTEM_TASKS]

    # 方案 A: 用户消息 — CORE 始终注入，其余按需
    segments = [prompts.RULES_CORE]

    user_text = (payload.get("text", "") if payload else "").lower() if payload else ""

    # 读书/影视：仅关键词触发
    _BOOKS_KW = ("看了", "读了", "推荐", "这本书", "书摘", "金句", "总结一下",
                 "在读", "在看", "电影", "剧", "纪录片", "动画", "影视")
    if any(kw in user_text for kw in _BOOKS_KW):
        segments.append(prompts.RULES_BOOKS_MEDIA)

    # 习惯/Top3：仅关键词触发
    _HABITS_KW = ("实验", "习惯", "top 3", "top3", "今天要做", "今天的目标",
                  "今天最重要")
    if any(kw in user_text for kw in _HABITS_KW):
        segments.append(prompts.RULES_HABITS)

    # 高级功能：语音 + 关键词触发
    _ADV_KW = ("要不要", "纠结", "犹豫", "决定了", "决策", "复盘",
               "回顾", "分析", "梳理", "深潜", "盘点", "之前写过",
               "帮我看看", "文件里")
    is_voice = payload.get("type") == "voice" if payload else False
    if is_voice or any(kw in user_text for kw in _ADV_KW):
        segments.append(prompts.RULES_ADVANCED)

    # V12: 财务规则 — 仅管理员且包含财务关键词时注入
    if ctx and ctx.is_admin:
        _FINANCE_KW = ("花了多少", "收支", "资产", "财务", "账单", "导入", "净值",
                       "财报", "月度报告", "快照")
        if any(kw in user_text for kw in _FINANCE_KW):
            segments.append(prompts.RULES_FINANCE)

    # V12: Skill 管理规则 — 关键词触发
    _SKILLS_KW = ("功能", "技能", "skill", "开启", "关闭", "禁用", "启用",
                  "关掉", "打开")
    if any(kw in user_text for kw in _SKILLS_KW):
        segments.append(prompts.RULES_SKILLS_MGMT)

    return segments


# ============ State 摘要 ============

def build_state_summary(state) -> str:
    """从 state 中提取关键信息，构建给 LLM 看的摘要"""
    beijing_tz = timezone(timedelta(hours=8))
    parts = []

    # 打卡状态
    if state.get("checkin_pending"):
        step = state.get("checkin_step", 0)
        questions = [
            "今天做了什么？",
            "今天状态打几分？(1-10)",
            "什么事让你纠结？",
            "脑子里最常冒出的念头是什么？"
        ]
        q = questions[step - 1] if 1 <= step <= 4 else "未知"
        parts.append(f"打卡进行中: 第 {step}/4 题, 当前问题: \"{q}\"")
        answers = state.get("checkin_answers", [])
        if answers:
            parts.append(f"已回答 {len(answers)} 题")
    else:
        parts.append("未在打卡")

    # 深度自问状态
    if state.get("reflect_pending"):
        reflect_q = state.get("reflect_question", "")
        reflect_cat = state.get("reflect_category", "")
        parts.append(f"深度自问进行中: [{reflect_cat}] \"{reflect_q}\"")

    # 活跃书籍/影视
    active_book = state.get("active_book", "")
    if active_book:
        parts.append(f"正在读: 《{active_book}》")

    active_media = state.get("active_media", "")
    if active_media:
        parts.append(f"正在看: 《{active_media}》")

    # V3-F12: 每日 Top 3
    daily_top3 = state.get("daily_top3", {})
    if daily_top3 and daily_top3.get("items"):
        today_str = datetime.now(beijing_tz).strftime("%Y-%m-%d")
        top3_date = daily_top3.get("date", "")
        items = daily_top3["items"]
        items_str = " / ".join(
            f"{'✅' if i.get('done') else '⬜'} {i.get('text', '')}"
            for i in items
        )
        if top3_date == today_str:
            parts.append(f"今日 Top 3: {items_str}")
        else:
            parts.append(f"昨日({top3_date}) Top 3: {items_str}")

    # V3-F11: 活跃实验
    exp = state.get("active_experiment")
    if exp and exp.get("status") == "active":
        tracking = exp.get("tracking", {})
        triggers_str = "、".join(exp.get("triggers", [])[:3]) if exp.get("triggers") else ""
        parts.append(
            f"活跃实验: 「{exp.get('name', '')}」"
            f"(触发词: {triggers_str}, "
            f"触发{tracking.get('trigger_count', 0)}次/"
            f"接受{tracking.get('accepted_count', 0)}次)"
        )

    # V3-F15: 待复盘决策
    pending_decisions = state.get("pending_decisions", [])
    unreviewed = [d for d in pending_decisions if not d.get("result")]
    if unreviewed:
        today_str = datetime.now(beijing_tz).strftime("%Y-%m-%d")
        due = [d for d in unreviewed if d.get("review_date", "9999") <= today_str]
        if due:
            topics = "、".join(f"「{d.get('topic', '')}」" for d in due[:3])
            parts.append(f"到期待复盘决策: {topics}")
        elif len(unreviewed) <= 3:
            topics = "、".join(f"「{d.get('topic', '')}」" for d in unreviewed)
            parts.append(f"待复盘决策({len(unreviewed)}): {topics}")
        else:
            parts.append(f"待复盘决策: {len(unreviewed)} 个")

    return "\n".join(parts) if parts else "无特殊状态"


# ============ System Prompt 组装 ============

def build_system_prompt(state, ctx, prompt_futs=None, payload=None) -> str:
    """组装完整的 System Prompt（多用户版，支持用户自定义 SOUL）

    prompt_futs: 可选，外部提前提交的 {"mem": Future} dict，用于与 state 读取并行
    payload: 可选，当前请求 payload，用于条件 RULES 注入
    """
    beijing_tz = timezone(timedelta(hours=8))
    now_bj = datetime.now(beijing_tz)
    current_time = build_time_string(now_bj)

    # memory 从该用户的文件加载
    if prompt_futs and "mem" in prompt_futs:
        mem = prompt_futs["mem"].result()
    else:
        mem = load_memory(ctx)

    recent = format_recent_messages(state)
    state_summary = build_state_summary(state)

    # SOUL 支持用户自定义覆写，并根据 storage_mode 动态替换存储名称
    storage_name = prompts.get_storage_display_name(getattr(ctx, 'storage_mode', 'local'))
    soul = prompts.SOUL.replace("{storage_name}", storage_name)
    soul_override = ctx.get_soul_override()
    if soul_override:
        soul += f"\n\n## 用户自定义\n{soul_override}"
    nickname = ctx.get_nickname()
    if nickname:
        soul += f"\n- 称呼用户为「{nickname}」"
    ai_name = ctx.get_user_config().get("ai_name", "")
    if ai_name:
        soul += f"\n- 用户给你起了昵称「{ai_name}」，在合适的时候可以用这个名字自称"

    # 方案 C+A: 条件注入 RULES（V12: 传入 ctx 用于 admin 判断）
    is_system = payload and payload.get("type") == "system"
    rules_segments = select_rules(state, payload, ctx=ctx)
    rules_text = "\n\n".join(rules_segments)

    # V12: system 类型不注入 SKILLS；其他场景根据用户权限动态生成
    if is_system:
        skills_block = ""
    else:
        allowed_names = get_skills_for_prompt(ctx)
        skills_block = prompts.build_skills_prompt(allowed_names)

    parts = [soul,
             f"\n## 长期记忆\n{mem}",
             f"\n## 最近对话\n{recent}",
             f"\n## 当前状态\n{state_summary}",
             f"\n## 当前时间\n{current_time}"]
    if skills_block:
        parts.append(f"\n{skills_block}")
    parts.append(f"\n{rules_text}")
    parts.append(f"\n{prompts.OUTPUT_FORMAT}")

    return "\n".join(parts)


# ============ User Message 构建 ============

def build_user_message(payload) -> str:
    """构建发给 LLM 的 user message"""
    msg_type = payload.get("type", "")

    if msg_type == "text":
        data = {"type": "text", "text": payload.get("text", "")}
        page_content = payload.get("page_content", "")
        if page_content:
            data["page_content"] = page_content
            detected_url = payload.get("detected_url", "")
            if detected_url:
                data["detected_url"] = detected_url
        return json.dumps(data, ensure_ascii=False)

    elif msg_type == "voice":
        asr_text = payload.get("text", "")
        return json.dumps({
            "type": "voice",
            "asr_text": asr_text,
            "text_length": len(asr_text),
            "attachment": payload.get("attachment", "")
        }, ensure_ascii=False)

    elif msg_type == "image":
        data = {
            "type": "image",
            "attachment": payload.get("attachment", "")
        }
        image_desc = payload.get("image_description", "")
        if image_desc:
            data["image_description"] = image_desc
        return json.dumps(data, ensure_ascii=False)

    elif msg_type == "video":
        return json.dumps({
            "type": "video",
            "attachment": payload.get("attachment", "")
        }, ensure_ascii=False)

    elif msg_type == "link":
        data = {
            "type": "link",
            "title": payload.get("title", ""),
            "url": payload.get("url", ""),
            "description": payload.get("description", "")
        }
        page_content = payload.get("content", "")
        if page_content:
            data["page_content"] = page_content
        return json.dumps(data, ensure_ascii=False)

    elif msg_type == "system":
        msg = {
            "type": "system",
            "action": payload.get("action", "")
        }
        context = payload.get("context", {})
        if context:
            msg["context"] = context
        return json.dumps(msg, ensure_ascii=False)

    return json.dumps(payload, ensure_ascii=False)
