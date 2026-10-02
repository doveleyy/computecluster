"""Bounded previews of dump entries.

Formats a browser renders safely are served raw. HTML and SVG are served
only under a sandboxing Content-Security-Policy (no scripts, no network), and
the page also shows them in a sandboxed frame or as an image. Everything else
is reduced on the server to bounded text, tables or listings, or to a preview
image that the file itself already contains (iWork, Office, OpenDocument
thumbnails). Nothing is converted with external software.

Every reader stops at a budget. A normal preview uses small budgets so even
huge files open quickly over the network; "load more" asks again with the
larger `FULL` budgets. Spreadsheets and Word documents are parsed as a stream,
so their size never matters beyond the rows actually shown.
"""

from __future__ import annotations

import csv
import email
import email.policy
import gzip
import io
import json
import os
import re
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any
from xml.etree import ElementTree


@dataclass(frozen=True)
class Limits:
    text_chars: int
    text_bytes: int
    table_bytes: int
    rows: int


PREVIEW = Limits(
    text_chars=20_000, text_bytes=64 * 1024, table_bytes=256 * 1024, rows=300
)
FULL = Limits(
    text_chars=400_000,
    text_bytes=2 * 1024 * 1024,
    table_bytes=4 * 1024 * 1024,
    rows=5_000,
)

MAX_XML_BYTES = 8 * 1024 * 1024
MAX_EMBEDDED_BYTES = 25 * 1024 * 1024
MAX_SHARED_STRINGS = 100_000
MAX_LISTING = 500
MAX_COLUMNS = 60
MAX_CELL = 200
MAX_SHEETS = 12
# Raw previews larger than this wait for an explicit "load" in the page.
LARGE_RAW_BYTES = 20 * 1024 * 1024

# Served raw: (preview mode, media type).
INLINE_TYPES = {
    ".pdf": ("pdf", "application/pdf"),
    ".png": ("image", "image/png"),
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".gif": ("image", "image/gif"),
    ".webp": ("image", "image/webp"),
    ".avif": ("image", "image/avif"),
    ".bmp": ("image", "image/bmp"),
    ".ico": ("image", "image/x-icon"),
    ".heic": ("image", "image/heic"),
    ".heif": ("image", "image/heif"),
    ".tif": ("image", "image/tiff"),
    ".tiff": ("image", "image/tiff"),
    ".svg": ("image", "image/svg+xml"),
    ".html": ("html", "text/html; charset=utf-8"),
    ".htm": ("html", "text/html; charset=utf-8"),
    ".mp4": ("video", "video/mp4"),
    ".mov": ("video", "video/quicktime"),
    ".m4v": ("video", "video/mp4"),
    ".webm": ("video", "video/webm"),
    ".ogv": ("video", "video/ogg"),
    ".mp3": ("audio", "audio/mpeg"),
    ".m4a": ("audio", "audio/mp4"),
    ".wav": ("audio", "audio/wav"),
    ".aac": ("audio", "audio/aac"),
    ".flac": ("audio", "audio/flac"),
    ".ogg": ("audio", "audio/ogg"),
    ".oga": ("audio", "audio/ogg"),
    ".opus": ("audio", "audio/ogg"),
}
# Audio and video stream with range requests, so size never blocks them.
STREAMED_MODES = {"video", "audio"}
# Documents that could run code are confined even when opened directly.
SANDBOXED_SUFFIXES = {".svg", ".html", ".htm"}
SANDBOX_POLICY = (
    "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
    "font-src data:"
)
TABLE_SUFFIXES = {".csv", ".tsv", ".tab"}
SPREADSHEET_SUFFIXES = {".xlsx", ".xlsm"}
WORD_SUFFIXES = {".docx", ".docm", ".dotx"}
SLIDES_SUFFIXES = {".pptx", ".pptm"}
IWORK_SUFFIXES = {".pages", ".numbers", ".key"}
OPENDOCUMENT_SUFFIXES = {".odt", ".ods", ".odp", ".odg"}
# Zip containers under another name: list what is inside.
ZIP_SUFFIXES = {".zip", ".npz", ".jar", ".whl", ".apk", ".ipa", ".xpi", ".cbz"}
TAR_SUFFIXES = (".tar", ".tgz", ".tar.gz", ".tbz2", ".tar.bz2", ".txz", ".tar.xz")
# Preview images a file may already carry, best first.
EMBEDDED_CANDIDATES = (
    "QuickLook/Preview.pdf",
    "preview.jpg",
    "preview-web.jpg",
    "QuickLook/Thumbnail.jpg",
    "preview-micro.jpg",
    "docProps/thumbnail.jpeg",
    "docProps/thumbnail.jpg",
    "docProps/thumbnail.png",
    "Thumbnails/thumbnail.png",
)
EMBEDDED_TYPES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def inline_type(path: Path) -> tuple[str, str] | None:
    return INLINE_TYPES.get(path.suffix.lower())


