# -*- coding: utf-8 -*-
"""
Skill: feishu.docs.read / feishu.docs.write
读写飞书知识库（Wiki）/云盘（Drive）中的云文档（Docx）与文本文件。
"""
from __future__ import annotations

import json
import re
import threading
import time

import requests
from requests.adapters import HTTPAdapter

from config import FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_DRIVE_ROOT_FOLDER_TOKEN
from storage.feishu_drive import FeishuDriveIO
from infra.logging import get_logger

logger = get_logger(__name__)

_API_BASE = "https://open.feishu.cn/open-apis"

_feishu_session = requests.Session()
_feishu_adapter = HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=1)
_feishu_session.mount("https://open.feishu.cn", _feishu_adapter)

_token_cache = {"token": None, "expire_time": 0.0}
_token_lock = threading.Lock()

_drive_io = None
_drive_io_lock = threading.Lock()

_sdk_client = None
_sdk_client_lock = threading.Lock()


def _get_sdk_client():
    global _sdk_client
    if _sdk_client is not None:
        return _sdk_client

    with _sdk_client_lock:
        if _sdk_client is not None:
            return _sdk_client
        try:
            import lark_oapi as lark  # type: ignore
        except Exception:
            return None

        if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
            return None

        _sdk_client = (
            lark.Client.builder()
            .app_id(FEISHU_APP_ID)
            .app_secret(FEISHU_APP_SECRET)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )
        return _sdk_client


def _get_tenant_access_token() -> str | None:
    now = time.time()
    if _token_cache["token"] and _token_cache["expire_time"] > now:
        return _token_cache["token"]

    with _token_lock:
        now = time.time()
        if _token_cache["token"] and _token_cache["expire_time"] > now:
            return _token_cache["token"]

        if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
            return None

        url = f"{_API_BASE}/auth/v3/tenant_access_token/internal"
        payload = {"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}
        try:
            resp = _feishu_session.post(url, json=payload, timeout=10)
            result = resp.json()
            if result.get("code") == 0:
                token = result.get("tenant_access_token")
                expire = float(result.get("expire", 7200))
                _token_cache["token"] = token
                _token_cache["expire_time"] = now + expire - 120
                return token
            logger.warning("[feishu.docs] 获取 tenant_access_token 失败: %s", result)
            return None
        except Exception:
            logger.exception("[feishu.docs] 获取 tenant_access_token 异常")
            return None


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}


def _oapi_request_json(
    method: str,
    uri: str,
    *,
    token: str | None = None,
    queries: list[tuple[str, str]] | None = None,
    body: dict | None = None,
    timeout: int = 15,
    retries: int = 3,
) -> dict:
    method = method.upper().strip()
    uri = uri if uri.startswith("/open-apis/") else f"/open-apis/{uri.lstrip('/')}"
    queries = queries or []

    client = _get_sdk_client()
    if client is not None:
        import lark_oapi as lark  # type: ignore

        http_method_map = {
            "GET": lark.HttpMethod.GET,
            "POST": lark.HttpMethod.POST,
            "PATCH": lark.HttpMethod.PATCH,
            "PUT": lark.HttpMethod.PUT,
            "DELETE": lark.HttpMethod.DELETE,
        }
        http_method = http_method_map.get(method)
        if http_method is None:
            raise ValueError(f"unsupported method: {method}")

        last_err = None
        for attempt in range(1, retries + 1):
            try:
                req = (
                    lark.BaseRequest.builder()
                    .http_method(http_method)
                    .uri(uri)
                    .token_types({lark.AccessTokenType.TENANT})
                    .queries(queries)
                    .body(body)
                    .build()
                )
                resp = client.request(req)
                if not resp.success():
                    last_err = RuntimeError(f"code={resp.code}, msg={resp.msg}, log_id={resp.get_log_id()}")
                    if resp.code in (99991400,) and attempt < retries:
                        time.sleep(0.5 * (2 ** (attempt - 1)))
                        continue
                    raise last_err

                raw = getattr(resp.raw, "content", b"") or b"{}"
                return json.loads(raw.decode("utf-8"))
            except Exception as e:
                last_err = e
                if attempt < retries:
                    time.sleep(0.5 * (2 ** (attempt - 1)))
                    continue
                raise

        raise last_err or RuntimeError("request failed")

    if not token:
        raise RuntimeError("missing token and sdk not available")

    url = f"{_API_BASE}{uri}"
    headers = _auth_headers(token)
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = _feishu_session.request(method, url, headers=headers, params=dict(queries), json=body, timeout=timeout)
            if resp.status_code in (429, 400) and attempt < retries:
                time.sleep(0.5 * (2 ** (attempt - 1)))
                continue
            return resp.json()
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.5 * (2 ** (attempt - 1)))
                continue
            raise
    raise last_err or RuntimeError("request failed")


