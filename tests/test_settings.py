import os
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.api import create_app
from src.settings import DEFAULT_DATA_DIR, PROJECT_ROOT, Settings
from src.storage import write_jsonl


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))

    def test_defaults_preserve_original_paths(self):
        settings = Settings.from_env({})
        self.assertEqual(settings.data_dir, DEFAULT_DATA_DIR.resolve())
        self.assertEqual(settings.upload_directory, PROJECT_ROOT / "data/raw/uploads")
        self.assertEqual(settings.parsed_directory, PROJECT_ROOT / "data/parsed/uploads")
        self.assertEqual(settings.baseline_chunks_path, PROJECT_ROOT / "data/parsed/lecture.chunks.jsonl")
        self.assertEqual(settings.index_cache_size, 4)

    def test_relative_paths_are_anchored_to_project_not_working_directory(self):
        with patch("os.getcwd", return_value=str(self.directory)):
            settings = Settings.from_env({
                "MATHSCOPE_DATA_DIR": "config-demo/data",
                "MATHSCOPE_BASELINE_CHUNKS": "config-demo/baseline.jsonl",
            })
        self.assertEqual(settings.data_dir, (PROJECT_ROOT / "config-demo/data").resolve())
        self.assertEqual(settings.baseline_chunks_path, (PROJECT_ROOT / "config-demo/baseline.jsonl").resolve())

    def test_environment_values_and_numeric_cache_are_parsed(self):
        settings = Settings.from_env({
            "MATHSCOPE_DATA_DIR": str(self.directory / "data with spaces"),
            "MATHSCOPE_BASELINE_CHUNKS": str(self.directory / "experiments/baseline.jsonl"),
            "MATHSCOPE_INDEX_CACHE_SIZE": "6",
        })
        self.assertEqual(settings.data_dir, self.directory / "data with spaces")
        self.assertEqual(settings.baseline_chunks_path, self.directory / "experiments/baseline.jsonl")
        self.assertEqual(settings.index_cache_size, 6)

    def test_custom_data_root_also_changes_default_baseline_location(self):
        settings = Settings(data_dir=self.directory / "new-data")
        self.assertEqual(settings.baseline_chunks_path, self.directory / "new-data/parsed/lecture.chunks.jsonl")

    def test_blank_environment_values_are_rejected(self):
        for key in ("MATHSCOPE_DATA_DIR", "MATHSCOPE_BASELINE_CHUNKS", "MATHSCOPE_INDEX_CACHE_SIZE"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    Settings.from_env({key: "  "})

    def test_invalid_cache_sizes_are_rejected(self):
        for value in ("0", "65", "-1", "1.5", "bad"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    Settings.from_env({"MATHSCOPE_INDEX_CACHE_SIZE": value})
        with self.assertRaises(ValueError):
            Settings(index_cache_size=True)

    def test_directory_and_file_configurations_are_checked(self):
        self.assertTrue(self.directory.is_dir())
        with self.assertRaises(ValueError):
            Settings(baseline_chunks_path=self.directory)
        path = self.directory / "not-a-directory"
        with path.open("x", encoding="utf-8") as output:
            output.write("fixture")
        with self.assertRaises(ValueError):
            Settings(data_dir=path)
        with self.assertRaises(ValueError):
            Settings(data_dir=Path(PROJECT_ROOT.anchor))

    @unittest.skipUnless(os.name == "nt", "Windows-specific path ambiguity")
    def test_drive_relative_and_root_relative_paths_are_rejected(self):
        for value in ("D:relative-folder", "\\relative-folder"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    Settings(data_dir=value)

    def test_reading_settings_does_not_create_directories(self):
        root = self.directory / "not-created"
        settings = Settings.from_env({"MATHSCOPE_DATA_DIR": str(root)})
        self.assertFalse(root.exists())
        self.assertFalse(settings.upload_directory.exists())
        self.assertFalse(settings.parsed_directory.exists())

    def test_settings_are_immutable_and_environment_is_a_snapshot(self):
        values = {"MATHSCOPE_DATA_DIR": str(self.directory / "first")}
        settings = Settings.from_env(values)
        values["MATHSCOPE_DATA_DIR"] = str(self.directory / "second")
        self.assertEqual(settings.data_dir, self.directory / "first")
        with self.assertRaises(FrozenInstanceError):
            settings.index_cache_size = 2

    def test_dotenv_file_is_not_implicitly_loaded(self):
        with (self.directory / ".env").open("x", encoding="utf-8") as output:
            output.write("MATHSCOPE_INDEX_CACHE_SIZE=9\n")
        with patch("os.getcwd", return_value=str(self.directory)):
            self.assertEqual(Settings.from_env({}).index_cache_size, 4)

    def test_factory_reads_environment_when_no_explicit_settings(self):
        with patch.dict(os.environ, {
            "MATHSCOPE_DATA_DIR": str(self.directory / "env-data"),
            "MATHSCOPE_BASELINE_CHUNKS": str(self.directory / "env-baseline.jsonl"),
            "MATHSCOPE_INDEX_CACHE_SIZE": "3",
        }):
            application = create_app()
        self.assertEqual(application.state.settings.data_dir, self.directory / "env-data")
        self.assertEqual(application.state.settings.index_cache_size, 3)

    def test_explicit_settings_take_priority_over_environment(self):
        settings = Settings(data_dir=self.directory / "explicit", index_cache_size=2)
        with patch.dict(os.environ, {"MATHSCOPE_INDEX_CACHE_SIZE": "not-an-integer"}):
            application = create_app(settings)
        self.assertIs(application.state.settings, settings)
        with TestClient(application) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(application.state.document_search.max_cached_indexes, 2)

    def test_application_factory_rejects_non_settings_input(self):
        with self.assertRaises(TypeError):
            create_app({"data_dir": str(self.directory)})

    def test_two_running_apps_keep_documents_baselines_and_caches_separate(self):
        root_a, root_b = self.directory / "app-a", self.directory / "app-b"
        baseline = self.directory / "only-a.jsonl"
        write_jsonl([
            {"document_name": "baseline.pdf", "pdf_page_number": 1,
             "start_char": 0, "end_char": 5, "text": "alpha"},
        ], baseline)
        app_a = create_app(Settings(data_dir=root_a, baseline_chunks_path=baseline, index_cache_size=2))
        app_b = create_app(Settings(data_dir=root_b, index_cache_size=5))
        self.assertIsNot(app_a, app_b)
        with TestClient(app_a) as client_a:
            with TestClient(app_b) as client_b:
                uploaded = client_a.post("/documents", files={"file": ("same.pdf", b"%PDF-1.7\nfixture-a")})
                self.assertEqual(uploaded.status_code, 201)
                id_a = uploaded.json()["document_id"]
                self.assertEqual(client_a.get("/documents").json()["total"], 1)
                self.assertEqual(client_b.get("/documents").json()["total"], 0)
                self.assertEqual(client_b.get(f"/documents/{id_a}").status_code, 404)
                uploaded = client_b.post("/documents", files={"file": ("same.pdf", b"%PDF-1.7\nfixture-b")})
                self.assertEqual(uploaded.status_code, 201)
                id_b = uploaded.json()["document_id"]
                self.assertEqual(client_a.get(f"/documents/{id_b}").status_code, 404)
                for client, document_id, text in ((client_a, id_a, "alpha"), (client_b, id_b, "omega")):
                    reader = SimpleNamespace(is_encrypted=False, pages=[
                        SimpleNamespace(extract_text=lambda value=text: value),
                    ])
                    with patch("src.document_processing.PdfReader", return_value=reader):
                        response = client.post(f"/documents/{document_id}/parse", json={"chunk_size": 500})
                        self.assertEqual(response.status_code, 200)
                result_a = client_a.post(f"/documents/{id_a}/search", json={"query": "alpha"})
                result_b = client_b.post(f"/documents/{id_b}/search", json={"query": "omega"})
                self.assertEqual(result_a.status_code, 200)
                self.assertEqual(result_b.status_code, 200)
                self.assertEqual(result_a.json()["results"][0]["chunk"]["text"], "alpha")
                self.assertEqual(result_b.json()["results"][0]["chunk"]["text"], "omega")
                self.assertEqual(client_a.post(f"/documents/{id_a}/search", json={"query": "omega"}).json()["results"], [])
                self.assertEqual(client_a.post("/search", json={"query": "alpha"}).status_code, 200)
                self.assertEqual(client_b.post("/search", json={"query": "alpha"}).status_code, 503)
                self.assertEqual(app_a.state.document_search.max_cached_indexes, 2)
                self.assertEqual(app_b.state.document_search.max_cached_indexes, 5)
                self.assertIsNot(app_a.state.document_search, app_b.state.document_search)
            self.assertEqual(client_a.get("/health").status_code, 200)
            self.assertTrue(client_a.post(f"/documents/{id_a}/search", json={"query": "alpha"}).json()["cache_hit"])
            self.assertEqual(app_b.state.document_search._cache, {})
        self.assertEqual(app_a.state.document_search._cache, {})


if __name__ == "__main__":
    unittest.main()
