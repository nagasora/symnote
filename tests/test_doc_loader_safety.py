"""文書取り込みが、展開すると巨大になる DOCX でメモリを使い果たさないことを確かめる。"""

from __future__ import annotations

import io
import zipfile

import pytest

from symnote.core import doc_loader


def _zip(entries: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_docx_that_expands_too_much_is_rejected_before_parsing(monkeypatch) -> None:
    """展開後の合計が上限を超える DOCX は、python-docx に渡す前に拒否する。"""
    monkeypatch.setattr(doc_loader, "MAX_DOCX_UNCOMPRESSED_BYTES", 1024)
    bomb = _zip({"word/media/huge.bin": b"\0" * 4096})
    called = []
    monkeypatch.setattr(doc_loader.docx, "Document", lambda *_: called.append(1))

    with pytest.raises(doc_loader.UnsafeDocumentError):
        doc_loader.extract_text_from_file(bomb, "docx")
    assert called == []


def test_docx_with_too_many_entries_is_rejected(monkeypatch) -> None:
    """項目数が上限を超える DOCX は拒否する。"""
    monkeypatch.setattr(doc_loader, "MAX_DOCX_ENTRIES", 3)

    with pytest.raises(doc_loader.UnsafeDocumentError):
        doc_loader.check_docx_archive(_zip({f"part{i}.xml": b"x" for i in range(4)}))


def test_normal_docx_text_is_extracted() -> None:
    """通常の DOCX は今までどおり本文を取り出せる。"""
    import docx

    document = docx.Document()
    document.add_paragraph("資料の本文")
    buffer = io.BytesIO()
    document.save(buffer)

    assert "資料の本文" in doc_loader.extract_text_from_file(buffer.getvalue(), "docx")
