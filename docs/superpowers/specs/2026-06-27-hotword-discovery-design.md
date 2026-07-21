# 热词智能发现与候选审阅设计

> 日期：2026-06-27
> 状态：设计已批准，待出实施计划
> 改造对象：aham-voice-web（web-conversion 分支）

## 一、目标

给热词系统增加"智能发现"输入源：从转写文本和会议纪要中，用 LLM 自动抽取候选热词（专业术语/产品/人名/疑似错字等），形成待选清单，用户在批量审阅面板里逐个确认/纠正/丢弃。

现有热词输入只有"手工添加"和"txt 导入"两种。本功能补齐第三种——LLM 自动发现，降低热词维护成本。

### 原项目预留的基础设施

原项目为"自动发现"预留了半成品但从未实现：
- `hotword_sources` 表（来源管理）+ `hotword_sync_runs` 表（同步记录）
- `state`/`confidence`/`frequency`/`source_key` 字段（为候选词/去重准备）
- 本设计激活这些设施，最小改动

## 二、决策汇总

| 维度 | 决策 |
|---|---|
| 词源 | 转写文本 + 纪要（两者互补） |
| 发现算法 | LLM 抽取（复用 OpenAI 兼容端点） |
| 发现时机 | 转写+纪要完成后自动触发（BackgroundTasks，用户无感） |
| 确认 UI | 批量审阅面板（单独一页，带来源/频次/例句） |
| 去重 | 自动过滤已有热词（active/candidate）+ 已丢弃词（discarded） |

## 三、数据流

```
转写完成 → summarize_recording（纪要生成）
                    │
                    ▼
          discover_hotwords(recording_id)   ← 纪要后触发一次
                    │
         ┌──────────┴──────────┐
         │ 1. 收集文本          │
         │   transcript_text()  │  ← 转写逐字稿
         │   summary content    │  ← 纪要（含 LLM 标的"疑似"词）
         ├──────────────────────┤
         │ 2. LLM 抽取           │
         │   _llm_extract_terms  │
         │   response_format=json│
         │   temperature=0.1     │
         │   复用 _deepseek_post │
         ├──────────────────────┤
         │ 3. 去重过滤           │
         │   _dedupe_against_db  │
         │   跳过 active/candidate│
         │   跳过 discarded      │
         ├──────────────────────┤
         │ 4. 入库（候选状态）    │
         │   insert hotwords     │
         │   state='candidate'   │
         │   source='llm-discover'│
         │   confidence=LLM 置信度│
         │   frequency=出现次数   │
         │   example=原话例句     │
         └──────────────────────┘
```

只在纪要后触发一次（转写+纪要此时都有，一次调用抽全两个来源，省 LLM 成本）。

## 四、LLM 抽取实现

### 新模块：backend/app/hotword_discover.py

独立于 hotwords.py（hotwords.py 是纯本地逻辑：打分/双轨包/过滤；发现是 LLM 调用 + 文本处理，职责不同）。

### 核心函数

```python
async def discover_hotwords(recording_id: str) -> dict:
    """转写/纪要后自动调用。从文本抽候选词，去重后入库。"""
    # 1. 收集文本
    with db() as conn:
        rec = can_access_recording(conn, recording_id, _LOCAL_USER)
        transcript = transcript_text(conn, recording_id)
        summary = 最新纪要内容（若有）
    if not transcript.strip():
        return {"discovered": 0}

    # 2. LLM 抽取（转写 + 纪要合并一次调用）
    candidates = await _llm_extract_terms(transcript, summary)

    # 3. 去重
    candidates = _dedupe_against_db(candidates, recording_id)

    # 4. 入库为 candidate 状态
    _insert_candidates(candidates, recording_id)
    return {"discovered": len(candidates)}
```

### System prompt

```
你是术语抽取助手。从会议转写和纪要中识别值得加入热词库的词。
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
- 只返回 JSON：{"terms": [{word,kind,confidence,example,is_uncertain}]}
```