def _get_drive_io() -> FeishuDriveIO | None:
    global _drive_io
    if _drive_io is not None:
        return _drive_io
    with _drive_io_lock:
        if _drive_io is not None:
            return _drive_io
        if not FEISHU_APP_ID or not FEISHU_APP_SECRET or not FEISHU_DRIVE_ROOT_FOLDER_TOKEN:
            return None
        _drive_io = FeishuDriveIO(
            {
                "app_id": FEISHU_APP_ID,
                "app_secret": FEISHU_APP_SECRET,
                "root_folder_token": FEISHU_DRIVE_ROOT_FOLDER_TOKEN,
            }
        )
        return _drive_io


def _parse_feishu_url(url: str) -> dict:
    url = (url or "").strip()
    if not url:
        return {}

    m = re.search(r"/wiki/([a-zA-Z0-9]+)", url)
    if m:
        return {"source": "wiki", "wiki_node_token": m.group(1)}

    m = re.search(r"/docx/([a-zA-Z0-9]+)", url)
    if m:
        return {"source": "docx", "document_id": m.group(1)}

    m = re.search(r"document_id=([a-zA-Z0-9]+)", url)
    if m:
        return {"source": "docx", "document_id": m.group(1)}

    return {"url": url}


def _wiki_to_docx_document_id(wiki_node_token: str, token: str) -> str | None:
    wiki_node_token = (wiki_node_token or "").strip()
    if not wiki_node_token:
        return None

    try:
        result = _oapi_request_json(
            "GET",
            "/open-apis/wiki/v2/spaces/get_node",
            token=token,
            queries=[("token", wiki_node_token), ("obj_type", "wiki")],
        )
        if result.get("code") != 0:
            logger.warning("[feishu.docs] wiki get_node 失败: %s", result)
            return None
        node = (result.get("data") or {}).get("node") or {}
        obj_type = node.get("obj_type") or node.get("objType")
        obj_token = node.get("obj_token") or node.get("objToken")
        if obj_type != "docx":
            logger.warning("[feishu.docs] wiki 节点非 docx: obj_type=%s", obj_type)
            return None
        return obj_token
    except Exception:
        logger.exception("[feishu.docs] wiki get_node 异常")
        return None


def _docx_get_document_title(document_id: str, token: str) -> str:
    try:
        result = _oapi_request_json("GET", f"/open-apis/docx/v1/documents/{document_id}", token=token)
        if result.get("code") == 0:
            doc = (result.get("data") or {}).get("document") or {}
            return (doc.get("title") or "").strip()
        return ""
    except Exception:
        return ""


def _docx_list_children(document_id: str, block_id: str, token: str, page_size: int = 200) -> list[dict]:
    page_token = ""
    out = []

    while True:
        q = [("page_size", str(page_size))]
        if page_token:
            q.append(("page_token", page_token))
        result = _oapi_request_json(
            "GET",
            f"/open-apis/docx/v1/documents/{document_id}/blocks/{block_id}/children",
            token=token,
            queries=q,
        )
        if result.get("code") != 0:
            raise RuntimeError(result.get("msg") or "docx list children failed")
        data = result.get("data") or {}
        out.extend((data.get("items") or data.get("children") or []))
        page_token = data.get("page_token") or data.get("next_page_token") or ""
        if not page_token:
            break

    return out


_TEXT_KEYS = (
    "text",
    "heading1",
    "heading2",
    "heading3",
    "heading4",
    "heading5",
    "heading6",
    "heading7",
    "heading8",
    "heading9",
    "bullet",
    "ordered",
    "todo",
    "quote",
    "code",
)


def _extract_text_from_block(block: dict) -> str:
    for key in _TEXT_KEYS:
        data = block.get(key)
        if not isinstance(data, dict):
            continue
        elements = data.get("elements")
        if not isinstance(elements, list):
            continue
        parts = []
        for el in elements:
            if not isinstance(el, dict):
                continue
            tr = el.get("text_run") or el.get("textRun") or {}
            if isinstance(tr, dict):
                c = tr.get("content")
                if c:
                    parts.append(c)
        text = "".join(parts).strip()
        if text:
            return text
    return ""


