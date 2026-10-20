import argparse
import copy
import io
import json
import unittest
from contextlib import nullcontext, redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

import httpx

from src.answer_evaluation import (
    AnswerEvaluationError, canonical_hash, load_suite, run_answer_evaluation, score_answer, validate_suite,
)
from src.evaluate_answers import main, validate_local_url
from test_reviewed_formula import ReviewFixture


class AnswerEvaluationTests(ReviewFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.suite = {
            "schema_version": "1.0", "dataset_id": "fixture-dev", "split": "development",
            "annotation_scope": "single_reviewed_statement_behavior", "config": self.query.copy(),
            "review_records": [self.record.copy()],
            "cases": [
                {"case_id": "RA001", "query": "alpha", "expected_status": "answered", "available_review_ids": ["FR001"], "annotation_note": "known fact"},
                {"case_id": "RA002", "query": "alpha proof", "expected_status": "refused", "available_review_ids": ["FR001"], "annotation_note": "no reviewed proof"},
                {"case_id": "RA003", "query": "nomatchinguniquephrase", "expected_status": "refused", "available_review_ids": [], "annotation_note": "local control"},
            ],
        }
        self.suite["config"].pop("query")
        self.http_requests = []
        self.override_http = None
        def route(request):
            self.http_requests.append(request)
            if self.override_http:
                result = self.override_http(request)
                if result is not None:
                    return result
            response = self.client.post(request.url.path, json=json.loads(request.content))
            return httpx.Response(response.status_code, content=response.content)
        self.eval_client = self.enterContext(httpx.Client(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(route)))
        def model(request):
            question = json.loads(json.loads(request.content)["messages"][1]["content"])["question"]
            draft = {"decision": "refuse", "reason": "No reviewed proof."} if "proof" in question else self.draft()
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(draft)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})
        self.install_model(handler=model)

    def run_suite(self, **kwargs):
        return run_answer_evaluation(self.eval_client, self.document_id, self.suite, **kwargs)

    def run_live(self, **kwargs):
        options = {"live": True, "allow_paid": True, "max_model_calls": 2, "checkpoint": lambda report: None, **kwargs}
        return self.run_suite(**options)

    def answer_requests(self):
        return [request for request in self.http_requests if request.url.path.endswith("/answer-reviewed")]

    def test_preview_calls_only_free_endpoint_and_never_fakes_scores(self):
        report = self.run_suite()
        self.assertEqual(report["status"], "preview_completed")
        self.assertEqual(report["mode"], "preview")
        self.assertEqual(len(self.http_requests), 3)
        self.assertEqual(self.answer_requests(), [])
        self.assertEqual(self.requests, [])
        self.assertEqual(report["summary"]["planned_model_branch_cases"], 2)
        self.assertEqual(report["summary"]["planned_local_branch_cases"], 1)
        for key in ("behavior_match_rate", "model_branch_match_rate", "answer_case_match_rate", "known_token_usage"):
            self.assertIsNone(report["summary"][key])
        self.assertTrue(all(row["score"] is None for row in report["cases"]))

    def test_explicit_live_mode_scores_model_and_local_branches_separately(self):
        snapshots = []
        report = self.run_live(checkpoint=lambda value: snapshots.append(copy.deepcopy(value)))
        self.assertEqual(report["status"], "completed")
        self.assertTrue(report["summary"]["fully_executed"])
        self.assertEqual(report["summary"]["behavior_match_rate"], 1)
        self.assertEqual(report["summary"]["model_branch_match_rate"], 1)
        self.assertEqual(report["summary"]["local_branch_match_rate"], 1)
        self.assertEqual(report["summary"]["known_token_usage"]["total_tokens"], 240)
        self.assertFalse(report["summary"]["usage_is_complete_account_bill"])
        self.assertEqual(report["model_call_reservations"], 2)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(self.answer_requests()), 3)
        self.assertTrue(all(request.url.path.endswith("/reviewed-context") for request in self.http_requests[:3]))
        for request, row in zip(self.answer_requests(), report["cases"]):
            body = json.loads(request.content)
            self.assertTrue(body["allow_external"])
            self.assertEqual(body["expected_reviewed_context_id"], row["preview"]["reviewed_context_id"])
        self.assertTrue(any(item["model_call_reservations"] == 1 and item["cases"][0]["execution_status"] == "pending" for item in snapshots))

    def test_literal_correct_but_inappropriate_answer_is_a_behavior_failure(self):
        self.install_model()  # Echoes the approved conclusion even for a proof request.
        report = self.run_live()
        self.assertEqual(report["cases"][1]["response"]["status"], "answered")
        self.assertFalse(report["cases"][1]["score"]["behavior_match"])
        self.assertAlmostEqual(report["summary"]["behavior_match_rate"], 2 / 3)
        self.assertEqual(report["summary"]["model_branch_match_rate"], 0.5)

    def test_guard_rejection_is_not_counted_as_correct_refusal(self):
        wrong = self.draft()
        wrong["claims"][0]["text"] = "unsupported proof"
        self.install_model(wrong)
        report = self.run_live()
        self.assertEqual(report["cases"][1]["score"]["observed_status"], "rejected")
        self.assertFalse(report["cases"][1]["score"]["behavior_match"])

    def test_authority_and_budget_flags_are_required_before_http(self):
        for kwargs in ({"live": True}, {"live": True, "allow_paid": True, "max_model_calls": 0},
                       {"live": True, "allow_paid": True, "max_model_calls": 2},
                       {"allow_paid": True}, {"max_model_calls": 1}, {"max_model_calls": True},
                       {"live": True, "allow_paid": True, "max_model_calls": 4, "checkpoint": lambda report: None}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(AnswerEvaluationError):
                    self.run_suite(**kwargs)
        self.assertEqual(self.http_requests, [])

    def test_too_small_cap_fails_after_preflight_before_any_answer_call(self):
        report = self.run_live(max_model_calls=1)
        self.assertEqual(report["error"]["code"], "MODEL_BUDGET_TOO_SMALL")
        self.assertEqual(report["model_call_reservations"], 0)
        self.assertEqual(self.answer_requests(), [])
        self.assertIsNone(report["summary"]["behavior_match_rate"])

    def test_preflight_source_review_or_expected_availability_mismatch_blocks_paid_mode(self):
        for change in (lambda suite: suite["review_records"][0].update(source_sha256="f" * 64),
                       lambda suite: suite["review_records"][0].update(reviewed_statement="a different registered statement"),
                       lambda suite: suite["cases"][1].update(available_review_ids=[])):
            original = copy.deepcopy(self.suite)
            change(self.suite)
            report = self.run_live()
            self.assertEqual(report["status"], "preflight_failed")
            self.assertEqual(report["error"]["code"], "PREFLIGHT_MISMATCH")
            self.assertEqual(self.answer_requests(), [])
            self.assertEqual(self.requests, [])
            self.suite = original

    def test_preflight_revision_race_returns_409_without_a_model_attempt(self):
        changed = False
        def checkpoint(report):
            nonlocal changed
            if report["status"] == "preflight_completed" and not changed:
                self.record["reviewed_statement"] += " Changed after preview."
                self.write_records([self.record])
                changed = True
        report = self.run_live(checkpoint=checkpoint)
        self.assertEqual(report["error"]["code"], "HTTP_409")
        self.assertEqual(report["model_call_reservations"], 1)  # Conservative reservation, not a bill.
        self.assertEqual(self.requests, [])
        self.assertEqual(len(self.answer_requests()), 1)

    def test_http_error_stops_remaining_cases_without_retry_or_partial_accuracy(self):
        answer_count = 0
        def override(request):
            nonlocal answer_count
            if request.url.path.endswith("/answer-reviewed"):
                answer_count += 1
                if answer_count == 2:
                    return httpx.Response(429, text="private upstream error")
        self.override_http = override
        report = self.run_live()
        self.assertEqual(report["status"], "execution_failed")
        self.assertEqual(report["error"]["code"], "HTTP_429")
        self.assertEqual(report["cases"][0]["execution_status"], "completed")
        self.assertEqual(report["cases"][2]["execution_status"], "skipped_after_error")
        self.assertIsNone(report["summary"]["behavior_match_rate"])
        self.assertEqual(report["summary"]["known_token_usage"]["total_tokens"], 120)
        self.assertNotIn("private upstream", json.dumps(report))
        self.assertEqual(len(self.answer_requests()), 2)

    def test_timeout_and_invalid_json_are_sanitized_without_retry(self):
        for timeout in (True, False):
            self.http_requests.clear()
            def override(request):
                if request.url.path.endswith("/answer-reviewed"):
                    if timeout:
                        raise httpx.ReadTimeout("private credential", request=request)
                    return httpx.Response(200, text="private bad json")
            self.override_http = override
            report = self.run_live()
            self.assertEqual(report["status"], "execution_failed")
            self.assertEqual(len(self.answer_requests()), 1)
            self.assertNotIn("private", json.dumps(report))

    def test_report_write_failure_prevents_the_next_answer_request(self):
        def checkpoint(report):
            if report["status"] == "executing" and report["model_call_reservations"] == 1:
                raise OSError("simulated full disk")
        with self.assertRaises(OSError):
            self.run_live(checkpoint=checkpoint)
        self.assertEqual(self.answer_requests(), [])
        self.assertEqual(self.requests, [])

    def test_no_reviewed_evidence_cases_are_all_free_even_in_live_mode(self):
        self.suite["cases"] = [self.suite["cases"][2]]
        report = self.run_live(max_model_calls=1)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["model_call_reservations"], 0)
        self.assertIsNone(report["summary"]["model_branch_match_rate"])
        self.assertEqual(report["summary"]["local_branch_match_rate"], 1)
        self.assertEqual(self.requests, [])

    def test_score_rejects_mismatched_sources_claims_and_usage(self):
        self.run_live()
        packet = self.client.post(self.url + "/reviewed-context", json=self.query).json()
        response = self.client.post(self.url + "/answer-reviewed", json={**self.query, "allow_external": True}).json()
        case = self.suite["cases"][0]
        for change in (lambda answer: answer.update(source_sha256="0" * 64),
                       lambda answer: answer["check"]["claims"][0].update(text="wrong condition"),
                       lambda answer: answer["generation"]["usage"].update(total_tokens=999),
                       lambda answer: answer["generation"].update(model_called=False),
                       lambda answer: answer["check"]["claims"][0]["citations"][0].update(raw_quote="wrong source")):
            changed = copy.deepcopy(response)
            change(changed)
            with self.assertRaises(AnswerEvaluationError):
                score_answer(case, packet, changed)

    def test_bad_suite_schema_duplicate_cases_or_unavailable_answer_are_rejected(self):
        for change in (lambda suite: suite.update(split="heldout"),
                       lambda suite: suite["cases"].append(suite["cases"][0]),
                       lambda suite: suite["cases"][0].update(available_review_ids=[]),
                       lambda suite: suite["cases"][0].update(query=" "),
                       lambda suite: suite["cases"][0].update(available_review_ids=["FR999"])):
            suite = copy.deepcopy(self.suite)
            change(suite)
            with self.assertRaises(AnswerEvaluationError):
                validate_suite(suite)

    def test_suite_loader_is_bounded_and_rejects_duplicate_json(self):
        path = self.root / "suite.json"
        path.write_text(json.dumps(self.suite), encoding="utf-8")
        self.assertEqual(load_suite(path)["dataset_id"], "fixture-dev")
        for text in ('{"schema_version":"1.0","schema_version":"1.0"}', "x" * (128 * 1024 + 1)):
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(AnswerEvaluationError):
                load_suite(path)

    def test_local_url_restriction_forbids_remote_hosts_credentials_and_paths(self):
        for value in ("http://127.0.0.1:8000", "http://localhost:8000/", "http://[::1]:8000"):
            self.assertTrue(validate_local_url(value).startswith("http://"))
        for value in ("https://remote.example", "http://127.0.0.1", "http://127.0.0.1:0", "http://127.0.0.1:65536",
                      "http://user:secret@127.0.0.1:8000", "http://127.0.0.1:8000/path", "http://127.0.0.1:8000?q=x",
                      "http://127.0.0.1:8000#fragment", "http://127.0.0.1:8000\n"):
            with self.assertRaises(argparse.ArgumentTypeError):
                validate_local_url(value)

    def test_cli_existing_report_is_never_overwritten_or_followed_by_requests(self):
        cases = self.root / "suite.json"
        cases.write_text(json.dumps(self.suite), encoding="utf-8")
        output = self.root / "existing.json"
        output.write_text("keep me", encoding="utf-8")
        with patch("src.evaluate_answers.httpx.Client", side_effect=AssertionError("must not connect")), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main(["--document-id", self.document_id, "--cases", str(cases), "--output", str(output)])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(output.read_text(encoding="utf-8"), "keep me")

    def test_cli_default_writes_only_a_preview_report_and_reports_no_scores(self):
        cases = self.root / "suite.json"
        cases.write_text(json.dumps(self.suite), encoding="utf-8")
        output = self.root / "new.json"
        printed = io.StringIO()
        with patch("src.evaluate_answers.httpx.Client", return_value=nullcontext(self.eval_client)), redirect_stdout(printed):
            status = main(["--document-id", self.document_id, "--cases", str(cases), "--output", str(output)])
        self.assertEqual(status, 0)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "preview_completed")
        self.assertIsNone(report["summary"]["behavior_match_rate"])
        self.assertIn("不代表模型答对", printed.getvalue())
        self.assertEqual(self.requests, [])

    def test_cli_invalid_paid_flags_fail_before_creating_report(self):
        output = self.root / "not-created.json"
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["--document-id", self.document_id, "--live", "--output", str(output)])
        self.assertFalse(output.exists())

    def test_client_suite_fingerprint_is_stable_and_sensitive(self):
        suite = validate_suite(self.suite)
        self.assertEqual(canonical_hash(suite), canonical_hash(copy.deepcopy(suite)))
        changed = copy.deepcopy(suite)
        changed["cases"][0]["annotation_note"] += " changed"
        self.assertNotEqual(canonical_hash(suite), canonical_hash(changed))


if __name__ == "__main__":
    unittest.main()