### LLM 调用

```python
async def _llm_extract_terms(transcript: str, summary: str) -> list[dict]:
    api_key, base, model = get_llm_config()
    payload = {
        "model": model,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},  # 强制 JSON
        "messages": [
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": f"转写：\n{transcript[:16000]}\n\n纪要：\n{summary[:4000]}"},
        ],
    }
    async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
        content = await _deepseek_post_with_retry(client, f"{base}/chat/completions", api_key, payload)
    return _parse_llm_json(content)
```

### 关键取舍

1. **response_format: json_object**：强制 JSON。OpenAI 兼容端点都支持。不支持时 fallback 到正则提取 `{...}`。
2. **转写截断 16000 字 + 纪要 4000 字**：长会议截断取首尾，先不分块（YAGNI）。
3. **转写 + 纪要一次调用**：省一半 LLM 成本。
4. **temperature 0.1**：抽取要稳定可复现。
5. **无 LLM key 时跳过**：和纪要逻辑一致（无 key 则功能不可用，不报错）。

## 五、数据模型 + 去重

### 复用 hotwords 表（不新建表）

```
hotwords 表：
  state        新增值：'candidate'（待确认）/ 'discarded'（用户丢弃）
  source       新增值：'llm-discover'
  confidence   LLM 给的置信度（已有字段）
  frequency    在文本中的出现次数（已有字段）
  source_key   'llm-discover:{recording_id}:{word}'（去重用，已有字段）
  example      【新增字段】原话例句（≤30字）
```

### schema 迁移（只加 1 个字段）

```sql
alter table hotwords add column example text;
```

### 状态流转

```
LLM 抽取 → candidate（待确认）
              │
   ┌──────────┼──────────┐
   ▼          ▼          ▼
 用户确认   用户编辑    用户丢弃
   │          │          │
   ▼          ▼          ▼
 active     active    discarded
（参与双轨包）（纠正后参与） （去重时跳过）
```

### 去重逻辑

```python
def _dedupe_against_db(candidates, recording_id):
    with db() as conn:
        existing = {已有词（active/candidate/protected）的小写集合}
        discarded = {已丢弃词（discarded）的小写集合}
    result = []
    for c in candidates:
        w = c["word"].lower()
        if w in existing or w in discarded:
            continue
        existing.add(w)  # 同批内去重
        result.append(c)
    return result
```

### 与双轨包的关系

- `build_hotword_package`：查询条件 `state in ('active','protected')`，**不含 candidate**
- `hotword_prompt`（ASR 热词）：同上
- `maintain_hotwords`（生命周期）：candidate **不自动过期**（等用户审），只有 active 按时间衰减
- `load_hotword_map`（纠偏）：只用 active
- **确认前候选词对转写零影响**

## 六、API

### 新增 3 个 API

```
GET  /api/hotwords/candidates
  → state='candidate' 的候选词列表
    [{id, word, kind, confidence, frequency, example,
      source, recording_id, created_at}]
  排序：按频次/置信度/时间（默认频次降序）
  分页：默认 50 条/页

POST /api/hotwords/candidates/confirm
  body: {ids: [...], edits?: {id: {word?, kind?, weight?}}}
  → 批量确认：state candidate→active
    edits 可选：确认时纠正写法/分类
  → 确认后立即参与双轨包

POST /api/hotwords/candidates/discard
  body: {ids: [...]}
  → 批量丢弃：state candidate→discarded
```

### 手动重新发现（兜底）

```
POST /api/recordings/{id}/discover-hotwords
→ 重新从该录音的转写+纪要抽取候选词
用途：改纪要后重抽、自动失败后重试
```

## 七、前端：候选审阅面板

### 路由

Hotwords 页加 tab：「正式热词」（现有）|「候选审阅 (N)」（新，N=候选数 badge）

```
pages/app/Hotwords.tsx              # 加 tab 切换
pages/app/HotwordCandidates.tsx     # 新：候选审阅面板
```

