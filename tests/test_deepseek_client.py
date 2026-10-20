import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from threading import Event
from unittest.mock import patch

import httpx

from src.deepseek_client import (
    DEEPSEEK_URL, MAX_OUTPUT_TOKENS, MAX_RESPONSE_BYTES, DeepSeekClient,
    DeepSeekSettings, GenerationError,
)


TEST_KEY = "offline-fixture-key-not-real"
MESSAGES = [{"role": "system", "content": "Output json"}, {"role": "user", "content": "fixture"}]


def completion_payload(content=None):
    if content is None:
        content = json.dumps({"decision": "refuse", "claims": [], "reason": "证据不足"}, ensure_ascii=False)
    return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}


class DeepSeekSettingsTests(unittest.TestCase):
    def test_default_is_disabled_and_does_not_load_key(self):
        settings = DeepSeekSettings.from_env({"DEEPSEEK_API_KEY": TEST_KEY})
        self.assertFalse(settings.enabled)
        self.assertIsNone(settings.api_key)
        self.assertEqual(settings.max_calls, 10)

    def test_opt_in_settings_are_explicit_and_immutable(self):
        env = {"MATHSCOPE_DEEPSEEK_ENABLED": "1", "DEEPSEEK_API_KEY": TEST_KEY, "MATHSCOPE_DEEPSEEK_MAX_CALLS": "3"}
        settings = DeepSeekSettings.from_env(env)
        env["DEEPSEEK_API_KEY"] = "another-fixture"
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.api_key, TEST_KEY)
        self.assertEqual(settings.max_calls, 3)
        with self.assertRaises(FrozenInstanceError):
            settings.max_calls = 4

    def test_key_is_not_in_repr_or_validation_errors(self):
        self.assertNotIn(TEST_KEY, repr(DeepSeekSettings(api_key=TEST_KEY)))
        invalid = TEST_KEY + "\n"
        with self.assertRaises(ValueError) as caught:
            DeepSeekSettings(api_key=invalid)
        self.assertNotIn(TEST_KEY, str(caught.exception))

    def test_bad_key_types_empty_whitespace_non_ascii_and_length_are_rejected(self):
        for key in (True, 123, "", "has space", "换行", "x" * 257, "key\x7f", "key\x00"):
            with self.subTest(key_type=type(key).__name__):
                with self.assertRaises(ValueError):
                    DeepSeekSettings(api_key=key)

    def test_enable_and_call_limits_are_strict(self):
        for enabled in ("true", "", "2", True):
            with self.subTest(enabled=enabled):
                with self.assertRaises(ValueError):
                    DeepSeekSettings.from_env({"MATHSCOPE_DEEPSEEK_ENABLED": enabled})
        for limit in ("0", "101", "-1", "", "bad", "2.0", "２", True):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    DeepSeekSettings.from_env({"MATHSCOPE_DEEPSEEK_MAX_CALLS": limit})
        for changes in ({"enabled": 1}, {"max_calls": True}, {"max_calls": 0}):
            with self.assertRaises(ValueError):
                DeepSeekSettings(**changes)

    def test_enabled_but_missing_key_is_allowed_for_retrieval_only_startup(self):
        settings = DeepSeekSettings.from_env({"MATHSCOPE_DEEPSEEK_ENABLED": "1"})
        self.assertTrue(settings.enabled)
        self.assertIsNone(settings.api_key)


