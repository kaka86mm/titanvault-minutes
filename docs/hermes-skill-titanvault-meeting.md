---
name: titanvault-meeting
title: TitanVault 会议纪要
description: 把用户发来的录音转成结构化会议纪要（Word docx）。收到录音后先问几个问题（标题/会议类型/客户项目/预计人数），填好后提交本机 titanvault-minutes 服务，自动完成转写+说话人分离+LLM 纪要生成（含方言纠错），结果以 docx 文件返回。触发词：会议纪要、做个纪要、整理会议、整理录音、会议记录、meeting minutes、帮我把这个会议整理一下。
tags: [meeting-minutes, summary, docx, feishu, voice-message, 录音, 纪要, 会议]
triggers:
  - User sends an audio file (mp3, wav, m4a, ogg, opus, flac, aac) and expects meeting minutes
  - User sends a voice message via Feishu/Telegram and says "会议纪要"/"整理会议"
  - User asks to generate meeting minutes from a recording
  - User says "会议纪要"/"做个纪要"/"整理会议"/"会议记录"/"帮我把这个会议整理一下"
---

# TitanVault 会议纪要 Skill

把录音转成结构化会议纪要（Word docx），复用本机 titanvault-minutes 服务的 FunASR 转写 + 说话人分离 + LLM 纪要能力（含方言口音纠错）。

## 关键原则

**收到录音后不要直接提交。先问用户几个问题**（对应 titanvault-minutes 表单字段），收集完整后再提交。这能显著提升纪要质量——会议类型决定纪要结构，客户/项目参与热词命中，预计人数让说话人分离更准。

## 环境约定

- **titanvault-minutes 服务**：`http://127.0.0.1:8800`（本机 Docker，host 网络）
- **认证**：`Authorization: Bearer $TITANVAULT_API_TOKEN`（固定 token，从环境变量读，已配置；旧名 $AHAMVOICE_API_TOKEN 仍兼容）
- **飞书音频格式**：通常 `.ogg`（Opus），服务端自动转码，无需预先转换
- **处理时长**：1 小时录音约 3-5 分钟（GPU 加速 + 纪要生成）

## 工作流

### Step 0: 确认服务可用

```bash
curl -sf -H "Authorization: Bearer $TITANVAULT_API_TOKEN" \
  http://127.0.0.1:8800/api/health > /dev/null && echo "服务正常" || echo "服务不可用"
```

如果服务不可用，告诉用户"titanvault-minutes 服务没启动，请联系管理员"，不要继续。

### Step 1: 收到录音 → 问用户表单问题（关键！）

收到录音文件后，**先回应用户并提问**，不要直接提交：

```
✅ 已收到录音（${文件名}，${时长}）。

生成会议纪要前，请告诉我：
1. **会议标题**？（如：7月销售周会）
2. **会议类型**？（内部会议 / 客户调研 / 方案汇报 / 销售电话，默认"内部会议"）
3. **客户/项目**？（可选，如：浙江南都电源·储能项目，会参与热词命中）
4. **预计说话人数**？（可选，填了让说话人分离更准，不确定可留空）

直接回复即可，如："标题：周三产品评审会；类型：内部会议；人数：5"
```

**推断规则**（减少用户负担）：
- 用户消息里已包含标题/类型等信息 → 提取后只确认，不重复问
- 用户说"不用问了直接弄"→ 用合理默认值提交（标题=文件名，类型=内部会议，其余留空）
- 用户只回了一部分 → 用已答的 + 默认值补全，不再追问

### Step 2: 提交录音 + 启动处理

收集到信息后，上传录音并启动自动处理（转写+纪要）：

```bash
# AUDIO_PATH=录音文件路径
# TITLE=会议标题（用户提供或文件名）
# MEETING_TYPE=会议类型（默认内部会议）
# TAG=客户/项目（可选）
# EXPECTED_SPK=预计说话人数（可选，留空则不传）
RESPONSE=$(curl -sf -X POST http://127.0.0.1:8800/api/recordings \
  -H "Authorization: Bearer $TITANVAULT_API_TOKEN" \
  -F "file=@${AUDIO_PATH}" \
  -F "title=${TITLE}" \
  -F "meeting_type=${MEETING_TYPE}" \
  -F "tag=${TAG}" \
  ${EXPECTED_SPK:+-F "expected_speakers=${EXPECTED_SPK}"} \
  -F "auto_process=true")

REC_ID=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
```

