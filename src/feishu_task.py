# -*- coding: utf-8 -*-
"""
Feishu Task V2 Integration
用于同步 TODO 待办到飞书任务
https://open.feishu.cn/document/task-v2/overview
"""
from __future__ import annotations

import json
import threading
import time

import lark_oapi as lark

from config import FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_TASK_LIST_ID
from log_utils import get_logger

logger = get_logger(__name__)


class FeishuTaskClient:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(FeishuTaskClient, cls).__new__(cls)
                cls._instance._client = None
                cls._instance._client_lock = threading.Lock()
        return cls._instance

    def is_enabled(self) -> bool:
        return bool(FEISHU_APP_ID and FEISHU_APP_SECRET and FEISHU_TASK_LIST_ID)

    def _get_client(self) -> lark.Client | None:
        if not self.is_enabled():
            return None

        if self._client is not None:
            return self._client

        with self._client_lock:
            if self._client is not None:
                return self._client
            self._client = (
                lark.Client.builder()
                .app_id(FEISHU_APP_ID)
                .app_secret(FEISHU_APP_SECRET)
                .log_level(lark.LogLevel.WARNING)
                .build()
            )
            return self._client

    def _request(self, method: lark.HttpMethod, uri: str, *, queries=None, body=None) -> dict | None:
        client = self._get_client()
        if client is None:
            return None

        req = (
            lark.BaseRequest.builder()
            .http_method(method)
            .uri(uri)
            .token_types({lark.AccessTokenType.TENANT})
            .queries(queries or [])
            .body(body)
            .build()
        )
        resp: lark.BaseResponse = client.request(req)
        if not resp.success():
            logger.error(
                "[FeishuTask] request failed: code=%s, msg=%s, log_id=%s",
                resp.code,
                resp.msg,
                resp.get_log_id(),
            )
            return None
        raw = getattr(resp.raw, "content", b"") or b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            logger.error("[FeishuTask] response parse failed: %s", raw[:200])
            return None

    def create_task(self, summary, description="", due_timestamp_ms=0, open_id=None):
        if not self.is_enabled():
            return None

        body = {"summary": summary, "description": description}
        if due_timestamp_ms:
            body["due"] = {"timestamp": str(due_timestamp_ms), "is_all_day": False}
        if open_id:
            body["members"] = [{"id": open_id, "type": "user", "role": "assignee"}]

        body["tasklists"] = [{"tasklist_guid": FEISHU_TASK_LIST_ID}]

        result = self._request(
            lark.HttpMethod.POST,
            "/open-apis/task/v2/tasks",
            queries=[("user_id_type", "open_id")],
            body=body,
        )
        if not result:
            return None
        if result.get("code") != 0:
            logger.error("[FeishuTask] Task create failed: %s", result)
            return None
        task = (result.get("data") or {}).get("task") or {}
        guid = task.get("guid") or task.get("task_guid") or ""
        if guid:
            logger.info("[FeishuTask] Task created: %s", guid)
            return guid
        logger.error("[FeishuTask] Task create success but guid missing: %s", result)
        return None

    def list_tasks(self, keyword: str | None = None, page_size: int = 50, max_pages: int = 20) -> list[dict]:
        if not self.is_enabled():
            return []

        keyword_lc = (keyword or "").strip().lower()
        page_token = ""
        out: list[dict] = []

        for _ in range(max_pages):
            queries = [("page_size", str(page_size)), ("user_id_type", "open_id")]
            if page_token:
                queries.append(("page_token", page_token))

            result = self._request(
                lark.HttpMethod.GET,
                f"/open-apis/task/v2/tasklists/{FEISHU_TASK_LIST_ID}/tasks",
                queries=queries,
            )
            if not result or result.get("code") != 0:
                logger.error("[FeishuTask] Tasklist tasks list failed: %s", result)
                break

            data = result.get("data") or {}
            items = data.get("items") or data.get("tasks") or []
            for it in items:
                t = (it.get("task") if isinstance(it, dict) else None) or it
                if not isinstance(t, dict):
                    continue
                if keyword_lc:
                    s = (t.get("summary") or "").lower()
                    d = (t.get("description") or "").lower()
                    if keyword_lc not in s and keyword_lc not in d:
                        continue
                out.append(t)

            page_token = data.get("page_token") or data.get("next_page_token") or ""
            if not page_token:
                break

        return out

    def complete_task_by_guid(self, task_guid: str) -> bool:
        return self.complete_task(task_guid)

    def complete_task_by_summary(self, summary: str):
        if not self.is_enabled():
            return {"success": False, "reply": "未启用飞书任务同步（缺少 FEISHU_TASK_LIST_ID）"}

        kw = (summary or "").strip()
        if not kw:
            return {"success": False, "reply": "缺少 summary"}

        tasks = self.list_tasks(keyword=kw)
        if not tasks:
            return {"success": False, "reply": "未在当前清单中找到匹配任务"}

        exact = [t for t in tasks if (t.get("summary") or "").strip() == kw]
        candidates = exact or tasks

        if len(candidates) == 1:
            guid = candidates[0].get("guid") or candidates[0].get("task_guid") or candidates[0].get("id")
            if not guid:
                return {"success": False, "reply": "匹配到任务但缺少 guid 字段"}
            ok = self.complete_task(str(guid))
            return {"success": ok, "reply": "" if ok else "飞书任务完成失败"}

        compact = []
        for t in candidates[:10]:
            compact.append(
                {
                    "guid": t.get("guid") or t.get("task_guid") or t.get("id"),
                    "summary": t.get("summary") or "",
                    "due": (t.get("due") or {}).get("timestamp") if isinstance(t.get("due"), dict) else t.get("due"),
                }
            )
        return {"success": False, "need_confirm": True, "candidates": compact, "reply": "匹配到多个同名任务"}

    def complete_task(self, task_guid):
        if not self.is_enabled():
            return False

        body = {
            "task": {"completed_at": str(int(time.time() * 1000))},
            "update_fields": ["completed_at"],
        }
        result = self._request(
            lark.HttpMethod.PATCH,
            f"/open-apis/task/v2/tasks/{task_guid}",
            queries=[("user_id_type", "open_id")],
            body=body,
        )
        if not result:
            return False
        if result.get("code") == 0:
            logger.info("[FeishuTask] Task completed: %s", task_guid)
            return True
        logger.error("[FeishuTask] Task complete failed: %s", result)
        return False

    def update_task(self, task_guid, summary=None, description=None, due_timestamp_ms=0):
        if not self.is_enabled():
            return False

        task_payload = {}
        update_fields = []

        if summary is not None:
            task_payload["summary"] = summary
            update_fields.append("summary")

        if description is not None:
            task_payload["description"] = description
            update_fields.append("description")

        if due_timestamp_ms > 0:
            task_payload["due"] = {"timestamp": str(due_timestamp_ms), "is_all_day": False}
            update_fields.append("due")
        elif due_timestamp_ms == -1:
            task_payload["due"] = {"timestamp": "0", "is_all_day": False}
            update_fields.append("due")

        if not update_fields:
            return True

        body = {"task": task_payload, "update_fields": update_fields}
        result = self._request(
            lark.HttpMethod.PATCH,
            f"/open-apis/task/v2/tasks/{task_guid}",
            queries=[("user_id_type", "open_id")],
            body=body,
        )
        if not result:
            return False
        if result.get("code") == 0:
            logger.info("[FeishuTask] Task updated: %s", task_guid)
            return True
        logger.error("[FeishuTask] Task update failed: %s", result)
        return False

    def delete_task(self, task_guid):
        if not self.is_enabled():
            return False

        result = self._request(
            lark.HttpMethod.DELETE,
            f"/open-apis/task/v2/tasks/{task_guid}",
            queries=[("user_id_type", "open_id")],
        )
        if not result:
            return False
        if result.get("code") == 0:
            logger.info("[FeishuTask] Task deleted: %s", task_guid)
            return True
        logger.error("[FeishuTask] Task delete failed: %s", result)
        return False


feishu_task_client = FeishuTaskClient()
