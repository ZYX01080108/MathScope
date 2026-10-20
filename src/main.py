from pathlib import Path
from pypdf import PdfReader
from src.extraction import extract_pages
from src.storage import write_jsonl
from src.chunking import chunk_page

def main():
    project_root = Path(__file__).resolve().parent.parent
    #从当前代码文件找到项目的根目录
    pdf_path = project_root / "data" / "raw" / "lecture.pdf"
    reader = PdfReader(pdf_path)
    pages= extract_pages(reader,pdf_path.name)
    chunk_size = 500
    chunks = []
    for page in pages:
        page_chunks = chunk_page(page, chunk_size= chunk_size)
        chunks.extend(page_chunks)
    print("分块大小（字符）：", chunk_size)
    print("生成块数：", len(chunks))
    source_pages = {
        (page["document_name"],page["pdf_page_number"]): page
        for page in pages
    }
    for chunk in chunks:
        source_key = (
            chunk["document_name"],
            chunk["pdf_page_number"],
        )
        source_page = source_pages[source_key]
        start = chunk["start_char"]
        end = chunk["end_char"]
        if not 0 <= start < end <= len(source_page["text"]):
            raise ValueError("块的字符位置超出了原页文本范围")

        if chunk["text"] != source_page["text"][start:end]:
            raise ValueError("块的文本与原页对应位置不一致")

    print("所有块的来源位置检查通过")
    chunks_path = (
        project_root / "data" / "parsed" / f"{pdf_path.stem}.chunks.jsonl"
    )
    write_jsonl(chunks, chunks_path)
    print("分块结果已保存到：", chunks_path)

    if chunks:
        sample_chunk = chunks[len(chunks) // 2]
        print("示例块：", sample_chunk)
    output_path = (project_root / "data" / "parsed" / f"{pdf_path.stem}.page.jsonl")
    write_jsonl(pages, output_path)
    print("解析结果已经保存到：",output_path)
    print("PDF 总页数: ", len(reader.pages))
    print("提取记录数：", len(pages))
    empty_page_numbers=[
        page["pdf_page_number"]
        for page in pages 
        if not page["text"].strip()
    ]
    print ("未提取到非空文本的页号：", empty_page_numbers)
    if not pages:
        print ("该pdf没有页面")
        return
    for index in sorted({0,len(pages)//2,len(pages)-1}):
        page=pages[index]
        print()
        print("文档：",page["document_name"])
        print("PDF物理页号：",page["pdf_page_number"])
        print("字符数：", len(page["text"]))
        print("原文预览：",page["text"][:300])
if __name__ == "__main__":
    main()