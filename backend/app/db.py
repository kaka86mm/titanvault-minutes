"""数据库层：连接、schema、迁移、中断恢复、task helper、cleanup。"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from .config import DB_PATH, TMP, EXPORTS, env_compat
from .state import _LOCAL_USER  # recording_payload 用 owner_name


@contextmanager
def db() -> Any:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()



def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")



def rowdict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None



def rowsdict(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]



def safe_json(value: str | None, default: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default



def slug(text: str) -> str:
    text = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", text.strip(), flags=re.UNICODE)
    return text[:80] or "recording"



def clean_sensevoice_text(text: str) -> str:
    text = re.sub(r"<\|[^|]+?\|>", "", text or "")
    return re.sub(r"\s+", " ", text).strip()



def seconds_label(seconds: float | int | None) -> str:
    total = int(seconds or 0)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"



def parse_local_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None



def ensure_schema() -> None:
    with db() as conn:
        conn.executescript(
            """
            create table if not exists app_settings (
                key text primary key,
                value text not null,
                updated_at text not null
            );
            create table if not exists recordings (
                id text primary key,
                title text not null,
                filename text not null,
                file_path text not null,
                meeting_type text not null,
                tag text,
                owner_id text not null,
                team_id text,
                duration real not null default 0,
                duration_label text not null,
                asr_status text not null default 'pending',
                summary_status text not null default 'pending',
                speaker_count integer,
                crm_sync_status text not null default 'pending',
                crm_sync_error text,
                crm_synced_at text,
                crm_recording_id text,
                crm_minute_id text,
                crm_recording_url text,
                crm_minute_url text,
                crm_relation_status text,
                crm_relation_source text,
                crm_relation_confidence real,
                crm_relation_target_type text,
                crm_relation_target_id text,
                crm_relation_target_name text,
                crm_sync_response text not null default '{}',
                created_at text not null,
                updated_at text not null
            );
            create table if not exists transcript_segments (
                id text primary key,
                recording_id text not null,
                start_sec real not null,
                end_sec real not null,
                start_label text not null,
                speaker text not null,
                speaker_name text,
                voiceprint_id text,
                speaker_confidence real,
                text text not null,
                confidence real
            );
            create table if not exists summaries (
                id text primary key,
                recording_id text not null,
                content text not null,
                model text not null,
                created_at text not null
            );
            create table if not exists emotion_analyses (
                id text primary key,
                recording_id text not null,
                content text not null,
                model text not null,
                acoustic_json text,
                created_at text not null,
                version integer not null default 1,
                is_current integer not null default 1
            );
            create table if not exists tasks (
                id text primary key,
                recording_id text,
                recording_title text not null,
                step text not null,
                status text not null,
                progress integer not null default 0,
                error text,
                created_at text not null,
                updated_at text not null
            );
            create table if not exists hotwords (
                id text primary key,
                word text not null,
                kind text not null,
                aliases text,
                source text not null,
                scope text not null,
                weight integer not null,
                active integer not null default 1,
                source_key text,
                state text not null default 'active',
                protected integer not null default 0,
                frequency integer not null default 1,
                confidence real not null default 0.75,
                score real not null default 0,
                team_id text,
                owner_id text,
                first_seen_at text,
                last_seen_at text,
                last_used_at text,
                expires_at text,
                hit_count integer not null default 0,
                updated_at text
            );
            create table if not exists hotword_sources (
                id text primary key,
                name text not null unique,
                source_type text not null,
                enabled integer not null default 1,
                schedule_minutes integer not null default 360,
                lookback_days integer not null default 30,
                asr_limit integer not null default 3000,
                correction_limit integer not null default 10000,
                candidate_limit integer not null default 20000,
                last_success_at text,
                last_error text,
                created_at text not null,
                updated_at text not null
            );
            create table if not exists hotword_sync_runs (
                id text primary key,
                source_name text not null,
                mode text not null,
                status text not null,
                started_at text not null,
                finished_at text,
                inserted integer not null default 0,
                updated integer not null default 0,
                reactivated integer not null default 0,
                expired integer not null default 0,
                skipped integer not null default 0,
                total integer not null default 0,
                report_path text,
                error text
            );
            create table if not exists recording_hotword_packages (
                id text primary key,
                recording_id text not null,
                version integer not null,
                asr_terms_count integer not null default 0,
                correction_terms_count integer not null default 0,
                protected_terms_count integer not null default 0,
                dynamic_terms_count integer not null default 0,
                source_summary text,
                asr_terms text,
                correction_terms text,
                created_at text not null
            );
            create table if not exists speaker_profiles (
                id text primary key,
                name text not null,
                note text,
                owner_id text,
                team_id text,
                scope text not null default 'team',
                sample_path text not null,
                threshold real not null default 0.66,
                active integer not null default 1,
                created_at text not null
            );
            create table if not exists speaker_samples (
                id text primary key,
                profile_id text not null,
                recording_id text not null,
                segment_id text not null,
                start_sec real not null,
                end_sec real not null,
                duration real not null,
                text text,
                created_by text,
                created_at text not null
            );
            """
        )
        recording_cols = {row["name"] for row in conn.execute("pragma table_info(recordings)").fetchall()}
        recording_migrations = {
            "crm_sync_status": "text not null default 'pending'",
            "crm_sync_error": "text",
            "crm_synced_at": "text",
            "crm_recording_id": "text",
            "crm_minute_id": "text",
            "crm_recording_url": "text",
            "crm_minute_url": "text",
            "crm_relation_status": "text",
            "crm_relation_source": "text",
            "crm_relation_confidence": "real",
            "crm_relation_target_type": "text",
            "crm_relation_target_id": "text",
            "crm_relation_target_name": "text",
            "crm_sync_response": "text not null default '{}'",
            "expected_speakers": "integer",
            "speaker_count": "integer",
        }
        for column, definition in recording_migrations.items():
            if column not in recording_cols:
                conn.execute(f"alter table recordings add column {column} {definition}")
        segment_cols = {row["name"] for row in conn.execute("pragma table_info(transcript_segments)").fetchall()}
        if "speaker_name" not in segment_cols:
            conn.execute("alter table transcript_segments add column speaker_name text")
        if "voiceprint_id" not in segment_cols:
            conn.execute("alter table transcript_segments add column voiceprint_id text")
        if "speaker_confidence" not in segment_cols:
            conn.execute("alter table transcript_segments add column speaker_confidence real")
        summary_cols = {row["name"] for row in conn.execute("pragma table_info(summaries)").fetchall()}
        summary_migrations = {
            "version": "integer not null default 1",
            "instruction": "text",
            "base_summary_id": "text",
            "is_current": "integer not null default 1",
        }
        for column, definition in summary_migrations.items():
            if column not in summary_cols:
                conn.execute(f"alter table summaries add column {column} {definition}")
        conn.execute(
            """
            update summaries
            set version = coalesce(version, 1),
                is_current = case when is_current is null then 1 else is_current end
            """
        )
        task_cols = {row["name"] for row in conn.execute("pragma table_info(tasks)").fetchall()}
        task_migrations = {
            "started_at": "text",
            "finished_at": "text",
            "phase": "text",
            "phase_index": "integer",
            "phase_total": "integer",
        }
        for column, definition in task_migrations.items():
            if column not in task_cols:
                conn.execute(f"alter table tasks add column {column} {definition}")
        conn.execute("update tasks set started_at = coalesce(started_at, created_at)")
        conn.execute(
            """
            update tasks
            set finished_at = coalesce(finished_at, updated_at)
            where status in ('done', 'failed')
            """
        )
        hotword_cols = {row["name"] for row in conn.execute("pragma table_info(hotwords)").fetchall()}
        hotword_migrations = {
            "source_key": "text",
            "state": "text not null default 'active'",
            "protected": "integer not null default 0",
            "frequency": "integer not null default 1",
            "confidence": "real not null default 0.75",
            "score": "real not null default 0",
            "team_id": "text",
            "owner_id": "text",
            "first_seen_at": "text",
            "last_seen_at": "text",
            "last_used_at": "text",
            "expires_at": "text",
            "hit_count": "integer not null default 0",
            "updated_at": "text",
            "example": "text",
        }
        for column, definition in hotword_migrations.items():
            if column not in hotword_cols:
                conn.execute(f"alter table hotwords add column {column} {definition}")
        timestamp = now()
        conn.execute(
            """
            update hotwords
            set state = coalesce(state, case when active = 1 then 'active' else 'expired' end),
                protected = coalesce(protected, 0),
                frequency = coalesce(frequency, 1),
                confidence = coalesce(confidence, 0.75),
                score = case when coalesce(score, 0) <= 0 then coalesce(weight, 6) * 10 else score end,
                source_key = coalesce(source_key, source || ':' || word),
                first_seen_at = coalesce(first_seen_at, ?),
                last_seen_at = coalesce(last_seen_at, ?),
                updated_at = coalesce(updated_at, ?)
            """,
            (timestamp, timestamp, timestamp),
        )
        conn.execute(
            """
            update hotwords
            set protected = 1, state = 'active', active = 1
            where source in ('系统内置', '产品库') or kind in ('产品', '系统')
            """
        )
        conn.execute("create index if not exists idx_hotwords_source on hotwords(source, active, state)")
        conn.execute("create index if not exists idx_hotwords_score on hotwords(active, state, protected, score)")
        conn.execute("create index if not exists idx_hotwords_word on hotwords(word)")
        default_sources = [
            ("protected", "保护热词", "protected", 1, 0, 0, 1200, 12000, 50000),
            ("manual", "手工维护", "manual", 1, 0, 0, 3000, 10000, 50000),
            ("txt-import", "txt 导入", "manual", 1, 0, 0, 3000, 10000, 50000),
        ]
        for source_id, name, source_type, enabled, schedule_minutes, lookback_days, asr_limit, correction_limit, candidate_limit in default_sources:
            if not conn.execute("select 1 from hotword_sources where id = ?", (source_id,)).fetchone():
                conn.execute(
                    """
                    insert into hotword_sources(
                        id,name,source_type,enabled,schedule_minutes,lookback_days,asr_limit,correction_limit,candidate_limit,created_at,updated_at
                    ) values(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        source_id,
                        name,
                        source_type,
                        enabled,
                        schedule_minutes,
                        lookback_days,
                        asr_limit,
                        correction_limit,
                        candidate_limit,
                        timestamp,
                        timestamp,
                    ),
                )
        profile_cols = {row["name"] for row in conn.execute("pragma table_info(speaker_profiles)").fetchall()}
        if "scope" not in profile_cols:
            conn.execute("alter table speaker_profiles add column scope text not null default 'team'")
            conn.execute("update speaker_profiles set scope = case when team_id is null then 'global' else 'team' end")
        if "note" not in profile_cols:
            conn.execute("alter table speaker_profiles add column note text")
        pkg_cols = {row["name"] for row in conn.execute("pragma table_info(recording_hotword_packages)").fetchall()}
        if "glossary" not in pkg_cols:
            conn.execute("alter table recording_hotword_packages add column glossary text not null default '{}'")
        if not conn.execute("select 1 from hotwords limit 1").fetchone():
            seed_hotwords = [
                ("TitanVault Minutes", "产品", "titanvault minutes,titan", "系统内置", "部门共享", 10, 1),
                ("ERP", "行业", "企业资源计划", "产品库", "部门共享", 8, 1),
                ("MES", "行业", "制造执行系统", "产品库", "部门共享", 8, 1),
                ("金蝶接口", "项目", "金蝶 API,金蝶系统", "系统内置", "团队共享", 9, 1),
                ("客户成功", "组织", "CS,售后成功", "通讯录", "部门共享", 6, 1),
            ]
            conn.executemany(
                "insert into hotwords(id,word,kind,aliases,source,scope,weight,active) values(?,?,?,?,?,?,?,?)",
                [(str(uuid.uuid4()), *row) for row in seed_hotwords],
            )



