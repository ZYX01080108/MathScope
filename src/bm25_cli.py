import json
from pathlib import Path

from src.bm25_retrieval import BM25Retriever


def main():
    project_root = Path(__file__).resolve().parent.parent
    chunks_path = project_root / "data" / "parsed" / "lecture.chunks.jsonl"

    with chunks_path.open("r", encoding="utf-8") as file:
        chunks = [json.loads(line) for line in file]

    retriever = BM25Retriever(chunks)
    print("当前方案：原始 BM25；输入 :quit 退出。")

    while True:
        query = input("\n请输入问题：")

        if query.strip() == ":quit":
            break

        try:
            hits = retriever.search(query, k=5)
        except ValueError as error:
            print("输入错误：", error)
            continue

        print("本次返回条数：", len(hits))

        if not hits:
            print("没有找到与查询共享词语的块。")
            continue

        for rank, hit in enumerate(hits, start=1):
            chunk = hit["chunk"]
            print()
            print(f"第 {rank} 名；分数：{hit['score']:.4f}")
            print("文档：", chunk["document_name"])
            print("PDF 物理页号：", chunk["pdf_page_number"])
            print("字符区间：", chunk["start_char"], chunk["end_char"])
            print("原文：", chunk["text"])


if __name__ == "__main__":
    main()