"""DeepSeek LLM 传输层。

只放与 DeepSeek HTTP 调用直接相关的、不依赖业务模板的函数：
- _deepseek_post_with_retry：async 重试 POST（summary/revision 用）
- call_deepseek_emotion：同步调用情绪分析（emotion 域用）

注意：call_deepseek_summary / call_deepseek_revision 没放这里——它们调用
summary 域的模板函数（meeting_template 等），属于 summary.py，避免
deepseek ↔ summary 循环依赖。summary.py 反过来 import deepseek 的
_post_with_retry，单向。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .config import get_llm_config, env_int


async def _deepseek_post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    api_key: str,
    payload: dict[str, Any],
    attempts: int = 5,
) -> str:
    """POST to a DeepSeek chat-completions endpoint, retrying on 5xx and on
    transient network errors (ConnectError, ReadTimeout, RemoteProtocolError).
    Without this, a single TCP blip during a 30-minute summary kills the whole
    job and the user has to re-run from scratch.
    """
    last_error = ""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    for attempt in range(attempts):
        try:
            res = await client.post(url, headers=headers, json=payload)
        except httpx.RequestError as exc:
            last_error = f"network error: {type(exc).__name__}: {exc}"
            if attempt < attempts - 1:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            break
        if res.status_code < 400:
            data = res.json()
            return data["choices"][0]["message"]["content"]
        last_error = f"HTTP {res.status_code}: {res.text[:500]}"
        if res.status_code in {408, 409, 425, 429, 500, 502, 503, 504} and attempt < attempts - 1:
            await asyncio.sleep(2 * (attempt + 1))
            continue
        break
    raise RuntimeError(f"DeepSeek request failed: {last_error}")


def call_deepseek_emotion(annotated_transcript: str, rec: dict[str, Any], acoustic_md: str) -> tuple[str, str]:
    api_key, base, model = get_llm_config()
    if not api_key:
        raise RuntimeError("LLM_API_KEY is not configured")
    transcript = annotated_transcript
    if len(transcript) > 48000:
        transcript = transcript[:26000] + "\n\n[中间过长省略，仅保留首尾]\n\n" + transcript[-18000:]
    payload = {
        "model": model,
        "temperature": 0.3,
        "max_tokens": env_int("AHAMVOICE_EMOTION_MAX_TOKENS", 6000, 2000, 12000),
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是销售 / 项目对话的情绪分析专家。基于带时间戳和声学情绪标注的对话转写，分析对话里的情绪与态度。"
                    "“（声学:情绪+置信）”来自语音情绪模型，是辅助证据，要结合说话内容判断，不要照搬数字。"
                    "重点突出情绪本身：整体氛围松紧、各方参与度、是否有防御 / 抵触 / 不耐烦 / 敷衍 / 热情，以及客户的购买意向信号和异议顾虑。"
                    "只基于转写内容，不编造；不确定的写“疑似”。"
                    "严禁输出行动项、待办、下一步、跟进建议或任何 CRM 模块——这是情绪分析，不是会议纪要。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"录音标题：{rec.get('title')}\n会议类型：{rec.get('meeting_type')}\n时长：{rec.get('duration_label')}\n\n"
                    "请严格按以下结构输出 Markdown（小节顺序不变；无内容的小节写“未明确”）：\n"
                    "# 对话情绪分析\n"
                    "## 整体情绪基调\n"
                    "## 各方情绪画像\n"
                    "## 情绪转折点\n"
                    "## 客户意向信号\n"
                    "## 异议与顾虑\n\n"
                    "每节尽量引用时间戳和原话作为证据；“各方情绪画像”按说话人分别写参与度与情绪主线。\n\n"
                    f"【声学情绪概览】\n{acoustic_md}\n\n"
                    f"【带情绪标注的对话转写】\n{transcript}\n"
                ),
            },
        ],
    }
    chat_url = f"{base}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_err: Any = None
    with httpx.Client(timeout=180, trust_env=False) as client:
        for attempt in range(3):
            try:
                res = client.post(chat_url, headers=headers, json=payload)
                if res.status_code >= 500:
                    last_err = RuntimeError(f"DeepSeek HTTP {res.status_code}: {res.text[:200]}")
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if res.status_code >= 400:
                    raise RuntimeError(f"DeepSeek HTTP {res.status_code}: {res.text[:300]}")
                return res.json()["choices"][0]["message"]["content"], model
            except httpx.RequestError as exc:
                last_err = exc
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"DeepSeek 调用失败：{last_err}")
