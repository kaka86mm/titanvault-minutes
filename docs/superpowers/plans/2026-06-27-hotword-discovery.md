# 热词智能发现实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从转写+纪要用 LLM 自动抽取候选热词，用户在批量审阅面板确认/纠正/丢弃。

**Architecture:** 新模块 hotword_discover.py（LLM 抽取+去重+入库）→ 复用 hotwords 表加 candidate/discarded 状态 → 审阅面板批量操作。纪要完成后自动触发。

**Tech Stack:** Python/FastAPI/SQLite/httpx（LLM 调用）/ React+TS（审阅面板）

**Spec:** `docs/superpowers/specs/2026-06-27-hotword-discovery-design.md`

---

## 重要约束

1. **原项目零测试传统已打破**：本项目已有 pytest（17 测试），新功能继续用 TDD。
2. **复用现有传输层**：`_deepseek_post_with_retry` + `get_llm_config`，不重写 HTTP 调用。
3. **候选词不影响转写**：candidate 状态不参与 build_hotword_package（查询条件 state in active/protected）。
4. **行号会漂移**：用函数名定位，不要死盯行号。
5. **测试隔离**：用 conftest 的 tmp_home fixture（RECORDING_AI_HOME）。

## File Structure

```
新增：
  backend/app/hotword_discover.py    # 发现算法（LLM 抽取+去重+入库）
  backend/tests/test_hotword_discover.py
  frontend-src/src/pages/app/HotwordCandidates.tsx  # 审阅面板

修改：
  backend/app/db.py                  # ensure_schema 加 example 字段迁移
  backend/app/asr.py                 # process_recording_background 加触发
  backend/app/main.py                # 4 个新 API
  frontend-src/src/api/endpoints.ts  # 候选词 API 调用
  frontend-src/src/api/types.ts      # 候选词类型
  frontend-src/src/pages/app/Hotwords.tsx  # tab 切换
  frontend-src/src/router.tsx        # 候选审阅路由
```

---

## Task 1: schema 迁移（example 字段）

**Files:**
- Modify: `backend/app/db.py`（ensure_schema 的 hotword 迁移段）

- [ ] **Step 1: 找到 hotword 迁移段**

在 `backend/app/db.py` 的 `ensure_schema` 里找到 `hotword_migrations` dict（约 line 340-355），它定义了要 add 的列。

- [ ] **Step 2: 加 example 字段到迁移 dict**

在 `hotword_migrations` dict 里加一行：

```python
"example": "text",
```

加在 `"updated_at": "text"` 前面（或 dict 末尾，位置不影响功能）。

- [ ] **Step 3: py_compile + 冒烟**

Run: `python -m py_compile backend/app/db.py`
Run:
```bash
RECORDING_AI_HOME=/tmp/aham-hw1 .venv/bin/python -c "
from backend.app.db import ensure_schema
ensure_schema()
print('OK: schema 迁移含 example 字段')
"
```
Expected: `OK: schema 迁移含 example 字段`

- [ ] **Step 4: 验证 example 列存在**

```bash
RECORDING_AI_HOME=/tmp/aham-hw1 .venv/bin/python -c "
from backend.app.db import db
with db() as conn:
    cols = {r['name'] for r in conn.execute('pragma table_info(hotwords)').fetchall()}
    assert 'example' in cols, f'example 列缺失: {cols}'
    print('OK: hotwords 表含 example 列')
"
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/db.py
git commit -m "feat: hotwords 表加 example 字段（候选词例句存储）"
```

---

## Task 2: _parse_llm_json（TDD）

**Files:**
- Create: `backend/app/hotword_discover.py`
- Create: `backend/tests/test_hotword_discover.py`

**Why:** LLM 返回的 JSON 可能被 markdown 包裹（```json...```）或带多余文本，需要容错解析。这是纯函数，最适合 TDD 起步。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_hotword_discover.py`：

```python
"""hotword_discover 的单元测试。"""
from backend.app.hotword_discover import _parse_llm_json


