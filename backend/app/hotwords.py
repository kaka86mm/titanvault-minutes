"""热词系统：多维打分、双轨包、ASR 热词可说性过滤。"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any

from .config import env_int
from .db import safe_json, parse_time, now, rowsdict, rowdict


HOTWORD_KIND_PRIORITY = {
    "产品": 90,
    "系统": 88,
    "行业": 82,
    "业务术语": 78,
    "项目": 74,
    "商机": 70,
    "人员": 68,
    "客户简称": 64,
    "客户": 60,
    "潜在客户": 54,
    "客户规模": 30,
}


_FORMAL_ORG_MARKER = re.compile(r"(公司|集团|股份|有限|责任)")


def code_like_hotword(text: str) -> bool:
    value = text.strip()
    if len(value) <= 3 and re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return True
    return bool(re.fullmatch(r"[A-Za-z]{2,}[-_]?\d{2,}[A-Za-z0-9_-]*", value))



def load_hotword_map(conn: sqlite3.Connection) -> dict[str, str]:
    mapping: dict[str, str] = {}
    rows = conn.execute("select word, aliases from hotwords where active = 1").fetchall()
    for row in rows:
        word = row["word"]
        for alias in (row["aliases"] or "").split(","):
            alias = alias.strip()
            if alias and len(alias) >= 2 and not code_like_hotword(alias):
                mapping[alias.lower()] = word
    return mapping



def apply_hotwords(text: str, hotwords: dict[str, str]) -> str:
    fixed = text
    for alias, word in sorted(hotwords.items(), key=lambda item: len(item[0]), reverse=True):
        fixed = re.sub(re.escape(alias), word, fixed, flags=re.IGNORECASE)
    return fixed



def hotword_terms(row: dict[str, Any], alias_limit: int = 2) -> list[str]:
    terms = [str(row.get("word") or "").strip()]
    aliases = [
        item.strip()
        for item in str(row.get("aliases") or "").split(",")
        if item.strip() and valid_asr_hotword(item.strip())
    ]
    terms.extend(aliases[:alias_limit])
    clean_terms: list[str] = []
    seen: set[str] = set()
    for term in terms:
        key = term.lower()
        if valid_asr_hotword(term) and key not in seen:
            clean_terms.append(term)
            seen.add(key)
    return clean_terms



def hotword_row_score(row: dict[str, Any], rec: dict[str, Any] | None = None, user: dict[str, Any] | None = None) -> float:
    score = float(row.get("score") or 0)
    weight = int(row.get("weight") or 0)
    kind = str(row.get("kind") or "业务术语")
    source = str(row.get("source") or "")
    score += weight * 12
    score += HOTWORD_KIND_PRIORITY.get(kind, 45)
    if int(row.get("protected") or 0):
        score += 220
    if source.startswith("CRM"):
        score += 40
    elif source.startswith("企微"):
        score += 32
    elif source in {"系统内置", "产品库", "手工维护"}:
        score += 60
    frequency = int(row.get("frequency") or 0)
    score += min(80, frequency * 3)
    last_seen = parse_time(row.get("last_seen_at"))
    if last_seen:
        age_days = max(0, (datetime.now() - last_seen).days)
        if age_days <= 7:
            score += 80
        elif age_days <= 30:
            score += 45
        elif age_days <= 90:
            score += 15
        else:
            score -= min(90, age_days // 3)
    if rec:
        context = " ".join(
            str(item or "")
            for item in [rec.get("title"), rec.get("filename"), rec.get("tag"), rec.get("meeting_type")]
        ).lower()
        for term in hotword_terms(row, alias_limit=6):
            if term.lower() in context:
                score += 180
                break
        if row.get("team_id") and row.get("team_id") == rec.get("team_id"):
            score += 90
        if row.get("owner_id") and row.get("owner_id") == rec.get("owner_id"):
            score += 70
    if user:
        if row.get("team_id") and row.get("team_id") == user.get("team_id"):
            score += 50
        if row.get("owner_id") and row.get("owner_id") == user.get("id"):
            score += 45
    word = str(row.get("word") or "")
    if len(word) > 28:
        score -= len(word) - 28
    return score



def hotword_limits() -> dict[str, int]:
    return {
        "asr": env_int("AHAMVOICE_HOTWORD_LIMIT", 3000, 200, 6000),
        "correction": env_int("AHAMVOICE_CORRECTION_HOTWORD_LIMIT", 10000, 1000, 20000),
        "protected": env_int("AHAMVOICE_PROTECTED_HOTWORD_LIMIT", 1200, 100, 5000),
    }



def build_hotword_package(conn: sqlite3.Connection, rec: dict[str, Any], user: dict[str, Any], persist: bool = True) -> dict[str, Any]:
    limits = hotword_limits()
    rows = rowsdict(
        conn.execute(
            """
            select * from hotwords
            where active = 1
              and coalesce(state, 'active') in ('active', 'protected')
              and (expires_at is null or expires_at = '' or datetime(expires_at) > datetime('now'))
            """
        ).fetchall()
    )
    ranked = sorted(rows, key=lambda row: hotword_row_score(row, rec, user), reverse=True)
    protected_rows = [row for row in ranked if int(row.get("protected") or 0)]
    dynamic_rows = [row for row in ranked if not int(row.get("protected") or 0)]
    selected_rows: list[dict[str, Any]] = []
    selected_terms: list[str] = []
    selected_keys: set[str] = set()

    def add_rows(candidates: list[dict[str, Any]], term_limit: int, alias_limit: int = 2) -> None:
        for row in candidates:
            row_terms = hotword_terms(row, alias_limit=alias_limit)
            new_terms = [term for term in row_terms if term.lower() not in selected_keys]
            if not new_terms:
                continue
            if len(selected_terms) + len(new_terms) > term_limit:
                continue
            selected_rows.append(row)
            for term in new_terms:
                selected_terms.append(term)
                selected_keys.add(term.lower())
            if len(selected_terms) >= term_limit:
                break

    add_rows(protected_rows, min(limits["protected"], limits["asr"]), alias_limit=6)
    selected_row_ids = {row.get("id") for row in selected_rows if row.get("id")}
    overflow_rows = [row for row in ranked if row.get("id") not in selected_row_ids]
    add_rows(overflow_rows or dynamic_rows, limits["asr"], alias_limit=3)

    correction_terms = selected_terms[:]
    correction_keys = {term.lower() for term in correction_terms}
    replacement_map: dict[str, str] = {}
    canonical_words = {str(row.get("word") or "").strip().lower() for row in rows}
    for row in selected_rows:
        word = str(row.get("word") or "").strip()
        for alias in hotword_terms(row, alias_limit=8):
            alias_key = alias.lower()
            if alias != word and alias_key not in canonical_words:
                existing = replacement_map.get(alias_key)
                if not existing or len(word) < len(existing):
                    replacement_map[alias_key] = word
            if alias_key not in correction_keys and len(correction_terms) < limits["correction"]:
                correction_terms.append(alias)
                correction_keys.add(alias_key)
    for row in rows:
        word = str(row.get("word") or "").strip()
        if not word or str(row.get("kind") or "") not in {"客户简称", "项目"}:
            continue
        if word.lower() not in correction_keys and word.lower() not in selected_keys:
            continue
        for alias in hotword_terms(row, alias_limit=8):
            alias_key = alias.lower()
            if alias == word or alias_key in canonical_words:
                continue
            existing = replacement_map.get(alias_key)
            if not existing or len(word) < len(existing):
                replacement_map[alias_key] = word

    source_counts: dict[str, int] = {}
    for row in selected_rows:
        source = str(row.get("source") or "未知来源")
        source_counts[source] = source_counts.get(source, 0) + 1
    # 规范名词表：按 kind 分组，喂给纪要 LLM，统一专有名词写法。
    # 只保留被选中的热词（asr_terms 命中的），避免把词库全量塞进 prompt。
    glossary: dict[str, list[str]] = {}
    seen_words: set[str] = set()
    for row in selected_rows:
        word = str(row.get("word") or "").strip()
        key = word.lower()
        if not word or key in seen_words:
            continue
        seen_words.add(key)
        kind = str(row.get("kind") or "业务术语").strip()
        glossary.setdefault(kind, []).append(word)
    package = {
        "asr_terms": selected_terms,
        "correction_terms": correction_terms,
        "asr_terms_count": len(selected_terms),
        "correction_terms_count": len(correction_terms),
        "protected_terms_count": sum(1 for row in selected_rows if int(row.get("protected") or 0)),
        "dynamic_terms_count": sum(1 for row in selected_rows if not int(row.get("protected") or 0)),
        "source_summary": source_counts,
        "replacement_map": replacement_map,
        "glossary": glossary,
    }
    if persist:
        version = (
            conn.execute("select coalesce(max(version), 0) + 1 from recording_hotword_packages where recording_id = ?", (rec["id"],)).fetchone()[0]
            or 1
        )
        conn.execute(
            """
            insert into recording_hotword_packages(
                id,recording_id,version,asr_terms_count,correction_terms_count,protected_terms_count,dynamic_terms_count,
                source_summary,asr_terms,correction_terms,created_at,glossary
            ) values(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()),
                rec["id"],
                version,
                package["asr_terms_count"],
                package["correction_terms_count"],
                package["protected_terms_count"],
                package["dynamic_terms_count"],
                json.dumps(source_counts, ensure_ascii=False),
                json.dumps(selected_terms, ensure_ascii=False),
                json.dumps(correction_terms, ensure_ascii=False),
                now(),
                json.dumps(glossary, ensure_ascii=False),
            ),
        )
        selected_ids = [row["id"] for row in selected_rows if row.get("id")]
        if selected_ids:
            placeholders = ",".join("?" for _ in selected_ids)
            conn.execute(
                f"update hotwords set last_used_at = ?, hit_count = coalesce(hit_count, 0) + 1 where id in ({placeholders})",
                (now(), *selected_ids),
            )
    return package