def recover_interrupted_tasks() -> int:
    with db() as conn:
        running = rowsdict(conn.execute("select * from tasks where status = 'running'").fetchall())
        if not running:
            return 0
        timestamp = now()
        recovered = 0
        for task in running:
            recording_id = task.get("recording_id")
            rec = rowdict(conn.execute("select * from recordings where id = ?", (recording_id,)).fetchone()) if recording_id else None
            segment_count = int(
                conn.execute("select count(*) from transcript_segments where recording_id = ?", (recording_id,)).fetchone()[0]
            ) if recording_id else 0
            summary_count = int(
                conn.execute("select count(*) from summaries where recording_id = ?", (recording_id,)).fetchone()[0]
            ) if recording_id else 0
            task_status = "failed"
            error = "服务重启或进程退出导致任务中断，请重新处理。"
            if rec:
                asr_status = rec["asr_status"]
                summary_status = rec["summary_status"]
                step = task.get("step") or ""
                if "转写" in step:
                    if segment_count > 0:
                        asr_status = "done"
                        task_status = "done"
                        error = None
                    else:
                        asr_status = "failed"
                        summary_status = "pending"
                elif "纪要" in step:
                    if summary_count > 0:
                        summary_status = "done"
                        task_status = "done"
                        error = None
                    else:
                        summary_status = "failed"
                else:
                    if segment_count > 0:
                        asr_status = "done"
                    elif asr_status == "running":
                        asr_status = "failed"
                    if summary_count > 0:
                        summary_status = "done"
                    elif summary_status == "running":
                        summary_status = "failed"
                conn.execute(
                    """
                    update recordings
                    set asr_status = ?, summary_status = ?, updated_at = ?
                    where id = ?
                    """,
                    (asr_status, summary_status, timestamp, recording_id),
                )
            conn.execute(
                """
                update tasks
                set status = ?, progress = 100, error = ?, updated_at = ?, finished_at = coalesce(finished_at, ?)
                where id = ?
                """,
                (task_status, error, timestamp, timestamp, task["id"]),
            )
            recovered += 1
        audit(conn, None, "system", f"恢复中断任务：{recovered} 个。")
        return recovered



