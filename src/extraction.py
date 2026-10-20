from src.validation import validate_page
def extract_pages(reader, document_name):
    pages=[]
    for pdf_page_number, pdf_page in enumerate(reader.pages, start=1):
        #按照PDF的物理顺序编号，从1开始！
        page = {
            "document_name": document_name,
            "pdf_page_number": pdf_page_number,
            "text": pdf_page.extract_text() or "",
        }
        validate_page(page)
        pages.append(page)
    return pages