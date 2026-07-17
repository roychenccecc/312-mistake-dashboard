from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.check_public_release import inspect_release


class PublicReleaseGuardTest(unittest.TestCase):
    def test_clean_source_tree_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text("print('synthetic demo')\n", encoding="utf-8")

            checked, violations = inspect_release(root)

        self.assertEqual(checked, 1)
        self.assertEqual(violations, [])

    def test_private_data_path_and_secret_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = root / "data"
            private.mkdir()
            private.joinpath("answers.txt").write_text(
                "api_" + "key = syntheticSecretValue123\n", encoding="utf-8"
            )

            _, violations = inspect_release(root)

        reasons = {violation.reason for violation in violations}
        self.assertIn("file is inside a private-data directory", reasons)
        self.assertIn("credential assignment", reasons)

    def test_test_fixture_exception_requires_same_line_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tests = root / "tests"
            tests.mkdir()
            fixture = tests / "fixture.py"
            fixture.write_text(
                "to" + "ken = syntheticSecretValue123\n", encoding="utf-8"
            )
            _, violations_without_marker = inspect_release(root)

            fixture.write_text(
                "to" + "ken = syntheticSecretValue123  # "
                "public-release-fixture: allow-sensitive-pattern\n",
                encoding="utf-8",
            )
            _, violations_with_marker = inspect_release(root)

        self.assertTrue(
            any(
                violation.reason == "credential assignment"
                for violation in violations_without_marker
            )
        )
        self.assertEqual(violations_with_marker, [])

    def test_absolute_user_path_database_and_env_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("notes.txt").write_text(
                "private = /" + "Users/example/study\n", encoding="utf-8"
            )
            root.joinpath("snapshot.sqlite3").write_text("synthetic\n", encoding="utf-8")
            root.joinpath(".env.example").write_text("PLACEHOLDER=1\n", encoding="utf-8")

            _, violations = inspect_release(root)

        reasons = {violation.reason for violation in violations}
        self.assertIn("absolute macOS user path", reasons)
        self.assertIn("forbidden binary/data file type", reasons)
        self.assertIn("environment files are not allowed", reasons)


if __name__ == "__main__":
    unittest.main()
