from __future__ import annotations

import fitz  # PyMuPDF
import io
import zipfile
from typing import List

import docx

def extract_text_from_pdf(file_content: bytes) -> str:
    """Extracts text from a PDF file."""
    text = []
    with fitz.open(stream=io.BytesIO(file_content), filetype="pdf") as doc:
        for page in doc:
            text.append(page.get_text())
    return "\n".join(text)


MAX_DOCX_ENTRIES = 2000
MAX_DOCX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024


class UnsafeDocumentError(ValueError):
    """展開すると大きすぎる（zip bomb の疑いがある）文書を表す。"""


def check_docx_archive(file_content: bytes) -> None:
    """DOCX（ZIP）の項目数と展開後の合計サイズが上限内かを、展開せずに確かめる。

    python-docx は参照されるパーツを丸ごと展開するため、読み込む前に弾く必要がある。
    """
    with zipfile.ZipFile(io.BytesIO(file_content)) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_DOCX_ENTRIES:
            raise UnsafeDocumentError("DOCX の項目が多すぎます。")
        if sum(entry.file_size for entry in entries) > MAX_DOCX_UNCOMPRESSED_BYTES:
            raise UnsafeDocumentError("DOCX の展開後のサイズが大きすぎます。")


def extract_text_from_docx(file_content: bytes) -> str:
    """Extracts text from a DOCX file."""
    check_docx_archive(file_content)
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
