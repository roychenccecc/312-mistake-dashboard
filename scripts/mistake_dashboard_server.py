#!/usr/bin/env python3
"""Local, read-only analytics server for the 312 mistake dashboard.

The server deliberately uses only the Python standard library.  It opens the
canonical SQLite database with ``mode=ro`` and enables ``query_only`` on every
connection.  All metrics are calculated at request time; nothing is cached or
written back to SQLite.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "demo.sqlite3"
DEFAULT_FRONTEND = PROJECT_ROOT / "integrations" / "mistake-dashboard"
DEFAULT_PORT = 4174
ELIGIBLE_PHASES = ("pre_review", "delayed_retest", "legacy")
REAL_EXAM_SOURCE = "真题"

SUBJECT_ALIASES = {
    "发展": "发展心理学",
    "发展心理学": "发展心理学",
    "实验": "实验心理学",
    "实验心理学": "实验心理学",
}


class APIError(Exception):
    """An expected client-facing API error."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _ratio(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 6)


def _safe_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _json_value(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def canonical_subject(subject: str | None) -> str:
    value = re.sub(r"\s+", "", (subject or "").strip())
    return SUBJECT_ALIASES.get(value, value)


def canonical_chapter_name(subject: str, name: str | None) -> str:
    value = re.sub(r"\s+", "", (name or "").strip())
    value = re.sub(r"^(?:第)?\d{1,2}(?:章)?\s*[-._、:：]?\s*", "", value)
    if canonical_subject(subject) == "实验心理学" and value in {"概述", "实验心理学概述"}:
        return "实验心理学概述"
    return value or "未命名章节"


def canonical_chapter_id(subject: str, chapter_name: str) -> str:
    digest = hashlib.sha256(f"{subject}\0{chapter_name}".encode("utf-8")).hexdigest()[:16]
    return f"canonical:chapter:{digest}"


@dataclass(frozen=True)
class QueryFilters:
    date_from: str | None = None
    date_to: str | None = None
    subject: str | None = None
    chapter_id: str | None = None
    chapter: str | None = None

    @classmethod
    def from_query(cls, query: Mapping[str, Sequence[str]]) -> "QueryFilters":
        def first(name: str) -> str | None:
            values = query.get(name, ())
            value = values[-1].strip() if values else ""
            return value or None

        date_from = first("date_from")
        date_to = first("date_to")
        for label, value in (("date_from", date_from), ("date_to", date_to)):
            if value is not None:
                try:
                    date.fromisoformat(value)
                except ValueError as error:
                    raise APIError(HTTPStatus.BAD_REQUEST, f"{label} 必须是 YYYY-MM-DD") from error
        if date_from and date_to and date_from > date_to:
            raise APIError(HTTPStatus.BAD_REQUEST, "date_from 不能晚于 date_to")
        subject = first("subject")
        return cls(
            date_from=date_from,
            date_to=date_to,
            subject=canonical_subject(subject) if subject else None,
            chapter_id=first("chapter_id"),
            chapter=first("chapter"),
        )

    def as_dict(self) -> dict[str, str | None]:
        return {
            "date_from": self.date_from,
            "date_to": self.date_to,
            "subject": self.subject,
            "chapter_id": self.chapter_id,
            "chapter": self.chapter,
        }


class ChapterCatalog:
    def __init__(self, rows: Iterable[sqlite3.Row]) -> None:
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            subject = canonical_subject(row["subject"])
            name = canonical_chapter_name(subject, row["name"])
            key = (subject, name)
            group = grouped.setdefault(
                key,
                {
                    "chapter_id": canonical_chapter_id(subject, name),
                    "subject": subject,
                    "name": name,
                    "source_chapter_ids": [],
                },
            )
            group["source_chapter_ids"].append(str(row["chapter_id"]))

        self.groups = sorted(
            grouped.values(), key=lambda item: (item["subject"], item["name"])
        )
        for group in self.groups:
            group["source_chapter_ids"].sort()
        self.by_id = {group["chapter_id"]: group for group in self.groups}
        self.source_to_group = {
            source_id: group
            for group in self.groups
            for source_id in group["source_chapter_ids"]
        }

    def resolve(self, chapter_id: str) -> dict[str, Any] | None:
        return self.by_id.get(chapter_id) or self.source_to_group.get(chapter_id)

    def selected_groups(self, filters: QueryFilters) -> list[dict[str, Any]]:
        groups = self.groups
        if filters.subject:
            groups = [group for group in groups if group["subject"] == filters.subject]
        if filters.chapter_id:
            selected = self.resolve(filters.chapter_id)
            if selected is None:
                raise APIError(HTTPStatus.NOT_FOUND, "未找到指定章节")
            groups = [group for group in groups if group["chapter_id"] == selected["chapter_id"]]
        if filters.chapter:
            normalized = {
                canonical_chapter_name(group["subject"], filters.chapter)
                for group in groups
            }
            groups = [group for group in groups if group["name"] in normalized]
        return groups


class DashboardRepository:
    """Read-only query and metric layer used by both HTTP handlers and tests."""

    def __init__(self, database: Path | str) -> None:
        self.database = Path(database).expanduser().resolve()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if not self.database.is_file():
            raise sqlite3.OperationalError(f"database does not exist: {self.database}")
        uri = f"{self.database.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None

    @staticmethod
    def _catalog(connection: sqlite3.Connection) -> ChapterCatalog:
        rows = connection.execute(
            "SELECT chapter_id, subject, name FROM chapters ORDER BY subject, name"
        ).fetchall()
        return ChapterCatalog(rows)

    @staticmethod
    def _load_attempt_rows(
        connection: sqlite3.Connection, filters: QueryFilters
    ) -> list[dict[str, Any]]:
        clauses = [
            "a.independent_answer = 1",
            "a.used_hint = 0",
            "a.attempt_phase IN ('pre_review', 'delayed_retest', 'legacy')",
        ]
        parameters: list[Any] = []
        if filters.date_from:
            clauses.append("a.attempt_date >= ?")
            parameters.append(filters.date_from)
        if filters.date_to:
            clauses.append("a.attempt_date <= ?")
            parameters.append(filters.date_to)
        rows = connection.execute(
            f"""
            SELECT
                a.attempt_id, a.question_id, a.session_id, a.attempt_date,
                a.result, a.score, a.max_score, a.confidence, a.error_type,
                a.error_notes, a.is_delayed_retest, a.needs_retest,
                a.attempt_phase, a.mastery_unit_id, a.score_weight,
                q.source_type, q.subject AS question_subject, q.year,
                q.question_type, q.prompt, q.answer, q.explanation,
                s.chapter_id AS session_chapter_id,
                mu.mastery_unit_id AS valid_mastery_unit_id,
                mu.chapter_id AS mastery_chapter_id
            FROM attempts a
            JOIN questions q ON q.question_id = a.question_id
            LEFT JOIN review_sessions s ON s.session_id = a.session_id
            LEFT JOIN (
                SELECT question_id, MIN(mastery_unit_id) AS mastery_unit_id
                FROM question_mastery_unit_links
                WHERE semantic_verification_status = 'verified'
                  AND tested_dimension = 'primary'
                GROUP BY question_id
                HAVING COUNT(DISTINCT mastery_unit_id) = 1
            ) verified_mapping
              ON verified_mapping.question_id = a.question_id
             AND verified_mapping.mastery_unit_id = a.mastery_unit_id
            LEFT JOIN chapter_mastery_units mu
              ON mu.mastery_unit_id = a.mastery_unit_id
             AND verified_mapping.mastery_unit_id = a.mastery_unit_id
            WHERE {' AND '.join(clauses)}
            ORDER BY a.attempt_date, a.attempt_id
            """,
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _row_group(
        row: Mapping[str, Any], catalog: ChapterCatalog
    ) -> dict[str, Any] | None:
        chapter_id = row.get("mastery_chapter_id") or row.get("session_chapter_id")
        if chapter_id:
            return catalog.source_to_group.get(str(chapter_id))
        return None

    def _filtered_rows(
        self,
        rows: Iterable[dict[str, Any]],
        catalog: ChapterCatalog,
        filters: QueryFilters,
    ) -> list[dict[str, Any]]:
        selected_groups = catalog.selected_groups(filters)
        selected_ids = {group["chapter_id"] for group in selected_groups}
        output: list[dict[str, Any]] = []
        for row in rows:
            group = self._row_group(row, catalog)
            if filters.subject or filters.chapter_id or filters.chapter:
                if group is None or group["chapter_id"] not in selected_ids:
                    continue
            row["canonical_chapter_id"] = group["chapter_id"] if group else None
            output.append(row)
        return output

    @staticmethod
    def _valid_score(row: Mapping[str, Any]) -> tuple[float, float, float] | None:
        score = _safe_float(row.get("score"))
        max_score = _safe_float(row.get("max_score"))
        weight = _safe_float(row.get("score_weight"))
        if (
            score is None
            or max_score is None
            or weight is None
            or max_score <= 0
            or weight <= 0
            or score < 0
            or score > max_score
        ):
            return None
        return score, max_score, weight

    @classmethod
    def metrics(cls, rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        row_list = list(rows)
        scored: list[tuple[Mapping[str, Any], float, float, float]] = []
        for row in row_list:
            valid = cls._valid_score(row)
            if valid:
                scored.append((row, *valid))
        total_weight = sum(item[3] for item in scored)
        weighted_success = sum(
            weight * score / max_score for _, score, max_score, weight in scored
        )
        success_rate = _ratio(weighted_success, total_weight)
        error_rate = None if success_rate is None else round(max(0.0, min(1.0, 1 - success_rate)), 6)
        return {
            "error_rate": error_rate,
            "success_rate": success_rate,
            "eligible_attempts": len(row_list),
            "scored_attempts": len(scored),
            "question_count": len({str(row["question_id"]) for row in row_list}),
            "scored_question_count": len(
                {str(row["question_id"]) for row, *_ in scored}
            ),
            "total_weight": round(total_weight, 6),
            "weighted_success": round(weighted_success, 6),
        }

    @staticmethod
    def _freshness(
        filtered_rows: Sequence[Mapping[str, Any]],
        all_rows: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        dates = [str(row["attempt_date"]) for row in filtered_rows if row.get("attempt_date")]
        all_dates = [str(row["attempt_date"]) for row in all_rows if row.get("attempt_date")]
        return {
            "start_date": min(dates) if dates else None,
            "end_date": max(dates) if dates else None,
            "data_cutoff_date": max(all_dates) if all_dates else None,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    @staticmethod
    def _audit_columns(connection: sqlite3.Connection) -> set[str]:
        return {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(exam_frequency_audits)")
        }

    def _load_audits(
        self, connection: sqlite3.Connection, unit_ids: Iterable[str]
    ) -> dict[str, dict[str, Any]]:
        identifiers = sorted(set(unit_ids))
        if not identifiers or not self._table_exists(connection, "exam_frequency_audits"):
            return {}
        columns = self._audit_columns(connection)
        if not {"mastery_unit_id", "status"}.issubset(columns):
            return {}
        desired = [
            "mastery_unit_id",
            "status",
            "year_start",
            "year_end",
            "audit_note",
            "blocker_reason",
            "evidence_json",
            "audited_date",
            "updated_at",
        ]
        projections = [
            column if column in columns else f"NULL AS {column}" for column in desired
        ]
        placeholders = ",".join("?" for _ in identifiers)
        rows = connection.execute(
            f"SELECT {', '.join(projections)} FROM exam_frequency_audits "
            f"WHERE mastery_unit_id IN ({placeholders})",
            identifiers,
        ).fetchall()
        output: dict[str, dict[str, Any]] = {}
        for row in rows:
            status = str(row["status"])
            if status not in {"partial", "complete", "blocked"}:
                status = "unknown"
            output[str(row["mastery_unit_id"])] = {
                "status": status,
                "year_start": row["year_start"],
                "year_end": row["year_end"],
                "audit_note": row["audit_note"],
                "blocker_reason": row["blocker_reason"],
                "evidence": _json_value(row["evidence_json"], []),
                "audited_date": row["audited_date"],
                "updated_at": row["updated_at"],
            }
        return output

    @staticmethod
    def _unknown_audit() -> dict[str, Any]:
        return {
            "status": "unknown",
            "year_start": None,
            "year_end": None,
            "audit_note": None,
            "blocker_reason": None,
            "evidence": [],
            "audited_date": None,
            "updated_at": None,
        }

    def _coverage(
        self,
        connection: sqlite3.Connection,
        rows: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        metrics = self.metrics(rows)
        mapped_rows = [row for row in rows if row.get("valid_mastery_unit_id")]
        eligible_questions = {str(row["question_id"]) for row in rows}
        rows_by_question: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            rows_by_question.setdefault(str(row["question_id"]), []).append(row)
        mapped_questions = {
            question_id
            for question_id, question_rows in rows_by_question.items()
            if all(row.get("valid_mastery_unit_id") for row in question_rows)
            and len({str(row["valid_mastery_unit_id"]) for row in question_rows}) == 1
        }
        conflicting_questions = {
            question_id
            for question_id, question_rows in rows_by_question.items()
            if len({
                str(row["valid_mastery_unit_id"])
                for row in question_rows
                if row.get("valid_mastery_unit_id")
            }) > 1
        }
        attempted_units = {
            str(row["valid_mastery_unit_id"]) for row in mapped_rows
        }
        audits = self._load_audits(connection, attempted_units)
        completed_units = {
            unit_id
            for unit_id, audit in audits.items()
            if audit["status"] == "complete"
            and audit["year_start"] == 2007
            and audit["year_end"] == 2026
        }
        known_units = {
            unit_id for unit_id, audit in audits.items() if audit["status"] != "unknown"
        }
        return {
            "eligible_attempts": metrics["eligible_attempts"],
            "scored_attempts": metrics["scored_attempts"],
            "scoring_rate": _ratio(metrics["scored_attempts"], metrics["eligible_attempts"]),
            "mapped_attempts": len(mapped_rows),
            "unmapped_attempts": len(rows) - len(mapped_rows),
            "mapping_rate": _ratio(len(mapped_rows), len(rows)),
            "eligible_questions": len(eligible_questions),
            "mapped_questions": len(mapped_questions),
            "question_mapping_rate": _ratio(len(mapped_questions), len(eligible_questions)),
            "conflicting_questions": len(conflicting_questions),
            "attempted_units": len(attempted_units),
            "audited_units": len(completed_units),
            "audit_known_units": len(known_units),
            "exam_audit_rate": _ratio(len(completed_units), len(attempted_units)),
            "exam_audit_known_rate": _ratio(len(known_units), len(attempted_units)),
        }

    def summary(self, query: Mapping[str, Sequence[str]]) -> dict[str, Any]:
        filters = QueryFilters.from_query(query)
        with self.connect() as connection:
            catalog = self._catalog(connection)
            all_rows = self._load_attempt_rows(connection, QueryFilters())
            dated_rows = self._load_attempt_rows(connection, filters)
            rows = self._filtered_rows(dated_rows, catalog, filters)
            real_rows = [row for row in rows if row["source_type"] == REAL_EXAM_SOURCE]
            selected_groups = catalog.selected_groups(filters)
            return {
                "filters": filters.as_dict(),
                "overall": self.metrics(rows),
                "real_exam": self.metrics(real_rows),
                "coverage": self._coverage(connection, rows),
                "freshness": self._freshness(rows, all_rows),
                "available_subjects": sorted({group["subject"] for group in catalog.groups}),
                "selected_chapters": [group["chapter_id"] for group in selected_groups],
            }

    def chapters(self, query: Mapping[str, Sequence[str]]) -> dict[str, Any]:
        filters = QueryFilters.from_query(query)
        with self.connect() as connection:
            catalog = self._catalog(connection)
            all_rows = self._load_attempt_rows(connection, QueryFilters())
            dated_rows = self._load_attempt_rows(connection, filters)
            rows = self._filtered_rows(dated_rows, catalog, filters)
            chapters: list[dict[str, Any]] = []
            for group in catalog.selected_groups(filters):
                group_rows = [
                    row for row in rows if row.get("canonical_chapter_id") == group["chapter_id"]
                ]
                real_rows = [
                    row for row in group_rows if row["source_type"] == REAL_EXAM_SOURCE
                ]
                mapped = [row for row in group_rows if row.get("valid_mastery_unit_id")]
                chapter = {
                    **group,
                    "metrics": self.metrics(group_rows),
                    "real_exam": self.metrics(real_rows),
                    "mapping": {
                        "eligible_attempts": len(group_rows),
                        "mapped_attempts": len(mapped),
                        "mapping_rate": _ratio(len(mapped), len(group_rows)),
                    },
                }
                chapters.append(chapter)
            chapters.sort(
                key=lambda item: (
                    item["metrics"]["error_rate"] is None,
                    -(item["metrics"]["error_rate"] or 0),
                    -item["metrics"]["scored_attempts"],
                    item["subject"],
                    item["name"],
                )
            )
            return {
                "filters": filters.as_dict(),
                "chapters": chapters,
                "freshness": self._freshness(rows, all_rows),
            }

    def _frequency_for_unit(
        self,
        occurrences: Sequence[Mapping[str, Any]],
        audit: Mapping[str, Any],
    ) -> dict[str, Any]:
        years = sorted({int(row["year"]) for row in occurrences})
        recent = [row for row in occurrences if 2024 <= int(row["year"]) <= 2026]
        recent_years = sorted({int(row["year"]) for row in recent})
        by_year = [
            {
                "year": year,
                "occurrence_count": sum(1 for row in occurrences if int(row["year"]) == year),
            }
            for year in range(2024, 2027)
        ]
        formal = (
            audit.get("status") == "complete"
            and audit.get("year_start") == 2007
            and audit.get("year_end") == 2026
        )
        detail = [
            {
                "occurrence_id": row["occurrence_id"],
                "question_id": row["question_id"],
                "year": row["year"],
                "question_number": row["question_number"],
                "question_part": row["question_part"],
                "original_question_type": row["original_question_type"],
                "question_family": row["question_family"],
                "exam_points": row["exam_points"],
                "source_reference": row["source_reference"],
                "verified_date": row["verified_date"],
            }
            for row in occurrences
        ]
        return {
            "status": audit.get("status", "unknown"),
            "formal": formal,
            "occurrence_count": len(occurrences) if formal else None,
            "distinct_year_count": len(years) if formal else None,
            "recent_occurrence_count": len(recent) if formal else None,
            "recent_year_count": len(recent_years) if formal else None,
            "verified_occurrence_lower_bound": len(occurrences),
            "verified_distinct_year_lower_bound": len(years),
            "years": years,
            "recent_years": recent_years,
            # A zero is evidence only after the full 2007–2026 audit is complete.
            # For partial/blocked/unknown audits, expose verified lower bounds only;
            # omitted years remain unknown rather than silently becoming zero.
            "recent_by_year": (
                by_year
                if formal
                else [entry for entry in by_year if entry["occurrence_count"] > 0]
            ),
            "question_types": sorted(
                {str(row["original_question_type"]) for row in occurrences if row["original_question_type"]}
            ),
            "points": sorted(
                {_safe_float(row["exam_points"]) for row in occurrences if _safe_float(row["exam_points"]) is not None}
            ),
            "occurrences": detail,
            "audit": dict(audit),
        }

    def knowledge(self, query: Mapping[str, Sequence[str]]) -> dict[str, Any]:
        filters = QueryFilters.from_query(query)
        if not filters.chapter_id:
            raise APIError(HTTPStatus.BAD_REQUEST, "chapter_id 为必填项")
        with self.connect() as connection:
            catalog = self._catalog(connection)
            group = catalog.resolve(filters.chapter_id)
            if group is None:
                raise APIError(HTTPStatus.NOT_FOUND, "未找到指定章节")
            if filters.subject and group["subject"] != filters.subject:
                raise APIError(HTTPStatus.BAD_REQUEST, "subject 与 chapter_id 不一致")
            source_ids = group["source_chapter_ids"]
            placeholders = ",".join("?" for _ in source_ids)
            units = connection.execute(
                f"""
                SELECT mastery_unit_id, chapter_id, name, module_name,
                       course_importance_stars, course_importance_label,
                       course_importance_source, coverage_status
                FROM chapter_mastery_units
                WHERE chapter_id IN ({placeholders})
                ORDER BY module_name, name, mastery_unit_id
                """,
                source_ids,
            ).fetchall()
            unit_ids = [str(unit["mastery_unit_id"]) for unit in units]

            all_rows = self._load_attempt_rows(connection, QueryFilters())
            dated_rows = self._load_attempt_rows(connection, filters)
            chapter_filters = QueryFilters(
                date_from=filters.date_from,
                date_to=filters.date_to,
                subject=group["subject"],
                chapter_id=group["chapter_id"],
            )
            rows = self._filtered_rows(dated_rows, catalog, chapter_filters)

            occurrence_rows: list[sqlite3.Row] = []
            if unit_ids:
                unit_placeholders = ",".join("?" for _ in unit_ids)
                occurrence_rows = connection.execute(
                    f"""
                    SELECT occurrence_id, mastery_unit_id, question_id, year,
                           question_number, question_part, original_question_type,
                           question_family, exam_points, source_reference, verified_date
                    FROM knowledge_exam_occurrences
                    WHERE mastery_unit_id IN ({unit_placeholders})
                      AND verification_status = 'verified'
                    ORDER BY year, question_number, question_part
                    """,
                    unit_ids,
                ).fetchall()
            occurrences_by_unit: dict[str, list[sqlite3.Row]] = {
                unit_id: [] for unit_id in unit_ids
            }
            for occurrence in occurrence_rows:
                occurrences_by_unit[str(occurrence["mastery_unit_id"])].append(occurrence)
            audits = self._load_audits(connection, unit_ids)

            knowledge: list[dict[str, Any]] = []
            for unit in units:
                unit_id = str(unit["mastery_unit_id"])
                unit_rows = [
                    row for row in rows if row.get("valid_mastery_unit_id") == unit_id
                ]
                real_rows = [
                    row for row in unit_rows if row["source_type"] == REAL_EXAM_SOURCE
                ]
                audit = audits.get(unit_id, self._unknown_audit())
                knowledge.append(
                    {
                        "mastery_unit_id": unit_id,
                        "source_chapter_id": unit["chapter_id"],
                        "name": unit["name"],
                        "module_name": unit["module_name"],
                        "course_importance_stars": unit["course_importance_stars"],
                        "course_importance_label": unit["course_importance_label"],
                        "course_importance_source": unit["course_importance_source"],
                        "coverage_status": unit["coverage_status"],
                        "metrics": self.metrics(unit_rows),
                        "real_exam": self.metrics(real_rows),
                        "exam_frequency": self._frequency_for_unit(
                            occurrences_by_unit[unit_id], audit
                        ),
                    }
                )
            knowledge.sort(
                key=lambda item: (
                    item["metrics"]["error_rate"] is None,
                    -(item["metrics"]["error_rate"] or 0),
                    -item["metrics"]["scored_attempts"],
                    item["module_name"] or "",
                    item["name"],
                )
            )
            return {
                "filters": {**filters.as_dict(), "chapter_id": group["chapter_id"]},
                "chapter": group,
                "knowledge": knowledge,
                "unknown_frequency": [
                    item["mastery_unit_id"]
                    for item in knowledge
                    if not item["exam_frequency"]["formal"]
                ],
                "coverage": self._coverage(connection, rows),
                "freshness": self._freshness(rows, all_rows),
            }

    @staticmethod
    def _source_rank(source_type: str | None) -> int:
        value = source_type or ""
        if value == "真题":
            return 0
        if "教材" in value or "课本" in value:
            return 1
        if "辅导书" in value:
            return 2
        if "真题" in value and any(label in value for label in ("改写", "汇编", "变式")):
            return 3
        if "AI" in value.upper() or "自编" in value:
            return 5
        if "改写" in value or "变式" in value:
            return 4
        return 6

    @classmethod
    def _is_failure(cls, row: Mapping[str, Any]) -> bool:
        if row.get("result") != "correct":
            return True
        valid = cls._valid_score(row)
        return bool(valid and valid[0] < valid[1])

    @staticmethod
    def _attempt_public(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "attempt_id": row["attempt_id"],
            "session_id": row["session_id"],
            "attempt_date": row["attempt_date"],
            "attempt_phase": row["attempt_phase"],
            "result": row["result"],
            "score": row["score"],
            "max_score": row["max_score"],
            "score_weight": row["score_weight"],
            "confidence": row["confidence"],
            "error_type": row["error_type"],
            "error_notes": row["error_notes"],
            "is_delayed_retest": bool(row["is_delayed_retest"]),
            "needs_retest": bool(row["needs_retest"]),
        }

    def questions(self, query: Mapping[str, Sequence[str]]) -> dict[str, Any]:
        filters = QueryFilters.from_query(query)
        values = query.get("mastery_unit_id", ())
        unit_id = values[-1].strip() if values else ""
        if not unit_id:
            raise APIError(HTTPStatus.BAD_REQUEST, "mastery_unit_id 为必填项")
        with self.connect() as connection:
            unit = connection.execute(
                """
                SELECT u.mastery_unit_id, u.chapter_id, u.name, u.module_name,
                       c.subject, c.name AS chapter_name
                FROM chapter_mastery_units u
                JOIN chapters c ON c.chapter_id = u.chapter_id
                WHERE u.mastery_unit_id = ?
                """,
                (unit_id,),
            ).fetchone()
            if unit is None:
                raise APIError(HTTPStatus.NOT_FOUND, "未找到指定知识点")
            catalog = self._catalog(connection)
            group = catalog.source_to_group[str(unit["chapter_id"])]
            if filters.subject and filters.subject != group["subject"]:
                raise APIError(HTTPStatus.BAD_REQUEST, "subject 与 mastery_unit_id 不一致")
            if filters.chapter_id:
                requested_group = catalog.resolve(filters.chapter_id)
                if requested_group is None or requested_group["chapter_id"] != group["chapter_id"]:
                    raise APIError(HTTPStatus.BAD_REQUEST, "chapter_id 与 mastery_unit_id 不一致")

            all_rows = self._load_attempt_rows(connection, QueryFilters())
            dated_rows = self._load_attempt_rows(connection, filters)
            rows = [
                row
                for row in dated_rows
                if row.get("valid_mastery_unit_id") == unit_id
            ]
            by_question: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                by_question.setdefault(str(row["question_id"]), []).append(row)

            questions: list[dict[str, Any]] = []
            for question_id, question_rows in by_question.items():
                first = question_rows[0]
                public_attempts = [self._attempt_public(row) for row in question_rows]
                failures = [
                    self._attempt_public(row)
                    for row in question_rows
                    if self._is_failure(row)
                ]
                questions.append(
                    {
                        "question_id": question_id,
                        "source_type": first["source_type"],
                        "year": first["year"],
                        "question_type": first["question_type"],
                        "prompt": first["prompt"],
                        "answer": first["answer"],
                        "explanation": first["explanation"],
                        "source_rank": self._source_rank(first["source_type"]),
                        "metrics": self.metrics(question_rows),
                        "attempts": public_attempts,
                        "failures": failures,
                    }
                )
            questions.sort(
                key=lambda item: (
                    item["source_rank"],
                    -(item["metrics"]["error_rate"] or 0),
                    item["question_id"],
                )
            )
            return {
                "filters": filters.as_dict(),
                "mastery_unit": {
                    "mastery_unit_id": unit["mastery_unit_id"],
                    "name": unit["name"],
                    "module_name": unit["module_name"],
                    "chapter": group,
                },
                "questions": questions,
                "metrics": self.metrics(rows),
                "freshness": self._freshness(rows, all_rows),
            }


class DashboardRequestHandler(SimpleHTTPRequestHandler):
    repository: DashboardRepository

    def __init__(self, *args: Any, repository: DashboardRepository, directory: str, **kwargs: Any) -> None:
        self.repository = repository
        super().__init__(*args, directory=directory, **kwargs)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'self'; form-action 'none'; frame-ancestors 'none'",
        )
        super().end_headers()

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api/"):
            query = parse_qs(parsed.query, keep_blank_values=True)
            try:
                if parsed.path == "/api/summary":
                    payload = self.repository.summary(query)
                elif parsed.path == "/api/chapters":
                    payload = self.repository.chapters(query)
                elif parsed.path == "/api/knowledge":
                    payload = self.repository.knowledge(query)
                elif parsed.path == "/api/questions":
                    payload = self.repository.questions(query)
                else:
                    raise APIError(HTTPStatus.NOT_FOUND, "未找到接口")
                self._send_json(HTTPStatus.OK, payload)
            except APIError as error:
                self._send_json(error.status, {"error": error.message})
            except sqlite3.Error:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "数据库只读查询失败，请检查本地数据库状态"},
                )
            return
        super().do_GET()

    def _method_not_allowed(self) -> None:
        body = json.dumps({"error": "只允许 GET 请求"}, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_POST(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def list_directory(self, path: str) -> None:
        self.send_error(HTTPStatus.NOT_FOUND)
        return None


def create_server(
    database: Path | str = DEFAULT_DATABASE,
    frontend: Path | str = DEFAULT_FRONTEND,
    port: int = DEFAULT_PORT,
    *,
    bind_and_activate: bool = True,
) -> ThreadingHTTPServer:
    repository = DashboardRepository(database)
    # Probe the exact read-only connection and schema before announcing a URL.
    with repository.connect() as connection:
        connection.execute("SELECT 1 FROM attempts LIMIT 1").fetchone()
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise RuntimeError("SQLite query_only guard was not enabled")
    frontend_path = Path(frontend).expanduser().resolve()
    if not frontend_path.is_dir():
        raise FileNotFoundError(f"dashboard frontend does not exist: {frontend_path}")
    handler = partial(
        DashboardRequestHandler,
        repository=repository,
        directory=str(frontend_path),
    )
    return ThreadingHTTPServer(
        ("127.0.0.1", port), handler, bind_and_activate=bind_and_activate
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="312 错题分析看板（本机只读）")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--frontend-dir", type=Path, default=DEFAULT_FRONTEND)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 0 <= args.port <= 65535:
        raise SystemExit("--port 必须在 0 到 65535 之间")
    server = create_server(args.database, args.frontend_dir, args.port)
    host, port = server.server_address[:2]
    print(f"312 错题分析看板：http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
