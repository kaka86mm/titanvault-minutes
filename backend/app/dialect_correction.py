"""方言口音导致的转写错字 LLM 纠错。

SeACo Paraformer 对方言（贵州话/四川话/粤语等）识别时，会产生大量
近音错字（如"附现"→"敷线"、"合动"→"合同"、"曲俗"→"取数"）。
这些错误 LLM 结合上下文能纠正——实测对贵州话纠错率约 80%，质量好。

流程：转写完成后（SeACo + 热词）→ 逐段调 LLM 纠错 → 存回 transcript_segments。
- 环境变量开关：AHAMVOICE_DIALECT_CORRECTION=true 启用（默认关）
- 失败不阻塞：纠错失败则保留原文，转写/纪要照常进行
- 保留原文：recording 元数据记 raw_transcript_hash，可追溯

设计取舍：
- 逐段纠错而非整篇：避免长文本 LLM 截断，且能保留段对齐
- 但段太短 LLM 缺上下文，所以按"轮次窗口"合并相邻段一起纠错
- temperature 0.1：要稳定，不要创造性
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from .config import get_llm_config

logger = logging.getLogger(__name__)

# 纠错窗口：把相邻 N 段合并成一个纠询单元，给 LLM 足够上下文。
# 太短→LLM 缺上下文；太长→延迟高、易截断。8 段约 1000-2000 字，合适。
CORRECTION_WINDOW = 8
# 整体超时上限：纠错总耗时不超过这么久。超时则剩余窗口保留原文。
# 避免长录音（多窗口）把转写线程卡住太久。
MAX_TOTAL_SECONDS = 180
# 连续失败这么多次后放弃剩余窗口（如 model 不支持长文本返回空）。
MAX_CONSECUTIVE_FAILURES = 3


SYSTEM_PROMPT = (
    "你是方言语音转写纠错专家。输入的转写文本来自带口音的说话人，"
    "语音识别产生了方言口音导致的同音/近音错字。"
    "你的任务：只纠正这类明显的识别错字，不改原意。"
    "规则：\n"
    "1. 只改同音/近音、口吃重复、口音音变导致的错字\n"
    "2. 不增删内容、不改原意、不润色改写、不改口语风格\n"
    "3. 不确定的保留原文，宁可漏改不要错改\n"
    "4. 专有名词（人名/公司名/地名）拿不准的保留原文\n"
    "5. 保持输入的行数和 [说话人X] 前缀格式，逐行对应输出\n"
    "6. 直接输出纠正后文本，不要解释、不要前后缀"
)


def correct_dialect_segments(
    segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """对转写段做方言纠错，返回纠正后的段（保持原段顺序和元数据）。

    segments: transcript_segments 行（dict），至少含 speaker, text 字段。
    返回：同结构的新 list，text 字段被纠正（失败则保留原文）。

    按窗口（CORRECTION_WINDOW 段）合并发给 LLM，逐窗口纠错。
    LLM 输出按行对齐回填到对应段。
    """
    if not segments:
        return segments

    api_key, base, model = get_llm_config()
    if not api_key:
        logger.warning("dialect correction skipped: no LLM API key")
        return segments

    corrected_segments = [dict(s) for s in segments]  # 浅拷贝，不污染原数据

    start_time = time.time()
    consecutive_failures = 0

    # 按窗口切分
    for window_start in range(0, len(segments), CORRECTION_WINDOW):
        # 整体超时检查
        if time.time() - start_time > MAX_TOTAL_SECONDS:
            logger.warning(
                "dialect correction total timeout (%.0fs), %d windows skipped",
                time.time() - start_time,
                (len(segments) - window_start + CORRECTION_WINDOW - 1) // CORRECTION_WINDOW,
            )
            break
        # 连续失败太多，放弃剩余
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            logger.warning(
                "dialect correction aborted: %d consecutive failures (model may not support long text)",
                consecutive_failures,
            )
            break

        window = segments[window_start : window_start + CORRECTION_WINDOW]
        window_lines = [
            f"[说话人{s.get('speaker', '?')}] {s.get('text', '')}" for s in window
        ]
        window_text = "\n".join(window_lines)

        try:
            corrected_text = _call_llm_correction(api_key, base, model, window_text)
            corrected_lines = _parse_corrected_lines(corrected_text, len(window))

            # 行数对齐检查：LLM 合并/拆分行会导致按位置回填错位（内容串段、
            # 重复、说话人错配）。行数不匹配时拒绝整个窗口，保留原文——
            # 错位比不纠错更糟糕。
            if len(corrected_lines) != len(window):
                logger.warning(
                    "dialect correction window %d-%d: line count mismatch "
                    "(expected %d, got %d), keeping original",
                    window_start, window_start + len(window),
                    len(window), len(corrected_lines),
                )
                consecutive_failures += 1
                continue

            # 按行对齐回填
            for i, seg in enumerate(window):
                idx = window_start + i
                corrected_segments[idx]["text"] = corrected_lines[i]
            consecutive_failures = 0  # 成功，重置失败计数
        except Exception as exc:
            consecutive_failures += 1
            logger.warning(
                "dialect correction failed for window %d-%d (failure #%d): %s: %s",
                window_start,
                window_start + len(window),
                consecutive_failures,
                type(exc).__name__,
                exc,
            )
            # 失败保留原文，继续下一个窗口
            continue

    return corrected_segments


def _call_llm_correction(
    api_key: str, base: str, model: str, transcript_text: str
) -> str:
    """调 LLM 纠错，返回纠正后的文本。

    用户消息只放转写原文，指令全在 system prompt（稳定输出格式）。
    timeout=30s（不是 120）：长录音有多个窗口，单窗口卡太久会拖垮整个转写。
    若 LLM 对长文本返回空（deepseek-v4-pro 的已知问题），立即抛异常，
    由上层按窗口跳过，不要等满超时。
    """
    payload = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": 8192,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": transcript_text},
        ],
    }
    with httpx.Client(timeout=30, trust_env=False) as client:
        r = client.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
        r.raise_for_status()
        data = r.json()
        content = data["choices"][0]["message"].get("content", "")
        if not content.strip():
            raise RuntimeError("LLM returned empty content")
        return content


def _parse_corrected_lines(corrected_text: str, expected_count: int) -> list[str]:
    """把 LLM 纠错输出解析回逐行文本（去掉 [说话人X] 前缀）。

    返回实际解析到的行数（不截断）。调用方 correct_dialect_segments 会检查
    len(result) == expected_count，不匹配则拒绝整个窗口——因为按位置回填
    错位比保留原文更糟。
    """
    lines = [ln.strip() for ln in corrected_text.split("\n") if ln.strip()]
    result: list[str] = []
    prefix_re = re.compile(r"^\[说话人\d+\]\s*")
    for ln in lines:
        text = prefix_re.sub("", ln).strip()
        if text:
            result.append(text)
    return result
