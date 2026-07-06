"""ASR 转写：FunASR 加载、语义段合并、transcribe_recording 枢纽。"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from . import state
from .config import (
    PARAFORMER, VAD, PUNC, CAMPLUS, FFMPEG, env_int, env_compat,
)
from .db import (
    db, now, rowdict, rowsdict, safe_json, seconds_label,
    clean_sensevoice_text,
    create_task, update_task, can_access_recording, audit,
)
from .hotwords import build_hotword_package, apply_hotwords
from .voiceprint import match_speaker_profiles, normalize_speaker_id
from .summary import summarize_recording
from .state import (
    asr_lock as _asr_lock,
    asr_init_lock as _asr_init_lock,
)
from .state import _LOCAL_USER


FILLER_TRANSCRIPT_TEXT = {
    "嗯",
    "嗯嗯",
    "啊",
    "哦",
    "噢",
    "额",
    "呃",
    "对",
    "对对",
    "好",
    "好的",
    "是",
    "是的",
    "可以",
    "行",
}

CONTINUATION_ENDINGS = (
    "因为",
    "然后",
    "但是",
    "所以",
    "包括",
    "这个",
    "那个",
    "就是",
    "如果",
    "我们",
    "客户",
    "它",
    "他",
    "她",
    "的",
    "和",
    "跟",
    "把",
    "在",
    "对",
)




def recover_queued_recordings() -> int:
    """Recordings stuck at asr_status='queued' lost their BackgroundTasks
    worker when the server died (BackgroundTasks lives in process memory).
    Drain them through a single recovery thread after startup; the model lock
    serializes them naturally with any new user-triggered transcribes.
    """
    with db() as conn:
        rows = rowsdict(
            conn.execute(
                "select * from recordings where asr_status = 'queued'"
            ).fetchall()
        )
        if not rows:
            return 0
        pending: list[tuple[str, dict[str, Any]]] = [(rec["id"], _LOCAL_USER) for rec in rows]
        if pending:
            audit(
                conn,
                None,
                "system",
                f"启动恢复 queued 录音：重新入队 {len(pending)} 个。",
            )

    if not pending:
        return 0

    def _drain() -> None:
        # Tiny stagger so the model load (first call) finishes before the
        # second recording also tries to grab the lock.
        time.sleep(1.0)
        for recording_id, user in pending:
            try:
                process_recording_background(recording_id, user)
            except Exception:
                # process_recording_background already records failure in DB.
                continue

    threading.Thread(target=_drain, name="tv-queued-recovery", daemon=True).start()
    return len(pending)



def split_audio(source: Path, workdir: Path, segment_seconds: int) -> list[Path]:
    chunk_pattern = workdir / "chunk_%04d.wav"
    cmd = [
        str(FFMPEG),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-ar",
        "16000",
        "-ac",
        "1",
        "-f",
        "segment",
        "-segment_time",
        str(segment_seconds),
        str(chunk_pattern),
    ]
    subprocess.run(cmd, check=True)
    return sorted(workdir.glob("chunk_*.wav"))



def get_asr_model() -> Any:
    if state.asr_model is None:
        with _asr_init_lock:
            if state.asr_model is None:
                missing = [str(path) for path in [PARAFORMER, VAD, PUNC, CAMPLUS] if not path.exists()]
                if missing:
                    raise RuntimeError(f"ASR/diarization model missing: {', '.join(missing)}")
                from funasr import AutoModel

                # Performance knobs. Default device stays CPU (the safe, always-works
                # path). Set AHAMVOICE_ASR_DEVICE=mps to try the Apple GPU. Threads
                # default to all cores; AHAMVOICE_ASR_THREADS overrides.
                device = (env_compat("TITANVAULT_ASR_DEVICE") or "cpu").strip() or "cpu"
                try:
                    import torch

                    threads = int(env_compat("TITANVAULT_ASR_THREADS") or (os.cpu_count() or 4))
                    torch.set_num_threads(max(1, threads))
                except Exception:
                    pass

                state.asr_model = AutoModel(
                    model=str(PARAFORMER),
                    vad_model=str(VAD),
                    vad_kwargs={"max_single_segment_time": int(env_compat("TITANVAULT_VAD_MAX_SEGMENT_MS") or "30000")},
                    punc_model=str(PUNC),
                    spk_model=str(CAMPLUS),
                    device=device,
                    disable_update=True,
                )
    return state.asr_model



def normalized_transcript_text(value: Any) -> str:
    text = clean_sensevoice_text(str(value or ""))
    text = re.sub(r"\s+", "", text)
    return text.strip()



def bare_transcript_text(value: str) -> str:
    return value.strip().strip("，。！？,.!?、 ")



def is_filler_transcript(text: str) -> bool:
    bare = bare_transcript_text(text)
    return bare in FILLER_TRANSCRIPT_TEXT or len(bare) <= 1



def transcript_needs_continuation(text: str) -> bool:
    bare = bare_transcript_text(text)
    if not bare:
        return False
    return bare.endswith(CONTINUATION_ENDINGS) or not text.endswith(("。", "？", "！", "?", "!"))



def join_transcript_text(left: str, right: str) -> str:
    left = left.strip()
    right = right.strip()
    if not left:
        return right
    if not right:
        return left
    if is_filler_transcript(right) and len(bare_transcript_text(right)) <= 2:
        return left + right
    return left + right



def semantic_segment_settings() -> dict[str, float | int]:
    return {
        "max_chars": env_int("AHAMVOICE_SEGMENT_MAX_CHARS", 120, 60, 240),
        "soft_chars": env_int("AHAMVOICE_SEGMENT_SOFT_CHARS", 80, 40, 180),
        "max_seconds": env_int("AHAMVOICE_SEGMENT_MAX_SECONDS", 35, 10, 90),
        "gap_seconds": float(env_compat("TITANVAULT_SEGMENT_GAP_SECONDS") or "2.0"),
    }



def merge_transcript_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    settings = semantic_segment_settings()
    max_chars = int(settings["max_chars"])
    soft_chars = int(settings["soft_chars"])
    max_seconds = float(settings["max_seconds"])
    gap_seconds = float(settings["gap_seconds"])
    merged: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def push_current() -> None:
        nonlocal current
        if not current:
            return
        current["start_label"] = seconds_label(current["start_sec"])
        current["text"] = current["text"].strip()
        merged.append(current)
        current = None

    for raw in sorted(items, key=lambda item: float(item.get("start_sec") or 0)):
        text = normalized_transcript_text(raw.get("text"))
        if not text:
            continue
        start_sec = float(raw.get("start_sec") or 0)
        end_sec = max(start_sec, float(raw.get("end_sec") or start_sec))
        speaker = str(raw.get("speaker") or "unknown")
        speaker_name = raw.get("speaker_name")
        voiceprint_id = raw.get("voiceprint_id")
        speaker_confidence = raw.get("speaker_confidence")
        item_id = raw.get("id") or str(uuid.uuid4())
        item = {
            "id": item_id,
            "start_sec": start_sec,
            "end_sec": end_sec,
            "start_label": seconds_label(start_sec),
            "speaker": speaker,
            "speaker_name": speaker_name,
            "voiceprint_id": voiceprint_id,
            "speaker_confidence": speaker_confidence,
            "text": text,
            "confidence": raw.get("confidence"),
            "source_ids": [item_id],
            "source_count": int(raw.get("source_count") or 1),
        }
        filler = is_filler_transcript(text)
        if filler and current is None:
            continue
        if current is None:
            current = item
            continue

        same_speaker = speaker == current["speaker"]
        gap = start_sec - float(current["end_sec"])
        combined_len = len(current["text"]) + len(text)
        combined_seconds = end_sec - float(current["start_sec"])
        can_merge_same_speaker = (
            same_speaker
            and gap <= gap_seconds
            and combined_len <= max_chars
            and combined_seconds <= max_seconds
        )
        can_merge_short_backchannel = (
            same_speaker
            and filler
            and gap <= gap_seconds
            and combined_len <= max_chars
            and combined_seconds <= max_seconds
        )
        if filler and not same_speaker:
            continue
        should_continue = transcript_needs_continuation(str(current["text"])) and combined_len <= max_chars
        if can_merge_same_speaker and (len(current["text"]) < soft_chars or should_continue or filler):
            current["text"] = join_transcript_text(current["text"], text)
            current["end_sec"] = end_sec
            current["source_ids"].extend(item["source_ids"])
            current["source_count"] += item["source_count"]
            if not current.get("speaker_name") and speaker_name:
                current["speaker_name"] = speaker_name
            if not current.get("voiceprint_id") and voiceprint_id:
                current["voiceprint_id"] = voiceprint_id
            if current.get("speaker_confidence") is None and speaker_confidence is not None:
                current["speaker_confidence"] = speaker_confidence
            continue
        if can_merge_short_backchannel:
            current["text"] = join_transcript_text(current["text"], text)
            current["end_sec"] = end_sec
            current["source_ids"].extend(item["source_ids"])
            current["source_count"] += item["source_count"]
            continue
        push_current()
        current = item
    push_current()
    return [row for row in merged if len(bare_transcript_text(str(row.get("text") or ""))) >= 2]



def sentence_info_to_transcript_segments(
    sentence_info: list[dict[str, Any]],
    hotwords: dict[str, str],
    speaker_matches: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in sentence_info:
        text = normalized_transcript_text(item.get("text", ""))
        text = apply_hotwords(text, hotwords)
        if not text:
            continue
        start_sec = float(item.get("start", 0)) / 1000.0
        end_sec = float(item.get("end", item.get("start", 0))) / 1000.0
        raw_spk = str(item.get("spk", "unknown"))
        speaker_match = speaker_matches.get(raw_spk) or {}
        items.append(
            {
                "id": str(uuid.uuid4()),
                "start_sec": start_sec,
                "end_sec": end_sec,
                "speaker": normalize_speaker_id(raw_spk),
                "speaker_name": speaker_match.get("name"),
                "voiceprint_id": speaker_match.get("voiceprint_id"),
                "speaker_confidence": speaker_match.get("score"),
                "text": text,
                "confidence": None,
            }
        )
    return merge_transcript_items(items)



def transcribe_recording(recording_id: str, user: dict[str, Any], segment_seconds: int = 60) -> dict[str, Any]:
    with db() as conn:
        rec = can_access_recording(conn, recording_id, user)
        task_id = create_task(conn, recording_id, rec["title"], "VAD+说话人分离转写")
        conn.execute("update recordings set asr_status = ?, updated_at = ? where id = ?", ("running", now(), recording_id))
        conn.execute("delete from transcript_segments where recording_id = ?", (recording_id,))
        conn.execute("delete from summaries where recording_id = ?", (recording_id,))
        conn.execute("delete from emotion_analyses where recording_id = ?", (recording_id,))
        conn.execute("update recordings set summary_status = ? where id = ?", ("pending", recording_id))
        conn.commit()

    try:
        model = get_asr_model()
        with db() as conn:
            rec_for_package = rowdict(conn.execute("select * from recordings where id = ?", (recording_id,)).fetchone()) or rec
            package = build_hotword_package(conn, rec_for_package, user)
            hotwords = package["replacement_map"]
            hotword_text = " ".join(package["asr_terms"])
        with db() as conn:
            update_task(conn, task_id, "running", 8)
        generate_kwargs: dict[str, Any] = {
            "input": str(Path(rec["file_path"])),
            "cache": {},
            "batch_size_s": int(env_compat("TITANVAULT_BATCH_SIZE_S") or "300"),
        }
        if hotword_text:
            generate_kwargs["hotword"] = hotword_text
        expected_spk = rec_for_package.get("expected_speakers")
        if expected_spk and int(expected_spk) >= 2:
            # 用户填了预计人数 → 固定聚类簇数，避免 CAM++ 过度聚类。
            generate_kwargs["preset_spk_num"] = int(expected_spk)
        with _asr_lock:
            result = model.generate(**generate_kwargs)
        if not result:
            raise RuntimeError("ASR returned empty result")
        sentence_info = result[0].get("sentence_info") or []
        if not sentence_info:
            raise RuntimeError("ASR did not return sentence_info with speaker labels")
        with db() as conn:
            update_task(conn, task_id, "running", 82)
        speaker_matches = match_speaker_profiles(rec, sentence_info)
        merged_segments = sentence_info_to_transcript_segments(sentence_info, hotwords, speaker_matches)

        # 方言口音纠错（可选，环境变量开关）。SeACo 对方言会产生大量近音错字，
        # LLM 结合上下文能纠正。实测贵州话纠错率约 80%，质量好。
        # 失败不阻塞——保留原文继续，转写/纪要照常。
        if (env_compat("TITANVAULT_DIALECT_CORRECTION") or "").lower() in ("1", "true", "yes"):
            try:
                from .dialect_correction import correct_dialect_segments
                with db() as conn:
                    update_task(conn, task_id, "running", 86)
                before_sample = merged_segments[0]["text"][:40] if merged_segments else ""
                merged_segments = correct_dialect_segments(merged_segments)
                print(f"[asr] 方言纠错完成，段数={len(merged_segments)}，原文样本: {before_sample}", flush=True)
            except Exception as exc:
                print(f"[asr] 方言纠错失败（不阻塞，保留原文）: {type(exc).__name__}: {exc}", flush=True)

        with db() as conn:
            update_task(conn, task_id, "running", 90)
            inserted = 0
            for item in merged_segments:
                conn.execute(
                    """
                    insert into transcript_segments(id,recording_id,start_sec,end_sec,start_label,speaker,speaker_name,voiceprint_id,speaker_confidence,text,confidence)
                    values(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        item["id"],
                        recording_id,
                        item["start_sec"],
                        item["end_sec"],
                        item["start_label"],
                        item["speaker"],
                        item.get("speaker_name"),
                        item.get("voiceprint_id"),
                        item.get("speaker_confidence"),
                        item["text"],
                        item.get("confidence"),
                    ),
                )
                inserted += 1
            if inserted == 0:
                raise RuntimeError("ASR returned no usable transcript segments")
            conn.execute(
                "update recordings set asr_status = ?, updated_at = ? where id = ?",
                ("done", now(), recording_id),
            )
            update_task(conn, task_id, "done", 100)
            spk_count = len({row.get("spk", "unknown") for row in sentence_info})
            audit(
                conn,
                user,
                "recording",
                f"完成录音转写和说话人分离：{rec['title']}，生成 {inserted} 个语义发言段，检测到 {spk_count} 个说话人，使用热词 {package['asr_terms_count']} 条。",
            )
        return {"recording_id": recording_id, "segments": inserted, "speakers": spk_count}
    except Exception as exc:
        with db() as conn:
            conn.execute(
                "update recordings set asr_status = ?, updated_at = ? where id = ?",
                ("failed", now(), recording_id),
            )
            update_task(conn, task_id, "failed", 100, str(exc))
            audit(conn, user, "recording", f"录音转写失败：{rec['title']}。")
        print(f"[error] transcription: {type(exc).__name__}: {exc}", flush=True)
        raise HTTPException(status_code=500, detail="转写失败，请查看日志") from exc



def process_recording_background(recording_id: str, user: dict[str, Any]) -> None:
    try:
        transcribe_recording(recording_id, user)
        asyncio.run(summarize_recording(recording_id, user))
    except HTTPException:
        return
    except Exception as exc:
        with db() as conn:
            rec = rowdict(conn.execute("select title from recordings where id = ?", (recording_id,)).fetchone())
            create_task(conn, recording_id, rec["title"] if rec else recording_id, "完整处理")
            last = rowdict(conn.execute("select id from tasks where recording_id = ? order by created_at desc limit 1", (recording_id,)).fetchone())
            if last:
                update_task(conn, last["id"], "failed", 100, str(exc))
        return
    # 纪要完成后触发候选词发现（转写+纪要此时都有，一次抽全）
    try:
        from .hotword_discover import discover_hotwords
        asyncio.run(discover_hotwords(recording_id))
    except Exception:
        pass  # 发现失败不阻塞主流程

