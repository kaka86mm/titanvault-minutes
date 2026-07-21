"""声纹：匹配、说话人合并、声纹样本管理。"""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from statistics import median
from typing import Any

from fastapi import HTTPException

from . import state
from .config import (
    CAMPLUS, VOICEPRINTS, TMP, FFMPEG, env_float, env_int,
)
from .db import db, seconds_label
from .state import (
    DEFAULT_VOICEPRINT_THRESHOLD,
    asr_lock as _asr_lock,
    verifier_init_lock as _verifier_init_lock,
)


def normalize_profile(row: dict[str, Any]) -> dict[str, Any]:
    if not row.get("scope"):
        row["scope"] = "global" if not row.get("team_id") else "team"
    return row



def get_speaker_verifier() -> Any:
    if state.speaker_verifier is None:
        with _verifier_init_lock:
            if state.speaker_verifier is None:
                from modelscope.pipelines import pipeline
                from modelscope.utils.constant import Tasks

                state.speaker_verifier = pipeline(task=Tasks.speaker_verification, model=str(CAMPLUS))
    return state.speaker_verifier



def voiceprint_threshold_default() -> float:
    return env_float("AHAMVOICE_VOICEPRINT_THRESHOLD", DEFAULT_VOICEPRINT_THRESHOLD, 0.45, 0.95)



def clamp_voiceprint_threshold(value: Any) -> float:
    try:
        threshold = float(value)
    except (TypeError, ValueError):
        threshold = voiceprint_threshold_default()
    return max(0.45, min(0.95, threshold))



def voiceprint_match_settings() -> dict[str, float | int]:
    return {
        "min_threshold": env_float("AHAMVOICE_VOICEPRINT_MIN_THRESHOLD", voiceprint_threshold_default(), 0.45, 0.95),
        "margin": env_float("AHAMVOICE_VOICEPRINT_MARGIN", 0.08, 0.0, 0.35),
        "sample_limit": env_int("AHAMVOICE_VOICEPRINT_MATCH_SAMPLES", 8, 3, 12),
        "max_sample_seconds": env_float("AHAMVOICE_VOICEPRINT_SAMPLE_SECONDS", 14.0, 5.0, 25.0),
        "min_sample_seconds": env_float("AHAMVOICE_VOICEPRINT_MIN_SAMPLE_SECONDS", 2.0, 1.0, 8.0),
    }



def ranked_voiceprint_intervals(sentence_info: list[dict[str, Any]], sample_limit: int, min_sample_seconds: float) -> dict[str, list[tuple[float, float]]]:
    intervals: dict[str, list[tuple[float, float, float]]] = {}
    for item in sentence_info:
        spk = str(item.get("spk", "unknown"))
        start = float(item.get("start", 0)) / 1000.0
        end = float(item.get("end", 0)) / 1000.0
        duration = max(0.0, end - start)
        if duration >= min_sample_seconds:
            quality = min(duration, 18.0)
            intervals.setdefault(spk, []).append((quality, start, end))
    return {
        spk: [(start, end) for _, start, end in sorted(ranges, reverse=True)[:sample_limit]]
        for spk, ranges in intervals.items()
    }



def aggregate_voiceprint_scores(scores: list[float]) -> float:
    if not scores:
        return -1.0
    top_scores = sorted(scores, reverse=True)[: min(5, len(scores))]
    return float(median(top_scores))