def test_parse_clean_json():
    """标准 JSON 对象。"""
    content = '{"terms": [{"word": "金蝶", "kind": "产品", "confidence": 0.9, "example": "金蝶接口报错", "is_uncertain": false}]}'
    terms = _parse_llm_json(content)
    assert len(terms) == 1
    assert terms[0]["word"] == "金蝶"
    assert terms[0]["kind"] == "产品"


def test_parse_markdown_wrapped_json():
    """markdown 代码块包裹的 JSON。"""
    content = '```json\n{"terms": [{"word": "ERP", "kind": "行业", "confidence": 0.85, "example": "ERP系统", "is_uncertain": false}]}\n```'
    terms = _parse_llm_json(content)
    assert len(terms) == 1
    assert terms[0]["word"] == "ERP"


def test_parse_empty_response():
    """空响应返回空列表。"""
    assert _parse_llm_json("") == []
    assert _parse_llm_json("无术语") == []


def test_parse_json_with_extra_text():
    """JSON 前后有解释文本。"""
    content = '好的，以下是抽取的术语：\n{"terms": [{"word": "MES", "kind": "行业", "confidence": 0.8, "example": "MES升级", "is_uncertain": false}]}\n希望有帮助'
    terms = _parse_llm_json(content)
    assert len(terms) == 1
    assert terms[0]["word"] == "MES"


def test_parse_missing_terms_key():
    """JSON 没有 terms 键（LLM 没按格式）返回空。"""
    content = '{"result": []}'
    assert _parse_llm_json(content) == []
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/bin/python -m pytest backend/tests/test_hotword_discover.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 创建 hotword_discover.py + 实现 _parse_llm_json**

Create `backend/app/hotword_discover.py`：

