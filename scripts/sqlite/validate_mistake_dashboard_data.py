#!/usr/bin/env python3
"""Validate the source coverage required by the read-only mistake dashboard."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "demo.sqlite3"
ELIGIBLE_PREDICATE = """
    a.independent_answer = 1
    AND a.used_hint = 0
    AND a.attempt_phase IN ('pre_review', 'delayed_retest', 'legacy')
"""
VALID_SCORE_PREDICATE = """
    a.score IS NOT NULL
    AND a.max_score IS NOT NULL
    AND a.score_weight IS NOT NULL
    AND a.max_score > 0
    AND a.score_weight > 0
    AND a.score >= 0
    AND a.score <= a.max_score
"""
UNIQUE_VERIFIED_MAPPING_CTE = """
    WITH unique_verified_mapping AS (
        SELECT question_id, MIN(mastery_unit_id) AS mastery_unit_id
        FROM question_mastery_unit_links
        WHERE semantic_verification_status = 'verified'
          AND tested_dimension = 'primary'
        GROUP BY question_id
        HAVING COUNT(DISTINCT mastery_unit_id) = 1
    )
"""


def connect_read_only(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{database.expanduser().resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 3000")
    return connection


def scalar(connection: sqlite3.Connection, query: str) -> int:
    return int(connection.execute(query).fetchone()[0] or 0)


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table})")
    }


def display_path(database: Path) -> str:
    resolved = database.expanduser().resolve()
    try:
        return str(resolved.relative_to(PROJECT_ROOT))
    except ValueError:
        return f"<external>/{resolved.name}"


def validate(database: Path) -> dict[str, object]:
    connection = connect_read_only(database)
    try:
        eligible_attempts = scalar(
            connection, f"SELECT COUNT(*) FROM attempts a WHERE {ELIGIBLE_PREDICATE}"
        )
        eligible_questions = scalar(
            connection,
            f"SELECT COUNT(DISTINCT a.question_id) FROM attempts a WHERE {ELIGIBLE_PREDICATE}",
        )
        scored_attempts = scalar(
            connection,
            f"""
            SELECT COUNT(*) FROM attempts a
            WHERE {ELIGIBLE_PREDICATE}
              AND {VALID_SCORE_PREDICATE}
            """,
        )
        mapped_questions = scalar(
            connection,
            f"""
            {UNIQUE_VERIFIED_MAPPING_CTE}
            SELECT COUNT(*)
            FROM (
                SELECT a.question_id
                FROM attempts a
                LEFT JOIN unique_verified_mapping vm
                  ON vm.question_id = a.question_id
                 AND vm.mastery_unit_id = a.mastery_unit_id
                LEFT JOIN chapter_mastery_units u
                  ON u.mastery_unit_id = a.mastery_unit_id
                 AND u.mastery_unit_id = vm.mastery_unit_id
                WHERE {ELIGIBLE_PREDICATE}
                GROUP BY a.question_id
                HAVING COUNT(*) = COUNT(u.mastery_unit_id)
                   AND COUNT(DISTINCT u.mastery_unit_id) = 1
            ) fully_mapped
            """,
        )
        unresolved_eligible_attempts = scalar(
            connection,
            f"""
            {UNIQUE_VERIFIED_MAPPING_CTE}
            SELECT COUNT(*)
            FROM attempts a
            LEFT JOIN unique_verified_mapping vm
              ON vm.question_id = a.question_id
             AND vm.mastery_unit_id = a.mastery_unit_id
            LEFT JOIN chapter_mastery_units u
              ON u.mastery_unit_id = a.mastery_unit_id
             AND u.mastery_unit_id = vm.mastery_unit_id
            WHERE {ELIGIBLE_PREDICATE}
              AND u.mastery_unit_id IS NULL
            """,
        )
        conflicting_verified_questions = scalar(
            connection,
            f"""
            SELECT COUNT(*)
            FROM (
                SELECT l.question_id
                FROM question_mastery_unit_links l
                WHERE l.semantic_verification_status = 'verified'
                  AND l.tested_dimension = 'primary'
                  AND EXISTS (
                      SELECT 1 FROM attempts a
                      WHERE a.question_id = l.question_id
                        AND {ELIGIBLE_PREDICATE}
                  )
                GROUP BY l.question_id
                HAVING COUNT(DISTINCT l.mastery_unit_id) > 1
            ) conflicting
            """,
        )
        invalid_attempt_units = scalar(
            connection,
            """
            SELECT COUNT(*)
            FROM attempts a
            LEFT JOIN chapter_mastery_units u ON u.mastery_unit_id = a.mastery_unit_id
            WHERE a.mastery_unit_id IS NOT NULL AND u.mastery_unit_id IS NULL
            """,
        )
        verified_occurrences = scalar(
            connection,
            "SELECT COUNT(*) FROM knowledge_exam_occurrences WHERE verification_status='verified'",
        )
        candidate_occurrences = scalar(
            connection,
            "SELECT COUNT(*) FROM knowledge_exam_occurrences WHERE verification_status='candidate'",
        )
        occurrence_columns = table_columns(connection, "knowledge_exam_occurrences")
        missing_verified_occurrence_provenance = scalar(
            connection,
            """
            SELECT COUNT(*)
            FROM knowledge_exam_occurrences
            WHERE verification_status='verified'
              AND TRIM(COALESCE(source_id, '')) = ''
              AND TRIM(COALESCE(source_reference, '')) = ''
            """,
        )
        if "evidence_json" in occurrence_columns:
            missing_verified_occurrence_evidence = scalar(
                connection,
                """
                SELECT COUNT(*)
                FROM knowledge_exam_occurrences
                WHERE verification_status='verified'
                  AND LOWER(TRIM(COALESCE(evidence_json, ''))) IN ('', '[]', '{}', 'null')
                """,
            )
        else:
            missing_verified_occurrence_evidence = verified_occurrences
        latest_attempt = connection.execute(
            f"SELECT MAX(attempt_date) FROM attempts a WHERE {ELIGIBLE_PREDICATE}"
        ).fetchone()[0]
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        schema_version_row = connection.execute(
            "SELECT value FROM system_meta WHERE key='schema_version'"
        ).fetchone()
        schema_version = int(schema_version_row[0]) if schema_version_row else 0

        attempted_units = scalar(
            connection,
            f"""
            {UNIQUE_VERIFIED_MAPPING_CTE}
            SELECT COUNT(DISTINCT a.mastery_unit_id)
            FROM attempts a
            JOIN unique_verified_mapping vm
              ON vm.question_id = a.question_id
             AND vm.mastery_unit_id = a.mastery_unit_id
            JOIN chapter_mastery_units u ON u.mastery_unit_id = vm.mastery_unit_id
            WHERE {ELIGIBLE_PREDICATE}
            """,
        )
        failed_units = scalar(
            connection,
            f"""
            {UNIQUE_VERIFIED_MAPPING_CTE}
            SELECT COUNT(DISTINCT a.mastery_unit_id)
            FROM attempts a
            JOIN unique_verified_mapping vm
              ON vm.question_id = a.question_id
             AND vm.mastery_unit_id = a.mastery_unit_id
            JOIN chapter_mastery_units u ON u.mastery_unit_id = vm.mastery_unit_id
            WHERE {ELIGIBLE_PREDICATE}
              AND {VALID_SCORE_PREDICATE}
              AND a.score < a.max_score
            """,
        )
        audit_counts: dict[str, int] = {}
        audited_attempted_units = 0
        audited_failed_units = 0
        if table_exists(connection, "exam_frequency_audits"):
            audit_counts = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM exam_frequency_audits GROUP BY status"
                )
            }
            audited_attempted_units = scalar(
                connection,
                f"""
                {UNIQUE_VERIFIED_MAPPING_CTE}
                SELECT COUNT(DISTINCT a.mastery_unit_id)
                FROM attempts a
                JOIN unique_verified_mapping vm
                  ON vm.question_id = a.question_id
                 AND vm.mastery_unit_id = a.mastery_unit_id
                JOIN chapter_mastery_units u ON u.mastery_unit_id = vm.mastery_unit_id
                JOIN exam_frequency_audits efa
                  ON efa.mastery_unit_id = a.mastery_unit_id
                 AND efa.status IN ('complete', 'blocked')
                WHERE {ELIGIBLE_PREDICATE}
                """,
            )
            audited_failed_units = scalar(
                connection,
                f"""
                {UNIQUE_VERIFIED_MAPPING_CTE}
                SELECT COUNT(DISTINCT a.mastery_unit_id)
                FROM attempts a
                JOIN unique_verified_mapping vm
                  ON vm.question_id = a.question_id
                 AND vm.mastery_unit_id = a.mastery_unit_id
                JOIN chapter_mastery_units u ON u.mastery_unit_id = vm.mastery_unit_id
                JOIN exam_frequency_audits efa
                  ON efa.mastery_unit_id = a.mastery_unit_id
                 AND efa.status IN ('complete', 'blocked')
                WHERE {ELIGIBLE_PREDICATE}
                  AND {VALID_SCORE_PREDICATE}
                  AND a.score < a.max_score
                """,
            )

        question_mapping_rate = (
            mapped_questions / eligible_questions if eligible_questions else None
        )
        score_coverage_rate = (
            scored_attempts / eligible_attempts if eligible_attempts else None
        )
        exam_audit_coverage_rate = (
            audited_attempted_units / attempted_units if attempted_units else None
        )
        issues: list[dict[str, object]] = []
        if mapped_questions != eligible_questions:
            issues.append(
                {
                    "severity": "high",
                    "code": "knowledge_mapping_incomplete",
                    "message": f"{eligible_questions - mapped_questions} eligible questions lack a valid mastery-unit mapping",
                }
            )
        if invalid_attempt_units:
            issues.append(
                {
                    "severity": "high",
                    "code": "invalid_mastery_unit_ids",
                    "message": f"{invalid_attempt_units} attempts contain a non-null mastery-unit ID that does not exist",
                }
            )
        if unresolved_eligible_attempts:
            issues.append(
                {
                    "severity": "high",
                    "code": "eligible_attempt_mapping_unverified",
                    "message": (
                        f"{unresolved_eligible_attempts} eligible attempts lack one matching "
                        "unique verified question-to-unit mapping"
                    ),
                }
            )
        if conflicting_verified_questions:
            issues.append(
                {
                    "severity": "high",
                    "code": "conflicting_verified_question_mappings",
                    "message": (
                        f"{conflicting_verified_questions} eligible questions point to "
                        "multiple verified mastery units"
                    ),
                }
            )
        if missing_verified_occurrence_provenance:
            issues.append(
                {
                    "severity": "high",
                    "code": "verified_exam_occurrence_provenance_missing",
                    "message": (
                        f"{missing_verified_occurrence_provenance} verified exam occurrences "
                        "lack source_id and source_reference"
                    ),
                }
            )
        if missing_verified_occurrence_evidence:
            issues.append(
                {
                    "severity": "high",
                    "code": "verified_exam_occurrence_evidence_missing",
                    "message": (
                        f"{missing_verified_occurrence_evidence} verified exam occurrences "
                        "lack non-empty semantic-verification evidence"
                    ),
                }
            )
        if schema_version < 9 or user_version < 9:
            issues.append(
                {
                    "severity": "high",
                    "code": "schema_version_incomplete",
                    "message": (
                        f"schema v9 is required (system_meta={schema_version}, "
                        f"PRAGMA user_version={user_version})"
                    ),
                }
            )
        if scored_attempts != eligible_attempts:
            issues.append(
                {
                    "severity": "medium",
                    "code": "score_coverage_incomplete",
                    "message": f"{eligible_attempts - scored_attempts} eligible attempts are excluded from error-rate calculations",
                }
            )
        if audited_attempted_units != attempted_units:
            issues.append(
                {
                    "severity": "high",
                    "code": "exam_frequency_audit_incomplete",
                    "message": (
                        f"{attempted_units - audited_attempted_units} attempted knowledge "
                        "points lack a complete or blocked exam-frequency audit"
                    ),
                }
            )

        return {
            "database": display_path(database),
            "schema_version": schema_version,
            "sqlite_user_version": user_version,
            "eligible_attempts": eligible_attempts,
            "scored_attempts": scored_attempts,
            "score_coverage_rate": score_coverage_rate,
            "eligible_questions": eligible_questions,
            "mapped_questions": mapped_questions,
            "question_mapping_rate": question_mapping_rate,
            "unresolved_eligible_attempt_mapping_rows": unresolved_eligible_attempts,
            "conflicting_verified_question_mappings": conflicting_verified_questions,
            "invalid_attempt_mastery_unit_rows": invalid_attempt_units,
            "attempted_knowledge_points": attempted_units,
            "audited_attempted_knowledge_points": audited_attempted_units,
            "failed_knowledge_points": failed_units,
            "audited_failed_knowledge_points": audited_failed_units,
            "exam_audit_coverage_rate": exam_audit_coverage_rate,
            "exam_frequency_audits": audit_counts,
            "verified_exam_occurrences": verified_occurrences,
            "verified_exam_occurrences_without_provenance": missing_verified_occurrence_provenance,
            "verified_exam_occurrences_without_evidence": missing_verified_occurrence_evidence,
            "candidate_exam_occurrences_excluded": candidate_occurrences,
            "data_through": latest_attempt,
            "issues": issues,
            "strict_ready": not any(issue["severity"] == "high" for issue in issues),
        }
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    database = args.database.expanduser().resolve()
    if not database.is_file():
        raise SystemExit(f"database does not exist: {database}")
    result = validate(database)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    if args.strict and not result["strict_ready"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
