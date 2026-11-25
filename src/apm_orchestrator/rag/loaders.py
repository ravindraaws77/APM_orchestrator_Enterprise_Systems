"""Turn raw files into ``Document``s of numbered pages.

Supported: plain text, markdown (one page each) and PDF (one page per
PDF page, via ``pypdf`` from the ``rag`` extra). Anything else is
skipped with a reason rather than guessed at -- a Google-native Doc,
for instance, has no bytes the Drive connector can return as-is.

``load_drive_documents`` reads through apm_connectors' read-only Drive
routes (``drive_list_files`` / ``drive_read_file``) on whatever client
it is given; it never imports that repo, and never writes.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass, field
from typing import Any, Protocol

TEXT_MIME_TYPES = {"text/plain", "text/markdown", "text/x-markdown"}
PDF_MIME_TYPE = "application/pdf"
_TEXT_EXTENSIONS = (".txt", ".md", ".markdown")


@dataclass(frozen=True)
class Page:
    number: int  # 1-based
    text: str


@dataclass(frozen=True)
class Document:
    doc_id: str
    name: str
    mime_type: str
    pages: list[Page]
    source: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SkippedFile:
    doc_id: str
    name: str
    reason: str


class UnsupportedDocument(ValueError):
    pass


def _resolve_kind(name: str, mime_type: str) -> str:
    mime = (mime_type or "").split(";")[0].strip().lower()
    if mime == PDF_MIME_TYPE or name.lower().endswith(".pdf"):
        return "pdf"
    if mime in TEXT_MIME_TYPES or name.lower().endswith(_TEXT_EXTENSIONS):
        return "text"
    raise UnsupportedDocument(f"unsupported mime type {mime_type!r} for {name!r}")


def _decode_text(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _pdf_pages(data: bytes) -> list[Page]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - depends on installed extras
        raise UnsupportedDocument("PDF support needs the 'rag' extra (pypdf)") from exc
    reader = PdfReader(io.BytesIO(data))
    return [Page(number=i, text=(p.extract_text() or "")) for i, p in enumerate(reader.pages, start=1)]


def load_bytes(doc_id: str, name: str, mime_type: str, data: bytes, source: dict[str, Any] | None = None) -> Document:
    """Load one file. Raises ``UnsupportedDocument`` for unknown types."""
    kind = _resolve_kind(name, mime_type)
    pages = _pdf_pages(data) if kind == "pdf" else [Page(number=1, text=_decode_text(data))]
    return Document(doc_id=doc_id, name=name, mime_type=mime_type, pages=pages, source=source or {})


class DriveReader(Protocol):
    async def drive_list_files(
        self, name_contains: str | None = None, max_results: int = 20, process_id: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def drive_read_file(self, file_id: str, process_id: str | None = None) -> dict[str, Any]: ...


async def load_drive_documents(
    client: DriveReader,
    name_contains: str | None = None,
    max_files: int = 10,
    process_id: str | None = None,
) -> tuple[list[Document], list[SkippedFile]]:
    """List and load matching Drive files. A file that can't be read or
    parsed is reported in the second list, never raised -- one bad
    attachment must not hide the others."""
    listed = await client.drive_list_files(name_contains=name_contains, max_results=max_files, process_id=process_id)
    docs: list[Document] = []
    skipped: list[SkippedFile] = []
    for meta in listed or []:
        file_id = str(meta.get("file_id", ""))
        name = str(meta.get("name", file_id))
        mime = str(meta.get("mime_type", ""))
        try:
            _resolve_kind(name, mime)
            payload = await client.drive_read_file(file_id, process_id=process_id)
            data = base64.b64decode(payload.get("content_base64") or "")
            doc = load_bytes(
                doc_id=file_id,
                name=payload.get("name", name),
                mime_type=payload.get("mime_type", mime),
                data=data,
                source={"web_view_link": meta.get("web_view_link"), "modified_time": meta.get("modified_time")},
            )
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            skipped.append(SkippedFile(doc_id=file_id, name=name, reason=f"{type(exc).__name__}: {exc}"))
            continue
        if not any(p.text.strip() for p in doc.pages):
            skipped.append(SkippedFile(doc_id=file_id, name=name, reason="no extractable text (scanned PDF?)"))
            continue
        docs.append(doc)
    return docs, skipped
