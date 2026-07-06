from __future__ import annotations

import csv
import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

import httpx
from fastapi import BackgroundTasks, Body, Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles


ROOT = Path(__file__).resolve().parents[2]

from .config import (
    BASE, APP_DATA, DB_PATH, RECORDINGS, EXPORTS, TMP,
    MODELS, VAD, PUNC, PARAFORMER, CAMPLUS, EMOTION, VOICEPRINTS,
    BIN_DIR, FFMPEG, FFPROBE, CONFIG_PATH,
    load_user_config, save_user_config, get_llm_config,
    env_int, env_float, env_bool, env_json, env_compat,
)


app = FastAPI(title="TitanVault Minutes API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://0.0.0.0:5173",
    ],
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1|0\.0\.0\.0|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(1[6-9]|2\d|3[0-1])\.\d+\.\d+):5173$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)
# 单密码门（密码为空则不拦截）。auth router 提供登录端点 + 单例 Security。
from .routes.auth import router as _auth_router, get_security
from .security import SecurityMiddleware
app.add_middleware(SecurityMiddleware, security=get_security())
app.include_router(_auth_router)

from . import state
from .state import (
    asr_lock as _asr_lock,
    asr_init_lock as _asr_init_lock,
    verifier_init_lock as _verifier_init_lock,
    emotion_init_lock as _emotion_init_lock,
    DEFAULT_VOICEPRINT_THRESHOLD,
)
# 模型单例（_asr_model/_speaker_verifier/_emotion_model）通过 state 模块属性
# 读写（state.asr_model = ...），不再用 main.py 的全局变量。
# 锁用别名（_asr_lock 等）保持现有 with _asr_lock 引用不变。

from .db import (
    db, now, rowdict, rowsdict, safe_json, slug, clean_sensevoice_text,
    seconds_label, parse_local_time, parse_time,
    ensure_schema, recover_interrupted_tasks, sweep_tmp_and_exports,
    _start_cleanup_loop, audit, get_setting, set_setting,
    can_access_recording, recording_payload, task_payload,
    update_task, create_task,
)
from .deepseek import _deepseek_post_with_retry, call_deepseek_emotion
from .hotwords import (
    HOTWORD_KIND_PRIORITY, code_like_hotword, load_hotword_map, apply_hotwords,
    hotword_terms, hotword_row_score, hotword_limits, build_hotword_package,
    latest_hotword_package, valid_asr_hotword, hotword_prompt,
)
from .voiceprint import (
    normalize_profile, get_speaker_verifier, voiceprint_threshold_default,
    clamp_voiceprint_threshold, voiceprint_match_settings,
    ranked_voiceprint_intervals, aggregate_voiceprint_scores,
    load_speaker_profiles, extract_interval, concat_audio,
    match_speaker_profiles, normalize_speaker_id, resolve_voiceprint_scope,
    segment_quality, candidate_sample_rows, speaker_candidate_payload,
    can_manage_voiceprint,
)
from .emotion import (
    emotion_label_cn, get_emotion_model, analyze_segment_emotion,
    analyze_acoustic_emotions, acoustic_markdown, emotion_annotated_transcript,
    next_emotion_version, current_emotion_analysis, generate_emotion_analysis,
    run_emotion_job,
)
from .summary import (
    transcript_text, summary_depth_instruction, meeting_focus_instruction,
    meeting_template, call_deepseek_summary, call_deepseek_revision,
    next_summary_version, summarize_recording, revise_summary,
    transcript_markdown, write_export, write_summary_export,
)
from .asr import (
    recover_queued_recordings, split_audio, get_asr_model,
    normalized_transcript_text, bare_transcript_text, is_filler_transcript,
    transcript_needs_continuation, join_transcript_text,
    semantic_segment_settings, merge_transcript_items,
    sentence_info_to_transcript_segments, transcribe_recording,
    process_recording_background,
)


