from rank_bm25 import BM25Okapi

from src.tokenization import tokenize


class BM25Retriever:
    def __init__(self, chunks, tokenizer=tokenize):
        self.chunks = [dict(chunk) for chunk in chunks]
        self.tokenizer = tokenizer

        tokenized = [
            tokenizer(chunk["text"])
            for chunk in self.chunks
        ]
        self.term_sets = [set(tokens) for tokens in tokenized]

        self.bm25 = BM25Okapi(tokenized) if any(tokenized) else None

    def search(self, query, k=5):
        if type(k) is not int or k < 1:
            raise ValueError("k 必须是正整数")

        query_tokens = self.tokenizer(query)
        if not query_tokens:
            raise ValueError("查询必须包含可检索的词")

        if self.bm25 is None:
            return []

        query_terms = set(query_tokens)
        candidates = [
            index
            for index, terms in enumerate(self.term_sets)
            if query_terms & terms
        ]

        if not candidates:
            return []

        scores = self.bm25.get_scores(query_tokens)
        ranked = sorted(
            candidates,
            key=lambda index: (-scores[index], index),
        )

        return [
            {
                "chunk": dict(self.chunks[index]),
                "score": float(scores[index]),
            }
            for index in ranked[:k]
        ]