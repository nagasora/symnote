from __future__ import annotations

import fitz  # PyMuPDF
import io
from typing import List

import docx

def extract_text_from_pdf(file_content: bytes) -> str:
    """Extracts text from a PDF file."""
    text = []
    with fitz.open(stream=io.BytesIO(file_content), filetype="pdf") as doc:
        for page in doc:
            text.append(page.get_text())
    return "\n".join(text)


def extract_text_from_docx(file_content: bytes) -> str:
    """Extracts text from a DOCX file."""
    doc = docx.Document(io.BytesIO(file_content))
    return "\n".join([para.text for para in doc.paragraphs])


def extract_text_from_txt(file_content: bytes) -> str:
    """Extracts text from a TXT or MD file."""
    return file_content.decode("utf-8", errors="ignore")


def extract_text_from_file(file_content: bytes, file_type: str) -> str:
    """Dispatcher for text extraction."""
    if file_type == "pdf":
        return extract_text_from_pdf(file_content)
    elif file_type == "docx":
        return extract_text_from_docx(file_content)
    elif file_type in ["txt", "md"]:
        return extract_text_from_txt(file_content)
    return ""
