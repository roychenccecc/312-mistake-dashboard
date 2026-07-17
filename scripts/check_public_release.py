#!/usr/bin/env python3
"""Fail-closed privacy guard for the public dashboard source release.

The release intentionally contains source code only.  Run this check before
generating ``data/demo.sqlite3``; generated databases must remain untracked.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_TEXT_FILE_BYTES = 2 * 1024 * 1024
TEST_FIXTURE_MARKER = "public-release-fixture: allow-sensitive-pattern"

IGNORED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}

# These project directories contain personal study records in the private
# workspace.  A public source release must never contain files beneath them.
FORBIDDEN_TOP_LEVEL_DIRECTORIES = {
    "backups",
    "data",
    "incoming",
    "materials",
    "notion-data",
    "outputs",
    "private",
    "question_bank",
    "review_logs",
}

# Documentation may explain where a locally generated demo database lives,
# without carrying the database itself.
ALLOWED_DATA_PLACEHOLDERS = {"data/.gitkeep", "data/README.md"}

FORBIDDEN_SUFFIXES = {
    # Databases and SQLite sidecars.
    ".db",
    ".db3",
    ".shm",
    ".sqlite",
    ".sqlite3",
    ".wal",
    # Documents, study assets, and images.
    ".bmp",
    ".gif",
    ".heic",
    ".ico",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".svg",
    ".tif",
    ".tiff",
    ".webp",
    ".xmind",
    # Private key and credential containers.
    ".cer",
    ".crt",
    ".der",
    ".key",
    ".keystore",
    ".p12",
    ".pem",
    ".pfx",
    # Tabular exports and archives can silently carry private records.
    ".7z",
    ".csv",
    ".gz",
    ".rar",
    ".tar",
    ".tsv",
    ".xls",
    ".xlsx",
    ".zip",
}

FORBIDDEN_EXACT_FILENAMES = {
    ".ds_store",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "id_dsa",
    "id_ed25519",
    "id_ecdsa",
    "id_rsa",
    "service-account.json",
}


def _private_key_pattern() -> re.Pattern[str]:
    # Construct this in pieces so the guard does not contain the forbidden
    # header it is designed to detect.
    begin = "-" * 5 + "BEGIN "
    end = "PRIVATE" + " KEY" + "-" * 5
    return re.compile(re.escape(begin) + r"(?:RSA |EC |OPENSSH )?" + re.escape(end))


SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "absolute macOS user path",
        re.compile(re.escape("/" + "Users/")),
    ),
    ("private-key header", _private_key_pattern()),
    (
        "GitHub access token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    ),
    (
        "OpenAI-style secret key",
        re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "AWS access key",
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    ),
    (
        "credential assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?key|client[_-]?secret|password|passwd|secret|token)\b"
            r"\s*[:=]\s*[\"']?[A-Za-z0-9_./+=-]{8,}"
        ),
    ),
    (
        "URL with embedded credentials",
        re.compile(
            r"(?i)\b(?:mongodb(?:\+srv)?|mysql|postgres(?:ql)?|redis)://"
            r"[^\s/:@]+:[^\s/@]+@"
        ),
    ),
)


@dataclass(frozen=True, order=True)
class Violation:
    path: str
    line: int
    reason: str

    def render(self) -> str:
        location = f"{self.path}:{self.line}" if self.line else self.path
        return f"{location}: {self.reason}"


def iter_release_files(root: Path) -> Iterable[Path]:
    """Yield ordinary files without following symlinked directories."""

    for current, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = sorted(
            name for name in directories if name not in IGNORED_DIRECTORIES
        )
        current_path = Path(current)
        for filename in sorted(filenames):
            yield current_path / filename


def path_violations(root: Path, path: Path) -> list[Violation]:
    rel = path.relative_to(root)
    rel_text = rel.as_posix()
    lower_name = rel.name.lower()
    violations: list[Violation] = []

    if path.is_symlink():
        violations.append(Violation(rel_text, 0, "symlinks are not allowed"))
        return violations

    if (
        rel.parts
        and rel.parts[0].lower() in FORBIDDEN_TOP_LEVEL_DIRECTORIES
        and rel_text not in ALLOWED_DATA_PLACEHOLDERS
    ):
        violations.append(
            Violation(rel_text, 0, "file is inside a private-data directory")
        )

    if lower_name == ".env" or lower_name.startswith(".env."):
        violations.append(Violation(rel_text, 0, "environment files are not allowed"))

    if lower_name in FORBIDDEN_EXACT_FILENAMES:
        violations.append(Violation(rel_text, 0, "credential-bearing filename"))

    if path.suffix.lower() in FORBIDDEN_SUFFIXES or lower_name.endswith(
        ("-wal", "-shm", ".tar.gz", ".tar.bz2", ".tar.xz")
    ):
        violations.append(Violation(rel_text, 0, "forbidden binary/data file type"))

    if "credential" in lower_name or "private-key" in lower_name:
        violations.append(Violation(rel_text, 0, "suspicious credential filename"))

    return violations


def content_violations(root: Path, path: Path) -> list[Violation]:
    rel = path.relative_to(root)
    rel_text = rel.as_posix()
    is_test_fixture = bool(rel.parts and rel.parts[0] == "tests")

    try:
        size = path.stat().st_size
    except OSError as error:
        return [Violation(rel_text, 0, f"cannot inspect file: {error}")]
    if size > MAX_TEXT_FILE_BYTES:
        return [
            Violation(
                rel_text,
                0,
                f"file exceeds {MAX_TEXT_FILE_BYTES} byte public-source limit",
            )
        ]

    try:
        raw = path.read_bytes()
    except OSError as error:
        return [Violation(rel_text, 0, f"cannot read file: {error}")]

    if raw.startswith(b"SQLite format 3") or raw.startswith(b"%PDF-"):
        return [Violation(rel_text, 0, "forbidden file signature")]
    if b"\x00" in raw:
        return [Violation(rel_text, 0, "binary content is not allowed")]

    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return [Violation(rel_text, 0, "file is not UTF-8 text")]

    violations: list[Violation] = []
    lines = content.splitlines()
    for reason, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(content):
            line_number = content.count("\n", 0, match.start()) + 1
            line = lines[line_number - 1] if line_number <= len(lines) else ""
            # Tests may exercise the detector with an obviously synthetic value,
            # but only through an explicit, same-line opt-in.  Everywhere else,
            # and by default in tests, matches fail closed.
            if is_test_fixture and TEST_FIXTURE_MARKER in line:
                continue
            violations.append(Violation(rel_text, line_number, reason))
    return violations


def inspect_release(root: Path) -> tuple[int, list[Violation]]:
    checked = 0
    violations: list[Violation] = []
    for path in iter_release_files(root):
        checked += 1
        path_issues = path_violations(root, path)
        violations.extend(path_issues)
        # Still scan a forbidden path when it is readable text; multiple reasons
        # make accidental inclusions easier to diagnose.
        if not path.is_symlink():
            violations.extend(content_violations(root, path))
    return checked, sorted(set(violations))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check that a public release contains source code only."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=PROJECT_ROOT,
        help="release root to inspect (defaults to this repository)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        print(f"release root does not exist: {root}", file=sys.stderr)
        return 2

    checked, violations = inspect_release(root)
    if violations:
        print(
            f"Public-release guard failed: {len(violations)} issue(s) in "
            f"{checked} file(s).",
            file=sys.stderr,
        )
        for violation in violations:
            print(f"- {violation.render()}", file=sys.stderr)
        return 1

    print(f"Public-release guard passed: {checked} UTF-8 source file(s) checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