提交后**立即告诉用户**："已收到「${TITLE}」（${MEETING_TYPE}），正在生成纪要，预计 3-5 分钟，完成后发 Word 文件给你。"

### Step 3: 轮询处理状态（每 20 秒查一次）

```bash
# 最长等待 10 分钟（30 次 × 20 秒）
for i in $(seq 1 30); do
  sleep 20
  STATUS=$(curl -sf -H "Authorization: Bearer $TITANVAULT_API_TOKEN" \
    http://127.0.0.1:8800/api/recordings/$REC_ID \
    | python3 -c "import sys,json; d=json.load(sys.stdin); r=d.get('recording',d); print(r['asr_status']+'/'+r['summary_status'])")

  if echo "$STATUS" | grep -q 'done/done'; then break; fi
  if echo "$STATUS" | grep -q 'failed'; then break; fi
  if [ $i -eq 30 ]; then STATUS="timeout"; fi
done
```

### Step 4a: 成功 → 下载 docx 纪要

```bash
if [ "$STATUS" = "done/done" ]; then
  DETAIL=$(curl -sf -H "Authorization: Bearer $TITANVAULT_API_TOKEN" \
    http://127.0.0.1:8800/api/recordings/$REC_ID)
  SID=$(echo "$DETAIL" | python3 -c "
import sys,json
d=json.load(sys.stdin)
s=d.get('summaries',[])
cur=[x for x in s if x.get('is_current')] or s
print(cur[0]['id'] if cur else '')
")
  REC_TITLE=$(echo "$DETAIL" | python3 -c "import sys,json; d=json.load(sys.stdin); r=d.get('recording',d); print(r.get('title','纪要'))")

  SAFE_TITLE=$(echo "$REC_TITLE" | tr '/\\:*?\"<>|' '_')
  DOCX_PATH="/tmp/${SAFE_TITLE}_纪要.docx"
  curl -sf -H "Authorization: Bearer $TITANVAULT_API_TOKEN" \
    "http://127.0.0.1:8800/api/recordings/$REC_ID/export/summaries/$SID.md?format=docx" \
    -o "$DOCX_PATH"
fi
```

### Step 4b: 失败或超时

```bash
if [ "$STATUS" != "done/done" ]; then
  echo "纪要未能完成（状态: $STATUS）。可去 Web 端查看: http://100.66.1.22:8800（录音ID: $REC_ID）"
  exit 0
fi
```

### Step 5: 把结果发给用户

```bash
# 下载 markdown 取「一句话概览」作为摘要
curl -sf -H "Authorization: Bearer $TITANVAULT_API_TOKEN" \
  "http://127.0.0.1:8800/api/recordings/$REC_ID/export/summaries/$SID.md" \
  -o /tmp/_summary_preview.md
python3 -c "
content=open('/tmp/_summary_preview.md',encoding='utf-8').read()
import re
m=re.search(r'##\s*一句话概览\s*\n(.+?)(?=\n##|\Z)', content, re.S)
print(m.group(1).strip()[:200] if m else '纪要已生成')
"
```

**回复格式**：
```
✅ 会议纪要已生成：${REC_TITLE}

${一句话概览}

📎 Word 文件见附件。
Web 端查看：http://100.66.1.22:8800
```

把 `$DOCX_PATH` 作为文件附件发送。

## 注意事项

- **必须先问表单问题再提交**，不要收到录音就自动跑（除非用户明确说"不用问直接弄"）
- **不要预先转码音频**：服务端 ffmpeg 处理，飞书 .ogg/.mp3/.m4a 都支持
- **超时处理**：10 分钟未完成就告诉用户去 Web 看
- **token 从环境变量读**：`$TITANVAULT_API_TOKEN`（旧名 `$AHAMVOICE_API_TOKEN` 仍兼容）
- **并发**：转写有全局锁，同时多个录音会排队

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| health 不通 | 容器没启动 | `docker compose -f docker-compose.yml -f docker-compose.rocm.yml up -d` |
| 401 | token 失效 | 检查 `$TITANVAULT_API_TOKEN` |
| 413 | 文件超限 | 默认 2GB |
| 一直 running | 排队/GPU 占用 | `docker logs aham-voice-web-ahamvoice-1` |
| summary failed | LLM Key 失效 | 检查 `.env` 的 LLM 配置 |
