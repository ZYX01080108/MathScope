import unittest
from importlib import metadata
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import check_environment as environment


class EnvironmentTests(unittest.TestCase):
    def read_fixture(self, text):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "requirements.txt"
            path.write_text(text, encoding="utf-8")
            return environment.read_pins(path)

    def test_pins_ignore_comments_and_normalize_names(self):
        self.assertEqual(
            self.read_fixture("# reference\n\nSome_Package==1.2.3\n"),
            {"some-package": "1.2.3"},
        )

    def test_duplicate_normalized_names_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.read_fixture("some_package==1.0\nsome-package==1.0\n")

    def test_unpinned_requirements_and_install_options_are_rejected(self):
        for value in ("package>=1.0", "-r other.txt", "https://example.invalid/package.whl", "bad=="):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.read_fixture(value)

    def test_empty_snapshot_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "no version pins"):
            self.read_fixture("# no packages\n")

    def test_matching_versions_pass(self):
        issues = environment.check_environment(
            {"example": "1.0"}, {"example": "1.0"},
            version_reader=lambda name: "1.0", python_version=(3, 11, 9),
        )
        self.assertEqual(issues, [])

    def test_missing_package_is_reported(self):
        def missing(name):
            raise metadata.PackageNotFoundError(name)
        issues = environment.check_environment(
            {"example": "1.0"}, {}, version_reader=missing, python_version=(3, 11),
        )
        self.assertIn("missing: example==1.0", issues)

    def test_wrong_version_is_reported(self):
        issues = environment.check_environment(
            {"example": "1.0"}, {}, version_reader=lambda name: "2.0", python_version=(3, 11),
        )
        self.assertIn("version differs: example, expected 1.0, installed 2.0", issues)

    def test_direct_dependencies_must_match_snapshot(self):
        issues = environment.check_environment(
            {"example": "1.0"}, {"example": "2.0", "other": "1.0"},
            version_reader=lambda name: "1.0", python_version=(3, 11),
        )
        self.assertEqual(len(issues), 2)

    def test_wrong_python_minor_is_reported(self):
        for version in ((3, 10), (3, 12)):
            with self.subTest(version=version):
                self.assertTrue(environment.check_environment(
                    {}, {}, python_version=version,
                ))

    def test_repository_lock_contains_all_direct_pins(self):
        locked = environment.read_pins(environment.PROJECT_ROOT / "requirements.lock.txt")
        direct = environment.read_pins(environment.PROJECT_ROOT / "requirements.txt")
        self.assertTrue(all(locked.get(name) == version for name, version in direct.items()))

    def test_main_returns_failure_for_bad_files(self):
        with patch.object(environment, "read_pins", side_effect=ValueError("bad pin")), patch("sys.stderr"):
            self.assertEqual(environment.main(), 1)

    def test_main_returns_failure_for_version_drift(self):
        with patch.object(environment, "read_pins", return_value={"example": "1.0"}), patch.object(
            environment, "check_environment", return_value=["version differs"],
        ), patch("builtins.print"):
            self.assertEqual(environment.main(), 1)

    def test_main_returns_success_when_versions_match(self):
        with patch.object(environment, "read_pins", return_value={"example": "1.0"}), patch.object(
            environment, "check_environment", return_value=[],
        ), patch("builtins.print"):
            self.assertEqual(environment.main(), 0)


if __name__ == "__main__":
    unittest.main()
