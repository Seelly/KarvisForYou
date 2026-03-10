# -*- coding: utf-8 -*-
"""
KarvisForAll 本地文件读写层
多用户版：路径由 UserContext 生成，LocalFileIO 直接使用传入的绝对路径。
"""
import os
import json
import threading

from infra.logging import BEIJING_TZ, get_logger
from storage.base import StorageBackend

logger = get_logger(__name__)


class LocalFileIO(StorageBackend):
    """本地文件存储"""

    _lock = threading.Lock()

    def _resolve_path(self, file_path):
        """直接返回传入的路径（UserContext 已经生成了正确的绝对路径）"""
        return file_path

    def get_token(self):
        """兼容接口 — 本地模式不需要 token"""
        return "local"

    # ---- 文本文件读写 ----

    def read_text(self, file_path, _retries=3):
        """读取文本文件，返回字符串。文件不存在返回空字符串，失败返回 None"""
        local_path = self._resolve_path(file_path)
        try:
            if not os.path.exists(local_path):
                return ""
            with open(local_path, "r", encoding="utf-8") as f:
                content = f.read()
            return content
        except Exception as e:
            logger.error("[LocalIO] 读取异常 %s: %s", file_path, e)
            return None

    def write_text(self, file_path, content, _retries=3):
        """写入文本文件（覆盖），返回 True/False"""
        local_path = self._resolve_path(file_path)
        try:
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with self._lock:
                with open(local_path, "w", encoding="utf-8") as f:
                    f.write(content)
            return True
        except Exception as e:
            logger.error("[LocalIO] 写入异常 %s: %s", file_path, e)
            return False

    # ---- JSON 文件读写 ----

    def read_json(self, file_path):
        """读取 JSON 文件，返回 dict/list。文件不存在返回空 dict，失败返回 None"""
        text = self.read_text(file_path)
        if text is None:
            return None
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            logger.error("[LocalIO] JSON 解析失败 %s: %s", file_path, e)
            return None

    def write_json(self, file_path, data):
        """写入 JSON 文件"""
        content = json.dumps(data, ensure_ascii=False, indent=2)
        return self.write_text(file_path, content)

    # ---- 追加到文件指定 section ----

    def append_to_section(self, file_path, section_header, content):
        """追加内容到文件的指定 section"""
        existing = self.read_text(file_path)
        if existing is None:
            return False

        if section_header in existing:
            parts = existing.split(section_header, 1)
            before = parts[0]
            after = parts[1]
            next_section_idx = after.find("\n## ")
            if next_section_idx >= 0:
                section_content = after[:next_section_idx]
                rest = after[next_section_idx:]
                new_content = before + section_header + section_content.rstrip() + "\n" + content + "\n" + rest
            else:
                new_content = before + section_header + after.rstrip() + "\n" + content + "\n"
        else:
            new_content = existing.rstrip() + f"\n\n{section_header}\n{content}\n"

        return self.write_text(file_path, new_content)

    # ---- 追加到 Quick-Notes（带去重） ----

    def append_to_quick_notes(self, file_path, message):
        """追加一条笔记到 Quick-Notes"""
        from datetime import datetime

        existing = self.read_text(file_path)
        if existing is None:
            return False

        if not existing.strip():
            existing = "# Quick Notes\n\n快速笔记，从微信同步。\n\n---\n\n"

        # 内容去重
        sections = existing.split('## ')
        for section in sections[1:6]:
            lines = section.strip().split('\n')
            if len(lines) >= 2:
                content_lines = '\n'.join(lines[1:]).strip().rstrip('-').strip()
                if content_lines == message.strip():
                    logger.info("[LocalIO] 内容重复，跳过: %s...", message[:30])
                    return True

        now = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
        new_entry = f"## {now}\n\n{message}\n\n---\n\n"

        lines = existing.split('\n')
        header_end = 0
        for i, line in enumerate(lines):
            if line.strip() == "---":
                header_end = i + 1
                break

        new_content = '\n'.join(lines[:header_end]) + '\n\n' + new_entry + '\n'.join(lines[header_end:])
        return self.write_text(file_path, new_content)

    # ---- 二进制文件上传 ----

    def upload_binary(self, file_path, data, content_type="application/octet-stream"):
        """上传（保存）二进制文件"""
        local_path = self._resolve_path(file_path)
        try:
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "wb") as f:
                f.write(data)
            return True
        except Exception as e:
            logger.error("[LocalIO] 二进制写入异常 %s: %s", file_path, e)
            return False

    # ---- 二进制文件下载 ----

    def download_binary(self, file_path, _retries=3):
        """读取二进制文件内容。文件不存在返回 None。"""
        local_path = self._resolve_path(file_path)
        try:
            if not os.path.exists(local_path):
                return None
            with open(local_path, "rb") as f:
                data = f.read()
            return data
        except Exception as e:
            logger.error("[LocalIO] 二进制读取异常 %s: %s", file_path, e)
            return None

    # ---- 目录列表 ----

    def list_children(self, folder_path, _retries=3):
        """列出文件夹下的子项"""
        local_path = self._resolve_path(folder_path)
        try:
            if not os.path.exists(local_path):
                return []
            items = []
            for entry in os.listdir(local_path):
                full = os.path.join(local_path, entry)
                item = {"name": entry}
                if os.path.isfile(full):
                    item["file"] = {"mimeType": "application/octet-stream"}
                    item["size"] = os.path.getsize(full)
                else:
                    item["folder"] = {"childCount": 0}
                items.append(item)
            return items
        except Exception as e:
            logger.error("[LocalIO] 列目录异常 %s: %s", folder_path, e)
            return None