def sweep_tmp_and_exports() -> dict[str, int]:
    """Delete stale segment/debug files in TMP and old exports.

    Without this, segment WAVs from /api/recordings/{id}/segments/.../audio
    accumulate forever (every playback writes a new one) — observed 131 files
    in TMP, 125 older than 1 day. Exports grow the same way.
    """
    tmp_ttl = int(env_compat("TITANVAULT_TMP_TTL_HOURS") or "24") * 3600
    export_ttl = int(env_compat("TITANVAULT_EXPORT_TTL_DAYS") or "14") * 86400
    now_ts = time.time()
    tmp_cutoff = now_ts - tmp_ttl
    export_cutoff = now_ts - export_ttl
    tmp_deleted = 0
    export_deleted = 0
    for path in TMP.glob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < tmp_cutoff:
                path.unlink()
                tmp_deleted += 1
        except OSError:
            continue
    for path in EXPORTS.glob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < export_cutoff:
                path.unlink()
                export_deleted += 1
        except OSError:
            continue
    return {"tmp_deleted": tmp_deleted, "export_deleted": export_deleted}



def _start_cleanup_loop() -> None:
    """Run sweep_tmp_and_exports on a timer in a daemon thread."""
    interval = int(env_compat("TITANVAULT_SWEEP_INTERVAL_MINUTES") or "60") * 60

    def _loop() -> None:
        # First sweep immediately so a fresh server reclaims stale files from
        # the prior run; subsequent sweeps run on the interval.
        while True:
            try:
                sweep_tmp_and_exports()
            except Exception:
                pass
            time.sleep(interval)

    threading.Thread(target=_loop, name="tv-cleanup", daemon=True).start()



