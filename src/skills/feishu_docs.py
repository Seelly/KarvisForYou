# -*- coding: utf-8 -*-
"""
Skill: feishu.docs.read / feishu.docs.write / feishu.docs.create / feishu.wiki.create
读写飞书知识库（Wiki）/云盘（Drive）中的云文档（Docx）与文本文件；支持创建新文档和知识库文档。
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


# ---------------------------------------------------------------------------
# Markdown → 飞书 Docx Block 转换
# ---------------------------------------------------------------------------

# 飞书 Docx block_type 常量
_BT_TEXT = 2       # 普通段落
_BT_H1 = 3
_BT_H2 = 4
_BT_H3 = 5
_BT_H4 = 6
_BT_H5 = 7
_BT_H6 = 8
_BT_H7 = 9
_BT_H8 = 10
_BT_H9 = 11
_BT_BULLET = 12    # 无序列表
_BT_ORDERED = 13   # 有序列表
_BT_CODE = 14      # 代码块
_BT_QUOTE = 15     # 引用
_BT_TODO = 17      # 待办

_HEADING_LEVEL_MAP = {1: _BT_H1, 2: _BT_H2, 3: _BT_H3, 4: _BT_H4, 5: _BT_H5, 6: _BT_H6}

_RE_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
_RE_BULLET = re.compile(r"^[-*+]\s+(.+)$")
_RE_ORDERED = re.compile(r"^\d+[.)\s]\s*(.+)$")
_RE_QUOTE = re.compile(r"^>\s?(.*)$")
_RE_TODO = re.compile(r"^-\s*\[([ xX])\]\s+(.+)$")
_RE_HR = re.compile(r"^([-*_]\s*){3,}$")

# 行内格式：加粗、斜体、行内代码、链接
_RE_INLINE = re.compile(
    r"(\*\*(.+?)\*\*)"           # 加粗 **text**
    r"|(\*(.+?)\*)"               # 斜体 *text*
    r"|(__(.+?)__)"               # 加粗 __text__
    r"|(_(.+?)_)"                 # 斜体 _text_
    r"|(~~(.+?)~~)"               # 删除线 ~~text~~
    r"|(`([^`]+)`)"               # 行内代码 `code`
    r"|(\[([^\]]+)\]\(([^)]+)\))"  # 链接 [text](url)
)


def _parse_inline_elements(text: str) -> list[dict]:
    """将一行文本中的行内 Markdown 语法解析为飞书 text_run / text_element 列表。"""
    elements = []
    pos = 0
    for m in _RE_INLINE.finditer(text):
        start = m.start()
        # 匹配之前的纯文本
        if start > pos:
            elements.append({"text_run": {"content": text[pos:start]}})

        if m.group(2):          # **bold**
            elements.append({"text_run": {"content": m.group(2), "text_element_style": {"bold": True}}})
        elif m.group(4):        # *italic*
            elements.append({"text_run": {"content": m.group(4), "text_element_style": {"italic": True}}})
        elif m.group(6):        # __bold__
            elements.append({"text_run": {"content": m.group(6), "text_element_style": {"bold": True}}})
        elif m.group(8):        # _italic_
            elements.append({"text_run": {"content": m.group(8), "text_element_style": {"italic": True}}})
        elif m.group(10):       # ~~strikethrough~~
            elements.append({"text_run": {"content": m.group(10), "text_element_style": {"strikethrough": True}}})
        elif m.group(12):       # `inline code`
            elements.append({"text_run": {"content": m.group(12), "text_element_style": {"inline_code": True}}})
        elif m.group(14):       # [text](url)
            link_text = m.group(15)
            link_url = m.group(16)
            elements.append({"text_run": {"content": link_text, "text_element_style": {"link": {"url": link_url}}}})

        pos = m.end()

    # 尾部纯文本
    if pos < len(text):
        elements.append({"text_run": {"content": text[pos:]}})

    if not elements:
        elements.append({"text_run": {"content": text}})

    return elements


def _make_block(block_type: int, text: str, **extra) -> dict:
    """构造一个飞书 Docx Block 字典。"""
    # 确定 block 内容键名
    type_key_map = {
        _BT_TEXT: "text", _BT_H1: "heading1", _BT_H2: "heading2", _BT_H3: "heading3",
        _BT_H4: "heading4", _BT_H5: "heading5", _BT_H6: "heading6", _BT_H7: "heading7",
        _BT_H8: "heading8", _BT_H9: "heading9",
        _BT_BULLET: "bullet", _BT_ORDERED: "ordered",
        _BT_CODE: "code", _BT_QUOTE: "quote", _BT_TODO: "todo",
    }
    key = type_key_map.get(block_type, "text")
    elements = _parse_inline_elements(text)
    block = {"block_type": block_type, key: {"elements": elements}}
    if extra:
        block[key].update(extra)
    return block


def _parse_md_to_blocks(content: str) -> list[dict]:
    """将 Markdown 文本解析为飞书 Docx Block 列表。

    支持：标题、无序/有序列表、引用、代码块（```）、待办、分割线，以及行内加粗/斜体/代码/链接。
    """
    lines = content.splitlines()
    blocks: list[dict] = []
    i = 0
    while i < len(lines) and len(blocks) < 200:
        line = lines[i]
        stripped = line.strip()

        # 空行跳过
        if not stripped:
            i += 1
            continue

        # 代码块 ```
        if stripped.startswith("```"):
            code_lines = []
            i += 1
            while i < len(lines):
                if lines[i].strip().startswith("```"):
                    i += 1
                    break
                code_lines.append(lines[i])
                i += 1
            code_text = "\n".join(code_lines) if code_lines else " "
            blocks.append(_make_block(_BT_CODE, code_text))
            continue

        # 分割线
        if _RE_HR.match(stripped):
            # 飞书没有 HR block，用空段落或分隔线 block_type=22 (divider) 替代
            blocks.append({"block_type": 22})
            i += 1
            continue

        # 待办
        m_todo = _RE_TODO.match(stripped)
        if m_todo:
            done = m_todo.group(1).lower() == "x"
            # 飞书 todo style: done 控制是否已完成
            blocks.append(_make_block(_BT_TODO, m_todo.group(2), style={"done": done}))
            i += 1
            continue

        # 标题
        m_heading = _RE_HEADING.match(stripped)
        if m_heading:
            level = min(len(m_heading.group(1)), 6)
            bt = _HEADING_LEVEL_MAP.get(level, _BT_H6)
            blocks.append(_make_block(bt, m_heading.group(2).strip()))
            i += 1
            continue

        # 引用（支持连续多行引用合并）
        m_quote = _RE_QUOTE.match(stripped)
        if m_quote:
            quote_parts = [m_quote.group(1)]
            i += 1
            while i < len(lines):
                mq = _RE_QUOTE.match(lines[i].strip())
                if mq:
                    quote_parts.append(mq.group(1))
                    i += 1
                else:
                    break
            blocks.append(_make_block(_BT_QUOTE, "\n".join(quote_parts)))
            continue

        # 无序列表
        m_bullet = _RE_BULLET.match(stripped)
        if m_bullet:
            blocks.append(_make_block(_BT_BULLET, m_bullet.group(1)))
            i += 1
            continue

        # 有序列表
        m_ordered = _RE_ORDERED.match(stripped)
        if m_ordered:
            blocks.append(_make_block(_BT_ORDERED, m_ordered.group(1)))
            i += 1
            continue

        # 普通段落
        blocks.append(_make_block(_BT_TEXT, stripped))
        i += 1

    return blocks


