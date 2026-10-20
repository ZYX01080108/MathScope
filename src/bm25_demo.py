import json
from pathlib import Path

from src.tokenization import tokenize, tokenize_normalized

from rank_bm25 import BM25Okapi

from src.tokenization import tokenize


def main():
    project_root = Path(__file__).resolve().parent.parent
    chunks_path = project_root / "data" / "parsed" / "lecture.chunks.jsonl"

    with chunks_path.open("r", encoding="utf-8") as file:
        chunks = [json.loads(line) for line in file]

    corpus_tokens = [tokenize(chunk["text"]) for chunk in chunks]
    bm25 = BM25Okapi(corpus_tokens)

    query = "为什么复数域不能成为与加法、乘法相容的有序域？"
    query_tokens = tokenize(query)
    scores = bm25.get_scores(query_tokens)

    ranked = sorted(
        enumerate(scores),
        key=lambda item: (-item[1], item[0]),
    )

    print("查询词：", query_tokens)
    print("参与排序的块数：", len(chunks))

    for rank, (index, score) in enumerate(ranked[:5], start=1):
        chunk = chunks[index]
        print()
        print(f"第 {rank} 名；BM25 分数：{score:.4f}")
        print("PDF 物理页号：", chunk["pdf_page_number"])
        print("字符区间：", chunk["start_char"], chunk["end_char"])
        print("原文预览：", chunk["text"][:150])
    
    
    first_index = ranked[0][0]
    target_index = next(
        index
        for index, chunk in enumerate(chunks)
        if chunk["pdf_page_number"] == 8
        and chunk["start_char"] == 0
    )

    target_rank = next(
        rank
        for rank, (index, _) in enumerate(ranked, start=1)
        if index == target_index
    )

    print()
    print("目标证据排名：", target_rank)
    print("词语 | 第 1 名的得分贡献 | 目标证据的得分贡献")

    for token in dict.fromkeys(query_tokens):
        token_scores = bm25.get_scores([token])
        print(
            token,
            f"{token_scores[first_index]:.3f}",
            f"{token_scores[target_index]:.3f}",
        )
    
    normalized_bm25 = BM25Okapi([
        tokenize_normalized(chunk["text"])
        for chunk in chunks
    ])

    normalized_scores = normalized_bm25.get_scores(
        tokenize_normalized(query)
    )

    normalized_ranking = sorted(
        enumerate(normalized_scores),
        key=lambda item: (-item[1], item[0]),
    )

    normalized_target_rank = next(
        rank
        for rank, (index, _) in enumerate(normalized_ranking, start=1)
        if index == target_index
    )

    print()
    print("原方案目标排名：", target_rank)
    print("术语统一后目标排名：", normalized_target_rank)

if __name__ == "__main__":
    main()