def audit(conn: sqlite3.Connection, user: dict[str, Any] | None, category: str, message: str, actor_name: str | None = None) -> None:
    # 审计日志已废弃（audit 表在 Task 2 删除）。保留为 no-op 兼容 ~23 个调用点，
    # 避免每个写路由都要改。签名保持不变，调用方无需感知。
    return None



def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("select value from app_settings where key = ?", (key,)).fetchone()
    return row["value"] if row else default



def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        insert into app_settings(key,value,updated_at) values(?,?,?)
        on conflict(key) do update set value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, now()),
    )



def can_access_recording(conn: sqlite3.Connection, recording_id: str, user: dict[str, Any]) -> dict[str, Any]:
    # Single-user mode: every recording belongs to local-admin. Only check existence.
    rec = rowdict(conn.execute("select * from recordings where id = ?", (recording_id,)).fetchone())
    if not rec:
        raise HTTPException(status_code=404, detail="recording not found")
    return rec



def recording_payload(conn: sqlite3.Connection, rec: dict[str, Any]) -> dict[str, Any]:
    # No users table — owner_name is fixed (every recording belongs to local-admin).
    payload = dict(rec)
    payload["owner_name"] = _LOCAL_USER["name"]
    # speaker_count: prefer live count over distinct speakers in transcript
    # (historical recordings without stored column still report correctly),
    # falling back to stored value when no segments exist yet.
    distinct = conn.execute(
        "select count(distinct coalesce(speaker_name, speaker)) from transcript_segments where recording_id = ?",
        (rec["id"],),
    ).fetchone()[0]
    if distinct:
        payload["speaker_count"] = int(distinct)
    else:
        stored = rec.get("speaker_count")
        payload["speaker_count"] = int(stored) if stored is not None else None
    return payload



