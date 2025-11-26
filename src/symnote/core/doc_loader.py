from __future__ import annotations

import fitz  # PyMuPDF
import io
from typing import List

def extract_text_from_pdf(file_content: bytes) -> str:
    """Extracts text from a PDF file."""
    text = []
    with fitz.open(stream=io.BytesIO(file_content), filetype="pdf") as doc:
        for page in doc:
            text.append(page.get_text())
    return "\n".join(text)