def _docx_read_plain_text(document_id: str, token: str, max_chars: int = 8000, max_blocks: int = 800) -> str:
    max_chars = max(200, int(max_chars or 8000))
    max_blocks = max(50, int(max_blocks or 800))

    queue = [document_id]
    seen = set()
    lines: list[str] = []

    while queue and len(seen) < max_blocks and sum(len(x) for x in lines) < max_chars:
        parent_id = queue.pop(0)
        if parent_id in seen:
            continue
        seen.add(parent_id)

        try:
            children = _docx_list_children(document_id, parent_id, token)
        except Exception as e:
            logger.warning("[feishu.docs] docx 读取子块失败: %s", e)
            continue

        for child in children:
            if not isinstance(child, dict):
                continue
            bid = child.get("block_id") or child.get("blockId") or ""
            if bid:
                queue.append(bid)
            t = _extract_text_from_block(child)
            if t:
                lines.append(t)
            if len(seen) >= max_blocks or sum(len(x) for x in lines) >= max_chars:
                break

    text = "\n".join(lines).strip()
    if len(text) > max_chars:
        return text[: max_chars - 1] + "…"
    return text


def _docx_append_text(document_id: str, token: str, content: str) -> tuple[bool, str]:
    content = (content or "").rstrip()
    if not content:
        return False, "没有可写入的内容"

    paragraphs = [p.strip() for p in content.splitlines()]
    paragraphs = [p for p in paragraphs if p]
    if not paragraphs:
        return False, "没有可写入的内容"

    children = []
    for p in paragraphs[:200]:
        children.append({"block_type": 2, "text": {"elements": [{"text_run": {"content": p}}]}})

    payload = {"index": -1, "children": children}
    try:
        result = _oapi_request_json(
            "POST",
            f"/open-apis/docx/v1/documents/{document_id}/blocks/{document_id}/children",
            token=token,
            body=payload,
            timeout=20,
        )
        if result.get("code") == 0:
            return True, ""
        logger.warning("[feishu.docs] docx 写入失败: %s", result)
        return False, result.get("msg") or "写入失败"
    except Exception:
        logger.exception("[feishu.docs] docx 写入异常")
        return False, "写入异常"


def _docx_patch_text(document_id: str, block_id: str, token: str, content: str) -> tuple[bool, str]:
    content = (content or "").strip()
    if not content:
        return False, "没有可写入的内容"

    payload = {"update_text_elements": {"elements": [{"text_run": {"content": content}}]}}
    try:
        result = _oapi_request_json(
            "PATCH",
            f"/open-apis/docx/v1/documents/{document_id}/blocks/{block_id}",
            token=token,
            body=payload,
            timeout=20,
        )
        if result.get("code") == 0:
            return True, ""
        logger.warning("[feishu.docs] docx patch 失败: %s", result)
        return False, result.get("msg") or "更新失败"
    except Exception:
        logger.exception("[feishu.docs] docx patch 异常")
        return False, "更新异常"