def task_payload(row: dict[str, Any], recording_duration: float | int | None = None) -> dict[str, Any]:
    payload = dict(row)
    started = parse_local_time(payload.get("started_at") or payload.get("created_at"))
    updated = parse_local_time(payload.get("updated_at"))
    finished = parse_local_time(payload.get("finished_at"))
    now_dt = datetime.now()
    end_dt = finished or (now_dt if payload.get("status") in {"running", "queued"} else updated)
    elapsed = max(0, int((end_dt - started).total_seconds())) if started and end_dt else 0
    payload["elapsed_seconds"] = elapsed
    payload["elapsed_label"] = seconds_label(elapsed)
    progress = int(payload.get("progress") or 0)
    eta = None
    if payload.get("status") == "running" and 0 < progress < 100 and elapsed > 0:
        eta = int(elapsed * (100 - progress) / progress)
    payload["eta_seconds"] = eta
    payload["eta_label"] = seconds_label(eta) if eta is not None else None
    silence = max(0, int((now_dt - updated).total_seconds())) if updated else 0
    stale_after = 900
    if "转写" in (payload.get("step") or ""):
        stale_after = max(900, int(float(recording_duration or 0) * 0.45))
    payload["stale_seconds"] = silence
    payload["is_stale"] = payload.get("status") == "running" and silence > stale_after
    return payload



def update_task(conn: sqlite3.Connection, task_id: str, status: str, progress: int, error: str | None = None) -> None:
    timestamp = now()
    conn.execute(
        """
        update tasks
        set status = ?,
            progress = ?,
            error = ?,
            updated_at = ?,
            finished_at = case when ? in ('done', 'failed') then coalesce(finished_at, ?) else finished_at end
        where id = ?
        """,
        (status, max(0, min(100, progress)), error, timestamp, status, timestamp, task_id),
    )
    conn.commit()



def create_task(conn: sqlite3.Connection, recording_id: str, title: str, step: str) -> str:
    task_id = str(uuid.uuid4())
    timestamp = now()
    conn.execute(
        """
        insert into tasks(id,recording_id,recording_title,step,status,progress,created_at,updated_at,started_at,phase,phase_index,phase_total)
        values(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (task_id, recording_id, title, step, "running", 0, timestamp, timestamp, timestamp, step, 1, 1),
    )
    conn.commit()
    return task_id



def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None

