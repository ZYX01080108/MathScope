from src.validation import validate_page

def chunk_page(page, chunk_size=500):
    validate_page(page)

    if type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError("chunk_size 必须是正整数")
    text = page["text"]
    chunks=[]
    for start in range(0, len(text),chunk_size):
        end = min(start+chunk_size,len(text))
        chunks.append({
            "document_name": page["document_name"],
            "pdf_page_number": page["pdf_page_number"],
            "start_char": start,
            "end_char": end,
            "text": text[start:end],
        })
    return chunks