### 布局

```
┌─────────────────────────────────────────────────┐
│ 候选词审阅              [全选] [确认选中] [丢弃选中] │
│ 来源：LLM 自动发现 · 共 N 个待确认                  │
├─────────────────────────────────────────────────┤
│ ☐ 金蝶接口    项目  频次×12  确信 0.92             │
│   例："金蝶接口那边报了个错"——张经理                 │
│   [编辑✎]                                          │
├─────────────────────────────────────────────────┤
│ ☐ 帕萨思      产品  频次×3   疑似 0.45  ⚠️         │
│   例："帕萨思这个产品还没上线"——李总                 │
│   [编辑✎]  ← 纠正写法（如改成"帕萨斯"）             │
└─────────────────────────────────────────────────┘
```

### 交互

1. **排序**：默认频次降序，疑似词（confidence < 0.6）带 ⚠️
2. **勾选 + 批量操作**：顶部确认/丢弃选中
3. **行内编辑**：点 ✎ 展开 word/kind 编辑，保存走 confirm 带 edits
4. **疑似词高亮**：is_uncertain / confidence < 0.6 标黄 ⚠️
5. **来源信息**：每条显示来自哪条录音 + 发现时间
6. **重新扫描**：审阅面板顶部按钮，触发 POST /discover-hotwords

## 八、自动触发衔接

### 改 1 处现有代码

```python
# asr.py process_recording_background（转写+纪要后）
def process_recording_background(recording_id, user):
    transcribe_recording(recording_id, user)
    asyncio.run(summarize_recording(recording_id, user))
    # 新增：纪要完成后触发候选词发现（转写+纪要此时都有）
    try:
        asyncio.run(discover_hotwords(recording_id))
    except Exception:
        pass  # 不阻塞主流程
```

只在纪要后触发一次（转写+纪要合并抽，省成本）。手动转写（auto_process=false）不触发（用户主动关了自动处理）。

## 九、边界处理

| 情况 | 处理 |
|---|---|
| LLM 未配置 key | 跳过发现，不报错（和纪要一致） |
| LLM 调用失败 | 记录日志，不阻塞（转写/纪要已成功） |
| LLM 返回非 JSON | _parse_llm_json 容错：正则提取 {...}，失败跳过 |
| 转写为空 | 不触发发现 |
| 纪要失败 | 仍从转写抽取（纪要是可选来源） |
| 同一录音重复发现 | source_key 去重，不重复入库 |
| LLM 抽出已有热词 | _dedupe_against_db 过滤 |
| 候选词积压 | 审阅面板分页（50 条/页） |

## 十、测试策略

- `_llm_extract_terms`：mock LLM 返回，测 JSON 解析 + 容错
- `_dedupe_against_db`：纯函数，测各种重复情况（已有/已丢弃/同批重复）
- `_parse_llm_json`：测正常 JSON / markdown 包裹 / 空响应
- 候选词 API：TestClient 测 confirm（含 edits）/ discard 状态流转
- `discover_hotwords`：mock LLM，测端到端（转写文本→候选入库）

## 十一、文件改动清单

**新增：**
- `backend/app/hotword_discover.py`（发现算法 + LLM 抽取 + 去重 + 入库）
- `backend/tests/test_hotword_discover.py`（单测）
- `frontend-src/src/pages/app/HotwordCandidates.tsx`（审阅面板）

**修改：**
- `backend/app/db.py`：ensure_schema 加 `example` 字段迁移
- `backend/app/asr.py`：process_recording_background 末尾加 discover_hotwords 调用
- `backend/app/main.py`：加 3 个候选词 API + 1 个重新发现 API
- `frontend-src/src/api/endpoints.ts`：加候选词 API 调用
- `frontend-src/src/api/types.ts`：加候选词类型
- `frontend-src/src/pages/app/Hotwords.tsx`：加 tab 切换
- `frontend-src/src/router.tsx`：加候选审阅路由
