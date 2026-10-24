import io
import re
import zipfile
import xml.etree.ElementTree as ElementTree

DOC_EXT = (".txt", ".md", ".docx")
MAX_CHARS = 40000

WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


class DocumentError(ValueError):
    pass


def _word_tag(name):
    return WORD_NAMESPACE + name


def _decode_text(raw):
    if raw[:2] in UTF16_BOMS:
        encodings = ("utf-8-sig", "utf-16")
    else:
        encodings = ("utf-8-sig", "cp1251")

    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _paragraph_text(paragraph):
    parts = []
    for element in paragraph.iter():
        if element.tag == _word_tag("t") and element.text:
            parts.append(element.text)
        elif element.tag == _word_tag("tab"):
            parts.append("\t")
        elif element.tag in (_word_tag("br"), _word_tag("cr")):
            parts.append("\n")

    text = "".join(parts).strip()
    list_marker_path = "./%s/%s" % (_word_tag("pPr"), _word_tag("numPr"))
    if text and paragraph.find(list_marker_path) is not None:
        text = "• " + text
    return text


def _table_lines(table):
    lines = []
    for row in table.iter(_word_tag("tr")):
        cells = []
        for cell in row.findall(_word_tag("tc")):
            paragraph_texts = []
            for paragraph in cell.iter(_word_tag("p")):
                paragraph_text = _paragraph_text(paragraph)
                if paragraph_text:
                    paragraph_texts.append(paragraph_text)
            cells.append(" ".join(paragraph_texts))

        if any(cells):
            lines.append("| " + " | ".join(cells) + " |")
    return lines


def _docx_text(raw):
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
        document_xml = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError):
        raise DocumentError("Файл не похож на DOCX (старый .doc не поддерживается — сохраните как .docx)")

    body = ElementTree.fromstring(document_xml).find(_word_tag("body"))
    if body is None:
        return ""

    lines = []
    for block in body:
        if block.tag == _word_tag("p"):
            text = _paragraph_text(block)
            if text:
                lines.append(text)
        elif block.tag == _word_tag("tbl"):
            lines.extend(_table_lines(block))
    return "\n".join(lines)


def _normalize(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extension(name):
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()


def extract_text(name, raw):
    extension = _extension(name)
    raw = raw or b""

    if extension in (".txt", ".md"):
        text = _decode_text(raw)
    elif extension == ".docx":
        text = _docx_text(raw)
    else:
        raise DocumentError("Неподдерживаемый формат документа: %s" % name)

    text = _normalize(text)
    if not text:
        raise DocumentError("В документе «%s» не найден текст" % name)

    truncated = len(text) > MAX_CHARS
    if truncated:
        text = text[:MAX_CHARS]

    return {
        "name": name,
        "text": text,
        "chars": len(text),
        "truncated": truncated,
    }
