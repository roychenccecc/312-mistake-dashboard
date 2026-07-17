#!/usr/bin/env python3
"""Audit and deterministically repair attempt-to-mastery-unit mappings.

Before ``--apply``, create and verify a database backup and apply the included
dashboard-integrity migrations. This backfill never fuzzy-matches question text
or knowledge-point names; unresolved rows remain NULL and are listed in the
JSON audit.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any


REQUIRED_SCHEMA_VERSION = 9
VERIFIED_PHASES = {"legacy", "pre_review", "delayed_retest"}
QUESTION_FAMILIES = {
    "single_choice",
    "multiple_choice",
    "short_answer",
    "extended_response",
    "other_objective",
}


def json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def nonempty(value: Any) -> bool:
    return value not in (None, "", [], {})


def valid_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def parse_json_text(value: str | None, default: Any) -> Any:
    if not value:
        return json_clone(default)
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return json_clone(default)


def connect(database: Path, apply: bool) -> sqlite3.Connection:
    database = database.expanduser().resolve()
    if not database.is_file():
        raise ValueError(f"database does not exist: {database}")
    if apply:
        connection = sqlite3.connect(database)
    else:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only = ON")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    version = connection.execute(
        "SELECT value FROM system_meta WHERE key='schema_version'"
    ).fetchone()
    if not version or int(version[0]) < REQUIRED_SCHEMA_VERSION:
        connection.close()
        raise ValueError(
            "SQLite schema v9 is required; apply the included integrity migrations first"
        )
    scope = connection.execute(
        "SELECT value FROM system_meta WHERE key='schema_scope'"
    ).fetchone()
    if scope and scope[0] == "dashboard-compatible-subset":
        connection.close()
        raise ValueError(
            "the synthetic demo uses a dashboard-compatible schema subset and cannot be backfilled"
        )
    return connection


def load_manifest(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "units": [],
            "mappings": [],
            "chapter_aliases": [],
            "exam_occurrences": [],
            "frequency_audits": [],
        }
    parsed = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if isinstance(parsed, list):
        parsed = {"mappings": parsed}
    if not isinstance(parsed, dict):
        raise ValueError("reviewed mappings JSON must be an object or a mapping-row list")
    result = {
        "units": parsed.get("units", []),
        "mappings": parsed.get("mappings", []),
        "chapter_aliases": parsed.get("chapter_aliases", []),
        "exam_occurrences": parsed.get("exam_occurrences", []),
        "frequency_audits": parsed.get("frequency_audits", []),
    }
    for key, value in result.items():
        if not isinstance(value, list):
            raise ValueError(f"manifest field {key} must be a list")
    return result


def chapter_alias_pairs(
    raw_groups: list[Any],
    chapter_ids: set[str],
) -> tuple[set[frozenset[str]], list[dict[str, Any]]]:
    pairs: set[frozenset[str]] = set()
    errors: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_groups):
        ids = raw.get("chapter_ids") if isinstance(raw, dict) else raw
        if not isinstance(ids, list) or len(ids) < 2:
            errors.append({
                "index": index,
                "reason": "chapter alias must contain at least two chapter_ids",
                "value": raw,
            })
            continue
        normalized = [str(item).strip() for item in ids if str(item).strip()]
        missing = sorted(set(normalized) - chapter_ids)
        if len(set(normalized)) < 2 or missing:
            errors.append({
                "index": index,
                "reason": "chapter alias contains duplicate or unknown chapter IDs",
                "missing_chapter_ids": missing,
                "value": raw,
            })
            continue
        for left_index, left in enumerate(normalized):
            for right in normalized[left_index + 1 :]:
                pairs.add(frozenset((left, right)))
    return pairs, errors


def chapters_compatible(
    session_chapter_id: str | None,
    unit_chapter_id: str,
    aliases: set[frozenset[str]],
) -> bool:
    if not session_chapter_id:
        return False
    return (
        session_chapter_id == unit_chapter_id
        or frozenset((session_chapter_id, unit_chapter_id)) in aliases
    )


def prepare_units(
    connection: sqlite3.Connection,
    rows: list[Any],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    existing_rows = connection.execute(
        "SELECT * FROM chapter_mastery_units ORDER BY mastery_unit_id"
    ).fetchall()
    catalog = {str(row["mastery_unit_id"]): dict(row) for row in existing_rows}
    name_index = {
        (str(row["chapter_id"]), str(row["name"])): str(row["mastery_unit_id"])
        for row in existing_rows
    }
    chapter_ids = {
        str(row[0]) for row in connection.execute("SELECT chapter_id FROM chapters")
    }
    actions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()

    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            errors.append({"index": index, "reason": "unit row must be an object"})
            continue
        unit_id = str(raw.get("mastery_unit_id") or "").strip()
        chapter_id = str(raw.get("chapter_id") or "").strip()
        name = str(raw.get("name") or "").strip()
        module_name = raw.get("module_name")
        module_name = str(module_name).strip() if module_name is not None else None
        evidence = raw.get("evidence")
        note = raw.get("note")
        if not unit_id or not chapter_id or not name or not nonempty(evidence):
            errors.append({
                "index": index,
                "mastery_unit_id": unit_id or None,
                "reason": "unit requires mastery_unit_id, chapter_id, name, and non-empty evidence",
            })
            continue
        if unit_id in seen:
            errors.append({
                "index": index,
                "mastery_unit_id": unit_id,
                "reason": "duplicate unit ID in manifest",
            })
            continue
        seen.add(unit_id)
        if chapter_id not in chapter_ids:
            errors.append({
                "index": index,
                "mastery_unit_id": unit_id,
                "reason": "unit chapter does not exist",
                "chapter_id": chapter_id,
            })
            continue

        existing = catalog.get(unit_id)
        conflicting_name_id = name_index.get((chapter_id, name))
        if conflicting_name_id and conflicting_name_id != unit_id:
            errors.append({
                "index": index,
                "mastery_unit_id": unit_id,
                "reason": "chapter already has this exact unit name under another ID",
                "existing_mastery_unit_id": conflicting_name_id,
            })
            continue
        if existing:
            mismatches = {}
            if existing["chapter_id"] != chapter_id:
                mismatches["chapter_id"] = [existing["chapter_id"], chapter_id]
            if existing["name"] != name:
                mismatches["name"] = [existing["name"], name]
            if module_name and existing.get("module_name") not in (None, "", module_name):
                mismatches["module_name"] = [existing.get("module_name"), module_name]
            if mismatches:
                errors.append({
                    "index": index,
                    "mastery_unit_id": unit_id,
                    "reason": "reviewed unit conflicts with existing exact fields",
                    "mismatches": mismatches,
                })
                continue
            action_name = "merge_review_evidence"
            metadata = parse_json_text(existing.get("metadata_json"), {})
        else:
            action_name = "insert"
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {"legacy_metadata": metadata}
        metadata["reviewed_mapping_seed"] = {
            "evidence": json_clone(evidence),
            "note": note,
        }
        proposed = {
            "mastery_unit_id": unit_id,
            "chapter_id": chapter_id,
            "name": name,
            "module_name": module_name or (existing.get("module_name") if existing else None),
            "metadata_json": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        }
        catalog[unit_id] = proposed
        name_index[(chapter_id, name)] = unit_id
        actions.append({
            "action": action_name,
            **proposed,
            "evidence": json_clone(evidence),
            "note": note,
        })
    return catalog, actions, errors


def prepare_reviewed_mappings(
    connection: sqlite3.Connection,
    rows: list[Any],
    unit_catalog: dict[str, dict[str, Any]],
    aliases: set[frozenset[str]],
    verified_links: dict[str, set[str]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    question_ids = {
        str(row[0]) for row in connection.execute("SELECT question_id FROM questions")
    }
    question_chapters: dict[str, set[str | None]] = defaultdict(set)
    for row in connection.execute(
        """
        SELECT a.question_id, rs.chapter_id
        FROM attempts a
        LEFT JOIN review_sessions rs ON rs.session_id=a.session_id
        """
    ):
        question_chapters[str(row["question_id"])].add(
            str(row["chapter_id"]) if row["chapter_id"] else None
        )

    accepted: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    seen_questions: set[str] = set()
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            errors.append({"index": index, "reason": "mapping row must be an object"})
            continue
        question_id = str(raw.get("question_id") or "").strip()
        unit_id = str(raw.get("mastery_unit_id") or "").strip()
        evidence = raw.get("evidence")
        note = raw.get("note")
        tested_dimension = str(raw.get("tested_dimension") or "primary").strip()
        allow_cross_chapter = raw.get("allow_cross_chapter") is True
        cross_chapter_reason = str(raw.get("cross_chapter_reason") or "").strip()
        if not question_id or not unit_id or not tested_dimension or not nonempty(evidence):
            errors.append({
                "index": index,
                "question_id": question_id or None,
                "reason": "mapping requires question_id, mastery_unit_id, tested_dimension, and non-empty evidence",
            })
            continue
        if tested_dimension != "primary":
            errors.append({
                "index": index,
                "question_id": question_id or None,
                "reason": "attempt denominator mappings must use tested_dimension primary",
            })
            continue
        if question_id in seen_questions:
            errors.append({
                "index": index,
                "question_id": question_id,
                "reason": "duplicate question mapping in manifest",
            })
            accepted.pop(question_id, None)
            continue
        seen_questions.add(question_id)
        if question_id not in question_ids:
            errors.append({
                "index": index,
                "question_id": question_id,
                "reason": "question does not exist",
            })
            continue
        unit = unit_catalog.get(unit_id)
        if not unit:
            errors.append({
                "index": index,
                "question_id": question_id,
                "reason": "mastery unit does not exist or was not validly seeded",
                "mastery_unit_id": unit_id,
            })
            continue
        chapters = question_chapters.get(question_id, set())
        missing_chapter = not chapters or None in chapters
        incompatible = sorted(
            str(chapter) if chapter else "<missing>"
            for chapter in chapters
            if chapter is not None
            and not chapters_compatible(chapter, str(unit["chapter_id"]), aliases)
        )
        if missing_chapter:
            errors.append({
                "index": index,
                "question_id": question_id,
                "reason": "reviewed mapping requires a known session chapter for every attempt",
                "mastery_unit_id": unit_id,
                "incompatible_session_chapters": ["<missing>"],
            })
            continue
        if incompatible and (not allow_cross_chapter or not cross_chapter_reason):
            errors.append({
                "index": index,
                "question_id": question_id,
                "reason": (
                    "cross-chapter reviewed mapping requires allow_cross_chapter=true "
                    "and a non-empty cross_chapter_reason"
                ),
                "mastery_unit_id": unit_id,
                "incompatible_session_chapters": incompatible,
            })
            continue
        if allow_cross_chapter and not incompatible:
            errors.append({
                "index": index,
                "question_id": question_id,
                "reason": "allow_cross_chapter is only valid for an actual chapter mismatch",
                "mastery_unit_id": unit_id,
            })
            continue
        conflicting_verified = sorted(verified_links.get(question_id, set()) - {unit_id})
        if conflicting_verified:
            errors.append({
                "index": index,
                "question_id": question_id,
                "reason": "reviewed mapping conflicts with an existing verified link",
                "mastery_unit_id": unit_id,
                "conflicting_verified_units": conflicting_verified,
            })
            continue
        accepted[question_id] = {
            "question_id": question_id,
            "mastery_unit_id": unit_id,
            "tested_dimension": tested_dimension,
            "evidence": json_clone(evidence),
            "note": note,
            "allow_cross_chapter": allow_cross_chapter,
            "cross_chapter_reason": cross_chapter_reason or None,
        }
    return accepted, errors


def dashboard_eligible(row: sqlite3.Row) -> bool:
    return bool(
        row["independent_answer"]
        and not row["used_hint"]
        and row["attempt_phase"] in VERIFIED_PHASES
    )


def resolve_attempts(
    connection: sqlite3.Connection,
    unit_catalog: dict[str, dict[str, Any]],
    aliases: set[frozenset[str]],
    reviewed: dict[str, dict[str, Any]],
    verified_links: dict[str, set[str]],
) -> list[dict[str, Any]]:
    name_index = {
        (str(unit["chapter_id"]), str(unit["name"])): unit_id
        for unit_id, unit in unit_catalog.items()
    }
    attempts = connection.execute(
        """
        SELECT a.rowid AS attempt_sequence, a.*,
               rs.chapter_id AS session_chapter_id,
               sl.mastery_unit_id AS slot_mastery_unit_id,
               sl.question_id AS slot_question_id,
               p.chapter_id AS slot_chapter_id
        FROM attempts a
        LEFT JOIN review_sessions rs ON rs.session_id=a.session_id
        LEFT JOIN review_selection_slots sl ON sl.slot_id=a.selection_slot_id
        LEFT JOIN review_selection_plans p ON p.plan_id=sl.plan_id
        ORDER BY a.attempt_date, attempt_sequence, a.attempt_id
        """
    ).fetchall()
    resolutions: list[dict[str, Any]] = []

    for row in attempts:
        question_id = str(row["question_id"])
        session_chapter = str(row["session_chapter_id"]) if row["session_chapter_id"] else None
        current_id = str(row["mastery_unit_id"]) if row["mastery_unit_id"] else None
        candidates: list[dict[str, str]] = []
        ignored: list[dict[str, Any]] = []

        def add_candidate(unit_id: str, method: str) -> None:
            unit = unit_catalog.get(unit_id)
            if not unit:
                ignored.append({
                    "method": method,
                    "mastery_unit_id": unit_id,
                    "reason": "unit_not_found",
                })
                return
            if not chapters_compatible(session_chapter, str(unit["chapter_id"]), aliases):
                ignored.append({
                    "method": method,
                    "mastery_unit_id": unit_id,
                    "reason": "unit_not_in_session_chapter_or_explicit_alias",
                    "unit_chapter_id": unit["chapter_id"],
                })
                return
            candidates.append({"method": method, "mastery_unit_id": unit_id})

        if current_id:
            add_candidate(current_id, "valid_existing_id")

        if row["slot_mastery_unit_id"]:
            slot_question = str(row["slot_question_id"]) if row["slot_question_id"] else None
            if slot_question and slot_question != question_id:
                ignored.append({
                    "method": "selection_slot",
                    "mastery_unit_id": str(row["slot_mastery_unit_id"]),
                    "reason": "selection_slot_question_mismatch",
                    "slot_question_id": slot_question,
                })
            elif row["slot_chapter_id"] and session_chapter != str(row["slot_chapter_id"]):
                ignored.append({
                    "method": "selection_slot",
                    "mastery_unit_id": str(row["slot_mastery_unit_id"]),
                    "reason": "selection_slot_session_chapter_mismatch",
                })
            else:
                add_candidate(str(row["slot_mastery_unit_id"]), "selection_slot")

        link_units = sorted(verified_links.get(question_id, set()))
        verified_link_conflict = len(link_units) > 1
        if len(link_units) == 1:
            add_candidate(link_units[0], "unique_verified_question_link")
        elif verified_link_conflict:
            ignored.append({
                "method": "verified_question_link",
                "reason": "multiple_verified_mastery_units",
                "mastery_unit_ids": link_units,
            })

        if current_id and current_id.startswith("lang:") and session_chapter:
            legacy_name = current_id[len("lang:") :]
            exact_id = name_index.get((session_chapter, legacy_name))
            if exact_id:
                add_candidate(exact_id, "exact_legacy_lang_name")

        reviewed_row = reviewed.get(question_id)
        if reviewed_row:
            selected_id = str(reviewed_row["mastery_unit_id"])
            status = "mapped"
            method = "reviewed_mapping"
            overridden = sorted(
                {
                    candidate["mastery_unit_id"]
                    for candidate in candidates
                    if candidate["mastery_unit_id"] != selected_id
                }
            )
            if overridden:
                ignored.append({
                    "method": "reviewed_mapping",
                    "reason": "reviewed_mapping_overrides_unverified_candidates",
                    "overridden_mastery_unit_ids": overridden,
                })
        else:
            candidate_ids = sorted({candidate["mastery_unit_id"] for candidate in candidates})
            if verified_link_conflict or len(candidate_ids) > 1:
                status = "ambiguous"
                method = None
                selected_id = None
            elif len(candidate_ids) == 1:
                status = "mapped"
                selected_id = candidate_ids[0]
                source_methods = sorted(
                    candidate["method"]
                    for candidate in candidates
                    if candidate["mastery_unit_id"] == selected_id
                )
                method = "+".join(source_methods)
            else:
                status = "unmapped"
                method = None
                selected_id = None

        current_exists = bool(current_id and current_id in unit_catalog)
        current_compatible = bool(
            current_exists
            and chapters_compatible(
                session_chapter,
                str(unit_catalog[current_id]["chapter_id"]),
                aliases,
            )
        )
        if status == "mapped":
            write_action = "unchanged" if current_id == selected_id else "set_mastery_unit"
        elif current_id:
            write_action = (
                "clear_invalid_mastery_unit"
                if not current_compatible
                else "clear_unverified_mastery_unit"
            )
        else:
            write_action = "preserve"
        resolutions.append({
            "attempt_id": str(row["attempt_id"]),
            "question_id": question_id,
            "session_id": str(row["session_id"]) if row["session_id"] else None,
            "session_chapter_id": session_chapter,
            "attempt_date": str(row["attempt_date"]),
            "dashboard_eligible": dashboard_eligible(row),
            "current_mastery_unit_id": current_id,
            "mastery_unit_id": selected_id,
            "status": status,
            "method": method,
            "candidate_evidence": candidates,
            "ignored_evidence": ignored,
            "write_action": write_action,
        })
    return resolutions


def prepare_question_links(
    connection: sqlite3.Connection,
    resolutions: list[dict[str, Any]],
    reviewed: dict[str, dict[str, Any]],
    verified_links: dict[str, set[str]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for resolution in resolutions:
        grouped[resolution["question_id"]].append(resolution)
    existing_keys = {
        (str(row["question_id"]), str(row["mastery_unit_id"]), str(row["tested_dimension"])):
        dict(row)
        for row in connection.execute("SELECT * FROM question_mastery_unit_links")
    }
    actions: list[dict[str, Any]] = []
    for question_id in sorted(grouped):
        rows = grouped[question_id]
        if any(row["status"] != "mapped" for row in rows):
            continue
        unit_ids = {str(row["mastery_unit_id"]) for row in rows}
        if len(unit_ids) != 1:
            continue
        unit_id = next(iter(unit_ids))
        if verified_links.get(question_id, set()) == {unit_id}:
            continue
        reviewed_row = reviewed.get(question_id)
        dimension = (
            str(reviewed_row["tested_dimension"]) if reviewed_row else "primary"
        )
        evidence: list[Any] = []
        if reviewed_row:
            evidence.append({
                "kind": "reviewed_mapping_manifest",
                "evidence": json_clone(reviewed_row["evidence"]),
                "note": reviewed_row.get("note"),
                "allow_cross_chapter": reviewed_row.get("allow_cross_chapter", False),
                "cross_chapter_reason": reviewed_row.get("cross_chapter_reason"),
            })
        evidence.append({
            "kind": "deterministic_attempt_backfill",
            "attempt_ids": sorted(row["attempt_id"] for row in rows),
            "methods": sorted({str(row["method"]) for row in rows}),
        })
        key = (question_id, unit_id, dimension)
        existing = existing_keys.get(key)
        if existing:
            old_evidence = parse_json_text(existing.get("evidence_json"), [])
            if isinstance(old_evidence, list):
                evidence = old_evidence + evidence
            else:
                evidence.insert(0, old_evidence)
        actions.append({
            "question_id": question_id,
            "mastery_unit_id": unit_id,
            "tested_dimension": dimension,
            "link_source": "reviewed_backfill" if reviewed_row else "deterministic_backfill",
            "evidence_json": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            "action": "verify_existing" if existing else "insert_verified",
        })
    return actions


def prepare_frequency_audits(
    rows: list[Any],
    unit_catalog: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    actions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            errors.append({"index": index, "reason": "frequency audit row must be an object"})
            continue
        unit_id = str(raw.get("mastery_unit_id") or "").strip()
        status = str(raw.get("status") or "").strip()
        year_start = raw.get("year_start")
        year_end = raw.get("year_end")
        evidence = raw.get("evidence")
        audited_date = str(raw.get("audited_date") or "").strip()
        blocker_reason = raw.get("blocker_reason")
        audit_note = raw.get("audit_note") or raw.get("note")
        reason: str | None = None
        if unit_id in seen:
            reason = "duplicate frequency audit in manifest"
        elif unit_id not in unit_catalog:
            reason = "frequency audit mastery unit does not exist"
        elif status not in {"partial", "complete", "blocked"}:
            reason = "invalid frequency audit status"
        elif not audited_date or not valid_iso_date(audited_date) or not nonempty(evidence):
            reason = "frequency audit requires audited_date and non-empty evidence"
        elif (year_start is None) != (year_end is None):
            reason = "frequency audit year range must be both present or both absent"
        elif year_start is not None and (
            not isinstance(year_start, int)
            or not isinstance(year_end, int)
            or not (2007 <= year_start <= year_end <= 2026)
        ):
            reason = "frequency audit year range must be within 2007-2026"
        elif status == "partial" and year_start is None:
            reason = "partial frequency audit requires a year range"
        elif status == "complete" and (year_start, year_end) != (2007, 2026):
            reason = "complete frequency audit must cover 2007-2026"
        elif status == "blocked" and not str(blocker_reason or "").strip():
            reason = "blocked frequency audit requires blocker_reason"
        if reason:
            errors.append({
                "index": index,
                "mastery_unit_id": unit_id or None,
                "reason": reason,
            })
            continue
        seen.add(unit_id)
        actions.append({
            "mastery_unit_id": unit_id,
            "status": status,
            "year_start": year_start,
            "year_end": year_end,
            "audit_note": audit_note,
            "blocker_reason": blocker_reason,
            "evidence_json": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            "audited_date": audited_date,
        })
    return actions, errors


def prepare_exam_occurrences(
    connection: sqlite3.Connection,
    rows: list[Any],
    unit_catalog: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    questions = {
        str(row["question_id"]): dict(row)
        for row in connection.execute(
            "SELECT question_id, source_id, source_type FROM questions"
        )
    }
    existing = {
        str(row["occurrence_id"]): dict(row)
        for row in connection.execute("SELECT * FROM knowledge_exam_occurrences")
    }
    source_ids = {
        str(row[0]) for row in connection.execute("SELECT source_id FROM source_materials")
    }
    actions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    claimed_keys = {
        (str(row["mastery_unit_id"]), int(row["year"]), str(row["question_number"])):
        str(row["occurrence_id"])
        for row in existing.values()
    }
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            errors.append({"index": index, "reason": "exam occurrence row must be an object"})
            continue
        occurrence_id = str(raw.get("occurrence_id") or "").strip()
        old = existing.get(occurrence_id)

        def supplied_or_old(key: str) -> Any:
            return raw[key] if key in raw else (old.get(key) if old else None)

        unit_id = str(supplied_or_old("mastery_unit_id") or "").strip()
        question_id = str(raw.get("question_id") or "").strip()
        year = supplied_or_old("year")
        question_number = str(supplied_or_old("question_number") or "").strip()
        source_id = supplied_or_old("source_id")
        source_reference = supplied_or_old("source_reference")
        original_type = str(raw.get("original_question_type") or "").strip()
        family = str(raw.get("question_family") or "").strip()
        points = raw.get("exam_points")
        note = raw.get("verification_note") or raw.get("note")
        verified_date = str(raw.get("verified_date") or "").strip()
        evidence = raw.get("evidence")
        reason: str | None = None
        if occurrence_id in seen:
            reason = "duplicate exam occurrence in manifest"
        elif not occurrence_id or not unit_id or not question_id:
            reason = "exam occurrence requires occurrence_id, mastery_unit_id, and question_id"
        elif unit_id not in unit_catalog:
            reason = "exam occurrence mastery unit does not exist"
        elif question_id not in questions or questions[question_id]["source_type"] != "真题":
            reason = "verified exam occurrence question must exist with source_type 真题"
        elif not isinstance(year, int) or not 2007 <= year <= 2026 or not question_number:
            reason = "exam occurrence requires a 2007-2026 year and question_number"
        elif not original_type or family not in QUESTION_FAMILIES:
            reason = "exam occurrence requires original type and a known non-unknown family"
        elif not isinstance(points, (int, float)) or points < 0:
            reason = "exam occurrence requires non-negative exam_points"
        elif (
            not str(note or "").strip()
            or not verified_date
            or not valid_iso_date(verified_date)
            or not nonempty(evidence)
        ):
            reason = "exam occurrence requires note, verified_date, and non-empty evidence"
        elif not source_id and not str(source_reference or "").strip():
            question_source = questions.get(question_id, {}).get("source_id")
            if question_source:
                source_id = question_source
            else:
                reason = "exam occurrence requires source_id or source_reference"
        if reason is None and source_id and str(source_id) not in source_ids:
            reason = "exam occurrence source_id does not exist"
        if reason is None and old and any(
            str(old[key]) != str(value)
            for key, value in (
                ("mastery_unit_id", unit_id),
                ("year", year),
                ("question_number", question_number),
            )
        ):
            reason = "manifest cannot change existing occurrence identity fields"
        key = (unit_id, int(year) if isinstance(year, int) else -1, question_number)
        claimed = claimed_keys.get(key)
        if reason is None and claimed and claimed != occurrence_id:
            reason = "another occurrence already claims this unit/year/question number"
        if reason:
            errors.append({
                "index": index,
                "occurrence_id": occurrence_id or None,
                "reason": reason,
            })
            continue
        seen.add(occurrence_id)
        claimed_keys[key] = occurrence_id
        unit = unit_catalog[unit_id]
        actions.append({
            "occurrence_id": occurrence_id,
            "chapter_id": str(unit["chapter_id"]),
            "mastery_unit_id": unit_id,
            "question_id": question_id,
            "year": int(year),
            "question_number": question_number,
            "question_part": supplied_or_old("question_part"),
            "original_question_type": original_type,
            "question_family": family,
            "exam_points": float(points),
            "source_id": source_id,
            "source_reference": source_reference,
            "verification_note": str(note),
            "verified_date": verified_date,
            "evidence_json": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            "action": "verify_existing" if old else "insert_verified",
        })
    return actions, errors


def apply_actions(
    connection: sqlite3.Connection,
    unit_actions: list[dict[str, Any]],
    resolutions: list[dict[str, Any]],
    link_actions: list[dict[str, Any]],
    occurrence_actions: list[dict[str, Any]],
    frequency_audit_actions: list[dict[str, Any]],
) -> dict[str, int]:
    counts: Counter[str] = Counter()
    connection.execute("BEGIN IMMEDIATE")
    try:
        for action in unit_actions:
            connection.execute(
                """
                INSERT INTO chapter_mastery_units(
                    mastery_unit_id, chapter_id, name, module_name, metadata_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(mastery_unit_id) DO UPDATE SET
                    module_name=COALESCE(chapter_mastery_units.module_name, excluded.module_name),
                    metadata_json=excluded.metadata_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    action["mastery_unit_id"], action["chapter_id"], action["name"],
                    action["module_name"], action["metadata_json"],
                ),
            )
            counts["units_upserted"] += 1

        for resolution in resolutions:
            if resolution["status"] == "mapped":
                if resolution["write_action"] != "unchanged":
                    connection.execute(
                        "UPDATE attempts SET mastery_unit_id=?, updated_at=CURRENT_TIMESTAMP WHERE attempt_id=?",
                        (resolution["mastery_unit_id"], resolution["attempt_id"]),
                    )
                    counts["attempts_mapped"] += 1
            elif resolution["write_action"] in {
                "clear_invalid_mastery_unit",
                "clear_unverified_mastery_unit",
            }:
                connection.execute(
                    "UPDATE attempts SET mastery_unit_id=NULL, updated_at=CURRENT_TIMESTAMP WHERE attempt_id=?",
                    (resolution["attempt_id"],),
                )
                counts[
                    "invalid_attempt_units_cleared"
                    if resolution["write_action"] == "clear_invalid_mastery_unit"
                    else "unverified_attempt_units_cleared"
                ] += 1

        for action in link_actions:
            connection.execute(
                """
                INSERT INTO question_mastery_unit_links(
                    question_id, mastery_unit_id, tested_dimension, link_source,
                    semantic_verification_status, evidence_json
                ) VALUES (?, ?, ?, ?, 'verified', ?)
                ON CONFLICT(question_id, mastery_unit_id, tested_dimension) DO UPDATE SET
                    link_source=excluded.link_source,
                    semantic_verification_status='verified',
                    evidence_json=excluded.evidence_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    action["question_id"], action["mastery_unit_id"],
                    action["tested_dimension"], action["link_source"],
                    action["evidence_json"],
                ),
            )
            counts["question_links_verified"] += 1

        for action in occurrence_actions:
            connection.execute(
                """
                INSERT INTO knowledge_exam_occurrences(
                    occurrence_id, chapter_id, mastery_unit_id, question_id,
                    year, question_number, question_part, original_question_type,
                    question_family, exam_points, source_id, source_reference,
                    verification_status, verification_note, verified_date,
                    evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'verified', ?, ?, ?)
                ON CONFLICT(occurrence_id) DO UPDATE SET
                    question_id=excluded.question_id,
                    original_question_type=excluded.original_question_type,
                    question_family=excluded.question_family,
                    exam_points=excluded.exam_points,
                    source_id=excluded.source_id,
                    source_reference=excluded.source_reference,
                    verification_status='verified',
                    verification_note=excluded.verification_note,
                    verified_date=excluded.verified_date,
                    evidence_json=excluded.evidence_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    action["occurrence_id"], action["chapter_id"],
                    action["mastery_unit_id"], action["question_id"], action["year"],
                    action["question_number"], action["question_part"],
                    action["original_question_type"], action["question_family"],
                    action["exam_points"], action["source_id"],
                    action["source_reference"], action["verification_note"],
                    action["verified_date"], action["evidence_json"],
                ),
            )
            counts["exam_occurrences_verified"] += 1

        for action in frequency_audit_actions:
            connection.execute(
                """
                INSERT INTO exam_frequency_audits(
                    mastery_unit_id, status, year_start, year_end, audit_note,
                    blocker_reason, evidence_json, audited_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mastery_unit_id) DO UPDATE SET
                    status=excluded.status,
                    year_start=excluded.year_start,
                    year_end=excluded.year_end,
                    audit_note=excluded.audit_note,
                    blocker_reason=excluded.blocker_reason,
                    evidence_json=excluded.evidence_json,
                    audited_date=excluded.audited_date,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    action["mastery_unit_id"], action["status"], action["year_start"],
                    action["year_end"], action["audit_note"],
                    action["blocker_reason"], action["evidence_json"],
                    action["audited_date"],
                ),
            )
            counts["frequency_audits_upserted"] += 1

        orphan_count = connection.execute(
            """
            SELECT COUNT(*) FROM attempts a
            WHERE a.mastery_unit_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM chapter_mastery_units u
                  WHERE u.mastery_unit_id=a.mastery_unit_id
              )
            """
        ).fetchone()[0]
        if orphan_count:
            raise RuntimeError(f"backfill would leave {orphan_count} orphan mastery unit IDs")
        unverified_non_null = connection.execute(
            """
            WITH unique_verified_mapping AS (
                SELECT question_id, MIN(mastery_unit_id) AS mastery_unit_id
                FROM question_mastery_unit_links
                WHERE semantic_verification_status='verified'
                  AND tested_dimension='primary'
                GROUP BY question_id
                HAVING COUNT(DISTINCT mastery_unit_id)=1
            )
            SELECT COUNT(*)
            FROM attempts a
            LEFT JOIN unique_verified_mapping vm
              ON vm.question_id=a.question_id
             AND vm.mastery_unit_id=a.mastery_unit_id
            WHERE a.mastery_unit_id IS NOT NULL
              AND vm.mastery_unit_id IS NULL
            """
        ).fetchone()[0]
        if unverified_non_null:
            raise RuntimeError(
                "backfill would leave "
                f"{unverified_non_null} non-null attempt mappings without one unique verified link"
            )
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity check failed after backfill: {integrity}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    counts["integrity_check_ok"] = 1
    return dict(counts)


def build_audit(
    connection: sqlite3.Connection,
    database: Path,
    manifest_path: Path | None,
    manifest: dict[str, Any],
    apply: bool,
) -> tuple[dict[str, Any], tuple[Any, ...]]:
    chapter_ids = {
        str(row[0]) for row in connection.execute("SELECT chapter_id FROM chapters")
    }
    aliases, alias_errors = chapter_alias_pairs(
        manifest["chapter_aliases"], chapter_ids
    )
    unit_catalog, unit_actions, unit_errors = prepare_units(
        connection, manifest["units"]
    )
    verified_links: dict[str, set[str]] = defaultdict(set)
    for row in connection.execute(
        """
        SELECT question_id, mastery_unit_id
        FROM question_mastery_unit_links
        WHERE semantic_verification_status='verified'
          AND tested_dimension='primary'
        """
    ):
        verified_links[str(row["question_id"])].add(str(row["mastery_unit_id"]))
    reviewed, mapping_errors = prepare_reviewed_mappings(
        connection,
        manifest["mappings"],
        unit_catalog,
        aliases,
        verified_links,
    )
    resolutions = resolve_attempts(
        connection, unit_catalog, aliases, reviewed, verified_links
    )
    link_actions = prepare_question_links(
        connection, resolutions, reviewed, verified_links
    )
    occurrence_actions, occurrence_errors = prepare_exam_occurrences(
        connection, manifest["exam_occurrences"], unit_catalog
    )
    frequency_actions, frequency_errors = prepare_frequency_audits(
        manifest["frequency_audits"], unit_catalog
    )

    mapped = [row for row in resolutions if row["status"] == "mapped"]
    ambiguous = [row for row in resolutions if row["status"] == "ambiguous"]
    unmapped = [row for row in resolutions if row["status"] == "unmapped"]
    methods = Counter(row["method"] or "none" for row in mapped)
    dashboard_rows = [row for row in resolutions if row["dashboard_eligible"]]
    dashboard_questions = {row["question_id"] for row in dashboard_rows}
    dashboard_rows_by_question: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in dashboard_rows:
        dashboard_rows_by_question[row["question_id"]].append(row)
    dashboard_mapped_questions = {
        question_id
        for question_id, question_rows in dashboard_rows_by_question.items()
        if all(row["status"] == "mapped" and row["mastery_unit_id"] for row in question_rows)
        and len({str(row["mastery_unit_id"]) for row in question_rows}) == 1
    }
    dashboard_mapped = [
        row for row in dashboard_rows if row["question_id"] in dashboard_mapped_questions
    ]
    dashboard_unresolved_questions = sorted(
        dashboard_questions - dashboard_mapped_questions
    )
    result: dict[str, Any] = {
        "status": "planned_apply" if apply else "dry_run",
        "database": str(database),
        "schema_version": REQUIRED_SCHEMA_VERSION,
        "backup_requirement": (
            "Create and verify a pre-migration backup before --apply. This backfill "
            "does not create a backup."
        ),
        "reviewed_mappings_path": str(manifest_path) if manifest_path else None,
        "summary": {
            "attempts": len(resolutions),
            "distinct_questions": len({row["question_id"] for row in resolutions}),
            "mapped_attempts": len(mapped),
            "ambiguous_attempts": len(ambiguous),
            "unmapped_attempts": len(unmapped),
            "dashboard_eligible_attempts": len(dashboard_rows),
            "dashboard_eligible_distinct_questions": len(dashboard_questions),
            "dashboard_eligible_mapped": len(dashboard_mapped),
            "dashboard_eligible_mapped_distinct_questions": len(
                dashboard_mapped_questions
            ),
            "dashboard_eligible_unmapped_distinct_questions": len(
                dashboard_unresolved_questions
            ),
            "dashboard_mapping_coverage": round(
                len(dashboard_mapped) / len(dashboard_rows), 6
            ) if dashboard_rows else None,
            "invalid_attempt_units_to_clear": sum(
                row["write_action"] == "clear_invalid_mastery_unit"
                for row in resolutions
            ),
            "unverified_attempt_units_to_clear": sum(
                row["write_action"] == "clear_unverified_mastery_unit"
                for row in resolutions
            ),
            "attempt_units_to_set": sum(
                row["write_action"] == "set_mastery_unit" for row in resolutions
            ),
            "verified_question_links_to_upsert": len(link_actions),
            "mapping_methods": dict(sorted(methods.items())),
        },
        "manifest": {
            "unit_actions": unit_actions,
            "accepted_reviewed_mappings": list(reviewed.values()),
            "question_link_actions": link_actions,
            "exam_occurrence_actions": occurrence_actions,
            "frequency_audit_actions": frequency_actions,
            "errors": {
                "chapter_aliases": alias_errors,
                "units": unit_errors,
                "mappings": mapping_errors,
                "exam_occurrences": occurrence_errors,
                "frequency_audits": frequency_errors,
            },
        },
        "mapped": mapped,
        "ambiguous": ambiguous,
        "unmapped": unmapped,
        "dashboard_unresolved_question_ids": dashboard_unresolved_questions,
    }
    actions = (
        unit_actions,
        resolutions,
        link_actions,
        occurrence_actions,
        frequency_actions,
    )
    return result, actions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministically audit/backfill attempt mastery units. Apply only after "
            "creating a verified backup and bringing the database to schema v9."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--database",
        type=Path,
        required=True,
        help="Path to a verified backup or private full-schema v9 database (never the demo fixture)",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "Allow --apply with unresolved dashboard-eligible questions. "
            "This is an explicit maintenance escape hatch; the default is fail closed."
        ),
    )
    parser.add_argument(
        "--reviewed-mappings",
        type=Path,
        help=(
            "Optional JSON manifest with units, mappings, explicit chapter_aliases, "
            "exam_occurrences, and frequency_audits."
        ),
    )
    parser.add_argument("--report", type=Path, help="Also write the full JSON audit here")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    database = args.database.expanduser().resolve()
    manifest_path = args.reviewed_mappings.expanduser().resolve() if args.reviewed_mappings else None
    try:
        manifest = load_manifest(manifest_path)
        connection = connect(database, apply=args.apply)
    except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as error:
        parser.error(str(error))
    exit_code = 0
    try:
        result, actions = build_audit(
            connection, database, manifest_path, manifest, apply=args.apply
        )
        if args.apply:
            manifest_error_count = sum(
                len(items) for items in result["manifest"]["errors"].values()
            )
            unresolved_count = result["summary"][
                "dashboard_eligible_unmapped_distinct_questions"
            ]
            blockers: list[str] = []
            if manifest_error_count:
                blockers.append(f"manifest contains {manifest_error_count} validation errors")
            if unresolved_count and not args.allow_partial:
                blockers.append(
                    f"{unresolved_count} dashboard-eligible questions remain unresolved"
                )
            if blockers:
                result["status"] = "apply_rejected"
                result["apply_blockers"] = blockers
                exit_code = 2
            else:
                result["applied"] = apply_actions(connection, *actions)
                result["status"] = "applied"
    finally:
        connection.close()
    result["generated_date"] = date.today().isoformat()
    if args.report:
        report_path = args.report.expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        result["report_path"] = str(report_path)
        report_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
