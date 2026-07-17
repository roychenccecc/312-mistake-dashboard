#!/usr/bin/env python3
"""Create a deterministic, entirely synthetic dashboard demonstration database."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "demo.sqlite3"

SCHEMA = r"""
PRAGMA foreign_keys = ON;
PRAGMA user_version = 9;

CREATE TABLE system_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE chapters (
    chapter_id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    name TEXT NOT NULL
);

CREATE TABLE review_sessions (
    session_id TEXT PRIMARY KEY,
    review_date TEXT NOT NULL,
    mode TEXT NOT NULL,
    subject TEXT NOT NULL,
    chapter_id TEXT REFERENCES chapters(chapter_id)
);

CREATE TABLE questions (
    question_id TEXT PRIMARY KEY,
    source_id TEXT,
    source_type TEXT NOT NULL,
    subject TEXT,
    year INTEGER,
    question_type TEXT,
    prompt TEXT NOT NULL,
    answer TEXT,
    explanation TEXT,
    question_family TEXT
);

CREATE TABLE chapter_mastery_units (
    mastery_unit_id TEXT PRIMARY KEY,
    chapter_id TEXT NOT NULL REFERENCES chapters(chapter_id),
    name TEXT NOT NULL,
    module_name TEXT,
    course_importance_stars INTEGER,
    course_importance_label TEXT,
    course_importance_source TEXT,
    coverage_status TEXT
);

CREATE TABLE question_mastery_unit_links (
    question_id TEXT NOT NULL REFERENCES questions(question_id),
    mastery_unit_id TEXT NOT NULL REFERENCES chapter_mastery_units(mastery_unit_id),
    tested_dimension TEXT NOT NULL,
    semantic_verification_status TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (question_id, mastery_unit_id, tested_dimension)
);

CREATE UNIQUE INDEX one_verified_primary_mastery_unit_per_question
ON question_mastery_unit_links(question_id)
WHERE semantic_verification_status = 'verified'
  AND tested_dimension = 'primary';

CREATE TABLE attempts (
    attempt_id TEXT PRIMARY KEY,
    question_id TEXT NOT NULL REFERENCES questions(question_id),
    session_id TEXT REFERENCES review_sessions(session_id),
    attempt_date TEXT NOT NULL,
    result TEXT,
    score REAL,
    max_score REAL,
    independent_answer INTEGER NOT NULL DEFAULT 0,
    used_hint INTEGER NOT NULL DEFAULT 0,
    confidence TEXT,
    error_type TEXT,
    error_notes TEXT,
    is_delayed_retest INTEGER NOT NULL DEFAULT 0,
    needs_retest INTEGER NOT NULL DEFAULT 0,
    attempt_phase TEXT NOT NULL,
    mastery_unit_id TEXT REFERENCES chapter_mastery_units(mastery_unit_id),
    score_weight REAL NOT NULL DEFAULT 1
);