def read(params, state, ctx):
    source = (params.get("source") or "").strip().lower()
    url = (params.get("url") or "").strip()
    document_id = (params.get("document_id") or "").strip()
    wiki_node_token = (params.get("wiki_node_token") or "").strip()
    file_path = (params.get("file_path") or "").strip()
    max_chars = params.get("max_chars", 8000)
    max_blocks = params.get("max_blocks", 800)

    parsed = _parse_feishu_url(url)
    source = source or (parsed.get("source") or "")
    document_id = document_id or (parsed.get("document_id") or "")
    wiki_node_token = wiki_node_token or (parsed.get("wiki_node_token") or "")

    if source in ("drive", "feishu_drive"):
        drive = _get_drive_io()
        if not drive:
            return {
                "success": False,
                "reply": "飞书云盘未配置：请设置 FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_DRIVE_ROOT_FOLDER_TOKEN",
            }
        if not file_path:
            return {"success": False, "reply": "缺少 file_path（例如 00-Inbox/Quick-Notes.md）"}
        text = drive.read_text(file_path)
        if text is None:
            return {"success": False, "reply": "读取失败（稍后重试）"}
        preview = text.strip()
        if not preview:
            return {"success": True, "reply": "文件为空或不存在"}
        if len(preview) > int(max_chars):
            preview = preview[: int(max_chars) - 1] + "…"
        return {"success": True, "reply": preview}

    token = _get_tenant_access_token()
    if not token:
        return {"success": False, "reply": "飞书鉴权未配置或 token 获取失败（检查 FEISHU_APP_ID/FEISHU_APP_SECRET）"}

    if source in ("wiki",) and not document_id:
        document_id = _wiki_to_docx_document_id(wiki_node_token, token)
        if not document_id:
            return {"success": False, "reply": "无法从该知识库节点解析到 docx 文档（检查权限或 token）"}

    if source in ("docx", "wiki"):
        if not document_id:
            return {"success": False, "reply": "缺少 document_id 或 wiki_node_token/url"}
        title = _docx_get_document_title(document_id, token)
        text = _docx_read_plain_text(document_id, token, max_chars=max_chars, max_blocks=max_blocks)
        if not text:
            return {"success": True, "reply": f"已读取「{title or document_id}」，但未提取到可读文本（可能主要是表格/图片/嵌入块）"}
        header = f"「{title}」\n\n" if title else ""
        return {"success": True, "reply": header + text}

    return {"success": False, "reply": "不支持的 source（请使用 docx/wiki/drive）"}


def write(params, state, ctx):
    source = (params.get("source") or "").strip().lower()
    url = (params.get("url") or "").strip()
    document_id = (params.get("document_id") or "").strip()
    wiki_node_token = (params.get("wiki_node_token") or "").strip()
    block_id = (params.get("block_id") or "").strip()
    file_path = (params.get("file_path") or "").strip()
    content = params.get("content") or ""
    mode = (params.get("mode") or "append").strip().lower()

    parsed = _parse_feishu_url(url)
    source = source or (parsed.get("source") or "")
    document_id = document_id or (parsed.get("document_id") or "")
    wiki_node_token = wiki_node_token or (parsed.get("wiki_node_token") or "")

    if source in ("drive", "feishu_drive"):
        drive = _get_drive_io()
        if not drive:
            return {
                "success": False,
                "reply": "飞书云盘未配置：请设置 FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_DRIVE_ROOT_FOLDER_TOKEN",
            }
        if not file_path:
            return {"success": False, "reply": "缺少 file_path（例如 00-Inbox/Quick-Notes.md）"}
        content = (content or "").strip()
        if not content:
            return {"success": False, "reply": "没有可写入的内容"}
        if mode == "replace":
            ok = drive.write_text(file_path, content)
        else:
            existing = drive.read_text(file_path)
            if existing is None:
                return {"success": False, "reply": "读取失败（稍后重试）"}
            new_content = (existing.rstrip() + "\n\n" + content + "\n") if existing.strip() else (content + "\n")
            ok = drive.write_text(file_path, new_content)
        return {"success": True} if ok else {"success": False, "reply": "写入失败（稍后重试）"}

    token = _get_tenant_access_token()
    if not token:
        return {"success": False, "reply": "飞书鉴权未配置或 token 获取失败（检查 FEISHU_APP_ID/FEISHU_APP_SECRET）"}

    if source in ("wiki",) and not document_id:
        document_id = _wiki_to_docx_document_id(wiki_node_token, token)
        if not document_id:
            return {"success": False, "reply": "无法从该知识库节点解析到 docx 文档（检查权限或 token）"}

    if source in ("docx", "wiki"):
        if not document_id:
            return {"success": False, "reply": "缺少 document_id 或 wiki_node_token/url"}
        if mode == "append":
            ok, err = _docx_append_text(document_id, token, str(content))
        elif mode in ("patch", "update", "modify"):
            if not block_id:
                return {"success": False, "reply": "缺少 block_id（需要指定要更新的块）"}
            ok, err = _docx_patch_text(document_id, block_id, token, str(content))
        else:
            return {"success": False, "reply": "docx/wiki 仅支持 mode=append 或 mode=patch"}
        if ok:
            return {"success": True}
        return {"success": False, "reply": err}

    return {"success": False, "reply": "不支持的 source（请使用 docx/wiki/drive）"}


SKILL_REGISTRY = {
    "feishu.docs.read": {"handler": read, "visibility": "public"},
    "feishu.docs.write": {"handler": write, "visibility": "public"},
}