def load_speaker_profiles(conn: sqlite3.Connection, team_id: str | None, owner_id: str | None) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select * from speaker_profiles
        where active = 1
          and (
            scope = 'global'
            or (scope = 'team' and team_id = ?)
            or (scope = 'personal' and owner_id = ?)
          )
        order by created_at desc
        """,
        (team_id, owner_id),
    ).fetchall()
    return [normalize_profile(dict(row)) for row in rows if Path(row["sample_path"]).exists()]



def extract_interval(source: Path, target: Path, start_sec: float, end_sec: float) -> None:
    duration = max(0.2, end_sec - start_sec)
    subprocess.run(
        [
            str(FFMPEG),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start_sec:.3f}",
            "-i",
            str(source),
            "-t",
            f"{duration:.3f}",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(target),
        ],
        check=True,
    )



def concat_audio(parts: list[Path], target: Path, workdir: Path) -> None:
    if not parts:
        raise RuntimeError("no audio parts to concat")
    concat_file = workdir / "concat.txt"
    concat_file.write_text(
        "\n".join(f"file '{str(part).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'" for part in parts),
        encoding="utf-8",
    )
    subprocess.run(
        [
            str(FFMPEG),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-ar",
            "16000",
            "-ac",
            "1",
            str(target),
        ],
        check=True,
    )



def match_speaker_profiles(rec: dict[str, Any], sentence_info: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    with db() as conn:
        profiles = load_speaker_profiles(conn, rec.get("team_id"), rec.get("owner_id"))
    if not profiles:
        return {}

    settings = voiceprint_match_settings()
    intervals = ranked_voiceprint_intervals(
        sentence_info,
        int(settings["sample_limit"]),
        float(settings["min_sample_seconds"]),
    )
    matches: dict[str, dict[str, Any]] = {}
    verifier = get_speaker_verifier()
    with tempfile.TemporaryDirectory(dir=TMP) as tmp:
        tmpdir = Path(tmp)
        for spk, ranges in intervals.items():
            profile_scores: dict[str, list[float]] = {profile["id"]: [] for profile in profiles}
            for idx, (start, end) in enumerate(ranges):
                sample = tmpdir / f"spk_{spk}_{idx}.wav"
                extract_interval(Path(rec["file_path"]), sample, start, min(end, start + float(settings["max_sample_seconds"])))
                for profile in profiles:
                    try:
                        with _asr_lock:
                            result = verifier([str(sample), profile["sample_path"]])
                    except Exception:
                        continue
                    if isinstance(result, list):
                        result = result[0] if result else {}
                    score = float(result.get("score", -1.0))
                    if score >= 0:
                        profile_scores[profile["id"]].append(score)
            name_results: dict[str, dict[str, Any]] = {}
            for profile in profiles:
                scores = profile_scores.get(profile["id"], [])
                if not scores:
                    continue
                aggregate = aggregate_voiceprint_scores(scores)
                threshold = max(
                    float(settings["min_threshold"]),
                    clamp_voiceprint_threshold(profile.get("threshold")),
                )
                hit_count = sum(1 for score in scores if score >= threshold - 0.03)
                name_key = str(profile["name"]).strip()
                current = name_results.get(name_key)
                if not current or aggregate > float(current["score"]):
                    name_results[name_key] = {
                        "name": name_key,
                        "voiceprint_id": profile["id"],
                        "score": aggregate,
                        "threshold": threshold,
                        "hit_count": hit_count,
                    }
            ranked = sorted(name_results.values(), key=lambda item: float(item["score"]), reverse=True)
            if not ranked:
                continue
            best = ranked[0]
            second_score = float(ranked[1]["score"]) if len(ranked) > 1 else -1.0
            margin = float(best["score"]) - second_score if second_score >= 0 else 1.0
            if (
                best["name"]
                and float(best["score"]) >= float(best["threshold"])
                and margin >= float(settings["margin"])
                and int(best["hit_count"]) >= min(2, len(ranges))
            ):
                matches[spk] = {
                    "name": best["name"],
                    "voiceprint_id": best["voiceprint_id"] or None,
                    "score": round(float(best["score"]), 5),
                }
    return matches



def normalize_speaker_id(value: Any) -> str:
    try:
        return str(int(value) + 1)
    except (TypeError, ValueError):
        raw = str(value or "unknown")
        return raw if raw.startswith("Speaker") else raw



def resolve_voiceprint_scope(user: dict[str, Any], scope: str = "", team_id: str = "") -> tuple[str, str | None]:
    # Single-user mode: no role gating. Default to global; team/personal still
    # honored if explicitly requested (team without team_id degrades to global).
    requested_scope = scope or "global"
    if requested_scope not in {"personal", "team", "global"}:
        raise HTTPException(status_code=400, detail="invalid voiceprint scope")
    profile_team_id = None
    if requested_scope == "team":
        profile_team_id = team_id or user.get("team_id")
        if not profile_team_id:
            # No fixed team → degrade to global (visible to the single user).
            return "global", None
    return requested_scope, profile_team_id



def segment_quality(row: dict[str, Any]) -> str:
    duration = max(0.0, float(row.get("end_sec") or 0) - float(row.get("start_sec") or 0))
    text = str(row.get("text") or "").strip()
    if 5 <= duration <= 40 and len(text) >= 8:
        return "good"
    if duration >= 2.0 and len(text) >= 4:
        return "usable"
    return "short"



def candidate_sample_rows(rows: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    rank = {"good": 3, "usable": 2, "short": 1}
    sorted_rows = sorted(
        rows,
        key=lambda row: (
            rank.get(segment_quality(row), 0),
            min(float(row.get("end_sec") or 0) - float(row.get("start_sec") or 0), 18.0),
            len(str(row.get("text") or "")),
        ),
        reverse=True,
    )
    return sorted_rows[:limit]



def speaker_candidate_payload(speaker: str, rows: list[dict[str, Any]], recording_id: str) -> dict[str, Any]:
    total = sum(max(0.0, float(row["end_sec"]) - float(row["start_sec"])) for row in rows)
    named = [row for row in rows if row.get("speaker_name")]
    voiceprinted = [row for row in rows if row.get("voiceprint_id")]
    samples = []
    for row in candidate_sample_rows(rows):
        duration = max(0.0, float(row["end_sec"]) - float(row["start_sec"]))
        samples.append(
            {
                "id": row["id"],
                "recording_id": recording_id,
                "speaker": speaker,
                "start_sec": row["start_sec"],
                "end_sec": row["end_sec"],
                "start_label": row["start_label"],
                "duration": duration,
                "duration_label": seconds_label(duration),
                "text": row["text"],
                "quality": segment_quality(row),
                "audio_url": f"/api/recordings/{recording_id}/segments/{row['id']}/audio",
            }
        )
    return {
        "speaker": speaker,
        "display_name": named[0]["speaker_name"] if named else f"Speaker {speaker}",
        "speaker_name": named[0]["speaker_name"] if named else None,
        "voiceprint_id": voiceprinted[0]["voiceprint_id"] if voiceprinted else None,
        "segment_count": len(rows),
        "total_duration": total,
        "total_duration_label": seconds_label(total),
        "sample_segments": samples,
    }



def can_manage_voiceprint(profile: dict[str, Any], user: dict[str, Any]) -> bool:
    # Single-user mode: local-admin owns everything → always manageable.
    return True