# ---------------------------------------------------------------------------
# Wiki 知识库操作
# ---------------------------------------------------------------------------

def _wiki_list_spaces(token: str) -> list[dict]:
    """列出应用可访问的所有飞书 Wiki 知识库。"""
    page_token = ""
    spaces = []
    while True:
        q = [("page_size", "50")]
        if page_token:
            q.append(("page_token", page_token))
        try:
            result = _oapi_request_json(
                "GET",
                "/open-apis/wiki/v2/spaces",
                token=token,
                queries=q,
                timeout=15,
            )
        except Exception:
            logger.exception("[feishu.wiki] 获取知识库列表异常")
            break

        if result.get("code") != 0:
            logger.warning("[feishu.wiki] 获取知识库列表失败: %s", result)
            break

        items = (result.get("data") or {}).get("items") or []
        spaces.extend(items)
        page_token = (result.get("data") or {}).get("page_token") or ""
        has_more = (result.get("data") or {}).get("has_more", False)
        if not has_more or not page_token:
            break
    return spaces


def _wiki_create_node(
    space_id: str,
    token: str,
    title: str,
    parent_node_token: str | None = None,
    obj_type: str = "docx",
) -> tuple[dict | None, str]:
    """在 Wiki 知识库下创建新节点（默认为 docx 文档）。

    Args:
        space_id: 知识库空间 ID
        token: tenant_access_token
        title: 节点标题
        parent_node_token: 父节点 token（不传则创建在知识库根目录）
        obj_type: 节点类型，默认 "docx"

    Returns:
        (node_info_dict, error_msg)  成功时 error_msg 为空
    """
    body: dict = {
        "obj_type": obj_type,
        "title": title,
    }
    if parent_node_token:
        body["parent_node_token"] = parent_node_token

    try:
        result = _oapi_request_json(
            "POST",
            f"/open-apis/wiki/v2/spaces/{space_id}/nodes",
            token=token,
            body=body,
            timeout=15,
        )
        if result.get("code") == 0:
            node = (result.get("data") or {}).get("node") or {}
            if node.get("node_token"):
                return node, ""
            return None, "创建成功但未返回 node_token"
        logger.warning("[feishu.wiki] 创建知识库节点失败: %s", result)
        return None, result.get("msg") or "创建知识库节点失败"
    except Exception:
        logger.exception("[feishu.wiki] 创建知识库节点异常")
        return None, "创建知识库节点异常"


