"""情绪分析：emotion2vec 声学层 + LLM 语义层。"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from . import state
from .config import EMOTION, TMP
from .db import (
    db, now, rowdict, rowsdict, safe_json, seconds_label,
    create_task, update_task, can_access_recording, audit,
)
from .deepseek import call_deepseek_emotion
from .voiceprint import extract_interval
from .state import (
    asr_lock as _asr_lock,
    emotion_init_lock as _emotion_init_lock,
)


_EMOTION_CN = {
    "生气": "生气", "angry": "生气", "厌恶": "厌恶", "disgusted": "厌恶",
    "恐惧": "恐惧", "fearful": "恐惧", "开心": "开心", "happy": "开心",
    "中立": "中立", "neutral": "中立", "难过": "难过", "sad": "难过",
    "吃惊": "吃惊", "surprised": "吃惊", "其他": "其他", "other": "其他",
    "unknown": "未知", "<unk>": "未知",
}
_EMOTION_NEGATIVE = {"生气", "厌恶", "恐惧", "难过"}




def emotion_label_cn(raw: str) -> str:
    parts = [p.strip() for p in str(raw or "").split("/") if p.strip()]
    for p in parts:
        if p in _EMOTION_CN:
            return _EMOTION_CN[p]
    return parts[0] if parts else "未知"



def get_emotion_model() -> Any:
    if state.emotion_model is None:
        with _emotion_init_lock:
            if state.emotion_model is None:
                if not EMOTION.exists():
                    raise RuntimeError(f"情绪模型缺失：{EMOTION}")
                from funasr import AutoModel

                state.emotion_model = AutoModel(model=str(EMOTION), disable_update=True)
    return state.emotion_model



def analyze_segment_emotion(wav_path: str) -> tuple[str, float]:
    with _asr_lock:
        res = get_emotion_model().generate(wav_path, granularity="utterance", extract_embedding=False)
    if not res:
        return "未知", 0.0
    item = res[0]
    labels = [emotion_label_cn(x) for x in (item.get("labels") or [])]
    scores = [float(s) for s in (item.get("scores") or [])]
    if not labels or not scores:
        return "未知", 0.0
    top = max(range(len(scores)), key=lambda i: scores[i])
    return labels[top], round(scores[top], 3)



def analyze_acoustic_emotions(rec: dict[str, Any], segments: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """逐段跑 emotion2vec。只取 >=1.2s 的段，最多 220 段（超出时优先取长段），
    避免几百个碎段拖慢。音频缺失时返回空，让语义层退化为纯文本分析。"""
    import shutil

    src = Path(rec.get("file_path") or "")
    if not src.exists():
        return [], {}
    cands = [s for s in segments if (float(s.get("end_sec") or 0) - float(s.get("start_sec") or 0)) >= 1.2]
    if len(cands) > 220:
        cands = sorted(cands, key=lambda s: float(s.get("end_sec") or 0) - float(s.get("start_sec") or 0), reverse=True)[:220]
        cands = sorted(cands, key=lambda s: float(s.get("start_sec") or 0))
    per_segment: list[dict[str, Any]] = []
    workdir = TMP / f"emotion_{rec['id']}_{uuid.uuid4().hex[:8]}"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        for idx, seg in enumerate(cands):
            start = float(seg.get("start_sec") or 0.0)
            end = float(seg.get("end_sec") or 0.0)
            speaker = seg.get("speaker_name") or (f"Speaker {seg.get('speaker')}" if seg.get("speaker") is not None else "未知")
            clip = workdir / f"seg_{idx}.wav"
            try:
                extract_interval(src, clip, start, min(end, start + 20.0))
                emotion, score = analyze_segment_emotion(str(clip))
            except Exception:
                continue
            finally:
                try:
                    clip.unlink()
                except Exception:
                    pass
            per_segment.append({
                "start": start,
                "start_label": seg.get("start_label") or seconds_label(start),
                "speaker": speaker,
                "emotion": emotion,
                "score": score,
                "text": (seg.get("text") or "")[:60],
            })
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    stats: dict[str, Any] = {}
    for item in per_segment:
        st = stats.setdefault(item["speaker"], {"count": 0, "emotions": {}, "negative": 0})
        st["count"] += 1
        st["emotions"][item["emotion"]] = st["emotions"].get(item["emotion"], 0) + 1
        if item["emotion"] in _EMOTION_NEGATIVE:
            st["negative"] += 1
    for st in stats.values():
        st["dominant"] = max(st["emotions"], key=st["emotions"].get) if st["emotions"] else "未知"
        st["negative_ratio"] = round(st["negative"] / st["count"], 2) if st["count"] else 0.0
    return per_segment, stats



def acoustic_markdown(per_speaker: dict[str, Any], per_segment: list[dict[str, Any]]) -> str:
    lines = ["## 声学情绪分布（emotion2vec 逐段识别）", ""]
    if not per_segment:
        lines.append("> 转写段落过短或音频缺失，本次未做声学情绪识别，以上分析仅基于文本。")
        return "\n".join(lines)
    lines.append("| 说话人 | 主导情绪 | 负面占比 | 采样段数 | 情绪分布 |")
    lines.append("|---|---|---|---|---|")
    for sp, st in sorted(per_speaker.items(), key=lambda kv: -kv[1]["count"]):
        dist = "、".join(f"{e}×{n}" for e, n in sorted(st["emotions"].items(), key=lambda kv: -kv[1]))
        lines.append(f"| {sp} | {st['dominant']} | {int(st['negative_ratio'] * 100)}% | {st['count']} | {dist} |")
    peaks = [s for s in per_segment if s["emotion"] in _EMOTION_NEGATIVE and s["score"] >= 0.6][:12]
    if peaks:
        lines += ["", "**声学上情绪强烈的片段（负面，置信 ≥ 0.6）：**", ""]
        for p in peaks:
            lines.append(f"- `{p['start_label']}` {p['speaker']}（{p['emotion']} {p['score']}）：{p['text']}")
    return "\n".join(lines)



def emotion_annotated_transcript(conn: sqlite3.Connection, recording_id: str, per_segment: list[dict[str, Any]]) -> str:
    emo_by_start = {round(s["start"], 1): (s["emotion"], s["score"]) for s in per_segment}
    rows = conn.execute(
        "select start_label, end_sec, start_sec, speaker, speaker_name, text from transcript_segments where recording_id = ? order by start_sec",
        (recording_id,),
    ).fetchall()
    lines = []
    for row in rows:
        label = row["speaker_name"] or f"Speaker {row['speaker']}"
        emo = emo_by_start.get(round(float(row["start_sec"] or 0), 1))
        tag = f"（声学:{emo[0]}{emo[1]}）" if emo else ""
        lines.append(f"[{row['start_label']}-{seconds_label(row['end_sec'])}] {label}{tag}: {row['text']}")
    return "\n".join(lines)



def next_emotion_version(conn: sqlite3.Connection, recording_id: str) -> int:
    row = conn.execute(
        "select coalesce(max(version), 0) from emotion_analyses where recording_id = ?",
        (recording_id,),
    ).fetchone()
    return int(row[0]) + 1



def current_emotion_analysis(conn: sqlite3.Connection, recording_id: str) -> dict[str, Any] | None:
    return rowdict(
        conn.execute(
            """
            select * from emotion_analyses
            where recording_id = ?
            order by is_current desc, version desc, created_at desc
            limit 1
            """,
            (recording_id,),
        ).fetchone()
    )



def generate_emotion_analysis(recording_id: str, user: dict[str, Any]) -> dict[str, Any]:
    with db() as conn:
        rec = can_access_recording(conn, recording_id, user)
        if rec["asr_status"] != "done":
            raise HTTPException(status_code=409, detail="转写尚未完成")
        segments = rowsdict(
            conn.execute(
                "select * from transcript_segments where recording_id = ? order by start_sec",
                (recording_id,),
            ).fetchall()
        )
        if not segments:
            raise HTTPException(status_code=409, detail="转写为空")
        task_id = create_task(conn, recording_id, rec["title"], "对话情绪分析")
        version = next_emotion_version(conn, recording_id)
        conn.commit()

    try:
        per_segment, per_speaker = analyze_acoustic_emotions(rec, segments)
        acoustic_md = acoustic_markdown(per_speaker, per_segment)
        with db() as conn:
            annotated = emotion_annotated_transcript(conn, recording_id, per_segment)
        analysis_md, model = call_deepseek_emotion(annotated, rec, acoustic_md)
        content = analysis_md.rstrip() + "\n\n" + acoustic_md + "\n"
        model_label = f"emotion2vec_plus_large + {model}"
        with db() as conn:
            emotion_id = str(uuid.uuid4())
            conn.execute("update emotion_analyses set is_current = 0 where recording_id = ?", (recording_id,))
            conn.execute(
                "insert into emotion_analyses(id,recording_id,content,model,acoustic_json,created_at,version,is_current) values(?,?,?,?,?,?,?,?)",
                (
                    emotion_id,
                    recording_id,
                    content,
                    model_label,
                    json.dumps({"per_speaker": per_speaker, "segments": per_segment[:400]}, ensure_ascii=False),
                    now(),
                    version,
                    1,
                ),
            )
            update_task(conn, task_id, "done", 100)
            audit(conn, user, "emotion", f"生成对话情绪分析：{rec['title']}，模型 {model_label}。")
        return {"recording_id": recording_id, "emotion_id": emotion_id, "model": model_label, "version": version}
    except HTTPException:
        with db() as conn:
            update_task(conn, task_id, "failed", 100, "emotion analysis failed")
        raise
    except Exception as exc:
        with db() as conn:
            update_task(conn, task_id, "failed", 100, str(exc))
            audit(conn, user, "emotion", f"对话情绪分析失败：{rec['title']}。")
        print(f"[error] emotion: {type(exc).__name__}: {exc}", flush=True)
        raise HTTPException(status_code=500, detail="情绪分析失败，请查看日志") from exc



def run_emotion_job(recording_id: str, user: dict[str, Any]) -> None:
    """Background worker for emotion analysis. Failures are already recorded on
    the task row by generate_emotion_analysis, so just swallow the exception."""
    try:
        generate_emotion_analysis(recording_id, user)
    except Exception:
        pass

