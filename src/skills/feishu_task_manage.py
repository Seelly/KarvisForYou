# -*- coding: utf-8 -*-
"""
Skill: feishu.task.confirm
用于在存在多个飞书任务候选时，确认并执行完成操作。
"""
from infra.logging import get_logger
from integrations.feishu_task import feishu_task_client

logger = get_logger(__name__)


def confirm(params, state, ctx):
    index = params.get("index")
    task_guid = (params.get("task_guid") or "").strip()

    pending = state.get("feishu_task_pending") or {}
    candidates = pending.get("candidates") or []
    if not candidates:
        return {"success": False, "reply": "当前没有待确认的飞书任务"}

    if not feishu_task_client.is_enabled():
        return {"success": False, "reply": "未启用飞书任务同步（缺少 FEISHU_TASK_LIST_ID）"}

    chosen_guid = ""
    if task_guid:
        chosen_guid = task_guid
    else:
        try:
            idx = int(index)
        except Exception:
            return {"success": False, "reply": "请提供要完成的序号（index）"}
        if idx < 1 or idx > len(candidates):
            return {"success": False, "reply": f"序号超出范围（1-{len(candidates)}）"}
        chosen_guid = str(candidates[idx - 1].get("guid") or "")

    if not chosen_guid:
        return {"success": False, "reply": "选中的任务缺少 guid"}

    ok = feishu_task_client.complete_task_by_guid(chosen_guid)
    if ok:
        state.pop("feishu_task_pending", None)
        return {"success": True, "reply": "已在飞书任务中标记完成 ✅"}

    logger.warning("飞书任务完成失败: %s", chosen_guid)
    return {"success": False, "reply": "飞书任务标记完成失败（稍后重试）"}


SKILL_REGISTRY = {
    "feishu.task.confirm": {"handler": confirm, "visibility": "public"},
}