def probe_duration(path: Path) -> float:
    if not FFPROBE.exists():
        return 0.0
    proc = subprocess.run(
        [
            str(FFPROBE),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


# Single-user mode: one fixed local user owns all data. No users table, no
# login state (the access-password gate lives in security.py / Task 6). The
# Header/Query params on current_user are kept so existing route signatures
# (Depends(current_user)) and media URLs carrying ?token= keep working.
# _LOCAL_USER / LOCAL_USER_ID 定义在 state.py（共享单用户身份，db.py 也用）。
from .state import LOCAL_USER_ID, _LOCAL_USER


def current_user(
    authorization: str | None = Header(default=None),
    token_query: str | None = Query(default=None, alias="token"),
) -> dict[str, Any]:
    # No DB lookup — fixed in-process user. Params accepted but ignored.
    return _LOCAL_USER


# ---------------------------------------------------------------------------
# 对话情绪分析（独立的第 3 类产物）
#   B 声学层：emotion2vec 逐段识别说话情绪（生气/难过/开心…）
#   A 语义层：DeepSeek 结合带情绪标注的转写做对话情绪分析（意向 + 异议）
#   只导出 markdown；不产出任何行动项 / 跟进建议。
# ---------------------------------------------------------------------------

@app.on_event("startup")
def startup() -> None:
    ensure_schema()
    # 容器场景：模型 volume 挂载，首次启动检测缺失并下载（Docker 内 modelscope 可用时）。
    # 本地开发无 modelscope 时 ensure_models 内部跳过（只警告），不阻塞启动。
    from .model_download import ensure_models
    from .config import MODELS
    try:
        ensure_models(MODELS)
    except Exception as exc:
        print(f"[startup] 模型检测/下载失败（不阻塞，转写时再报）: {exc}", flush=True)
    recover_interrupted_tasks()
    recover_queued_recordings()
    _start_cleanup_loop()


@app.get("/api/me")
def me(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    return user


@app.get("/api/health")
def health() -> dict[str, bool]:
    """健康检查端点。Docker HEALTHCHECK 和密码门白名单都用它，无需鉴权。"""
    return {"ok": True}


def _settings_view() -> dict[str, Any]:
    api_key, base, model = get_llm_config()
    # 同时返回 llm_* (新) 和 deepseek_* (旧别名)，前端切换期间两边都能读。
    return {
        "llm_configured": bool(api_key),
        "llm_api_base": base,
        "llm_model": model,
        # 旧别名（前端改造完成前兼容；读取时 get_llm_config 已回退 deepseek_*）
        "deepseek_configured": bool(api_key),
        "deepseek_api_base": base,
        "deepseek_model": model,
    }


@app.get("/api/settings")
def get_settings(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    return _settings_view()


@app.patch("/api/settings")
def patch_settings(
    payload: dict[str, Any] = Body(...),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    # 接受 llm_* (新) 和 deepseek_* (旧)，统一存到 llm_* 键。
    api_key = payload.get("llm_api_key")
    if api_key is None:
        api_key = payload.get("deepseek_api_key")
    if api_key is not None:
        updates["llm_api_key"] = api_key.strip()
    api_base = payload.get("llm_api_base")
    if api_base is None:
        api_base = payload.get("deepseek_api_base")
    if api_base is not None:
        updates["llm_api_base"] = api_base.strip()
    model = payload.get("llm_model")
    if model is None:
        model = payload.get("deepseek_model")
    if model is not None:
        updates["llm_model"] = model.strip()
    if updates:
        save_user_config(updates)
    return _settings_view()


@app.get("/api/recordings")
def recordings(
    scope: str = "mine",
    q: str = "",
    meeting_type: str = "",
    user: dict[str, Any] = Depends(current_user),
) -> list[dict[str, Any]]:
    # Single-user mode: every recording belongs to local-admin, no scoping by
    # owner/team. Only optional q (title/filename/tag) and meeting_type filters.
    filters: list[str] = []
    args: list[Any] = []
    if meeting_type and meeting_type != "全部":
        filters.append("recordings.meeting_type = ?")
        args.append(meeting_type)
    if q.strip():
        like = f"%{q.strip()}%"
        filters.append("(recordings.title like ? or recordings.filename like ? or recordings.tag like ?)")
        args.extend([like, like, like])
    where = (" where " + " and ".join(f"({f})" for f in filters)) if filters else ""
    with db() as conn:
        rows = conn.execute(
            f"select * from recordings{where} order by recordings.updated_at desc",
            args,
        ).fetchall()
        return [recording_payload(conn, dict(row)) for row in rows]


@app.post("/api/recordings")
def upload_recording(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    title: str = Form(...),
    meeting_type: str = Form("内部会议"),
    tag: str = Form(""),
    auto_process: bool = Form(True),
    expected_speakers: int | None = Form(None),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    if user["role"] == "admin":
        raise HTTPException(status_code=403, detail="admin cannot upload recordings")
    rec_id = str(uuid.uuid4())
    ext = Path(file.filename or "recording.mp3").suffix or ".mp3"
    target = RECORDINGS / f"{rec_id}{ext}"
    max_mb = env_int("AHAMVOICE_UPLOAD_MAX_MB", 2048, 16, 16384)
    max_bytes = max_mb * 1024 * 1024
    chunk_size = 1024 * 1024  # 1 MB
    written = 0
    try:
        with target.open("wb") as out:
            while True:
                chunk = file.file.read(chunk_size)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    out.close()
                    target.unlink(missing_ok=True)
                    file.file.close()
                    raise HTTPException(
                        status_code=413,
                        detail=f"upload exceeds {max_mb} MB limit (AHAMVOICE_UPLOAD_MAX_MB)",
                    )
                out.write(chunk)
    except HTTPException:
        raise
    except Exception:
        target.unlink(missing_ok=True)
        raise
    if written == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="uploaded file is empty")
    duration = probe_duration(target)
    with db() as conn:
        conn.execute(
            """
            insert into recordings(id,title,filename,file_path,meeting_type,tag,owner_id,team_id,duration,duration_label,asr_status,summary_status,expected_speakers,created_at,updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                rec_id,
                title.strip() or Path(file.filename or "录音").stem,
                file.filename or target.name,
                str(target),
                meeting_type,
                tag,
                user["id"],
                user.get("team_id"),
                duration,
                seconds_label(duration),
                "queued" if auto_process else "pending",
                "pending",
                (expected_speakers if (expected_speakers and 2 <= expected_speakers <= 50) else None),
                now(),
                now(),
            ),
        )
        audit(conn, user, "recording", f"上传录音：{title.strip() or file.filename}。")
        rec = rowdict(conn.execute("select * from recordings where id = ?", (rec_id,)).fetchone())
        payload = recording_payload(conn, rec)
    if auto_process:
        background_tasks.add_task(process_recording_background, rec_id, dict(user))
    return payload


@app.post("/api/recordings/{recording_id}/process")
def process_api(
    recording_id: str,
    background_tasks: BackgroundTasks,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    with db() as conn:
        rec = can_access_recording(conn, recording_id, user)
        if rec["asr_status"] == "running" or rec["summary_status"] == "running":
            raise HTTPException(status_code=409, detail="recording is already processing")
        conn.execute(
            "update recordings set asr_status = ?, summary_status = ?, updated_at = ? where id = ?",
            ("queued", "pending", now(), recording_id),
        )
        audit(conn, user, "recording.process", f"{user['name']} 启动完整处理：{rec['title']}。")
    background_tasks.add_task(process_recording_background, recording_id, dict(user))
    return {"recording_id": recording_id, "status": "queued"}


@app.post("/api/recordings/process-and-wait")
def process_and_wait(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    title: str = Form(...),
    meeting_type: str = Form("内部会议"),
    tag: str = Form(""),
    expected_speakers: int | None = Form(None),
    timeout_seconds: int = Form(600),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    """上传录音 + 同步等待转写+纪要完成，返回结果（供 Dify/脚本调用）。

    与 /api/recordings 的区别：这个端点阻塞直到处理完成（或超时），
    调用方一次请求拿到结果，不用自己轮询。适合 Dify HTTP 节点等
    同步场景。超时返回 recording_id，调用方可去 Web 端查看。

    返回：{recording_id, status: done/timeout/failed, summary_id, overview, web_url}
    """
    import time as _time
    # 复用上传逻辑
    rec_id = str(uuid.uuid4())
    ext = Path(file.filename or "recording.mp3").suffix or ".mp3"
    target = RECORDINGS / f"{rec_id}{ext}"
    chunk_size = 1024 * 1024
    written = 0
    try:
        with target.open("wb") as out:
            while True:
                chunk = file.file.read(chunk_size)
                if not chunk:
                    break
                written += len(chunk)
                if written > env_int("AHAMVOICE_UPLOAD_MAX_MB", 2048, 16, 16384) * 1024 * 1024:
                    out.close()
                    target.unlink(missing_ok=True)
                    file.file.close()
                    raise HTTPException(status_code=413, detail="upload exceeds limit")
                out.write(chunk)
    except HTTPException:
        raise
    except Exception:
        target.unlink(missing_ok=True)
        raise
    if written == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="uploaded file is empty")
    duration = probe_duration(target)
    with db() as conn:
        conn.execute(
            """insert into recordings(id,title,filename,file_path,meeting_type,tag,owner_id,team_id,duration,duration_label,asr_status,summary_status,expected_speakers,created_at,updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rec_id, title.strip(), file.filename or target.name, str(target), meeting_type, tag,
             user["id"], user.get("team_id"), duration, seconds_label(duration),
             "queued", "pending",
             (expected_speakers if (expected_speakers and 2 <= expected_speakers <= 50) else None),
             now(), now()),
        )
        conn.commit()
    # 后台触发转写+纪要
    background_tasks.add_task(process_recording_background, rec_id, dict(user))

    # 阻塞轮询等完成
    deadline = _time.time() + min(max(timeout_seconds, 60), 1200)
    final_status = "timeout"
    while _time.time() < deadline:
        _time.sleep(10)
        with db() as conn:
            rec = rowdict(conn.execute("select asr_status, summary_status from recordings where id = ?", (rec_id,)).fetchone())
        if not rec:
            final_status = "failed"
            break
        s = f"{rec['asr_status']}/{rec['summary_status']}"
        if "failed" in s:
            final_status = "failed"
            break
        if s == "done/done":
            final_status = "done"
            break

    # 组装结果
    result: dict[str, Any] = {
        "recording_id": rec_id,
        "status": final_status,
        "web_url": f"http://100.66.1.22:8800",
    }
    if final_status == "done":
        with db() as conn:
            summ = rowdict(conn.execute(
                "select id, content from summaries where recording_id = ? and is_current = 1 order by version desc limit 1",
                (rec_id,),
            ).fetchone())
        if summ:
            result["summary_id"] = summ["id"]
            result["docx_url"] = f"/api/recordings/{rec_id}/export/summaries/{summ['id']}.md?format=docx"
            # 提取一句话概览
            import re
            m = re.search(r"##\s*一句话概览\s*\n(.+?)(?=\n##|\Z)", summ["content"] or "", re.S)
            result["overview"] = (m.group(1).strip()[:300] if m else "纪要已生成")
    return result


@app.get("/api/recordings/{recording_id}")
def recording_detail(recording_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with db() as conn:
        rec = can_access_recording(conn, recording_id, user)
        rec_payload = recording_payload(conn, rec)
        segments = rowsdict(
            conn.execute(
                "select * from transcript_segments where recording_id = ? order by start_sec",
                (recording_id,),
            ).fetchall()
        )
        summary = rowdict(
            conn.execute(
                """
                select * from summaries
                where recording_id = ?
                order by is_current desc, version desc, created_at desc
                limit 1
                """,
                (recording_id,),
            ).fetchone()
        )
        summaries = rowsdict(
            conn.execute(
                """
                select id, recording_id, content, model, created_at, version, instruction, base_summary_id, is_current, length(content) as content_length
                from summaries
                where recording_id = ?
                order by version desc, created_at desc
                """,
                (recording_id,),
            ).fetchall()
        )
        emotion = current_emotion_analysis(conn, recording_id)
        tasks = [
            task_payload(dict(row), rec["duration"])
            for row in conn.execute(
                "select * from tasks where recording_id = ? order by datetime(created_at)",
                (recording_id,),
            ).fetchall()
        ]
        hotword_package = latest_hotword_package(conn, recording_id)
        outputs = []
        if segments:
            outputs.append(
                {
                    "id": "transcript",
                    "kind": "transcript",
                    "title": "逐字稿",
                    "format": "Markdown",
                    "status": rec["asr_status"],
                    "download_url": f"/api/recordings/{recording_id}/export/transcript.md",
                    "segment_count": len(segments),
                    "speaker_count": len({row.get("speaker_name") or row.get("speaker") for row in segments}),
                }
            )
        if summary:
            outputs.append(
                {
                    "id": summary["id"],
                    "kind": "summary",
                    "title": f"会议纪要 v{summary.get('version') or 1}",
                    "format": "Markdown",
                    "status": rec["summary_status"],
                    "download_url": f"/api/recordings/{recording_id}/export/summary.md",
                    "model": summary["model"],
                    "created_at": summary["created_at"],
                    "version": summary.get("version") or 1,
                }
            )
        if emotion:
            outputs.append(
                {
                    "id": emotion["id"],
                    "kind": "emotion",
                    "title": f"对话情绪分析 v{emotion.get('version') or 1}",
                    "format": "Markdown",
                    "status": "done",
                    "download_url": f"/api/recordings/{recording_id}/export/emotion.md",
                    "model": emotion["model"],
                    "created_at": emotion["created_at"],
                    "version": emotion.get("version") or 1,
                }
            )
        return {
            "recording": rec_payload,
            "segments": segments,
            "summary": summary,
            "summaries": summaries,
            "emotion_analysis": emotion,
            "tasks": tasks,
            "outputs": outputs,
            "hotword_package": hotword_package,
        }


@app.get("/api/recordings/{recording_id}/audio")
def recording_audio(recording_id: str, user: dict[str, Any] = Depends(current_user)) -> FileResponse:
    with db() as conn:
        rec = can_access_recording(conn, recording_id, user)
    return FileResponse(Path(rec["file_path"]), filename=rec["filename"])


@app.post("/api/recordings/{recording_id}/transcribe")
def transcribe_api(recording_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    return transcribe_recording(recording_id, user)


@app.post("/api/recordings/{recording_id}/summarize")
async def summarize_api(recording_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    return await summarize_recording(recording_id, user)


@app.post("/api/recordings/{recording_id}/summary/revise")
async def revise_summary_api(recording_id: str, payload: dict[str, str], user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    return await revise_summary(recording_id, payload.get("instruction") or "", user)


def _resolve_export_format(fmt: str) -> str:
    """校验导出格式，非法值 400。"""
    if fmt not in ("md", "docx"):
        raise HTTPException(status_code=400, detail=f"unsupported export format: {fmt} (支持 md/docx)")
    return fmt


def _export_media_type(fmt: str) -> str:
    return "application/vnd.openxmlformats-officedocument.wordprocessingml.document" if fmt == "docx" else "text/markdown; charset=utf-8"


@app.get("/api/recordings/{recording_id}/export/transcript.md")
def export_transcript(recording_id: str, format: str = "md", user: dict[str, Any] = Depends(current_user)) -> FileResponse:
    fmt = _resolve_export_format(format)
    path = write_export(recording_id, "transcript", user, fmt=fmt)
    return FileResponse(path, media_type=_export_media_type(fmt), filename=path.name)


@app.get("/api/recordings/{recording_id}/export/summary.md")
def export_summary(recording_id: str, format: str = "md", user: dict[str, Any] = Depends(current_user)) -> FileResponse:
    fmt = _resolve_export_format(format)
    path = write_export(recording_id, "summary", user, fmt=fmt)
    return FileResponse(path, media_type=_export_media_type(fmt), filename=path.name)


@app.get("/api/recordings/{recording_id}/export/summaries/{summary_id}.md")
def export_summary_version(recording_id: str, summary_id: str, format: str = "md", user: dict[str, Any] = Depends(current_user)) -> FileResponse:
    fmt = _resolve_export_format(format)
    path = write_summary_export(recording_id, summary_id, user, fmt=fmt)
    return FileResponse(path, media_type=_export_media_type(fmt), filename=path.name)


@app.post("/api/recordings/{recording_id}/emotion")
def emotion_api(
    recording_id: str,
    background_tasks: BackgroundTasks,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    with db() as conn:
        rec = can_access_recording(conn, recording_id, user)
        if rec["asr_status"] != "done":
            raise HTTPException(status_code=409, detail="转写完成后才能做情绪分析")
        count = conn.execute(
            "select count(*) from transcript_segments where recording_id = ?",
            (recording_id,),
        ).fetchone()[0]
        if not count:
            raise HTTPException(status_code=409, detail="转写为空")
    background_tasks.add_task(run_emotion_job, recording_id, dict(user))
    return {"status": "started", "recording_id": recording_id}


@app.get("/api/recordings/{recording_id}/export/emotion.md")
def export_emotion(recording_id: str, user: dict[str, Any] = Depends(current_user)) -> FileResponse:
    path = write_export(recording_id, "emotion", user)
    return FileResponse(path, media_type="text/markdown; charset=utf-8", filename=path.name)


@app.post("/api/recordings/{recording_id}/discover-hotwords")
async def rediscover_hotwords(recording_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """手动重新从该录音的转写+纪要抽取候选词。"""
    from .hotword_discover import discover_hotwords
    with db() as conn:
        can_access_recording(conn, recording_id, user)
    return await discover_hotwords(recording_id)


@app.get("/api/tasks")
def tasks(user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    # Single-user mode: every task belongs to local-admin, no scoping.
    with db() as conn:
        rows = conn.execute("select * from tasks order by updated_at desc limit 100").fetchall()
        return [task_payload(dict(row)) for row in rows]


def normalize_hotword(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["active"] = bool(payload.get("active"))
    payload["protected"] = bool(payload.get("protected"))
    payload["frequency"] = int(payload.get("frequency") or 0)
    payload["weight"] = int(payload.get("weight") or 0)
    payload["score"] = round(float(payload.get("score") or hotword_row_score(payload)), 1)
    payload["example"] = payload.get("example")
    return payload


def hotword_status_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    total = conn.execute("select count(*) from hotwords").fetchone()[0]
    active = conn.execute("select count(*) from hotwords where active = 1 and coalesce(state, 'active') = 'active'").fetchone()[0]
    protected = conn.execute("select count(*) from hotwords where active = 1 and protected = 1").fetchone()[0]
    expired = conn.execute("select count(*) from hotwords where coalesce(state, 'active') = 'expired' or active = 0").fetchone()[0]
    dynamic = max(0, active - protected)
    by_source = rowsdict(
        conn.execute(
            """
            select source, count(*) as total, sum(case when active = 1 then 1 else 0 end) as active,
                   sum(case when protected = 1 then 1 else 0 end) as protected
            from hotwords
            group by source
            order by active desc, total desc
            """
        ).fetchall()
    )
    sources = rowsdict(conn.execute("select * from hotword_sources order by source_type, name").fetchall())
    runs = rowsdict(conn.execute("select * from hotword_sync_runs order by started_at desc limit 8").fetchall())
    return {
        "total": total,
        "active": active,
        "protected": protected,
        "dynamic": dynamic,
        "expired": expired,
        "limits": hotword_limits(),
        "by_source": by_source,
        "sources": sources,
        "recent_runs": runs,
    }


def maintain_hotwords(conn: sqlite3.Connection, stale_days: int = 60, expire_days: int = 120) -> dict[str, int]:
    stale_cutoff = datetime.now() - timedelta(days=stale_days)
    expire_cutoff = datetime.now() - timedelta(days=expire_days)
    expired = 0
    rescored = 0
    rows = rowsdict(conn.execute("select * from hotwords").fetchall())
    for row in rows:
        protected = int(row.get("protected") or 0)
        state = str(row.get("state") or "active")
        active = int(row.get("active") or 0)
        last_seen = parse_time(row.get("last_seen_at"))
        next_state = state
        next_active = active
        if protected:
            next_state = "active"
            next_active = 1
        elif last_seen and last_seen < expire_cutoff:
            next_state = "expired"
            next_active = 0
            expired += 1 if active else 0
        elif last_seen and last_seen < stale_cutoff and state == "active":
            next_state = "active"
            next_active = 1
        score = hotword_row_score({**row, "active": next_active, "state": next_state})
        conn.execute(
            "update hotwords set state = ?, active = ?, score = ?, updated_at = ? where id = ?",
            (next_state, next_active, score, now(), row["id"]),
        )
        rescored += 1
    return {"expired": expired, "rescored": rescored}


@app.get("/api/hotwords")
def hotwords(
    state: str = "",
    q: str = "",
    protected: str = "",
    user: dict[str, Any] = Depends(current_user),
) -> list[dict[str, Any]]:
    with db() as conn:
        where: list[str] = []
        args: list[Any] = []
        if state:
            where.append("coalesce(state, 'active') = ?")
            args.append(state)
        else:
            # 不传 state 时（"全部"tab），排除 candidate——候选词有专门的审阅页面，
            # 不该混进正式热词表。之前这里没过滤导致候选词显示在正式库里。
            where.append("(coalesce(state, 'active') != 'candidate')")
        if protected in {"0", "1"}:
            where.append("protected = ?")
            args.append(int(protected))
        if q:
            where.append("(word like ? or aliases like ? or source like ? or kind like ?)")
            like = f"%{q}%"
            args.extend([like, like, like, like])
        rows = conn.execute(
            f"""
            select * from hotwords
            where {' and '.join(where)}
            order by protected desc, active desc, score desc, weight desc, word
            limit 1000
            """,
            args,
        ).fetchall()
        return [normalize_hotword(dict(row)) for row in rows]


@app.get("/api/hotwords/status")
def hotword_status(_: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with db() as conn:
        return hotword_status_payload(conn)


@app.patch("/api/hotwords/{hotword_id}")
def patch_hotword(hotword_id: str, payload: dict[str, Any], user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if user["role"] not in {"manager", "admin"}:
        raise HTTPException(status_code=403, detail="manager or admin only")
    allowed = {"kind", "aliases", "scope", "weight", "active", "state", "protected"}
    updates = {key: payload[key] for key in allowed if key in payload}
    if not updates:
        raise HTTPException(status_code=400, detail="no supported hotword fields")
    if "active" in updates:
        updates["active"] = 1 if updates["active"] else 0
    if "protected" in updates:
        updates["protected"] = 1 if updates["protected"] else 0
    if "weight" in updates:
        updates["weight"] = max(1, min(int(updates["weight"]), 10))
    with db() as conn:
        row = rowdict(conn.execute("select * from hotwords where id = ?", (hotword_id,)).fetchone())
        if not row:
            raise HTTPException(status_code=404, detail="hotword not found")
        next_protected = bool(updates["protected"]) if "protected" in updates else bool(row.get("protected"))
        if next_protected:
            updates["state"] = "active"
            updates["active"] = 1
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(f"update hotwords set {assignments}, updated_at = ? where id = ?", (*updates.values(), now(), hotword_id))
        changed = rowdict(conn.execute("select * from hotwords where id = ?", (hotword_id,)).fetchone())
        conn.execute(
            "update hotwords set score = ? where id = ?",
            (hotword_row_score(changed), hotword_id),
        )
        audit(conn, user, "hotword.update", f"{user['name']} 修改热词：{row['word']}。")
        changed = rowdict(conn.execute("select * from hotwords where id = ?", (hotword_id,)).fetchone())
        return normalize_hotword(changed)


@app.post("/api/hotwords")
def create_hotword(payload: dict[str, Any], user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    # Single-user desktop build: hotwords are maintained by hand.
    word = (payload.get("word") or "").strip()
    if not word:
        raise HTTPException(status_code=400, detail="word is required")
    kind = (payload.get("kind") or "term").strip() or "term"
    aliases = (payload.get("aliases") or "").strip()
    scope = (payload.get("scope") or "global").strip() or "global"
    weight = max(1, min(int(payload.get("weight") or 5), 10))
    protected = 1 if payload.get("protected") else 0
    ts = now()
    with db() as conn:
        if conn.execute("select 1 from hotwords where word = ?", (word,)).fetchone():
            raise HTTPException(status_code=409, detail="热词已存在")
        hid = str(uuid.uuid4())
        conn.execute(
            """
            insert into hotwords(
                id,word,kind,aliases,source,scope,weight,active,state,protected,
                frequency,confidence,score,first_seen_at,last_seen_at,updated_at
            )
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (hid, word, kind, aliases, "manual", scope, weight, 1, "active", protected,
             1, 0.95, 0, ts, ts, ts),
        )
        row = rowdict(conn.execute("select * from hotwords where id = ?", (hid,)).fetchone())
        conn.execute("update hotwords set score = ? where id = ?", (hotword_row_score(row), hid))
        audit(conn, user, "hotword.create", f"{user['name']} 新增热词：{word}。")
        row = rowdict(conn.execute("select * from hotwords where id = ?", (hid,)).fetchone())
        return normalize_hotword(row)


@app.post("/api/hotwords/import")
async def import_hotwords(
    file: UploadFile = File(...),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    """Bulk-import hotwords from a .txt file.

    Per line: `#`-comments and blank lines are ignored. Otherwise the line is
    either a bare word, or up to 4 comma-separated fields:
    `word, aliases(;-separated), kind, weight`. Existing words (case-insensitive)
    and in-file duplicates are skipped. Limits: 2MB / 20000 lines.
    """
    raw = await file.read()
    if len(raw) > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件过大（上限 2MB）")
    text: str | None = None
    for enc in ("utf-8-sig", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise HTTPException(status_code=400, detail="文件编码无法识别（请用 UTF-8 或 GBK）")
    lines = text.splitlines()
    if len(lines) > 20000:
        raise HTTPException(status_code=400, detail="行数过多（上限 20000 行）")

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = [part.strip() for part in stripped.split(",")]
        word = parts[0]
        if not word:
            continue
        key = word.lower()
        if key in seen:
            continue
        seen.add(key)
        aliases = ",".join(a.strip() for a in parts[1].split(";") if a.strip()) if len(parts) > 1 else ""
        kind = parts[2] if len(parts) > 2 and parts[2] else "术语"
        weight = 8
        if len(parts) > 3 and parts[3]:
            try:
                weight = max(1, min(int(parts[3]), 10))
            except ValueError:
                weight = 8
        candidates.append({"word": word, "aliases": aliases, "kind": kind, "weight": weight})

    inserted = 0
    skipped = 0
    ts = now()
    with db() as conn:
        existing = {str(row[0]).lower() for row in conn.execute("select word from hotwords").fetchall()}
        for cand in candidates:
            if cand["word"].lower() in existing:
                skipped += 1
                continue
            score = hotword_row_score(
                {
                    "score": 0,
                    "weight": cand["weight"],
                    "kind": cand["kind"],
                    "source": "txt-import",
                    "protected": 0,
                    "frequency": 1,
                    "last_seen_at": ts,
                }
            )
            conn.execute(
                """
                insert into hotwords(
                    id,word,kind,aliases,source,scope,weight,active,state,protected,
                    frequency,confidence,score,first_seen_at,last_seen_at,updated_at
                )
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (str(uuid.uuid4()), cand["word"], cand["kind"], cand["aliases"], "txt-import", "global",
                 cand["weight"], 1, "active", 0, 1, 0.95, score, ts, ts, ts),
            )
            existing.add(cand["word"].lower())
            inserted += 1
        audit(conn, user, "hotword.import", f"{user['name']} 从 txt 导入热词：新增 {inserted}，跳过 {skipped}。")
    return {"inserted": inserted, "skipped": skipped, "total": len(candidates)}


@app.delete("/api/hotwords/{hotword_id}")
def delete_hotword(hotword_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with db() as conn:
        row = rowdict(conn.execute("select * from hotwords where id = ?", (hotword_id,)).fetchone())
        if not row:
            raise HTTPException(status_code=404, detail="hotword not found")
        conn.execute("delete from hotwords where id = ?", (hotword_id,))
        audit(conn, user, "hotword.delete", f"{user['name']} 删除热词：{row['word']}。")
        return {"ok": True, "id": hotword_id}


@app.post("/api/hotwords/maintain")
def maintain_hotwords_api(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if user["role"] not in {"manager", "admin"}:
        raise HTTPException(status_code=403, detail="manager or admin only")
    with db() as conn:
        result = maintain_hotwords(conn)
        audit(conn, user, "hotword.maintain", f"{user['name']} 重新计算热词评分，过期 {result.get('expired', 0)} 条。")
        return {**result, "status": hotword_status_payload(conn)}


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
    """批量确认候选词：state candidate→active。edits 可纠正写法/分类。

    重复词检查：如果确认后的 word 已存在于 active/protected，跳过（防重复）。
    """
    if not isinstance(payload.get("ids"), list):
        raise HTTPException(status_code=400, detail="ids 必须是数组")
    ids = payload.get("ids") or []
    edits = payload.get("edits") or {}
    ts = now()
    confirmed = 0
    skipped_duplicate = 0
    with db() as conn:
        for hid in ids:
            e = edits.get(hid, {})
            final_word = (e.get("word") or "").strip().lower()
            if not final_word:
                # 没编辑，取数据库里现有的 word 查重
                row = conn.execute("select word from hotwords where id = ?", (hid,)).fetchone()
                final_word = (row[0].lower() if row else "")
            # 查重：word 已在 active/protected（排除自己）
            if final_word and conn.execute(
                "select 1 from hotwords where lower(word) = ? and state in ('active','protected') and id != ?",
                (final_word, hid),
            ).fetchone():
                skipped_duplicate += 1
                continue
            assignments = "state = 'active', active = 1, updated_at = ?"
            params: list[Any] = [ts]
            if e.get("word"):
                assignments += ", word = ?"
                params.append(e["word"].strip())
            if e.get("kind"):
                assignments += ", kind = ?"
                params.append(e["kind"])
            params.append(hid)
            cursor = conn.execute(f"update hotwords set {assignments} where id = ? and state = 'candidate'", params)
            confirmed += cursor.rowcount
        audit(conn, user, "hotword.confirm", f"确认 {confirmed} 个候选热词（跳过 {skipped_duplicate} 个重复）。")
    return {"confirmed": confirmed, "skipped_duplicate": skipped_duplicate}


@app.patch("/api/hotwords/candidates/{candidate_id}")
def edit_candidate(
    candidate_id: str,
    payload: dict[str, Any],
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    """只编辑候选词的写法/分类，不改变 state（不确认进库）。

    编辑保存 ≠ 确认。用户可能想先纠正写法，回头再批量确认。
    重复词检查：如果改后的 word 已存在于 active/protected，拒绝（409）。
    """
    word = (payload.get("word") or "").strip()
    kind = (payload.get("kind") or "").strip()
    if not word:
        raise HTTPException(status_code=400, detail="word 不能为空")
    with db() as conn:
        # 查重：word 已在 active/protected
        if conn.execute(
            "select 1 from hotwords where lower(word) = ? and state in ('active','protected') and id != ?",
            (word.lower(), candidate_id),
        ).fetchone():
            raise HTTPException(status_code=409, detail=f"热词「{word}」已存在")
        # kind 空字符串允许清除（传 NULL），不再静默忽略
        assignments = "word = ?, kind = ?, updated_at = ?"
        params: list[Any] = [word, kind if kind else None, now()]
        params.append(candidate_id)
        cursor = conn.execute(
            f"update hotwords set {assignments} where id = ? and state = 'candidate'",
            params,
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="候选词不存在或已处理")
        audit(conn, user, "hotword.edit", f"编辑候选词：{word}。")
    return {"id": candidate_id, "word": word, "kind": kind}


@app.post("/api/hotwords/candidates/discard")
def discard_candidates(payload: dict[str, Any], user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """批量丢弃候选词：state candidate→discarded。"""
    ids = payload.get("ids") or []
    if not ids:
        return {"discarded": 0}
    ts = now()
    placeholders = ",".join("?" for _ in ids)
    with db() as conn:
        cursor = conn.execute(
            f"update hotwords set state = 'discarded', active = 0, updated_at = ? where id in ({placeholders}) and state = 'candidate'",
            [ts, *ids],
        )
        audit(conn, user, "hotword.discard", f"丢弃 {cursor.rowcount} 个候选热词。")
    return {"discarded": cursor.rowcount}


@app.get("/api/voiceprints")
def voiceprints(user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    # Single-user mode: see every voiceprint (no team/owner scoping).
    with db() as conn:
        return [
            normalize_profile(row)
            for row in rowsdict(
                conn.execute(
                    "select id,name,note,owner_id,team_id,scope,threshold,active,created_at from speaker_profiles order by created_at desc",
                ).fetchall()
            )
        ]


@app.get("/api/recordings/{recording_id}/speaker-candidates")
def recording_speaker_candidates(recording_id: str, user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    with db() as conn:
        can_access_recording(conn, recording_id, user)
        rows = rowsdict(
            conn.execute(
                "select * from transcript_segments where recording_id = ? order by start_sec",
                (recording_id,),
            ).fetchall()
        )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["speaker"]), []).append(row)
    return [
        speaker_candidate_payload(speaker, grouped[speaker], recording_id)
        for speaker in sorted(grouped, key=lambda value: (not str(value).isdigit(), int(value) if str(value).isdigit() else str(value)))
    ]


@app.get("/api/recordings/{recording_id}/segments/{segment_id}/audio")
def recording_segment_audio(recording_id: str, segment_id: str, user: dict[str, Any] = Depends(current_user)) -> FileResponse:
    with db() as conn:
        rec = can_access_recording(conn, recording_id, user)
        segment = rowdict(
            conn.execute(
                "select * from transcript_segments where id = ? and recording_id = ?",
                (segment_id, recording_id),
            ).fetchone()
        )
    if not segment:
        raise HTTPException(status_code=404, detail="segment not found")
    start = max(0.0, float(segment["start_sec"]) - 0.2)
    end = min(float(segment["end_sec"]) + 0.2, float(rec.get("duration") or segment["end_sec"]))
    target = TMP / f"segment_{recording_id}_{segment_id}.wav"
    extract_interval(Path(rec["file_path"]), target, start, end)
    return FileResponse(target, media_type="audio/wav", filename=f"{slug(rec['title'])}_{segment['start_label']}.wav")


@app.post("/api/voiceprints")
def create_voiceprint(
    name: str = Form(...),
    threshold: float | None = Form(None),
    scope: str = Form(""),
    team_id: str = Form(""),
    file: UploadFile = File(...),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    requested_scope, profile_team_id = resolve_voiceprint_scope(user, scope, team_id)
    profile_id = str(uuid.uuid4())
    ext = Path(file.filename or "voiceprint.wav").suffix or ".wav"
    raw = TMP / f"{profile_id}{ext}"
    target = VOICEPRINTS / f"{profile_id}.wav"
    with raw.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    if probe_duration(raw) < 5:
        raw.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="voiceprint sample must be at least 5 seconds")
    subprocess.run(
        [
            str(FFMPEG),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(raw),
            "-ar",
            "16000",
            "-ac",
            "1",
            str(target),
        ],
        check=True,
    )
    raw.unlink(missing_ok=True)
    profile_threshold = clamp_voiceprint_threshold(threshold)
    with db() as conn:
        conn.execute(
            """
            insert into speaker_profiles(id,name,note,owner_id,team_id,sample_path,threshold,active,created_at)
            values(?,?,?,?,?,?,?,1,?)
            """,
            (profile_id, name.strip(), None, user["id"], profile_team_id, str(target), profile_threshold, now()),
        )
        conn.execute("update speaker_profiles set scope = ? where id = ?", (requested_scope, profile_id))
        audit(conn, user, "voiceprint.create", f"{user['name']} 登记{requested_scope}声纹样本：{name.strip()}。")
        row = rowdict(conn.execute("select id,name,note,owner_id,team_id,scope,threshold,active,created_at from speaker_profiles where id = ?", (profile_id,)).fetchone())
    return normalize_profile(row)


@app.post("/api/voiceprints/from-recording")
def create_voiceprint_from_recording(payload: dict[str, Any], user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    recording_id = str(payload.get("recording_id") or "").strip()
    speaker = str(payload.get("speaker") or "").strip()
    if not recording_id or not speaker:
        raise HTTPException(status_code=400, detail="recording_id and speaker are required")
    raw_segment_ids = payload.get("segment_ids") or []
    segment_ids = [str(item) for item in raw_segment_ids if str(item).strip()]
    profile_id = str(payload.get("profile_id") or "").strip()
    update_current = bool(payload.get("update_current_recording", True))
    threshold = clamp_voiceprint_threshold(payload.get("threshold"))
    note = str(payload.get("note") or "").strip() or None

    with db() as conn:
        rec = can_access_recording(conn, recording_id, user)
        existing_profile = None
        if profile_id:
            existing_profile = rowdict(conn.execute("select * from speaker_profiles where id = ?", (profile_id,)).fetchone())
            if not existing_profile:
                raise HTTPException(status_code=404, detail="voiceprint not found")
            existing_profile = normalize_profile(existing_profile)
            if not can_manage_voiceprint(existing_profile, user):
                raise HTTPException(status_code=403, detail="voiceprint is outside current permission scope")
            name = existing_profile["name"]
            requested_scope = existing_profile["scope"]
            profile_team_id = existing_profile.get("team_id")
            threshold = clamp_voiceprint_threshold(existing_profile.get("threshold") or threshold)
        else:
            name = str(payload.get("name") or "").strip()
            if not name:
                raise HTTPException(status_code=400, detail="name is required")
            requested_scope, profile_team_id = resolve_voiceprint_scope(user, str(payload.get("scope") or ""), str(payload.get("team_id") or ""))
        if segment_ids:
            placeholders = ",".join("?" for _ in segment_ids)
            rows = rowsdict(
                conn.execute(
                    f"""
                    select * from transcript_segments
                    where recording_id = ? and speaker = ? and id in ({placeholders})
                    order by start_sec
                    """,
                    [recording_id, speaker, *segment_ids],
                ).fetchall()
            )
        else:
            all_rows = rowsdict(
                conn.execute(
                    "select * from transcript_segments where recording_id = ? and speaker = ? order by start_sec",
                    (recording_id, speaker),
                ).fetchall()
            )
            rows = candidate_sample_rows(all_rows, limit=8)
        if not rows:
            raise HTTPException(status_code=404, detail="speaker segments not found")

    total_duration = sum(max(0.0, float(row["end_sec"]) - float(row["start_sec"])) for row in rows)
    if total_duration < 5:
        raise HTTPException(status_code=400, detail="selected voiceprint samples must be at least 5 seconds in total")

    final_profile_id = profile_id or str(uuid.uuid4())
    target = VOICEPRINTS / f"{final_profile_id}.wav"
    with tempfile.TemporaryDirectory(dir=TMP) as tmp:
        tmpdir = Path(tmp)
        parts: list[Path] = []
        if existing_profile and Path(existing_profile.get("sample_path") or "").exists():
            parts.append(Path(existing_profile["sample_path"]))
        for index, row in enumerate(rows):
            start = max(0.0, float(row["start_sec"]))
            end = min(float(row["end_sec"]), start + 20.0)
            part = tmpdir / f"sample_{index}.wav"
            extract_interval(Path(rec["file_path"]), part, start, end)
            parts.append(part)
        output = tmpdir / "voiceprint.wav" if existing_profile else target
        concat_audio(parts, output, tmpdir)
        if existing_profile:
            shutil.move(str(output), target)

    with db() as conn:
        if existing_profile:
            conn.execute(
                "update speaker_profiles set sample_path = ?, threshold = ?, note = ? where id = ?",
                (str(target), threshold, note, final_profile_id),
            )
        else:
            conn.execute(
                """
                insert into speaker_profiles(id,name,note,owner_id,team_id,scope,sample_path,threshold,active,created_at)
                values(?,?,?,?,?,?,?,?,1,?)
                """,
                (final_profile_id, name, note, user["id"], profile_team_id, requested_scope, str(target), threshold, now()),
            )
        conn.executemany(
            """
            insert into speaker_samples(id,profile_id,recording_id,segment_id,start_sec,end_sec,duration,text,created_by,created_at)
            values(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    str(uuid.uuid4()),
                    final_profile_id,
                    recording_id,
                    row["id"],
                    row["start_sec"],
                    row["end_sec"],
                    max(0.0, float(row["end_sec"]) - float(row["start_sec"])),
                    row["text"],
                    user["id"],
                    now(),
                )
                for row in rows
            ],
        )
        updated_segments = 0
        if update_current:
            cursor = conn.execute(
                """
                update transcript_segments
                set speaker_name = ?, voiceprint_id = ?, speaker_confidence = null
                where recording_id = ? and speaker = ?
                """,
                (name, final_profile_id, recording_id, speaker),
            )
            updated_segments = cursor.rowcount
            conn.execute("update recordings set updated_at = ? where id = ?", (now(), recording_id))
        audit(
            conn,
            user,
            "voiceprint.create",
            f"{user['name']} 从录音《{rec['title']}》的 Speaker {speaker} 保存{name}声纹，样本 {len(rows)} 段。",
        )
        profile = rowdict(
            conn.execute(
                "select id,name,note,owner_id,team_id,scope,threshold,active,created_at from speaker_profiles where id = ?",
                (final_profile_id,),
            ).fetchone()
        )
    return {
        "profile": normalize_profile(profile),
        "sample_count": len(rows),
        "sample_duration": total_duration,
        "sample_duration_label": seconds_label(total_duration),
        "updated_segments": updated_segments,
    }


@app.patch("/api/recordings/{recording_id}/speakers/{speaker}")
def patch_recording_speaker(recording_id: str, speaker: str, payload: dict[str, Any], user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    profile_id = str(payload.get("voiceprint_id") or "").strip() or None
    with db() as conn:
        rec = can_access_recording(conn, recording_id, user)
        if profile_id:
            profile = rowdict(conn.execute("select * from speaker_profiles where id = ?", (profile_id,)).fetchone())
            if not profile:
                raise HTTPException(status_code=404, detail="voiceprint not found")
            profile = normalize_profile(profile)
            if not can_manage_voiceprint(profile, user):
                raise HTTPException(status_code=403, detail="voiceprint is outside current permission scope")
        cursor = conn.execute(
            """
            update transcript_segments
            set speaker_name = ?, voiceprint_id = coalesce(?, voiceprint_id), speaker_confidence = null
            where recording_id = ? and speaker = ?
            """,
            (name, profile_id, recording_id, speaker),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="speaker not found")
        conn.execute("update recordings set updated_at = ? where id = ?", (now(), recording_id))
        audit(conn, user, "voiceprint.assign", f"{user['name']} 将录音《{rec['title']}》的 Speaker {speaker} 标记为{name}。")
    return {"recording_id": recording_id, "speaker": speaker, "name": name, "updated_segments": cursor.rowcount}


@app.post("/api/recordings/{recording_id}/speakers/merge")
def merge_recording_speakers(recording_id: str, payload: dict[str, Any], user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """把过度聚类拆出来的多个 Speaker 合并成一个人：from 的所有段并入 into。"""
    src = str(payload.get("from") or "").strip()
    dst = str(payload.get("into") or "").strip()
    if not src or not dst:
        raise HTTPException(status_code=400, detail="from / into are required")
    if src == dst:
        raise HTTPException(status_code=400, detail="from and into must differ")
    with db() as conn:
        rec = can_access_recording(conn, recording_id, user)
        cursor = conn.execute(
            "update transcript_segments set speaker = ? where recording_id = ? and speaker = ?",
            (dst, recording_id, src),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="source speaker not found")
        # 若 into 已命名，把整簇统一成该姓名 / 声纹
        named = rowdict(
            conn.execute(
                """
                select speaker_name, voiceprint_id from transcript_segments
                where recording_id = ? and speaker = ? and speaker_name is not null and speaker_name != ''
                order by start_sec limit 1
                """,
                (recording_id, dst),
            ).fetchone()
        )
        if named and named.get("speaker_name"):
            conn.execute(
                "update transcript_segments set speaker_name = ?, voiceprint_id = coalesce(?, voiceprint_id) where recording_id = ? and speaker = ?",
                (named["speaker_name"], named.get("voiceprint_id"), recording_id, dst),
            )
        conn.execute("update recordings set updated_at = ? where id = ?", (now(), recording_id))
        audit(conn, user, "speaker.merge", f"{user['name']} 把录音《{rec['title']}》的 Speaker {src} 合并到 Speaker {dst}（{cursor.rowcount} 段）。")
    return {"recording_id": recording_id, "merged_from": src, "into": dst, "moved_segments": cursor.rowcount}


@app.patch("/api/voiceprints/{profile_id}")
def patch_voiceprint(profile_id: str, payload: dict[str, Any], user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with db() as conn:
        profile = rowdict(conn.execute("select * from speaker_profiles where id = ?", (profile_id,)).fetchone())
        if not profile:
            raise HTTPException(status_code=404, detail="voiceprint not found")
        profile = normalize_profile(profile)
        if not can_manage_voiceprint(profile, user):
            raise HTTPException(status_code=403, detail="voiceprint is outside current permission scope")
        updates: dict[str, Any] = {}
        if "name" in payload:
            updates["name"] = str(payload["name"]).strip()
        if "note" in payload:
            note_val = payload["note"]
            updates["note"] = str(note_val).strip() if note_val is not None else None
        if "threshold" in payload:
            updates["threshold"] = clamp_voiceprint_threshold(payload["threshold"])
        if "active" in payload:
            updates["active"] = 1 if payload["active"] else 0
        if not updates:
            raise HTTPException(status_code=400, detail="no supported voiceprint fields")
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(f"update speaker_profiles set {assignments} where id = ?", (*updates.values(), profile_id))
        audit(conn, user, "voiceprint.update", f"{user['name']} 修改声纹样本：{profile['name']}。")
        changed = rowdict(conn.execute("select id,name,note,owner_id,team_id,scope,threshold,active,created_at from speaker_profiles where id = ?", (profile_id,)).fetchone())
    return normalize_profile(changed)


@app.delete("/api/voiceprints/{profile_id}")
def delete_voiceprint(profile_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """硬删除声纹：删 profile 行 + 级联 sample 行 + 删音频文件 + 置空引用它的
    transcript_segments.voiceprint_id（保留 speaker_name 文本，转写标签不丢）。"""
    with db() as conn:
        profile = rowdict(conn.execute("select * from speaker_profiles where id = ?", (profile_id,)).fetchone())
        if not profile:
            raise HTTPException(status_code=404, detail="voiceprint not found")
        profile = normalize_profile(profile)
        if not can_manage_voiceprint(profile, user):
            raise HTTPException(status_code=403, detail="voiceprint is outside current permission scope")
        sample_path = str(profile.get("sample_path") or "").strip()
        conn.execute("update transcript_segments set voiceprint_id = null where voiceprint_id = ?", (profile_id,))
        conn.execute("delete from speaker_samples where profile_id = ?", (profile_id,))
        conn.execute("delete from speaker_profiles where id = ?", (profile_id,))
        audit(conn, user, "voiceprint.delete", f"{user['name']} 删除声纹样本：{profile['name']}。")
    if sample_path:
        try:
            Path(sample_path).unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True, "id": profile_id}


@app.get("/api/system/status")
def system_status(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    # Single-user mode: no admin gate (require_admin removed).
    return {
        "base": str(BASE),
        "db": str(DB_PATH),
        "paraformer": PARAFORMER.exists(),
        "vad": VAD.exists(),
        "punc": PUNC.exists(),
        "voiceprint": CAMPLUS.exists(),
        "ffmpeg": FFMPEG.exists(),
        "llm_configured": bool(get_llm_config()[0]),
        "llm_model": get_llm_config()[2],
        # 旧别名（前端切换期兼容）
        "deepseek_configured": bool(get_llm_config()[0]),
        "deepseek_model": get_llm_config()[2],
        "segmentation": "fsmn-vad dynamic segmentation",
        "diarization": "cam++ speaker diarization",
    }


# ---------------------------------------------------------------------------
# Serve the built frontend from the same process (single-port desktop app).
# Registered LAST so every /api route above takes precedence; unknown non-API
# paths fall back to index.html for the client-side router (SPA).
# ---------------------------------------------------------------------------
FRONTEND_DIR = Path(env_compat("TITANVAULT_FRONTEND_DIR") or (ROOT / "frontend" / "dist"))

if (FRONTEND_DIR / "index.html").exists():
    if (FRONTEND_DIR / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIR / "assets")), name="assets")

    @app.get("/{full_path:path}")
    def serve_spa(full_path: str) -> FileResponse:
        if full_path.startswith("api/") or full_path == "api":
            raise HTTPException(status_code=404, detail="not found")
        root = FRONTEND_DIR.resolve()
        candidate = (FRONTEND_DIR / full_path).resolve()
        # Serve a real top-level file (favicon, manifest, …) when it exists and
        # stays inside the dist dir; otherwise hand back the SPA shell.
        if full_path and candidate.is_file() and (candidate == root or root in candidate.parents):
            return FileResponse(str(candidate))
        return FileResponse(str(FRONTEND_DIR / "index.html"))
