def validate_page(page):
    if page["pdf_page_number"]<1:
        raise ValueError("PDF 页码必须从1开始")