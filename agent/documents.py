# -*- coding: utf-8 -*-
"""Извлечение текста из прикреплённых документов (описания задач печати).

DOCX и TXT/MD разбираются стандартной библиотекой Python.
"""
import io
import re
import zipfile
import xml.etree.ElementTree as ET

DOC_EXT = (".txt", ".md", ".docx")
MAX_CHARS = 40000

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class DocumentError(Exception):
    pass


def _decode_text(raw):
    for encoding in ("utf-8-sig", "utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _paragraph_text(p):
    parts = []
    for el in p.iter():
        if el.tag == _W + "t" and el.text:
            parts.append(el.text)
        elif el.tag == _W + "tab":
            parts.append("\t")
        elif el.tag in (_W + "br", _W + "cr"):
            parts.append("\n")
    text = "".join(parts).strip()
    if text and p.find("./%spPr/%snumPr" % (_W, _W)) is not None:
        text = "• " + text  # элемент списка
    return text


def _docx(raw):
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
        xml = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError):
        raise DocumentError("Файл не похож на DOCX (старый .doc не поддерживается — сохраните как .docx)")
    body = ET.fromstring(xml).find(_W + "body")
    if body is None:
        return ""
    lines = []
    for block in body:
        if block.tag == _W + "p":
            text = _paragraph_text(block)
            if text:
                lines.append(text)
        elif block.tag == _W + "tbl":
            for row in block.iter(_W + "tr"):
                cells = []
                for cell in row.findall(_W + "tc"):
                    cells.append(" ".join(t for t in (_paragraph_text(p) for p in cell.iter(_W + "p")) if t))
                if any(cells):
                    lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def extract_text(name, raw):
    """Возвращает {"name", "text", "chars", "truncated"}."""
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if ext in (".txt", ".md"):
        text = _decode_text(raw or b"")
    elif ext == ".docx":
        text = _docx(raw or b"")
    else:
        raise DocumentError("Неподдерживаемый формат документа: %s" % name)
    text = re.sub(r"[ \t]+\n", "\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise DocumentError("В документе «%s» не найден текст" % name)
    truncated = len(text) > MAX_CHARS
    if truncated:
        text = text[:MAX_CHARS]
    return {"name": name, "text": text, "chars": len(text), "truncated": truncated}
