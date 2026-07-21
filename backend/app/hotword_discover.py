"""热词智能发现：从转写+纪要用 LLM 抽取候选词，去重后入库。"""
from __future__ import annotations

import json
import re
from typing import Any


def _parse_llm_json(content: str) -> list[dict[str, Any]]:
    """从容许格式的 LLM 响应中解析候选词列表。

    处理：标准 JSON / markdown 包裹 / 前后多余文本 / 空响应。
    要求 JSON 含 "terms" 键（值为候选词数组）。
    """
    if not content or not content.strip():
        return []
    # 去掉 markdown 代码块标记
    cleaned = re.sub(r"```(?:json)?\s*", "", content).replace("```", "").strip()
    # 用 JSONDecoder.raw_decode 从第一个 { 开始解析完整 JSON（支持嵌套）
    start = cleaned.find("{")
    if start == -1:
        return []
    try:
        data, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    except json.JSONDecodeError:
        return []
    terms = data.get("terms")
    if not isinstance(terms, list):
        return []
    return terms


def _dedupe_against_db(candidates: list[dict[str, Any]], recording_id: str) -> list[dict[str, Any]]:
    """过滤掉已在库的词（active/candidate/protected 跳过，discarded 跳过）+ 同批去重。

    recording_id 当前不参与去重逻辑——热词是全局的（不分录音），同一词不管
    从哪条录音发现都只保留一条。参数保留是为了和 _insert_candidates 签名一致
    + 未来可能按录音维度追溯来源。
    """
    from .db import db
    with db() as conn:
        existing = {
            row["word"].lower()
            for row in conn.execute(
                "select word from hotwords where state in ('active', 'candidate', 'protected')"
            ).fetchall()
        }
        discarded = {
            row["word"].lower()
            for row in conn.execute("select word from hotwords where state = 'discarded'").fetchall()
        }
    result = []
    for c in candidates:
        w = (c.get("word") or "").strip().lower()
        if not w or w in existing or w in discarded:
            continue
        existing.add(w)  # 同批内去重
        result.append(c)
    return result


EXTRACTION_SYSTEM_PROMPT = """你是术语抽取助手。从会议转写和纪要中识别值得加入热词库的词。
抽取范围：
- 专业术语（ERP/MES/中台 等行业词）
- 产品名/系统名/项目代号
- 客户名/公司简称/人名
- 转写里"疑似"的词（可能是错字、听不清、张冠李戴）

每个词给出：
- word: 词本身
- kind: 分类（产品/系统/行业/业务术语/项目/人员/客户/客户简称）
- confidence: 0-1 置信度（明确的词 0.9，疑似的 0.4-0.6）
- example: 文中原话例句（≤30字，带说话人）
- is_uncertain: 是否疑似/不确定（true/false）

规则：
- 跳过常见词、语气词、通用动词（"然后/我们/这个"）
- 跳过"XX有限公司"等全称（口语不会说）
- 同一词的不同写法合并，取最可能的写法
- 只返回 JSON：{"terms": [{word, kind, confidence, example, is_uncertain}]}"""


async def _llm_extract_terms(transcript: str, summary: str) -> list[dict[str, Any]]:
    """调 LLM 抽取候选词。复用 _deepseek_post_with_retry + get_llm_config。"""
    import httpx
    from .config import get_llm_config
    from .deepseek import _deepseek_post_with_retry
    api_key, base, model = get_llm_config()
    if not api_key:
        return []  # 无 key 静默跳过
    user_content = f"转写：\n{transcript[:16000]}\n\n纪要：\n{summary[:4000]}"
    payload = {
        "model": model,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
            content = await _deepseek_post_with_retry(client, f"{base}/chat/completions", api_key, payload)
        return _parse_llm_json(content)
    except Exception as exc:
        print(f"[hotword-discover] LLM 抽取失败（不阻塞）: {type(exc).__name__}: {exc}", flush=True)
        return []


def _insert_candidates(candidates: list[dict[str, Any]], recording_id: str) -> int:
    """候选词入库为 candidate 状态。返回插入数。"""
    import uuid
    from .db import db, now
    from .summary import transcript_text
    if not candidates:
        return 0
    # 取转写全文算 frequency
    with db() as conn:
        full_text = transcript_text(conn, recording_id)
    ts = now()
    inserted = 0
    with db() as conn:
        for c in candidates:
            word = (c.get("word") or "").strip()
            if not word:
                continue
            source_key = f"llm-discover:{recording_id}:{word.lower()}"
            # 防重复：同一录音 discover 两次时，source_key 已存在则跳过
            if conn.execute("select 1 from hotwords where source_key = ?", (source_key,)).fetchone():
                continue
            kind = c.get("kind") or "业务术语"
            confidence = float(c.get("confidence") or 0.5)
            example = (c.get("example") or "")[:200]
            frequency = max(1, full_text.count(word)) if full_text else 1
            conn.execute(
                """insert into hotwords(
                    id, word, kind, source, scope, weight, active, state, protected,
                    frequency, confidence, score, example, source_key,
                    first_seen_at, last_seen_at, last_used_at, updated_at
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()), word, kind, "llm-discover", "global", 5, 0, "candidate", 0,
                    frequency, confidence, 0, example,
                    f"llm-discover:{recording_id}:{word.lower()}",
                    ts, ts, ts, ts,
                ),
            )
            inserted += 1
    return inserted


async def discover_hotwords(recording_id: str) -> dict[str, int]:
    """转写/纪要后自动调用。从文本抽候选词，去重后入库。"""
    from .db import db
    from .summary import transcript_text
    with db() as conn:
        transcript = transcript_text(conn, recording_id)
    if not transcript.strip():
        return {"discovered": 0}
    # 取最新纪要
    with db() as conn:
        summary_row = conn.execute(
            "select content from summaries where recording_id = ? and is_current = 1 order by version desc limit 1",
            (recording_id,),
        ).fetchone()
    summary = summary_row["content"] if summary_row else ""
    candidates = await _llm_extract_terms(transcript, summary)
    candidates = _dedupe_against_db(candidates, recording_id)
    inserted = _insert_candidates(candidates, recording_id)
    print(f"[hotword-discover] 录音 {recording_id}：抽取 {len(candidates)} 候选词，入库 {inserted}", flush=True)
    return {"discovered": inserted}
