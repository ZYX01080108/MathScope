import copy
import unittest

from src.answer_check import AnswerCheckError, check_answer_draft
from src.evidence_context import build_evidence_context, compute_context_id


def packet(texts=None, budget=6000):
    texts = ["alpha beta", "omega delta"] if texts is None else texts
    response = {
        "document_id": "a" * 32, "document_name": "test.pdf", "source_sha256": "b" * 64,
        "query": "alpha", "method": "normalized", "k": 5, "returned_count": len(texts),
        "indexed_chunk_count": len(texts), "cache_hit": False,
        "results": [{"score": 1.0, "chunk": {
            "document_id": "a" * 32, "document_name": "test.pdf", "pdf_page_number": index + 1,
            "start_char": 10, "end_char": 10 + len(text), "text": text,
        }} for index, text in enumerate(texts)],
    }
    return build_evidence_context(response, budget)


def answer(quote="alpha", citation_id="E1", text="测试草稿"):
    return {"decision": "answer", "claims": [{"text": text, "citations": [{"citation_id": citation_id, "quote": quote}]}]}


class AnswerCheckTests(unittest.TestCase):
    def codes(self, result):
        return {item["code"] for item in result["issues"]}

    def test_valid_quote_is_resolved_from_registry(self):
        result = check_answer_draft(packet(), answer("beta"))
        self.assertEqual(result["status"], "passed")
        self.assertFalse(result["semantics_verified"])
        citation = result["claims"][0]["citations"][0]
        self.assertEqual(citation["document_id"], "a" * 32)
        self.assertEqual(citation["pdf_page_number"], 1)
        self.assertEqual((citation["quote_start_char"], citation["quote_end_char"]), (16, 20))
        self.assertEqual((citation["evidence_start_char"], citation["evidence_end_char"]), (10, 20))

    def test_unknown_id_rejects_whole_draft(self):
        result = check_answer_draft(packet(), answer(citation_id="E99"))
        self.assertIn("unknown_citation", self.codes(result))
        self.assertEqual(result["claims"], [])

    def test_quote_in_other_evidence_does_not_validate_wrong_id(self):
        result = check_answer_draft(packet(), answer("omega", "E1"))
        self.assertIn("quote_not_found", self.codes(result))

    def test_quote_is_exact_not_casefolded_or_paraphrased(self):
        for quote in ("Alpha", "alpha  beta", "a different statement"):
            with self.subTest(quote=quote):
                self.assertIn("quote_not_found", self.codes(check_answer_draft(packet(), answer(quote))))

    def test_unicode_and_newline_quote_offsets_use_python_characters(self):
        context = packet(["中文😀\nalpha"])
        result = check_answer_draft(context, answer("😀\nalpha"))
        citation = result["claims"][0]["citations"][0]
        self.assertEqual((citation["quote_start_char"], citation["quote_end_char"]), (12, 19))

    def test_repeated_quote_resolves_first_occurrence(self):
        result = check_answer_draft(packet(["alpha and alpha"]), answer())
        self.assertEqual(result["claims"][0]["citations"][0]["quote_start_char"], 10)

    def test_identical_citation_duplicate_is_rejected(self):
        draft = answer()
        draft["claims"][0]["citations"] *= 2
        self.assertIn("duplicate_citation", self.codes(check_answer_draft(packet(), draft)))

    def test_distinct_quotes_from_same_evidence_are_allowed(self):
        draft = answer()
        draft["claims"][0]["citations"].append({"citation_id": "E1", "quote": "beta"})
        result = check_answer_draft(packet(), draft)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(result["claims"][0]["citations"]), 2)

    def test_same_evidence_may_be_reused_for_different_claims(self):
        draft = answer()
        draft["claims"].append(copy.deepcopy(draft["claims"][0]))
        self.assertEqual(check_answer_draft(packet(), draft)["status"], "passed")

    def test_empty_answer_is_rejected(self):
        result = check_answer_draft(packet(), {"decision": "answer", "claims": []})
        self.assertIn("empty_answer", self.codes(result))

    def test_answer_cannot_have_refusal_reason(self):
        result = check_answer_draft(packet(), {**answer(), "reason": "insufficient"})
        self.assertIn("answer_has_refusal_reason", self.codes(result))

    def test_refusal_needs_nonblank_reason(self):
        for reason in (None, "", "   "):
            with self.subTest(reason=reason):
                result = check_answer_draft(packet(), {"decision": "refuse", "reason": reason})
                self.assertIn("missing_refusal_reason", self.codes(result))

    def test_refusal_cannot_include_answer_claims(self):
        draft = {**answer(), "decision": "refuse", "reason": "not enough evidence"}
        self.assertIn("refusal_has_claims", self.codes(check_answer_draft(packet(), draft)))

    def test_refusal_format_can_pass_with_empty_context(self):
        result = check_answer_draft(packet([]), {"decision": "refuse", "reason": "没有候选片段"})
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["claims"], [])
        self.assertEqual(result["refusal_reason"], "没有候选片段")

    def test_refusal_format_does_not_judge_whether_refusal_is_correct(self):
        result = check_answer_draft(packet(), {"decision": "refuse", "reason": "无法据此回答"})
        self.assertEqual(result["status"], "passed")
        self.assertFalse(result["semantics_verified"])

    def test_answer_with_empty_or_budget_empty_context_is_rejected(self):
        for context in (packet([]), packet(["a" * 1000], 128)):
            self.assertIn("no_evidence_for_answer", self.codes(check_answer_draft(context, answer())))

    def test_invalid_schema_returns_rejection_without_echoing_bad_input(self):
        for draft in ({"decision": "unknown"}, {"decision": "answer", "claims": [{}]}, "not JSON data"):
            result = check_answer_draft(packet(), draft)
            self.assertEqual(result["status"], "rejected")
            self.assertIn("invalid_draft", self.codes(result))

    def test_client_supplied_page_and_source_fields_are_not_trusted(self):
        draft = answer()
        draft["claims"][0]["citations"][0]["pdf_page_number"] = 99
        self.assertIn("invalid_draft", self.codes(check_answer_draft(packet(), draft)))

    def test_empty_and_whitespace_quotes_are_rejected(self):
        for quote in ("", "   "):
            self.assertIn("invalid_draft", self.codes(check_answer_draft(packet(), answer(quote))))

    def test_inline_citation_ids_must_not_bypass_structured_registry(self):
        for text in ("句子 [E99]", "句子 [ E99 ]", "句子 [E1, E999]"):
            self.assertIn("inline_citation_not_allowed", self.codes(check_answer_draft(packet(), answer(text=text))))

    def test_refusal_reason_cannot_embed_unregistered_reference_markers(self):
        result = check_answer_draft(packet(), {"decision": "refuse", "reason": "参见 [E99]"})
        self.assertIn("inline_citation_not_allowed", self.codes(result))

    def test_valid_literal_quote_does_not_prove_claim_semantics(self):
        result = check_answer_draft(packet(), answer(text="所有复数都小于零"))
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["checks_scope"], "format_and_literal_citations")
        self.assertFalse(result["semantics_verified"])

    def test_modified_packet_fingerprint_is_rejected(self):
        context = packet()
        context["query"] = "another question"
        with self.assertRaises(AnswerCheckError) as caught:
            check_answer_draft(context, answer())
        self.assertEqual(caught.exception.status_code, 409)

    def test_even_recomputed_hash_cannot_hide_wrong_source_registry(self):
        context = packet()
        context["evidence"][0]["document_id"] = "c" * 32
        context["context_id"] = compute_context_id(context)
        with self.assertRaises(AnswerCheckError):
            check_answer_draft(context, answer())

    def test_inconsistent_context_text_and_counters_are_rejected(self):
        for key, value in (("context_text", "[]"), ("included_count", 0), ("status", "no_matches")):
            context = packet()
            context[key] = value
            context["context_id"] = compute_context_id(context)
            with self.subTest(key=key), self.assertRaises(AnswerCheckError):
                check_answer_draft(context, answer())

    def test_check_does_not_mutate_packet_or_draft(self):
        context, draft = packet(), answer()
        before = copy.deepcopy((context, draft))
        first = check_answer_draft(context, draft)
        self.assertEqual(first, check_answer_draft(context, draft))
        first["claims"][0]["text"] = "changed result"
        self.assertEqual((context, draft), before)


if __name__ == "__main__":
    unittest.main()