def latest_hotword_package(conn: sqlite3.Connection, recording_id: str) -> dict[str, Any] | None:
    row = rowdict(
        conn.execute(
            """
            select * from recording_hotword_packages
            where recording_id = ?
            order by version desc, created_at desc
            limit 1
            """,
            (recording_id,),
        ).fetchone()
    )
    if not row:
        return None
    row["source_summary"] = safe_json(row.get("source_summary"), {})
    row["asr_terms"] = safe_json(row.get("asr_terms"), [])
    row["correction_terms"] = safe_json(row.get("correction_terms"), [])
    row["glossary"] = safe_json(row.get("glossary"), {})
    return row



def valid_asr_hotword(text: str) -> bool:
    value = text.strip()
    # 只喂能被说出口的短词：超长全称口语召回≈0、且会误偏置。
    max_len = env_int("AHAMVOICE_HOTWORD_MAX_LEN", 8, 4, 20)
    if len(value) < 2 or len(value) > max_len:
        return False
    if re.search(r"\s", value):
        return False
    if value.isdigit() or code_like_hotword(value):
        return False
    if _FORMAL_ORG_MARKER.search(value):
        return False
    return True



def hotword_prompt(conn: sqlite3.Connection) -> str:
    limit = hotword_limits()["asr"]
    rows = conn.execute(
        """
        select * from hotwords
        where active = 1
          and coalesce(state, 'active') in ('active', 'protected')
          and (expires_at is null or expires_at = '' or datetime(expires_at) > datetime('now'))
        """
    ).fetchall()
    kind_limits = {
        "产品": 600,
        "系统": 600,
        "业务术语": 900,
        "项目": 700,
        "商机": 700,
        "人员": 500,
        "行业": 500,
        "客户简称": 500,
        "客户": 900,
        "潜在客户": 700,
    }
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        row_data = dict(row)
        kind = row_data.get("kind") or "业务术语"
        score = hotword_row_score(row_data)
        terms = hotword_terms(row_data, alias_limit=2)
        for raw in terms:
            term = raw.strip()
            key = term.lower()
            if not valid_asr_hotword(term) or key in seen:
                continue
            seen.add(key)
            length_penalty = max(0, len(term) - 20)
            candidates.append(
                {
                    "term": term,
                    "kind": kind,
                    "score": score - length_penalty,
                }
            )
    candidates.sort(key=lambda item: (item["score"], -len(item["term"])), reverse=True)
    selected: list[str] = []
    selected_keys: set[str] = set()
    counts: dict[str, int] = {}
    remainder: list[dict[str, Any]] = []
    for item in candidates:
        kind = item["kind"]
        if counts.get(kind, 0) >= kind_limits.get(kind, 50):
            remainder.append(item)
            continue
        selected.append(item["term"])
        selected_keys.add(item["term"].lower())
        counts[kind] = counts.get(kind, 0) + 1
        if len(selected) >= limit:
            return " ".join(selected)
    for item in remainder:
        key = item["term"].lower()
        if key in selected_keys:
            continue
        selected.append(item["term"])
        if len(selected) >= limit:
            break
    return " ".join(selected)