def _wiki_get_node_url(wiki_node_token: str) -> str:
    """根据 wiki node_token 构建 Wiki 页面链接。"""
    return f"https://open.feishu.cn/wiki/{wiki_node_token}"


def wiki_create(params, state, ctx):
    """在飞书 Wiki 知识库中创建新文档，可选写入初始内容。

    参数:
        space_id: 知识库空间 ID（可选，不传则列出可用知识库让用户选择）
        title: 文档标题（必填）
        content: 初始内容，支持 Markdown（可选）
        parent_node_token: 父节点 token，不传则创建在知识库根目录（可选）
    """
    space_id = (params.get("space_id") or "").strip()
    title = (params.get("title") or "").strip()
    content = (params.get("content") or "").strip()
    parent_node_token = (params.get("parent_node_token") or "").strip()

    token = _get_tenant_access_token()
    if not token:
        return {"success": False, "reply": "飞书鉴权未配置或 token 获取失败（检查 FEISHU_APP_ID/FEISHU_APP_SECRET）"}

    # 如果没有指定 space_id，列出可用知识库
    if not space_id:
        spaces = _wiki_list_spaces(token)
        if not spaces:
            return {
                "success": False,
                "reply": "没有找到可访问的知识库。请确认：\n"
                         "1. 应用已开通 wiki 相关权限\n"
                         "2. 已将应用添加为知识库成员",
            }
        lines = ["找到以下可用知识库，请指定 space_id 重新调用：\n"]
        for sp in spaces:
            sid = sp.get("space_id", "")
            name = sp.get("name", "(无名称)")
            desc = sp.get("description", "")
            line = f"- **{name}**  space_id=`{sid}`"
            if desc:
                line += f"  ({desc})"
            lines.append(line)
        return {"success": False, "reply": "\n".join(lines)}

    if not title:
        return {"success": False, "reply": "请提供文档标题（title 参数）"}

    # 创建 Wiki 节点
    node, err = _wiki_create_node(space_id, token, title, parent_node_token or None)
    if not node:
        return {"success": False, "reply": f"创建知识库文档失败：{err}"}

    node_token = node.get("node_token", "")
    obj_token = node.get("obj_token", "")
    wiki_url = _wiki_get_node_url(node_token)

    # 如果有初始内容，通过 docx API 写入（Wiki docx 节点的 obj_token 就是 document_id）
    write_note = ""
    if content and obj_token:
        ok, write_err = _docx_append_text(obj_token, token, content)
        if not ok:
            write_note = f"\n⚠️ 初始内容写入失败：{write_err}"

    return {
        "success": True,
        "reply": f"知识库文档「{title}」已创建成功！\n链接：{wiki_url}{write_note}",
        "node_token": node_token,
        "obj_token": obj_token,
        "space_id": space_id,
        "url": wiki_url,
    }


