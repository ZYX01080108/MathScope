import json
from pathlib import Path


def search_chunks(chunks, query):
    keyword = query.strip().casefold()

    if not keyword:
        raise ValueError("查询关键词不能为空")

    return [
        chunk
        for chunk in chunks
        if keyword in chunk["text"].casefold()
    ]


def main():
    project_root = Path(__file__).resolve().parent.parent
    chunks_path = project_root / "data" / "parsed" / "lecture.chunks.jsonl"

    with chunks_path.open("r", encoding="utf-8") as file:
        chunks = [json.loads(line) for line in file]

    query = input("请输入关键词：")
    matches = search_chunks(chunks, query)

    print("匹配块数：", len(matches))

    if not matches:
        print("未找到包含该关键词的片段。")
        return

    print("以下按文件顺序展示前 5 条匹配，不代表相关性排名。")

    for number, chunk in enumerate(matches[:5], start=1):
        print()
        print(f"结果 {number}")
        print("文档：", chunk["document_name"])
        print("PDF 物理页号：", chunk["pdf_page_number"])
        print(
            "页内字符区间：",
            f'[{chunk["start_char"]}, {chunk["end_char"]})',
        )
        print("原文：", chunk["text"])


if __name__ == "__main__":
    main()