CREATE TABLE knowledge_exam_occurrences (
    occurrence_id TEXT PRIMARY KEY,
    chapter_id TEXT NOT NULL REFERENCES chapters(chapter_id),
    mastery_unit_id TEXT NOT NULL REFERENCES chapter_mastery_units(mastery_unit_id),
    question_id TEXT NOT NULL REFERENCES questions(question_id),
    year INTEGER NOT NULL,
    question_number TEXT,
    question_part TEXT,
    original_question_type TEXT,
    question_family TEXT,
    exam_points REAL,
    source_id TEXT,
    source_reference TEXT,
    verification_status TEXT NOT NULL,
    verification_note TEXT,
    verified_date TEXT,
    evidence_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE exam_frequency_audits (
    mastery_unit_id TEXT PRIMARY KEY REFERENCES chapter_mastery_units(mastery_unit_id),
    status TEXT NOT NULL CHECK (status IN ('partial', 'complete', 'blocked')),
    year_start INTEGER,
    year_end INTEGER,
    audit_note TEXT,
    blocker_reason TEXT,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    audited_date TEXT NOT NULL,
    CHECK (status != 'complete' OR (year_start = 2007 AND year_end = 2026)),
    CHECK (status != 'blocked' OR blocker_reason IS NOT NULL)
);
"""


def seed(connection: sqlite3.Connection) -> None:
    connection.executemany(
        "INSERT INTO system_meta(key, value) VALUES (?, ?)",
        (
            ("schema_version", "9"),
            ("dataset_kind", "synthetic_demo"),
            ("schema_scope", "dashboard-compatible-subset"),
        ),
    )
    connection.executemany(
        "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
        ((8, "dashboard_mapping_integrity"), (9, "unique_verified_primary_mapping")),
    )
    connection.executemany(
        "INSERT INTO chapters(chapter_id, subject, name) VALUES (?, ?, ?)",
        (
            ("demo:chapter:memory", "示例心理学", "01-记忆实验"),
            ("demo:chapter:memory-alias", "示例心理学", "记忆实验"),
            ("demo:chapter:learning", "示例心理学", "02-学习实验"),
            ("demo:chapter:empty", "示例统计", "未作答示例章"),
        ),
    )
    connection.executemany(
        """
        INSERT INTO chapter_mastery_units(
            mastery_unit_id, chapter_id, name, module_name,
            course_importance_stars, course_importance_label,
            course_importance_source, coverage_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (
                "demo:unit:retrieval",
                "demo:chapter:memory",
                "示例知识点 A：提取线索",
                "记忆过程",
                5,
                "合成高重要度",
                "synthetic-demo",
                "covered",
            ),
            (
                "demo:unit:interference",
                "demo:chapter:memory-alias",
                "示例知识点 B：干扰辨析",
                "记忆过程",
                4,
                "合成中重要度",
                "synthetic-demo",
                "covered",
            ),
            (
                "demo:unit:reinforcement",
                "demo:chapter:learning",
                "示例知识点 C：强化程序",
                "学习机制",
                3,
                "合成中重要度",
                "synthetic-demo",
                "covered",
            ),
            (
                "demo:unit:unknown",
                "demo:chapter:learning",
                "示例知识点 D：尚无证据",
                "学习机制",
                None,
                "重要度未知",
                "synthetic-demo",
                "uncovered",
            ),
        ),
    )

    questions = (
        (
            "demo:q:real-a",
            "真题",
            2024,
            "单项选择题",
            "【完全虚构】在示例任务中，哪一种提示最能帮助提取项目 A？",
            "选项甲",
            "这是合成演示解析，不对应任何真实试题。",
            "single_choice",
        ),
        (
            "demo:q:textbook-a",
            "教材课后习题",
            None,
            "单项选择题",
            "【完全虚构】项目 A 与线索 A 同时出现时，示例结果如何？",
            "结果甲",
            "这是从零编写的合成题。",
            "single_choice",
        ),
        (
            "demo:q:ai-a",
            "AI变式题",
            None,
            "多项选择题",
            "【完全虚构】哪些条件属于示例提取任务的控制变量？",
            "条件甲、条件乙",
            "这是从零编写的合成变式。",
            "multiple_choice",
        ),
        (
            "demo:q:workbook-unanswered",
            "辅导书题",
            None,
            "单项选择题",
            "【完全虚构】示例练习册中，线索 A 缺失时应选择哪一项？",
            "选项乙",
            "这是从零编写的合成辅导书样例，不对应任何出版物。",
            "single_choice",
        ),
        (
            "demo:q:rewrite-b",
            "真题改写",
            None,
            "单项选择题",
            "【完全虚构】示例材料 B 受到干扰后，最可能出现哪种模式？",
            "模式乙",
            "该题不是统考真题，仅用于展示来源隔离。",
            "single_choice",
        ),
        (
            "demo:q:real-c",
            "真题",
            2026,
            "单项选择题",
            "【完全虚构】示例强化程序 C 的判别线索是什么？",
            "线索丙",
            "这是合成演示解析，不对应任何真实试题。",
            "single_choice",
        ),
        (
            "demo:q:missing-score",
            "自编诊断题",
            None,
            "简答题",
            "【完全虚构】说明示例强化程序的两个组成部分。",
            "组成部分甲与乙",
            "该记录故意不提供数值分数，用于展示覆盖率。",
            "short_answer",
        ),
        (
            "demo:q:occurrence-2026",
            "真题",
            2026,
            "单项选择题",
            "【完全虚构】仅用于完整考频证据的示例题。",
            "选项丁",
            "无个人作答记录。",
            "single_choice",
        ),
        (
            "demo:q:occurrence-2025",
            "真题",
            2025,
            "多项选择题",
            "【完全虚构】仅用于受阻考频下限的示例题。",
            "选项甲、丁",
            "无个人作答记录。",
            "multiple_choice",
        ),
    )
    connection.executemany(
        """
        INSERT INTO questions(
            question_id, source_type, subject, year, question_type,
            prompt, answer, explanation, question_family
        ) VALUES (?, ?, '示例心理学', ?, ?, ?, ?, ?, ?)
        """,
        questions,
    )

    mappings = (
        ("demo:q:real-a", "demo:unit:retrieval"),
        ("demo:q:textbook-a", "demo:unit:retrieval"),
        ("demo:q:ai-a", "demo:unit:retrieval"),
        ("demo:q:workbook-unanswered", "demo:unit:retrieval"),
        ("demo:q:rewrite-b", "demo:unit:interference"),
        ("demo:q:real-c", "demo:unit:reinforcement"),
        ("demo:q:missing-score", "demo:unit:reinforcement"),
        ("demo:q:occurrence-2026", "demo:unit:retrieval"),
        ("demo:q:occurrence-2025", "demo:unit:interference"),
    )
    connection.executemany(
        """
        INSERT INTO question_mastery_unit_links(
            question_id, mastery_unit_id, tested_dimension,
            semantic_verification_status, evidence_json
        ) VALUES (?, ?, 'primary', 'verified', '["synthetic mapping"]')
        """,
        mappings,
    )
    connection.execute(
        """
        INSERT INTO question_mastery_unit_links(
            question_id, mastery_unit_id, tested_dimension,
            semantic_verification_status, evidence_json
        ) VALUES (
            'demo:q:real-a', 'demo:unit:interference', 'secondary',
            'verified', '["synthetic secondary link"]'
        )
        """
    )

    connection.executemany(
        """
        INSERT INTO review_sessions(
            session_id, review_date, mode, subject, chapter_id
        ) VALUES (?, ?, 'chapter', '示例心理学', ?)
        """,
        (
            ("demo:session:memory", "2026-06-10", "demo:chapter:memory"),
            (
                "demo:session:memory-alias",
                "2026-06-10",
                "demo:chapter:memory-alias",
            ),
            ("demo:session:learning", "2026-06-11", "demo:chapter:learning"),
        ),
    )

    attempts = (
        (
            "demo:a:real-correct", "demo:q:real-a", "demo:session:memory",
            "2026-06-10", "correct", 1.0, 1.0, 1, 0, None, None, None,
            0, 0, "pre_review", "demo:unit:retrieval", 2.0,
        ),
        (
            "demo:a:textbook-partial", "demo:q:textbook-a", "demo:session:memory",
            "2026-06-10", "partial", 0.5, 1.0, 1, 0, "low",
            "概念边界", "合成错因：遗漏一个虚构条件。", 0, 1,
            "pre_review", "demo:unit:retrieval", 1.0,
        ),
        (
            "demo:a:ai-wrong", "demo:q:ai-a", "demo:session:memory",
            "2026-06-10", "incorrect", 0.0, 1.0, 1, 0, None,
            "多选漏选", "合成错因：漏选虚构条件乙。", 0, 1,
            "pre_review", "demo:unit:retrieval", 1.0,
        ),
        (
            "demo:a:workbook-unanswered", "demo:q:workbook-unanswered",
            "demo:session:memory", "2026-06-10", "unanswered", 0.0, 1.0,
            1, 0, None, "未作答", "合成未答记录以零分进入公式。", 0, 1,
            "pre_review", "demo:unit:retrieval", 2.0,
        ),
        (
            "demo:a:rewrite-wrong", "demo:q:rewrite-b",
            "demo:session:memory-alias",
            "2026-06-10", "incorrect", 0.0, 1.0, 1, 0, None,
            "概念混淆", "合成错因：混淆虚构模式甲与乙。", 0, 1,
            "legacy", "demo:unit:interference", 1.0,
        ),
        (
            "demo:a:real-retest-wrong", "demo:q:real-c", "demo:session:learning",
            "2026-06-11", "incorrect", 0.0, 1.0, 1, 0, "low",
            "提取失败", "合成错因：未提取虚构线索丙。", 1, 1,
            "delayed_retest", "demo:unit:reinforcement", 1.0,
        ),
        (
            "demo:a:missing-score", "demo:q:missing-score", "demo:session:learning",
            "2026-06-11", "unanswered", None, None, 1, 0, None,
            "未作答", "合成记录故意缺少数值分数。", 0, 1,
            "pre_review", "demo:unit:reinforcement", 1.0,
        ),
        (
            "demo:a:guided-excluded", "demo:q:ai-a", "demo:session:memory",
            "2026-06-10", "incorrect", 0.0, 1.0, 1, 0, None,
            "引导练习", "此合成记录不进入错误率。", 0, 0,
            "guided_repair", "demo:unit:retrieval", 1.0,
        ),
        (
            "demo:a:immediate-excluded", "demo:q:workbook-unanswered",
            "demo:session:memory", "2026-06-12", "correct", 1.0, 1.0,
            1, 0, None, "即时修复", "此合成记录不进入错误率或截止日期。",
            0, 0, "immediate_repair", "demo:unit:retrieval", 1.0,
        ),
        (
            "demo:a:hinted-excluded", "demo:q:real-a", "demo:session:memory",
            "2026-06-10", "incorrect", 0.0, 1.0, 1, 1, None,
            "提示后作答", "此合成记录不进入错误率。", 0, 0,
            "pre_review", "demo:unit:retrieval", 1.0,
        ),
        (
            "demo:a:dependent-excluded", "demo:q:rewrite-b", "demo:session:memory",
            "2026-06-10", "incorrect", 0.0, 1.0, 0, 0, None,
            "非独立作答", "此合成记录不进入错误率。", 0, 0,
            "pre_review", "demo:unit:interference", 1.0,
        ),
    )
    connection.executemany(
        """
        INSERT INTO attempts(
            attempt_id, question_id, session_id, attempt_date, result,
            score, max_score, independent_answer, used_hint, confidence,
            error_type, error_notes, is_delayed_retest, needs_retest,
            attempt_phase, mastery_unit_id, score_weight
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        attempts,
    )

    occurrences = (
        (
            "demo:occ:2024-a", "demo:chapter:memory", "demo:unit:retrieval",
            "demo:q:real-a", 2024, "A1", "单项选择题", "single_choice", 2.0,
            "synthetic-source-2024-A1", "synthetic-demo/2024/A1", "verified",
            "Synthetic verification only", "2026-06-01", '["synthetic original-question check"]',
        ),
        (
            "demo:occ:2026-a", "demo:chapter:memory", "demo:unit:retrieval",
            "demo:q:occurrence-2026", 2026, "A2", "单项选择题", "single_choice", 2.0,
            "synthetic-source-2026-A2", "synthetic-demo/2026/A2", "verified",
            "Synthetic verification only", "2026-06-01", '["synthetic original-question check"]',
        ),
        (
            "demo:occ:candidate", "demo:chapter:memory", "demo:unit:retrieval",
            "demo:q:occurrence-2026", 2025, "A3", "单项选择题", "single_choice", 2.0,
            None, "synthetic-demo/candidate-chart-row", "candidate", None, None,
            '["synthetic candidate only"]',
        ),
        (
            "demo:occ:blocked-lower-bound", "demo:chapter:memory-alias",
            "demo:unit:interference", "demo:q:occurrence-2025", 2025, "B1",
            "多项选择题", "multiple_choice", 3.0, "synthetic-source-2025-B1",
            "synthetic-demo/2025/B1", "verified",
            "Synthetic lower-bound evidence only", "2026-06-01",
            '["synthetic original-question check"]',
        ),
        (
            "demo:occ:learning-candidate", "demo:chapter:learning",
            "demo:unit:reinforcement", "demo:q:real-c", 2024, "C1",
            "单项选择题", "single_choice", 2.0, None,
            "synthetic-demo/candidate-chart-row", "candidate", None, None,
            '["synthetic candidate only"]',
        ),
    )
    connection.executemany(
        """
        INSERT INTO knowledge_exam_occurrences(
            occurrence_id, chapter_id, mastery_unit_id, question_id, year,
            question_number, original_question_type, question_family,
            exam_points, source_id, source_reference, verification_status,
            verification_note, verified_date, evidence_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        occurrences,
    )

    connection.executemany(
        """
        INSERT INTO exam_frequency_audits(
            mastery_unit_id, status, year_start, year_end, audit_note,
            blocker_reason, evidence_json, audited_date
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (
                "demo:unit:retrieval", "complete", 2007, 2026,
                "Synthetic full-range audit", None,
                '["synthetic complete audit"]', "2026-06-01",
            ),
            (
                "demo:unit:interference", "blocked", 2007, 2026,
                "Synthetic blocked audit", "部分虚构年份资料被设为缺失",
                '["synthetic lower-bound audit"]', "2026-06-01",
            ),
            (
                "demo:unit:reinforcement", "blocked", 2007, 2026,
                "Synthetic blocked audit", "未提供完整的虚构原题集合",
                '["synthetic blocker"]', "2026-06-01",
            ),
        ),
    )


def create_database(database: Path, *, force: bool = False) -> dict[str, object]:
    database = Path(os.path.abspath(database.expanduser()))
    if database.is_symlink():
        raise ValueError("refusing to replace a symlink")
    if database.exists() and not force:
        raise FileExistsError(f"database already exists: {database}; pass --force to replace it")
    database.parent.mkdir(parents=True, exist_ok=True)
    temporary = database.with_name(f".{database.name}.tmp")
    if temporary.exists():
        temporary.unlink()

    connection = sqlite3.connect(temporary)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA)
        seed(connection)
        connection.commit()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"integrity check failed: {integrity}")
        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("chapters", "chapter_mastery_units", "questions", "attempts")
        }
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
    finally:
        connection.close()

    if database.is_symlink():
        temporary.unlink(missing_ok=True)
        raise ValueError("refusing to replace a symlink")
    os.replace(temporary, database)
    os.chmod(database, 0o600)
    try:
        display_database = str(database.relative_to(PROJECT_ROOT))
    except ValueError:
        display_database = f"<external>/{database.name}"
    return {
        "status": "created",
        "synthetic": True,
        "database": display_database,
        "schema_version": 9,
        "schema_scope": "dashboard-compatible-subset",
        "integrity_check": integrity,
        "counts": counts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        result = create_database(args.database, force=args.force)
    except (FileExistsError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
