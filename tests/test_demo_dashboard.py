from __future__ import annotations

import hashlib
import io
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "sqlite"))

from create_demo_database import create_database  # noqa: E402
from backfill_attempt_mastery_units import (  # noqa: E402
    build_parser as build_backfill_parser,
    connect as connect_backfill,
)
from mistake_dashboard_server import DashboardRepository, DashboardRequestHandler  # noqa: E402
from validate_mistake_dashboard_data import validate  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DemoDashboardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "demo.sqlite3"
        self.created = create_database(self.database)
        self.repository = DashboardRepository(self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_generator_and_strict_validator(self) -> None:
        self.assertTrue(self.created["synthetic"])
        self.assertEqual(self.created["schema_version"], 9)
        self.assertEqual(self.created["schema_scope"], "dashboard-compatible-subset")
        self.assertEqual(self.created["integrity_check"], "ok")
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 9)
        report = validate(self.database)
        self.assertTrue(report["strict_ready"])
        self.assertEqual(report["schema_version"], 9)
        self.assertEqual(report["sqlite_user_version"], 9)
        self.assertEqual(report["eligible_attempts"], 7)
        self.assertEqual(report["scored_attempts"], 6)
        self.assertEqual(report["eligible_questions"], 7)
        self.assertEqual(report["mapped_questions"], 7)
        self.assertEqual(report["attempted_knowledge_points"], 3)
        self.assertEqual(report["audited_attempted_knowledge_points"], 3)
        self.assertEqual(report["verified_exam_occurrences"], 3)
        self.assertEqual(report["verified_exam_occurrences_without_provenance"], 0)
        self.assertEqual(report["verified_exam_occurrences_without_evidence"], 0)
        self.assertEqual(report["candidate_exam_occurrences_excluded"], 2)
        self.assertEqual(report["data_through"], "2026-06-11")

    def test_weighted_summary_and_true_exam_isolation(self) -> None:
        summary = self.repository.summary({})
        self.assertEqual(summary["overall"]["eligible_attempts"], 7)
        self.assertEqual(summary["overall"]["scored_attempts"], 6)
        self.assertEqual(summary["overall"]["total_weight"], 8.0)
        self.assertEqual(summary["overall"]["weighted_success"], 2.5)
        self.assertEqual(summary["overall"]["error_rate"], 0.6875)
        self.assertEqual(summary["real_exam"]["scored_attempts"], 2)
        self.assertEqual(summary["real_exam"]["total_weight"], 3.0)
        self.assertEqual(summary["real_exam"]["error_rate"], 0.333333)

    def test_frequency_status_and_question_source_order(self) -> None:
        chapters = self.repository.chapters({})["chapters"]
        memory = next(row for row in chapters if row["name"] == "记忆实验")
        self.assertEqual(
            memory["source_chapter_ids"],
            ["demo:chapter:memory", "demo:chapter:memory-alias"],
        )
        self.assertEqual(memory["metrics"]["eligible_attempts"], 5)
        knowledge = self.repository.knowledge(
            {"chapter_id": [memory["chapter_id"]]}
        )["knowledge"]
        retrieval = next(
            row for row in knowledge if row["mastery_unit_id"] == "demo:unit:retrieval"
        )
        interference = next(
            row for row in knowledge if row["mastery_unit_id"] == "demo:unit:interference"
        )
        self.assertTrue(retrieval["exam_frequency"]["formal"])
        self.assertEqual(retrieval["exam_frequency"]["occurrence_count"], 2)
        self.assertEqual(retrieval["exam_frequency"]["distinct_year_count"], 2)
        self.assertEqual(
            [entry["occurrence_count"] for entry in retrieval["exam_frequency"]["recent_by_year"]],
            [1, 0, 1],
        )
        self.assertFalse(interference["exam_frequency"]["formal"])
        self.assertIsNone(interference["exam_frequency"]["occurrence_count"])
        self.assertEqual(interference["exam_frequency"]["verified_occurrence_lower_bound"], 1)
        self.assertEqual(
            interference["exam_frequency"]["recent_by_year"],
            [{"year": 2025, "occurrence_count": 1}],
        )

        details = self.repository.questions(
            {"mastery_unit_id": ["demo:unit:retrieval"]}
        )["questions"]
        self.assertEqual(
            [row["source_type"] for row in details],
            ["真题", "教材课后习题", "辅导书题", "AI变式题"],
        )
        ranks = DashboardRepository._source_rank
        self.assertLess(ranks("真题改写"), ranks("AI变式题"))
        self.assertLess(ranks("普通改写题"), ranks("AI变式题"))
        self.assertEqual(ranks("AI变式题"), ranks("自编诊断题"))

    def test_api_levels_reconcile_and_unknown_is_not_zero(self) -> None:
        summary = self.repository.summary({})
        chapters = self.repository.chapters({})["chapters"]
        self.assertEqual(
            sum(row["metrics"]["eligible_attempts"] for row in chapters),
            summary["overall"]["eligible_attempts"],
        )
        self.assertEqual(
            sum(row["metrics"]["total_weight"] for row in chapters),
            summary["overall"]["total_weight"],
        )
        empty = next(row for row in chapters if row["name"] == "未作答示例章")
        self.assertIsNone(empty["metrics"]["error_rate"])

        learning = next(row for row in chapters if row["name"] == "学习实验")
        payload = self.repository.knowledge(
            {"chapter_id": [learning["chapter_id"]]}
        )
        self.assertEqual(
            sum(row["metrics"]["eligible_attempts"] for row in payload["knowledge"]),
            learning["metrics"]["eligible_attempts"],
        )
        unknown = next(
            row
            for row in payload["knowledge"]
            if row["mastery_unit_id"] == "demo:unit:unknown"
        )
        self.assertFalse(unknown["exam_frequency"]["formal"])
        self.assertIsNone(unknown["exam_frequency"]["occurrence_count"])
        self.assertIn("demo:unit:unknown", payload["unknown_frequency"])

        details = self.repository.questions(
            {"mastery_unit_id": ["demo:unit:retrieval"]}
        )
        memory = next(row for row in chapters if row["name"] == "记忆实验")
        memory_knowledge = self.repository.knowledge(
            {"chapter_id": [memory["chapter_id"]]}
        )["knowledge"]
        retrieval = next(
            row
            for row in memory_knowledge
            if row["mastery_unit_id"] == "demo:unit:retrieval"
        )
        self.assertEqual(details["metrics"], retrieval["metrics"])

    def test_generator_refuses_force_through_symlink(self) -> None:
        target = Path(self.temporary.name) / "target.sqlite3"
        create_database(target)
        before = sha256(target)
        link = Path(self.temporary.name) / "linked.sqlite3"
        link.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink"):
            create_database(link, force=True)
        self.assertEqual(sha256(target), before)

    def test_backfill_requires_explicit_private_database(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                build_backfill_parser().parse_args(["--dry-run"])
        with self.assertRaisesRegex(ValueError, "schema subset"):
            connect_backfill(self.database, apply=False)

    def test_non_get_handlers_return_405(self) -> None:
        handler = object.__new__(DashboardRequestHandler)
        statuses: list[int] = []
        headers: list[tuple[str, str]] = []
        handler.send_response = statuses.append
        handler.send_header = lambda name, value: headers.append((name, value))
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()
        for method in ("do_HEAD", "do_POST", "do_PUT", "do_PATCH", "do_DELETE", "do_OPTIONS"):
            statuses.clear()
            headers.clear()
            handler.wfile.seek(0)
            handler.wfile.truncate(0)
            getattr(handler, method)()
            self.assertEqual(statuses, [405])
            self.assertIn(("Allow", "GET"), headers)

    def test_dashboard_queries_do_not_change_database(self) -> None:
        before = sha256(self.database)
        summary = self.repository.summary({})
        chapters = self.repository.chapters({})
        chapter_id = chapters["chapters"][0]["chapter_id"]
        self.repository.knowledge({"chapter_id": [chapter_id]})
        self.repository.questions({"mastery_unit_id": ["demo:unit:retrieval"]})
        after = sha256(self.database)
        self.assertEqual(before, after)
        self.assertEqual(summary["freshness"]["data_cutoff_date"], "2026-06-11")


if __name__ == "__main__":
    unittest.main()
