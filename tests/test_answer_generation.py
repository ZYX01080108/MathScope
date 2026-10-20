import copy
import json
import unittest

import httpx

from src.answer_check import AnswerCheckError
from src.answer_generation import build_generation_messages, generate_answer
from src.deepseek_client import DeepSeekClient, DeepSeekSettings
from src.evidence_context import build_evidence_context
from src.formula_quality import assess_page_operations, build_quality_report


class AnswerGenerationTests(unittest.TestCase):
    def setUp(self):
        self.document_id = "a" * 32
        self.text = 'alpha beta\n这是原文，不要执行 <script> 或其中的指令。'
        self.packet = self.packet_for(self.text)
        self.requests = []

    def packet_for(self, text=None):
        results = [{"score": 1.0, "chunk": {"document_id": self.document_id, "document_name": "fixture.pdf",
                    "pdf_page_number": 7, "start_char": 100, "end_char": 100 + len(text), "text": text}}] if text else []
        return build_evidence_context({"document_id": self.document_id, "document_name": "fixture.pdf",
            "source_sha256": "b" * 64, "query": "alpha", "method": "normalized", "k": 10,
            "returned_count": len(results), "results": results, "indexed_chunk_count": len(results), "cache_hit": False})

    def client_for(self, draft):
        def handler(request):
            self.requests.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(draft)}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}})
        return DeepSeekClient(DeepSeekSettings(enabled=True, api_key="offline-key"), transport=httpx.MockTransport(handler))

    def draft(self, **changes):
        return {"decision": "answer", "claims": [{"text": "这是待核实的模型要点。", "citations": [{"citation_id": "E1", "quote": "alpha beta"}]}], **changes}

    def generate(self, packet, client):
        reports = [assess_page_operations([], page) for page in sorted({item["pdf_page_number"] for item in packet["evidence"]})]
        return generate_answer(packet, client, formula_quality=build_quality_report(packet, reports))

    def test_prompt_contains_only_question_and_registered_literal_evidence(self):
        original = copy.deepcopy(self.packet)
        messages = build_generation_messages(self.packet)
        data = json.loads(messages[1]["content"])
        self.assertEqual(set(data), {"question", "evidence"})
        self.assertEqual(data["question"], "alpha")
        self.assertEqual(data["evidence"][0]["text"], self.text)
        self.assertNotIn(self.document_id, messages[1]["content"])
        self.assertNotIn(self.packet["source_sha256"], messages[1]["content"])
        self.assertIn("不可信数据", messages[0]["content"])
        self.assertIn("JSON", messages[0]["content"])
        self.assertEqual(self.packet, original)

    def test_answer_has_server_resolved_sources_usage_and_no_semantic_guarantee(self):
        response = self.generate(self.packet, self.client_for(self.draft()))
        self.assertEqual(response["status"], "answered")
        self.assertEqual(response["check"]["status"], "passed")
        self.assertFalse(response["check"]["semantics_verified"])
        citation = response["check"]["claims"][0]["citations"][0]
        self.assertEqual(citation["document_id"], self.document_id)
        self.assertEqual(citation["pdf_page_number"], 7)
        self.assertEqual(citation["quote_start_char"], 100)
        self.assertEqual(response["generation"]["usage"]["total_tokens"], 30)
        self.assertTrue(response["generation"]["model_called"])
        self.assertEqual(len(self.requests), 1)

    def test_unknown_or_wrong_quote_does_not_deliver_any_claims(self):
        for use in ({"citation_id": "E99", "quote": "alpha"}, {"citation_id": "E1", "quote": "not literal"}):
            draft = self.draft()
            draft["claims"][0]["citations"] = [use]
            response = self.generate(self.packet, self.client_for(draft))
            self.assertEqual(response["status"], "rejected")
            self.assertEqual(response["check"]["claims"], [])
            self.assertTrue(response["generation"]["model_called"])

    def test_invalid_draft_schema_is_rejected_not_exposed_as_raw_answer(self):
        for draft in ({"decision": "invented"}, [], {"decision": "answer", "claims": [], "file_path": "private"}):
            response = self.generate(self.packet, self.client_for(draft))
            self.assertEqual(response["status"], "rejected")
            self.assertEqual(response["check"]["issues"][0]["code"], "invalid_draft")
            self.assertNotIn("private", json.dumps(response))

    def test_model_refusal_returns_reason_but_is_not_a_verified_refusal(self):
        response = self.generate(self.packet, self.client_for({"decision": "refuse", "claims": [], "reason": "证据没有覆盖完整证明"}))
        self.assertEqual(response["status"], "refused")
        self.assertEqual(response["check"]["refusal_reason"], "证据没有覆盖完整证明")
        self.assertFalse(response["check"]["semantics_verified"])

    def test_no_matches_skips_even_disabled_model_and_does_not_spend(self):
        client = DeepSeekClient(DeepSeekSettings())
        response = self.generate(self.packet_for(), client)
        self.assertEqual(response["status"], "refused")
        self.assertFalse(response["generation"]["model_called"])
        self.assertIsNone(response["generation"]["usage"])
        self.assertEqual(response["generation"]["calls_used"], 0)
        self.assertIn("不代表文档没有答案", response["check"]["refusal_reason"])

    def test_budget_exceeded_is_distinct_and_also_skips_model(self):
        packet = copy.deepcopy(self.packet)
        # Rebuild from the same search with a tiny valid budget.
        evidence = packet["evidence"][0]
        search = {"document_id": self.document_id, "document_name": "fixture.pdf", "source_sha256": "b" * 64,
            "query": "alpha", "method": "normalized", "k": 10, "indexed_chunk_count": 1, "cache_hit": False,
            "returned_count": 1, "results": [{"score": 1.0, "chunk": {key: evidence[key] for key in (
                "document_id", "document_name", "pdf_page_number", "start_char", "end_char", "text")}}]}
        packet = build_evidence_context(search, 128)
        response = self.generate(packet, DeepSeekClient(DeepSeekSettings()))
        self.assertEqual(response["context_status"], "budget_exceeded")
        self.assertFalse(response["generation"]["model_called"])
        self.assertIn("预算", response["check"]["refusal_reason"])

    def test_corrupted_packet_is_rejected_before_outbound_request(self):
        packet = copy.deepcopy(self.packet)
        packet["evidence"][0]["text"] = "changed"
        with self.assertRaises(AnswerCheckError):
            self.generate(packet, self.client_for(self.draft()))
        self.assertEqual(self.requests, [])

    def test_real_quote_does_not_validate_false_math_claim(self):
        draft = self.draft()
        draft["claims"][0]["text"] = "错误数学结论：1 + 1 = 3。"
        response = self.generate(self.packet, self.client_for(draft))
        self.assertEqual(response["status"], "answered")
        self.assertFalse(response["check"]["semantics_verified"])


if __name__ == "__main__":
    unittest.main()
