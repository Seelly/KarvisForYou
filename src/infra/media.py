# -*- coding: utf-8 -*-
"""
媒体处理模块。

职责：
1. ASR 语音识别（腾讯云极速版 + 一句话识别降级）
2. URL 检测与网页正文抓取
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time

import requests
from bs4 import BeautifulSoup

from config import TENCENT_APPID, TENCENT_SECRET_ID, TENCENT_SECRET_KEY
from infra.logging import get_logger

logger = get_logger(__name__)


# ============ ASR 语音识别 ============

def recognize_voice(audio_data: bytes, voice_format: str = "amr") -> str | None:
    """腾讯云录音文件识别极速版，降级到一句话识别"""
    if not TENCENT_APPID:
        logger.warning("[ASR] 未配置 APPID，降级到一句话识别")
        return _recognize_voice_sentence(audio_data)

    try:
        timestamp = int(time.time())
        params = {
            "convert_num_mode": 1,
            "engine_type": "16k_zh",
            "filter_dirty": 0,
            "filter_modal": 0,
            "filter_punc": 0,
            "first_channel_only": 1,
            "secretid": TENCENT_SECRET_ID,
            "speaker_diarization": 0,
            "timestamp": timestamp,
            "voice_format": voice_format,
            "word_info": 0,
        }
        query_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        sign_str = f"POSTasr.cloud.tencent.com/asr/flash/v1/{TENCENT_APPID}?{query_str}"
        signature = base64.b64encode(
            hmac.new(TENCENT_SECRET_KEY.encode("utf-8"),
                     sign_str.encode("utf-8"), hashlib.sha1).digest()
        ).decode("utf-8")

        url = f"https://asr.cloud.tencent.com/asr/flash/v1/{TENCENT_APPID}?{query_str}"
        headers = {
            "Host": "asr.cloud.tencent.com",
            "Authorization": signature,
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(audio_data)),
        }
        resp = requests.post(url, headers=headers, data=audio_data, timeout=30)
        result = resp.json()
        logger.info("[ASR极速版] code=%s", result.get("code"))

        if result.get("code") != 0:
            logger.error("[ASR极速版] 失败: %s", result.get("message"))
            return _recognize_voice_sentence(audio_data)

        flash_result = result.get("flash_result", [])
        if flash_result:
            text = flash_result[0].get("text", "")
            logger.info("[ASR极速版] 识别: %s", text[:80])
            return text if text else None
        return None
    except Exception as e:
        logger.error("[ASR极速版] 异常: %s", e)
        return _recognize_voice_sentence(audio_data)


def _recognize_voice_sentence(audio_data: bytes) -> str | None:
    """降级：腾讯云一句话识别"""
    try:
        from tencentcloud.common import credential
        from tencentcloud.common.profile.client_profile import ClientProfile
        from tencentcloud.common.profile.http_profile import HttpProfile
        from tencentcloud.asr.v20190614 import asr_client, models

        cred = credential.Credential(TENCENT_SECRET_ID, TENCENT_SECRET_KEY)
        httpProfile = HttpProfile()
        httpProfile.endpoint = "asr.tencentcloudapi.com"
        httpProfile.reqTimeout = 30
        clientProfile = ClientProfile()
        clientProfile.httpProfile = httpProfile

        client = asr_client.AsrClient(cred, "", clientProfile)
        req = models.SentenceRecognitionRequest()
        req.EngSerViceType = "16k_zh"
        req.SourceType = 1
        req.VoiceFormat = "amr"
        req.Data = base64.b64encode(audio_data).decode("utf-8")
        req.DataLen = len(audio_data)

        resp = client.SentenceRecognition(req)
        logger.info("[ASR一句话] 成功: %s", resp.Result[:50] if resp.Result else "empty")
        return resp.Result
    except Exception as e:
        logger.error("[ASR一句话] 失败: %s", e)
        return None


# ============ F1: URL 检测与网页正文抓取 ============

_URL_PATTERN = re.compile(
    r"https?://[^\s<>\"')\]]+",
    re.IGNORECASE,
)


def extract_url(text: str) -> str | None:
    """
    从文本中提取 URL。
    仅当文本主体是 URL 时才提取（纯 URL 或 URL + 少量描述文字）。
    避免对正常聊天中偶尔出现的 URL 做不必要的抓取。
    """
    text = text.strip()
    match = _URL_PATTERN.search(text)
    if not match:
        return None
    url = match.group(0)
    # 只有当 URL 占文本大部分时才抓取（纯 URL 或 URL + 简短描述）
    non_url_text = text.replace(url, "").strip()
    if len(non_url_text) <= 30:
        return url
    return None


def fetch_link_content(url: str) -> str:
    """
    F1: 抓取链接正文内容，失败返回空字符串（优雅降级）。
    支持微信公众号文章、普通网页。截断到 2000 字符。
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=5,
                           allow_redirects=True, verify=True)
        resp.encoding = resp.apparent_encoding or "utf-8"

        if resp.status_code != 200:
            logger.info("[链接抓取] HTTP %s: %s", resp.status_code, url[:80])
            return ""

        content_type = resp.headers.get("Content-Type", "")
        if "text/html" not in content_type and "text/plain" not in content_type:
            logger.info("[链接抓取] 非网页内容(%s): %s", content_type, url[:80])
            return ""

        soup = BeautifulSoup(resp.text, "html.parser")

        # 移除无用标签
        for tag in soup.find_all(["script", "style", "nav", "header",
                                   "footer", "aside", "iframe"]):
            tag.decompose()

        # 优先取 article 标签（通用）或微信文章专用结构
        article = (soup.find("article")
                   or soup.find("div", class_="rich_media_content")
                   or soup.find("body"))

        if not article:
            logger.info("[链接抓取] 无法提取正文: %s", url[:80])
            return ""

        text = article.get_text(separator="\n", strip=True)
        result = text[:2000] if text else ""
        logger.info("[链接抓取] 成功: %s 字符, url=%s", len(result), url[:80])
        return result
    except Exception as e:
        logger.warning("[链接抓取] 异常(%s): %s", e, url[:80])
        return ""