def _docx_create_document(title: str, token: str, folder_token: str | None = None) -> tuple[str | None, str]:
    """创建一个新的飞书云文档（Docx）。

    Args:
        title: 文档标题
        token: tenant_access_token
        folder_token: 可选，创建到指定文件夹下（不传则创建到应用根目录）

    Returns:
        (document_id, error_msg)  成功时 error_msg 为空
    """

    def _do_create(body: dict) -> dict:
        return _oapi_request_json(
            "POST",
            "/open-apis/docx/v1/documents",
            token=token,
            body=body,
            timeout=15,
        )

    def _extract_doc_id(result: dict) -> str:
        doc = (result.get("data") or {}).get("document") or {}
        return doc.get("document_id") or ""

    body: dict = {"title": title}
    if folder_token:
        body["folder_token"] = folder_token

    try:
        result = _do_create(body)

        # 如果指定了 folder_token 但出现权限错误，降级为不带 folder_token 重试
        if result.get("code") != 0 and folder_token:
            err_code = result.get("code", 0)
            # 1770040: no folder permission; 1770041: folder not found 等常见文件夹权限/不存在错误
            if err_code in (1770040, 1770041, 1771001):
                logger.warning(
                    "[feishu.docs] folder_token=%s 权限不足(code=%s)，降级到应用根目录创建",
                    folder_token, err_code,
                )
                body.pop("folder_token", None)
                result = _do_create(body)

        if result.get("code") == 0:
            doc_id = _extract_doc_id(result)
            if doc_id:
                return doc_id, ""
            return None, "创建成功但未返回 document_id"
        logger.warning("[feishu.docs] 创建文档失败: %s", result)
        return None, result.get("msg") or "创建文档失败"
    except Exception:
        logger.exception("[feishu.docs] 创建文档异常")
        return None, "创建文档异常"


def _docx_append_text(document_id: str, token: str, content: str) -> tuple[bool, str]:
    content = (content or "").rstrip()
    if not content:
        return False, "没有可写入的内容"

    children = _parse_md_to_blocks(content)
    if not children:
        return False, "没有可写入的内容"

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

    # patch 只能更新单个 block 的文本内容，解析行内格式
    elements = _parse_inline_elements(content)
    payload = {"update_text_elements": {"elements": elements}}
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


def create(params, state, ctx):
    """创建新的飞书云文档，可选写入初始内容。"""
    title = (params.get("title") or "").strip()
    content = (params.get("content") or "").strip()
    folder_token = (params.get("folder_token") or "").strip()

    if not title:
        return {"success": False, "reply": "请提供文档标题（title 参数）"}

    token = _get_tenant_access_token()
    if not token:
        return {"success": False, "reply": "飞书鉴权未配置或 token 获取失败（检查 FEISHU_APP_ID/FEISHU_APP_SECRET）"}

    # 如果没有指定 folder_token，使用配置的云盘根目录
    if not folder_token and FEISHU_DRIVE_ROOT_FOLDER_TOKEN:
        folder_token = FEISHU_DRIVE_ROOT_FOLDER_TOKEN

    doc_id, err = _docx_create_document(title, token, folder_token or None)
    if not doc_id:
        return {"success": False, "reply": f"创建文档失败：{err}"}

    doc_url = f"https://open.feishu.cn/docx/{doc_id}"

    # 如果有初始内容，写入文档
    if content:
        ok, write_err = _docx_append_text(doc_id, token, content)
        if not ok:
            return {
                "success": True,
                "reply": f"文档「{title}」已创建（{doc_url}），但写入初始内容失败：{write_err}",
                "document_id": doc_id,
                "url": doc_url,
            }

    return {
        "success": True,
        "reply": f"文档「{title}」已创建成功！\n链接：{doc_url}",
        "document_id": doc_id,
        "url": doc_url,
    }


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
    "feishu.docs.create": {"handler": create, "visibility": "public"},
    "feishu.docs.read": {"handler": read, "visibility": "public"},
    "feishu.docs.write": {"handler": write, "visibility": "public"},
    "feishu.wiki.create": {"handler": wiki_create, "visibility": "public"},
}