```python
"""热词智能发现：从转写+纪要用 LLM 抽取候选词，去重后入库。

独立于 hotwords.py（纯本地打分/双轨包逻辑）——本模块是 LLM 调用 + 文本处理。
"""
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
    # 尝试提取 JSON 对象（可能被 markdown ``` 包裹或前后有文本）
    match = re.search(r"\{[^{}]*\}", content, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return []
    terms = data.get("terms")
    if not isinstance(terms, list):
        return []
    return terms
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/bin/python -m pytest backend/tests/test_hotword_discover.py -v`
Expected: 5 个 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/hotword_discover.py backend/tests/test_hotword_discover.py
git commit -m "feat: hotword_discover._parse_llm_json（LLM 响应容错解析，TDD）"
```

---

## Task 3: _dedupe_against_db（TDD）

**Files:**
- Modify: `backend/app/hotword_discover.py`
- Modify: `backend/tests/test_hotword_discover.py`

- [ ] **Step 1: 写失败测试**

在 `test_hotword_discover.py` 追加：

```python
def test_dedupe_skips_existing_active(tmp_home):
    """已在库的 active 热词跳过。"""
    from backend.app.hotword_discover import _dedupe_against_db
    from backend.app.db import db
    import uuid
    with db() as conn:
        conn.execute(
            "insert into hotwords(id,word,kind,source,scope,weight,active,state) values(?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), "金蝶", "产品", "manual", "global", 8, 1, "active"),
        )
    candidates = [{"word": "金蝶", "kind": "产品"}, {"word": "新词", "kind": "术语"}]
    result = _dedupe_against_db(candidates, "rec-1")
    assert len(result) == 1
    assert result[0]["word"] == "新词"


def test_dedupe_skips_discarded(tmp_home):
    """用户已丢弃的词不再推。"""
    from backend.app.hotword_discover import _dedupe_against_db
    from backend.app.db import db
    import uuid
    with db() as conn:
        conn.execute(
            "insert into hotwords(id,word,kind,source,scope,weight,active,state) values(?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), "废弃词", "术语", "llm-discover", "global", 5, 0, "discarded"),
        )
    candidates = [{"word": "废弃词", "kind": "术语"}, {"word": "好词", "kind": "产品"}]
    result = _dedupe_against_db(candidates, "rec-1")
    assert len(result) == 1
    assert result[0]["word"] == "好词"


def test_dedupe_same_batch_duplicates(tmp_home):
    """同批候选内的重复词去重。"""
    from backend.app.hotword_discover import _dedupe_against_db
    candidates = [
        {"word": "ERP", "kind": "行业"},
        {"word": "erp", "kind": "行业"},  # 大小写重复
        {"word": "MES", "kind": "行业"},
    ]
    result = _dedupe_against_db(candidates, "rec-1")
    assert len(result) == 2  # ERP + MES，erp 被去重


def test_dedupe_skips_candidate_state(tmp_home):
    """已是 candidate 状态的词跳过（不重复推）。"""
    from backend.app.hotword_discover import _dedupe_against_db
    from backend.app.db import db
    import uuid
    with db() as conn:
        conn.execute(
            "insert into hotwords(id,word,kind,source,scope,weight,active,state) values(?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), "待审词", "术语", "llm-discover", "global", 5, 1, "candidate"),
        )
    candidates = [{"word": "待审词", "kind": "术语"}, {"word": "新词", "kind": "产品"}]
    result = _dedupe_against_db(candidates, "rec-1")
    assert len(result) == 1
    assert result[0]["word"] == "新词"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/bin/python -m pytest backend/tests/test_hotword_discover.py -k dedupe -v`
Expected: FAIL（_dedupe_against_db 不存在）

- [ ] **Step 3: 实现 _dedupe_against_db**

在 `hotword_discover.py` 加：

```python
def _dedupe_against_db(candidates: list[dict[str, Any]], recording_id: str) -> list[dict[str, Any]]:
    """过滤掉已在库的词（active/candidate/protected 跳过，discarded 跳过）+ 同批去重。"""
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
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/bin/python -m pytest backend/tests/test_hotword_discover.py -k dedupe -v`
Expected: 4 个 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/hotword_discover.py backend/tests/test_hotword_discover.py
git commit -m "feat: hotword_discover._dedupe_against_db（去重，TDD）"
```

---

## Task 4: _insert_candidates + discover_hotwords 编排

**Files:**
- Modify: `backend/app/hotword_discover.py`

**Why:** 把 LLM 抽取、去重、入库串成完整编排函数。_insert_candidates 是入库逻辑。

- [ ] **Step 1: 实现 _insert_candidates**

在 `hotword_discover.py` 加：

```python
def _insert_candidates(candidates: list[dict[str, Any]], recording_id: str) -> int:
    """候选词入库为 candidate 状态。返回插入数。"""
    import uuid
    from .db import db, now
    from .summary import transcript_text
    inserted = 0
    if not candidates:
        return 0
    # 取转写全文算 frequency
    with db() as conn:
        full_text = transcript_text(conn, recording_id)
    ts = now()
    with db() as conn:
        for c in candidates:
            word = (c.get("word") or "").strip()
            if not word:
                continue
            kind = c.get("kind") or "业务术语"
            confidence = float(c.get("confidence") or 0.5)
            example = (c.get("example") or "")[:200]
            frequency = full_text.count(word) if full_text else 1
            frequency = max(1, frequency)
            conn.execute(
                """insert into hotwords(
                    id, word, kind, source, scope, weight, active, state, protected,
                    frequency, confidence, score, example, source_key,
                    first_seen_at, last_seen_at, last_used_at, updated_at
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()), word, kind, "llm-discover", "global", 5, 1, "candidate", 0,
                    frequency, confidence, 0, example,
                    f"llm-discover:{recording_id}:{word.lower()}",
                    ts, ts, ts, ts,
                ),
            )
            inserted += 1
    return inserted
```

- [ ] **Step 2: 实现 _llm_extract_terms（LLM 调用）**

在 `hotword_discover.py` 加：

```python
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
    except Exception:
        return []  # LLM 失败不阻塞
```

注意：不设 `response_format`——部分兼容端点不支持，用 prompt 要求 JSON + _parse_llm_json 容错更稳。

- [ ] **Step 3: 实现 discover_hotwords 编排**

在 `hotword_discover.py` 加：

```python
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
```

- [ ] **Step 4: py_compile + 冒烟（无 key 时应静默返回空）**

Run: `python -m py_compile backend/app/hotword_discover.py`
Run:
```bash
RECORDING_AI_HOME=/tmp/aham-hw4 .venv/bin/python -c "
import asyncio
from backend.app.hotword_discover import discover_hotwords
r = asyncio.run(discover_hotwords('nonexistent-rec'))
print('无 key/无文本:', r)
assert r == {'discovered': 0}
print('OK')
"
```
Expected: `无 key/无文本: {'discovered': 0}` + `OK`

- [ ] **Step 5: Commit**

```bash
git add backend/app/hotword_discover.py
git commit -m "feat: hotword_discover 完整编排（LLM 抽取+去重+入库）"
```

---

## Task 5: API + 自动触发

**Files:**
- Modify: `backend/app/main.py`（4 个新 API）
- Modify: `backend/app/asr.py`（process_recording_background 加触发）

### Task 5a: 候选词 API

- [ ] **Step 1: 加候选词列表/确认/丢弃 3 个 API**

在 `backend/app/main.py` 找到 hotword 路由段（`@app.get("/api/hotwords")` 附近），在其后加：

```python
@app.get("/api/hotwords/candidates")
def hotword_candidates(
    sort: str = "frequency",
    user: dict[str, Any] = Depends(current_user),
) -> list[dict[str, Any]]:
    """候选词（state=candidate）列表。默认按频次降序。"""
    order = {"frequency": "frequency desc, confidence desc", "confidence": "confidence desc", "time": "last_seen_at desc"}.get(sort, "frequency desc, confidence desc")
    with db() as conn:
        rows = conn.execute(
            f"select * from hotwords where state = 'candidate' order by {order} limit 200"
        ).fetchall()
        return [normalize_hotword(dict(r)) for r in rows]


@app.post("/api/hotwords/candidates/confirm")
def confirm_candidates(payload: dict[str, Any], user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """批量确认候选词：state candidate→active。支持 edits 纠正写法/分类。"""
    ids = payload.get("ids") or []
    edits = payload.get("edits") or {}
    ts = now()
    confirmed = 0
    with db() as conn:
        for hid in ids:
            e = edits.get(hid, {})
            word = e.get("word")
            kind = e.get("kind")
            assignments = "state = 'active', active = 1, updated_at = ?"
            params: list[Any] = [ts]
            if word:
                assignments += ", word = ?"
                params.append(word.strip())
            if kind:
                assignments += ", kind = ?"
                params.append(kind)
            params.append(hid)
            cursor = conn.execute(f"update hotwords set {assignments} where id = ? and state = 'candidate'", params)
            confirmed += cursor.rowcount
        audit(conn, user, "hotword.confirm", f"确认 {confirmed} 个候选热词。")
    return {"confirmed": confirmed}


@app.post("/api/hotwords/candidates/discard")
def discard_candidates(payload: dict[str, Any], user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """批量丢弃候选词：state candidate→discarded。"""
    ids = payload.get("ids") or []
    ts = now()
    with db() as conn:
        cursor = conn.execute(
            "update hotwords set state = 'discarded', active = 0, updated_at = ? where id in ({}) and state = 'candidate'".format(
                ",".join("?" for _ in ids) or "''"
            ),
            [ts, *ids],
        )
        audit(conn, user, "hotword.discard", f"丢弃 {cursor.rowcount} 个候选热词。")
    return {"discarded": cursor.rowcount}
```

- [ ] **Step 2: 加重新发现 API**

在 main.py 的 recordings 路由段加：

```python
@app.post("/api/recordings/{recording_id}/discover-hotwords")
async def rediscover_hotwords(recording_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """手动重新从该录音的转写+纪要抽取候选词。"""
    from .hotword_discover import discover_hotwords
    with db() as conn:
        can_access_recording(conn, recording_id, user)
    result = await discover_hotwords(recording_id)
    return result
```

- [ ] **Step 3: normalize_hotword 加 example 字段返回**

找到 `normalize_hotword` 函数（main.py ~529），在其返回 payload 里加：

```python
payload["example"] = row.get("example")
```

- [ ] **Step 4: py_compile + 冒烟**

Run: `python -m py_compile backend/app/main.py`
Run:
```bash
RECORDING_AI_HOME=/tmp/aham-hw5 .venv/bin/python -c "
from backend.app.main import app, ensure_schema
ensure_schema()
from fastapi.testclient import TestClient
with TestClient(app) as c:
    assert c.get('/api/hotwords/candidates').status_code == 200
    assert c.post('/api/hotwords/candidates/confirm', json={'ids': []}).status_code == 200
    assert c.post('/api/hotwords/candidates/discard', json={'ids': []}).status_code == 200
print('OK: 候选词 API 通')
"
```

- [ ] **Step 5: Commit（5a）**

```bash
git add backend/app/main.py
git commit -m "feat: 候选热词 API（列表/确认/丢弃/重新发现）"
```

### Task 5b: 自动触发

- [ ] **Step 1: 改 process_recording_background**

在 `backend/app/asr.py` 找到 `process_recording_background`（~455），末尾加发现调用：

```python
def process_recording_background(recording_id: str, user: dict[str, Any]) -> None:
    try:
        transcribe_recording(recording_id, user)
        asyncio.run(summarize_recording(recording_id, user))
    except HTTPException:
        return
    # 纪要完成后触发候选词发现（转写+纪要此时都有，一次抽全）
    try:
        asyncio.run(discover_hotwords(recording_id))
    except Exception:
        pass  # 发现失败不阻塞主流程
```

注意：在 asr.py 顶部加 `from .hotword_discover import discover_hotwords`。

- [ ] **Step 2: py_compile + 冒烟**

Run: `python -m py_compile backend/app/asr.py`

- [ ] **Step 3: 全测试确认无回归**

Run: `.venv/bin/python -m pytest backend/tests/ -q`
Expected: 全部 PASS（原有 + 新增）

- [ ] **Step 4: Commit（5b）**

```bash
git add backend/app/asr.py
git commit -m "feat: 纪要完成后自动触发热词发现"
```

---

## Task 6: 前端审阅面板

**Files:**
- Modify: `frontend-src/src/api/types.ts`（候选词类型）
- Modify: `frontend-src/src/api/endpoints.ts`（候选词 API 调用）
- Create: `frontend-src/src/pages/app/HotwordCandidates.tsx`
- Modify: `frontend-src/src/pages/app/Hotwords.tsx`（tab 切换）
- Modify: `frontend-src/src/router.tsx`（候选路由）

### Task 6a: 类型 + API 调用

- [ ] **Step 1: types.ts 加候选词类型**

在 `types.ts` 加：

```typescript
export interface HotwordCandidate {
  id: string;
  word: string;
  kind: string;
  confidence: number;
  frequency: number;
  example: string | null;
  source: string;
  state: string;
  last_seen_at: string;
}
```

- [ ] **Step 2: endpoints.ts 加候选词 API**

在 `endpoints.ts` 加：

```typescript
// -------- hotword candidates --------

export async function fetchCandidates(sort?: string): Promise<HotwordCandidate[]> {
  const { data } = await api.get<HotwordCandidate[]>("/hotwords/candidates", { params: sort ? { sort } : {} });
  return data;
}

export async function confirmCandidates(ids: string[], edits?: Record<string, { word?: string; kind?: string }>): Promise<{ confirmed: number }> {
  const { data } = await api.post("/hotwords/candidates/confirm", { ids, edits: edits || {} });
  return data;
}

export async function discardCandidates(ids: string[]): Promise<{ discarded: number }> {
  const { data } = await api.post("/hotwords/candidates/discard", { ids });
  return data;
}
```

注意：endpoints.ts 顶部 import 加 `HotwordCandidate`。

- [ ] **Step 3: Commit（6a）**

```bash
git add frontend-src/src/api/types.ts frontend-src/src/api/endpoints.ts
git commit -m "feat: 前端候选词类型 + API 调用"
```

### Task 6b: 审阅面板组件

- [ ] **Step 1: 创建 HotwordCandidates.tsx**

Create `frontend-src/src/pages/app/HotwordCandidates.tsx`：

```tsx
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { confirmCandidates, discardCandidates, fetchCandidates } from "@/api/endpoints";
import { Button } from "@/components/Button";
import { Diag } from "@/components/Diag";
import { EmptyState } from "@/components/EmptyState";
import { readApiError } from "@/api/client";

export function HotwordCandidates() {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editWord, setEditWord] = useState("");
  const [editKind, setEditKind] = useState("");
  const [error, setError] = useState<string | null>(null);

  const candidates = useQuery({ queryKey: ["candidates"], queryFn: () => fetchCandidates() });

  const confirm = useMutation({
    mutationFn: (edits?: Record<string, { word?: string; kind?: string }>) => confirmCandidates([...selected], edits),
    onSuccess: () => { setSelected(new Set()); qc.invalidateQueries({ queryKey: ["candidates"] }); },
    onError: (e) => setError(readApiError(e)),
  });

  const discard = useMutation({
    mutationFn: () => discardCandidates([...selected]),
    onSuccess: () => { setSelected(new Set()); qc.invalidateQueries({ queryKey: ["candidates"] }); },
    onError: (e) => setError(readApiError(e)),
  });

  function toggle(id: string) {
    setSelected((prev) => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; });
  }

  function startEdit(id: string, word: string, kind: string) {
    setEditingId(id); setEditWord(word); setEditKind(kind);
  }

  function confirmWithEdit() {
    const edits: Record<string, { word?: string; kind?: string }> = {};
    if (editingId && (editWord || editKind)) edits[editingId] = {};
    if (editWord) edits[editingId!].word = editWord;
    if (editKind) edits[editingId!].kind = editKind;
    // 把编辑的词加入选中一起确认
    if (editingId) selected.add(editingId);
    confirm.mutate(Object.keys(edits).length > 0 ? edits : undefined);
    setEditingId(null);
  }

  const items = candidates.data ?? [];
  const allSelected = items.length > 0 && selected.size === items.length;

  return (
    <div>
      {error && <Diag code="CAND_E">{error}</Diag>}
      <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "center", marginBottom: "var(--space-4)" }}>
        <Button variant="ghost" size="sm" onClick={() => setSelected(allSelected ? new Set() : new Set(items.map((i) => i.id)))}>
          {allSelected ? "取消全选" : "全选"}
        </Button>
        <Button variant="primary" size="sm" disabled={selected.size === 0} loading={confirm.isPending} onClick={() => confirm.mutate()}>
          确认选中 ({selected.size})
        </Button>
        <Button variant="danger" size="sm" disabled={selected.size === 0} loading={discard.isPending} onClick={() => discard.mutate()}>
          丢弃选中
        </Button>
        <span className="meta" style={{ marginLeft: "auto", fontSize: "var(--text-xs)", color: "var(--fg-subtle)" }}>
          共 {items.length} 个待确认
        </span>
      </div>

      {items.length === 0 ? (
        <EmptyState title="暂无候选词" hint="转写并生成纪要后，LLM 会自动发现候选热词。" />
      ) : (
        items.map((c) => (
          <div key={c.id} style={{ display: "flex", gap: "var(--space-3)", padding: "var(--space-3)", borderBottom: "1px solid var(--border-default)", alignItems: "flex-start" }}>
            <input type="checkbox" checked={selected.has(c.id)} onChange={() => toggle(c.id)} style={{ marginTop: 4 }} />
            <div style={{ flex: 1 }}>
              {editingId === c.id ? (
                <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "center" }}>
                  <input className="field" value={editWord} onChange={(e) => setEditWord(e.target.value)} style={{ width: 140 }} />
                  <input className="field" value={editKind} onChange={(e) => setEditKind(e.target.value)} style={{ width: 100 }} />
                  <Button size="sm" variant="primary" onClick={confirmWithEdit}>保存</Button>
                  <Button size="sm" variant="ghost" onClick={() => setEditingId(null)}>取消</Button>
                </div>
              ) : (
                <>
                  <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "center" }}>
                    <strong>{c.word}</strong>
                    <span className="tag">{c.kind}</span>
                    <span className="meta" style={{ fontSize: "var(--text-xs)" }}>频次×{c.frequency}</span>
                    {c.confidence < 0.6 && <span style={{ color: "var(--amber-600, #d97706)", fontSize: "var(--text-xs)" }}>⚠️ 疑似</span>}
                    <span className="meta" style={{ fontSize: "var(--text-xs)", color: "var(--fg-subtle)" }}>{c.confidence.toFixed(2)}</span>
                  </div>
                  {c.example && <div className="meta" style={{ fontSize: "var(--text-xs)", color: "var(--fg-muted)", marginTop: 2 }}>例：{c.example}</div>}
                  <Button size="sm" variant="ghost" onClick={() => startEdit(c.id, c.word, c.kind)} style={{ marginTop: 4, padding: 0 }}>✎ 编辑</Button>
                </>
              )}
            </div>
          </div>
        ))
      )}
    </div>
  );
}
```

- [ ] **Step 2: Hotwords.tsx 加 tab 切换**

在 `Hotwords.tsx` 的组件顶部加 tab 状态 + 渲染切换。在 `return` 的最外层 div 里，PageHead 下方加：

```tsx
const [tab, setTab] = useState<"active" | "candidates">("active");
```

在 PageHead 下方加 tab 切换栏（两个按钮），tab==="candidates" 时渲染 `<HotwordCandidates />`，否则渲染现有热词列表。import HotwordCandidates。

- [ ] **Step 3: router.tsx 加候选路由（可选，如果用子路由）**

如果 Hotwords 用 tab 而非独立路由，这步可跳过。tab 在 Hotwords 组件内部切换即可。

- [ ] **Step 4: 构建验证**

Run: `cd frontend-src && npm run build`
Expected: 无 TS 错误，dist 产出

- [ ] **Step 5: Commit（6b）**

```bash
git add frontend-src/
git commit -m "feat: 候选热词审阅面板（批量勾选/确认/丢弃/行内编辑）"
```

---

## Self-Review 记录

**Spec coverage:**
- 第一节数据流 → Task 4 编排 ✓
- 第二节 LLM 抽取 → Task 4 _llm_extract_terms ✓
- 第三节数据模型 → Task 1 example 字段 + Task 3 去重 ✓
- 第四节 API → Task 5a ✓
- 第五节触发 → Task 5b ✓
- 第六节前端 → Task 6 ✓
- 测试 → Task 2/3 TDD + Task 5 冒烟 ✓

**Placeholder 扫描：** 无 TBD/TODO，每步有完整代码。

**命名一致性：** `_parse_llm_json`/`_dedupe_against_db`/`_insert_candidates`/`discover_hotwords`/`_llm_extract_terms` 跨 task 引用一致。`state='candidate'`/`'discarded'` 一致。API 路径 `/api/hotwords/candidates` 前后端一致。