def raw_headers(path: Path) -> dict[str, str]:
    headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}
    if path.suffix.lower() in SANDBOXED_SUFFIXES:
        headers["Content-Security-Policy"] = SANDBOX_POLICY
    return headers


def _text(text: str, limits: Limits, cut: bool = False) -> dict[str, Any]:
    return {
        "mode": "text",
        "text": text[: limits.text_chars],
        "truncated": cut or len(text) > limits.text_chars,
    }


def _read(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        return stream.read(limit)


def _plain(path: Path, limits: Limits) -> tuple[str, bool]:
    data = _read(path, limits.text_bytes + 1)
    cut = len(data) > limits.text_bytes
    return data[: limits.text_bytes].decode("utf-8", errors="replace"), cut


def looks_like_text(data: bytes) -> bool:
    """Content sniffing for unknown extensions: no NULs, mostly printable."""
    if not data or b"\x00" in data:
        return False
    sample = data.decode("utf-8", errors="replace")
    bad = sum(
        1
        for char in sample
        if char == "�" or (ord(char) < 32 and char not in "\n\r\t\f")
    )
    return bad <= len(sample) * 0.02


# ---------- XML containers (Office, OpenDocument) ----------


def _xml(archive: zipfile.ZipFile, name: str) -> ElementTree.Element | None:
    if name not in archive.namelist():
        return None
    if archive.getinfo(name).file_size > MAX_XML_BYTES:
        raise ValueError("Document XML is too large to preview")
    return ElementTree.fromstring(archive.read(name))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _runs(element: ElementTree.Element) -> str:
    return "".join(
        child.text or "" for child in element.iter() if _local(child.tag) == "t"
    )


def _stream_paragraphs(
    stream: IO[bytes], paragraph: str, limits: Limits
) -> tuple[list[str], bool]:
    """Paragraph text from a large XML part without loading it whole."""
    lines: list[str] = []
    used = 0
    for _, element in ElementTree.iterparse(stream, events=("end",)):
        if _local(element.tag) != paragraph:
            continue
        text = _runs(element)
        element.clear()
        if text.strip():
            lines.append(text)
            used += len(text)
            if used > limits.text_chars:
                return lines, True
    return lines, False


def _docx(archive: zipfile.ZipFile, limits: Limits) -> tuple[str, bool]:
    if "word/document.xml" not in archive.namelist():
        return "", False
    with archive.open("word/document.xml") as stream:
        lines, cut = _stream_paragraphs(stream, "p", limits)
    return "\n".join(lines), cut


def _pptx(archive: zipfile.ZipFile, limits: Limits) -> tuple[str, bool]:
    def number(name: str) -> int:
        match = re.search(r"(\d+)\.xml$", name)
        return int(match.group(1)) if match else 0

    slides = sorted(
        (
            name
            for name in archive.namelist()
            if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
        ),
        key=number,
    )
    sections = []
    for index, name in enumerate(slides, start=1):
        with archive.open(name) as stream:
            lines, _ = _stream_paragraphs(stream, "p", limits)
        sections.append(f"— Slide {index} —\n" + "\n".join(lines))
        if sum(len(section) for section in sections) > limits.text_chars:
            return "\n\n".join(sections), True
    return "\n\n".join(sections), False


def _column(reference: str) -> int:
    letters = re.match(r"[A-Za-z]*", reference)
    index = 0
    for char in (letters.group(0) if letters else "").upper():
        index = index * 26 + ord(char) - 64
    return max(index - 1, 0)


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    shared: list[str] = []
    with archive.open("xl/sharedStrings.xml") as stream:
        for _, element in ElementTree.iterparse(stream, events=("end",)):
            if _local(element.tag) == "si":
                shared.append(_runs(element)[:MAX_CELL])
                element.clear()
                if len(shared) >= MAX_SHARED_STRINGS:
                    break
    return shared


def _sheet_rows(
    archive: zipfile.ZipFile, member: str, shared: list[str], limits: Limits
) -> tuple[list[list[str]], bool]:
    rows: list[list[str]] = []
    with archive.open(member) as stream:
        for _, row in ElementTree.iterparse(stream, events=("end",)):
            if _local(row.tag) != "row":
                continue
            if len(rows) >= limits.rows:
                return rows, True
            cells: list[str] = []
            for cell in row:
                if _local(cell.tag) != "c":
                    continue
                reference = cell.get("r")
                index = _column(reference) if reference else len(cells)
                if index >= MAX_COLUMNS:
                    continue
                kind = cell.get("t")
                value = ""
                if kind == "inlineStr":
                    value = _runs(cell)
                else:
                    for child in cell:
                        if _local(child.tag) == "v" and child.text is not None:
                            value = child.text
                    if kind == "s" and value.isdigit() and int(value) < len(shared):
                        value = shared[int(value)]
                    elif kind == "b":
                        value = "TRUE" if value == "1" else "FALSE"
                cells.extend([""] * (index - len(cells)))
                cells.append(value[:MAX_CELL])
            rows.append(cells)
            row.clear()
    return rows, False


def _xlsx(archive: zipfile.ZipFile, limits: Limits) -> dict[str, Any]:
    targets: dict[str, str] = {}
    relations = _xml(archive, "xl/_rels/workbook.xml.rels")
    if relations is not None:
        for relation in relations:
            target = relation.get("Target", "").lstrip("/")
            targets[relation.get("Id", "")] = (
                target if target.startswith("xl/") else f"xl/{target}"
            )
    entries: list[tuple[str, str]] = []
    workbook = _xml(archive, "xl/workbook.xml")
    if workbook is not None:
        for element in workbook.iter():
            if _local(element.tag) != "sheet":
                continue
            relation_id = next(
                (value for key, value in element.attrib.items() if _local(key) == "id"),
                "",
            )
            entries.append((element.get("name", "Sheet"), targets.get(relation_id, "")))
    if not entries:
        entries = [("Sheet1", "xl/worksheets/sheet1.xml")]
    shared = _shared_strings(archive)
    names = set(archive.namelist())
    sheets = []
    for name, target in entries[:MAX_SHEETS]:
        if target not in names:
            continue
        rows, truncated = _sheet_rows(archive, target, shared, limits)
        sheets.append({"name": name, "rows": rows, "truncated": truncated})
    return {
        "mode": "table",
        "sheets": sheets,
        "more_sheets": max(len(entries) - MAX_SHEETS, 0),
        "note": "Dates show as Excel serial numbers; formulas show their last value.",
    }


def _opendocument(archive: zipfile.ZipFile, limits: Limits) -> tuple[str, bool]:
    if "content.xml" not in archive.namelist():
        return "", False
    lines: list[str] = []
    used = 0
    with archive.open("content.xml") as stream:
        for _, element in ElementTree.iterparse(stream, events=("end",)):
            if _local(element.tag) not in {"p", "h"}:
                continue
            text = "".join(element.itertext())
            element.clear()
            if text.strip():
                lines.append(text)
                used += len(text)
                if used > limits.text_chars:
                    return "\n".join(lines), True
    return "\n".join(lines), False


def _epub(archive: zipfile.ZipFile, limits: Limits) -> tuple[str, bool]:
    names = archive.namelist()
    order: list[str] = []
    container = _xml(archive, "META-INF/container.xml")
    if container is not None:
        rootfile = next(
            (
                e.get("full-path")
                for e in container.iter()
                if _local(e.tag) == "rootfile"
            ),
            None,
        )
        package = _xml(archive, rootfile) if rootfile else None
        if package is not None and rootfile:
            base = rootfile.rpartition("/")[0]
            manifest = {
                item.get("id"): item.get("href", "")
                for item in package.iter()
                if _local(item.tag) == "item"
            }
            for itemref in package.iter():
                if _local(itemref.tag) == "itemref":
                    href = manifest.get(itemref.get("idref"), "")
                    order.append(f"{base}/{href}" if base else href)
    if not order:
        order = sorted(
            name for name in names if name.endswith((".xhtml", ".html", ".htm"))
        )
    parts: list[str] = []
    for name in order[:200]:
        if name not in names or archive.getinfo(name).file_size > MAX_XML_BYTES:
            continue
        text = re.sub(r"<[^>]+>", " ", archive.read(name).decode("utf-8", "replace"))
        text = re.sub(r"[ \t\r\f]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()
        if text:
            parts.append(text)
        if sum(len(part) for part in parts) > limits.text_chars:
            return "\n\n".join(parts), True
    return "\n\n".join(parts), False


# ---------- embedded preview images ----------


def _embedded_from_zip(archive: zipfile.ZipFile) -> str | None:
    names = set(archive.namelist())
    for candidate in EMBEDDED_CANDIDATES:
        if (
            candidate in names
            and archive.getinfo(candidate).file_size <= MAX_EMBEDDED_BYTES
        ):
            return candidate
    return None


def _embedded_from_bundle(path: Path) -> str | None:
    for candidate in EMBEDDED_CANDIDATES:
        member = path.joinpath(*candidate.split("/"))
        if (
            member.is_file()
            and not member.is_symlink()
            and member.stat().st_size <= MAX_EMBEDDED_BYTES
        ):
            return candidate
    return None


def embedded_bytes(path: Path, member: str) -> tuple[bytes, str]:
    """The bytes of a preview image stored inside a document, by known name."""
    if member not in EMBEDDED_CANDIDATES:
        raise ValueError("Not a known preview image name")
    media_type = EMBEDDED_TYPES[Path(member).suffix.lower()]
    if path.is_dir():
        target = path.joinpath(*member.split("/"))
        if (
            target.is_symlink()
            or not target.is_file()
            or not target.resolve().is_relative_to(path.resolve())
            or target.stat().st_size > MAX_EMBEDDED_BYTES
        ):
            raise ValueError("This document has no such preview")
        return target.read_bytes(), media_type
    with zipfile.ZipFile(path) as archive:
        if member not in archive.namelist():
            raise ValueError("This document has no such preview")
        if archive.getinfo(member).file_size > MAX_EMBEDDED_BYTES:
            raise ValueError("Preview image is too large")
        return archive.read(member), media_type


def _with_embedded(
    member: str | None, limits: Limits, text: str = "", cut: bool = False
) -> dict[str, Any]:
    if member is None:
        return _text(text, limits, cut) if text else {"mode": "none"}
    return {
        "mode": "embedded",
        "member": member,
        "media_type": EMBEDDED_TYPES[Path(member).suffix.lower()],
        "text": text[: limits.text_chars],
        "truncated": cut or len(text) > limits.text_chars,
    }


# ---------- tables, archives, mail, rich text ----------


def _delimited(path: Path, limits: Limits) -> dict[str, Any]:
    data = _read(path, limits.table_bytes + 1)
    cut = len(data) > limits.table_bytes
    text = data[: limits.table_bytes].decode("utf-8-sig", errors="replace")
    if cut:
        text = text.rpartition("\n")[0]
    if path.suffix.lower() in {".tsv", ".tab"}:
        delimiter = "\t"
    else:
        try:
            delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    rows: list[list[str]] = []
    for row in csv.reader(io.StringIO(text), delimiter=delimiter):
        if len(rows) >= limits.rows:
            cut = True
            break
        rows.append([cell[:MAX_CELL] for cell in row[:MAX_COLUMNS]])
    return {
        "mode": "table",
        "sheets": [{"name": path.name, "rows": rows, "truncated": cut}],
        "more_sheets": 0,
    }


def _tar_listing(path: Path) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    more = False
    with tarfile.open(path, "r:*") as archive:
        for member in archive:
            if len(items) >= MAX_LISTING:
                more = True
                break
            items.append(
                {
                    "path": member.name,
                    "kind": "folder" if member.isdir() else "file",
                    "size": None if member.isdir() else member.size,
                }
            )
    return {"mode": "listing", "total": len(items), "more": more, "items": items}


def _gzip(path: Path, limits: Limits) -> dict[str, Any]:
    with gzip.open(path, "rb") as stream:
        data = stream.read(limits.text_bytes + 1)
    if looks_like_text(data[:8192]):
        result = _text(
            data[: limits.text_bytes].decode("utf-8", errors="replace"),
            limits,
            len(data) > limits.text_bytes,
        )
        result["note"] = "Decompressed beginning of the file."
        return result
    return {"mode": "none", "error": "Compressed binary data"}


def _email(path: Path) -> str:
    message = email.message_from_bytes(
        _read(path, 2 * 1024 * 1024), policy=email.policy.default
    )
    header = "\n".join(
        f"{name}: {message[name]}"
        for name in ("From", "To", "Cc", "Date", "Subject")
        if message[name]
    )
    body = ""
    part = message.get_body(preferencelist=("plain", "html"))
    if part is not None:
        body = part.get_content()
        if part.get_content_type() == "text/html":
            body = re.sub(r"[ \t]+", " ", re.sub(r"<[^>]+>", " ", body))
    attachments = [
        attachment.get_filename() or "unnamed"
        for attachment in message.iter_attachments()
    ]
    footer = f"\n\nAttachments: {', '.join(attachments)}" if attachments else ""
    return f"{header}\n\n{body.strip()}{footer}"


RTF_SKIP = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "header", "footer"}
RTF_TOKEN = re.compile(
    r"\\'([0-9a-fA-F]{2})"  # hex-escaped byte
    r"|\\u(-?\d+)\??"  # unicode escape with its fallback character
    r"|\\([a-z]+)(-?\d+)? ?"  # control word
    r"|\\(.)"  # control symbol
    r"|([{}])"  # group
    r"|([^\\{}]+)"  # text
)


def _rtf(path: Path, limits: Limits) -> str:
    """Plain text from RTF: keep text, drop control words and hidden groups."""
    source = _read(path, limits.table_bytes).decode("latin-1")
    out: list[str] = []
    skipping: list[bool] = [False]
    group_start = False
    for match in RTF_TOKEN.finditer(source):
        hexed, unicode, word, _, symbol, brace, text = match.groups()
        if brace == "{":
            skipping.append(skipping[-1])
            group_start = True
            continue
        if brace == "}":
            if len(skipping) > 1:
                skipping.pop()
            continue
        if group_start and (symbol == "*" or (word and word in RTF_SKIP)):
            skipping[-1] = True
        group_start = False
        if skipping[-1]:
            continue
        if hexed:
            out.append(bytes([int(hexed, 16)]).decode("cp1252", errors="replace"))
        elif unicode:
            out.append(chr(int(unicode) % 65536))
        elif word in {"par", "line"}:
            out.append("\n")
        elif word == "tab":
            out.append("\t")
        elif symbol in {"\\", "{", "}"}:
            out.append(symbol)
        elif text:
            out.append(text.replace("\r", "").replace("\n", ""))
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


def _notebook(path: Path, limits: Limits) -> tuple[str, bool]:
    if path.stat().st_size > MAX_XML_BYTES:
        return _plain(path, limits)
    cells = json.loads(path.read_text(encoding="utf-8", errors="replace")).get(
        "cells", []
    )
    parts = []
    for cell in cells:
        source = cell.get("source", "")
        text = "".join(source) if isinstance(source, list) else str(source)
        parts.append(f"[{cell.get('cell_type', 'cell')}]\n{text}")
    return "\n\n".join(parts), False


# ---------- listings ----------


def _zip_listing(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        return {
            "mode": "listing",
            "total": len(members),
            "items": [
                {
                    "path": member.filename,
                    "kind": "folder" if member.is_dir() else "file",
                    "size": None if member.is_dir() else member.file_size,
                }
                for member in members[:MAX_LISTING]
            ],
        }


def folder_listing(path: Path) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    total = 0
    for current, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not name.startswith("."))
        base = Path(current)
        for name in [*dirs, *sorted(files)]:
            if name.startswith("."):
                continue
            total += 1
            if len(items) < MAX_LISTING:
                child = base / name
                is_dir = name in dirs
                items.append(
                    {
                        "path": str(child.relative_to(path)),
                        "kind": "folder" if is_dir else "file",
                        "size": None if is_dir else child.lstat().st_size,
                    }
                )
        if total > MAX_LISTING * 4:
            break
    return {"mode": "listing", "total": total, "items": items}


# ---------- dispatch ----------


def describe(path: Path, full: bool = False) -> dict[str, Any]:
    """Return how the browser should preview one dump entry.

    `full` raises every budget for an explicit "load more"; it never changes
    what kind of preview is produced.
    """
    limits = FULL if full else PREVIEW
    suffix = path.suffix.lower()
    if path.is_dir():
        # Older iWork documents are folder bundles that carry their own preview.
        if suffix in IWORK_SUFFIXES:
            member = _embedded_from_bundle(path)
            if member:
                return _with_embedded(member, limits)
        return folder_listing(path)
    try:
        return _describe_file(path, suffix, limits)
    except (
        OSError,
        ValueError,
        LookupError,
        EOFError,
        zipfile.BadZipFile,
        tarfile.TarError,
        csv.Error,
        ElementTree.ParseError,
    ):
        return {"mode": "none", "error": "This file could not be read for preview"}


def _describe_file(path: Path, suffix: str, limits: Limits) -> dict[str, Any]:
    inline = inline_type(path)
    if inline:
        result: dict[str, Any] = {"mode": inline[0], "media_type": inline[1]}
        if suffix in SANDBOXED_SUFFIXES:
            source, cut = _plain(path, limits)
            result["source"] = source[: limits.text_chars]
            result["truncated"] = cut or len(source) > limits.text_chars
        if inline[0] not in STREAMED_MODES and path.stat().st_size > LARGE_RAW_BYTES:
            # The page asks before fetching; nothing large crosses the network
            # just because an entry reached the front of the queue.
            result["large"] = True
        return result
    if suffix in {".md", ".markdown"}:
        text, cut = _plain(path, limits)
        result = _text(text, limits, cut)
        result["mode"] = "markdown"
        return result
    if suffix in TABLE_SUFFIXES:
        return _delimited(path, limits)
    if suffix in SPREADSHEET_SUFFIXES:
        with zipfile.ZipFile(path) as archive:
            return _xlsx(archive, limits)
    if suffix in WORD_SUFFIXES or suffix in SLIDES_SUFFIXES:
        with zipfile.ZipFile(path) as archive:
            reader = _docx if suffix in WORD_SUFFIXES else _pptx
            text, cut = reader(archive, limits)
            return _with_embedded(_embedded_from_zip(archive), limits, text, cut)
    if suffix in OPENDOCUMENT_SUFFIXES:
        with zipfile.ZipFile(path) as archive:
            text, cut = _opendocument(archive, limits)
            return _with_embedded(_embedded_from_zip(archive), limits, text, cut)
    if suffix in IWORK_SUFFIXES:
        with zipfile.ZipFile(path) as archive:
            result = _with_embedded(_embedded_from_zip(archive), limits)
        if result["mode"] == "none":
            result["error"] = "This document carries no preview image"
        return result
    if suffix == ".epub":
        with zipfile.ZipFile(path) as archive:
            text, cut = _epub(archive, limits)
        return _text(text, limits, cut)
    if suffix == ".ipynb":
        text, cut = _notebook(path, limits)
        return _text(text, limits, cut)
    if suffix in ZIP_SUFFIXES:
        return _zip_listing(path)
    if path.name.lower().endswith(TAR_SUFFIXES):
        return _tar_listing(path)
    if suffix == ".gz":
        try:
            return _tar_listing(path)
        except tarfile.ReadError:
            return _gzip(path, limits)
    if suffix == ".eml":
        return _text(_email(path), limits)
    if suffix == ".rtf":
        return _text(_rtf(path, limits), limits)
    # Any other extension, or none: show it if the bytes are plainly text.
    if looks_like_text(_read(path, 8192)):
        text, cut = _plain(path, limits)
        return _text(text, limits, cut)
    return {"mode": "none"}