class DeepSeekClientTests(unittest.TestCase):
    def make_client(self, handler=None, max_calls=10, settings=None):
        self.requests = []
        def handle(request):
            self.requests.append(request)
            return handler(request) if handler else httpx.Response(200, json=completion_payload())
        return DeepSeekClient(settings or DeepSeekSettings(enabled=True, api_key=TEST_KEY, max_calls=max_calls),
                              transport=httpx.MockTransport(handle))

    def assert_error(self, client, status):
        with self.assertRaises(GenerationError) as caught:
            client.complete(MESSAGES)
        self.assertEqual(caught.exception.status_code, status)
        self.assertNotIn(TEST_KEY, str(caught.exception))
        return str(caught.exception)

    def test_fixed_destination_auth_json_and_safe_cost_controls(self):
        client = self.make_client()
        completion = client.complete(MESSAGES)
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(str(request.url), DEEPSEEK_URL)
        self.assertEqual(request.headers["authorization"], "Bearer " + TEST_KEY)
        payload = json.loads(request.content)
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["max_tokens"], MAX_OUTPUT_TOKENS)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["messages"], MESSAGES)
        self.assertNotIn("tools", payload)
        self.assertEqual(completion.usage["total_tokens"], 120)
        self.assertEqual(client.call_state(), {"calls_used": 1, "max_calls": 10})

    def test_disabled_or_missing_key_never_reaches_transport(self):
        for settings in (DeepSeekSettings(), DeepSeekSettings(enabled=True), DeepSeekSettings(api_key=TEST_KEY)):
            client = self.make_client(settings=settings)
            self.assert_error(client, 503)
            self.assertEqual(self.requests, [])
            self.assertEqual(client.call_state()["calls_used"], 0)

    def test_invalid_or_oversized_prompt_does_not_consume_attempt(self):
        client = self.make_client()
        for messages in (None, [], [None, None], [{"content": 1}, {"content": ""}],
                         [{"content": "x" * 10001}, {"content": ""}]):
            with self.subTest(messages_type=type(messages).__name__):
                with self.assertRaises(GenerationError) as caught:
                    client.complete(messages)
                self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(self.requests, [])
        self.assertEqual(client.call_state()["calls_used"], 0)

    def test_status_errors_are_sanitized_and_never_retried(self):
        for upstream, expected in ((401, 503), (403, 503), (402, 503), (429, 503), (500, 502), (400, 502), (302, 502)):
            with self.subTest(upstream=upstream):
                client = self.make_client(lambda request: httpx.Response(upstream, json={"error": TEST_KEY},
                                                                          headers={"Location": "https://not-allowed.example"}))
                self.assert_error(client, expected)
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(client.call_state()["calls_used"], 1)

    def test_transport_timeout_and_connection_failure_are_single_attempts(self):
        for failure, expected in ((httpx.ReadTimeout, 504), (httpx.ConnectError, 502)):
            def fail(request):
                raise failure(TEST_KEY, request=request)
            client = self.make_client(fail)
            self.assert_error(client, expected)
            self.assertEqual(len(self.requests), 1)
            self.assertEqual(client.call_state()["calls_used"], 1)

    def test_call_limit_counts_failed_attempts_and_blocks_next_request(self):
        client = self.make_client(lambda request: httpx.Response(500), max_calls=1)
        self.assert_error(client, 502)
        self.assert_error(client, 429)
        self.assertEqual(len(self.requests), 1)

    def test_in_flight_request_is_not_duplicated(self):
        entered, release = Event(), Event()
        def handler(request):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test release timeout")
            return httpx.Response(200, json=completion_payload())
        client = self.make_client(handler)
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(client.complete, MESSAGES)
            try:
                self.assertTrue(entered.wait(5))
                self.assert_error(client, 429)
            finally:
                release.set()
            first.result(timeout=5)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(client.call_state()["calls_used"], 1)

    def test_error_releases_in_flight_guard(self):
        client = self.make_client(lambda request: httpx.Response(500))
        self.assert_error(client, 502)
        self.assert_error(client, 502)
        self.assertEqual(len(self.requests), 2)

    def test_read_deadline_and_response_size_are_bounded(self):
        client = self.make_client()
        with patch("src.deepseek_client.monotonic", side_effect=[0, 31]):
            self.assert_error(client, 504)
        client = self.make_client(lambda request: httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1)))
        self.assert_error(client, 502)

    def test_empty_cut_off_tool_or_nonassistant_response_is_rejected(self):
        cases = []
        for reason in ("length", "content_filter", "tool_calls", "aborted", "insufficient_system_resource"):
            data = completion_payload()
            data["choices"][0]["finish_reason"] = reason
            cases.append(data)
        for content in (None, "", "   ", "```json\n{}\n```", "{broken", '{"value":NaN}', '{"decision":"answer","decision":"refuse"}'):
            data = completion_payload()
            data["choices"][0]["message"]["content"] = content
            cases.append(data)
        for change in ({"role": "tool"}, {"tool_calls": [{"id": "unexpected"}]}):
            data = completion_payload()
            data["choices"][0]["message"].update(change)
            cases.append(data)
        for data in cases:
            with self.subTest(data=data):
                self.assert_error(self.make_client(lambda request, data=data: httpx.Response(200, json=data)), 502)

    def test_outer_json_usage_and_choice_grammar_are_strict(self):
        cases = [[], {}, {"choices": []}, {"choices": [None]}, {"choices": [1, 2]}]
        for usage in ({}, {"prompt_tokens": True, "completion_tokens": 20, "total_tokens": 21},
                      {"prompt_tokens": -1, "completion_tokens": 20, "total_tokens": 19},
                      {"prompt_tokens": "100", "completion_tokens": 20, "total_tokens": 120},
                      {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 999}):
            data = completion_payload()
            data["usage"] = usage
            cases.append(data)
        for data in cases:
            with self.subTest(data=data):
                self.assert_error(self.make_client(lambda request, data=data: httpx.Response(200, json=data)), 502)
        for raw in (b"not json", b'{"choices":[],"choices":[]}', b'{"bad":NaN}'):
            self.assert_error(self.make_client(lambda request, raw=raw: httpx.Response(200, content=raw)), 502)

    def test_valid_json_with_invalid_draft_is_left_for_existing_validator(self):
        data = completion_payload(content='{"decision":"answer","claims":[]}')
        completion = self.make_client(lambda request: httpx.Response(200, json=data)).complete(MESSAGES)
        self.assertEqual(completion.draft, {"decision": "answer", "claims": []})

    def test_clients_do_not_share_call_quotas(self):
        first, second = self.make_client(max_calls=1), self.make_client(max_calls=1)
        first.complete(MESSAGES)
        self.assertEqual(second.call_state()["calls_used"], 0)
        second.complete(MESSAGES)

    def test_client_disables_proxies_and_redirects_with_explicit_timeouts(self):
        client = self.make_client()
        real_constructor = httpx.Client
        with patch("src.deepseek_client.httpx.Client", wraps=real_constructor) as constructor:
            client.complete(MESSAGES)
        options = constructor.call_args.kwargs
        self.assertFalse(options["trust_env"])
        self.assertFalse(options["follow_redirects"])
        self.assertEqual(options["timeout"].connect, 5)
        self.assertEqual(options["timeout"].read, 30)


if __name__ == "__main__":
    unittest.main()
