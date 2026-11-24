"""Document attachments to Markdown or CSV text, with page and figure images.

Every converter is deterministic for the same bytes and library versions and
returns sections of ``[kind, text]`` parts: ``text`` parts may be truncated
to fit the message, ``link`` parts (asset lines) are always kept.
"""

import codecs
import csv
import io
import re
import shutil
import subprocess
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from .assets import StoreError

# Bump when a converter's output changes, so cached results are redone when
# enrichment is enabled. Cache-only runs keep using older results.
REVISION = 1

EXTENSIONS = {
    "txt": "text",
    "text": "text",
    "log": "text",
    "md": "markdown",
    "markdown": "markdown",
    "csv": "csv",
    "tsv": "csv",
    "pdf": "pdf",
    "docx": "docx",
    "xlsx": "xlsx",
    "xlsm": "xlsx",
    "html": "html",
    "htm": "html",
    "xhtml": "html",
    "odt": "odt",
    "pptx": "pptx",
}
MIMES = {
    "text/plain": "text",
    "text/markdown": "markdown",
    "text/x-markdown": "markdown",
    "text/csv": "csv",
    "text/tab-separated-values": "csv",
    "application/csv": "csv",
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel.sheet.macroenabled.12": "xlsx",
    "text/html": "html",
    "application/xhtml+xml": "html",
    "application/vnd.oasis.opendocument.text": "odt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}
TEXT_KINDS = {"text", "markdown", "csv", "html"}
BINARY_KINDS = {"pdf", "docx", "xlsx", "odt", "pptx"}
IMAGE_TYPES = {
    "PNG": "png",
    "JPEG": "jpg",
    "GIF": "gif",
    "WEBP": "webp",
    "BMP": "png",
    "TIFF": "png",
}


class ConversionError(Exception):
    """Permanent: the file cannot be converted (status failed)."""


class Encrypted(ConversionError):
    pass


@dataclass
class Limits:
    max_pages: int = 500
    max_image_pages: int = 40
    max_images: int = 40
    max_cells: int = 500_000
    max_unzipped: int = 200 * 1024 * 1024
    min_image_side: int = 64
    render_long_side: int = 1600


def safe_name(value):
    value = " ".join(clean(str(value or "")).split())[:200]
    return value or "file"


def candidate(name, mime):
    """Kind to convert from file name and declared type, before any download."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in (name or "") else ""
    if ext in EXTENSIONS:
        return EXTENSIONS[ext]
    mime = (mime or "").split(";")[0].strip().lower()
    return MIMES.get(mime)


def sniff(data, hinted):
    """Confirm or correct the kind from content; None when unsupported."""
    head = data[:8192]
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
                if "word/document.xml" in names:
                    return "docx"
                if "xl/workbook.xml" in names:
                    return "xlsx"
                if "ppt/presentation.xml" in names:
                    return "pptx"
                if "mimetype" in names and archive.read("mimetype").startswith(
                    b"application/vnd.oasis.opendocument.text"
                ):
                    return "odt"
        except (zipfile.BadZipFile, KeyError, OSError):
            return None
        return None
    lowered = head.lstrip()[:200].lower()
    looks_html = lowered.startswith((b"<!doctype html", b"<html"))
    if hinted in BINARY_KINDS:
        if looks_html:
            return None  # A login or error page served in place of a document.
        hinted = None  # Named like a document but not one: maybe text.
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return hinted or "text"
    if b"\x00" in head:
        return None
    if hinted is None and looks_html:
        return "html"
    if hinted is None:
        try:
            head.decode("utf-8")
        except UnicodeDecodeError as error:
            if error.start < len(head) - 4:
                return None
        return "text"
    return hinted


def clean(text):
    """Normalize newlines; drop control characters (NUL above all) and lone
    surrogates, keeping format characters such as emoji joiners."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        ch
        for ch in text
        if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cs")
    )


def collapse(text):
    lines = [line.rstrip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def decode(data):
    if data.startswith(codecs.BOM_UTF8):
        return clean(data[3:].decode("utf-8", "replace"))
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return clean(data.decode("utf-16", "replace"))
    try:
        return clean(data.decode("utf-8"))
    except UnicodeDecodeError:
        return clean(data.decode("cp1252", "replace"))


def markdown_section(name, text, parts=None):
    return {
        "header": f"attachment {name}: Markdown-converted content",
        "parts": parts if parts is not None else [["text", text]],
    }


def csv_section(name, text, sheet=None):
    label = f"{name} sheet {safe_name(sheet)}" if sheet is not None else name
    return {
        "header": f"attachment {label}: CSV-converted content",
        "parts": [["text", text]],
    }


def html_to_markdown(html):
    from bs4 import BeautifulSoup
    from markdownify import markdownify

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(
        ["script", "style", "noscript", "template", "iframe", "object", "svg", "head"]
    ):
        tag.decompose()
    for img in soup.find_all("img"):
        if str(img.get("src", "")).startswith("data:"):
            img.replace_with(img.get("alt") or "")
    text = markdownify(
        str(soup),
        heading_style="ATX",
        bullets="-",
        escape_underscores=False,
        escape_asterisks=False,
        keep_inline_images_in=[
            "td",
            "th",
            "li",
            "a",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        ],
    )
    return collapse(clean(text))


def split_links(text, links):
    """Lines that are only an asset line become link parts."""
    parts, buffer = [], []
    for line in text.split("\n"):
        if line.strip() in links:
            if buffer and "\n".join(buffer).strip():
                parts.append(["text", "\n".join(buffer).strip()])
            buffer = []
            parts.append(["link", line.strip()])
        else:
            buffer.append(line)
    if "\n".join(buffer).strip():
        parts.append(["text", "\n".join(buffer).strip()])
    return parts or [["text", "(no text)"]]


def store_image(blob, store, limits):
    """Store a figure in a browser-friendly format; None when too small/unknown."""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(blob)) as image:
            fmt = IMAGE_TYPES.get(image.format or "")
            if not fmt or min(image.size) < limits.min_image_side:
                return None
            if image.format in {"BMP", "TIFF"}:
                out = io.BytesIO()
                image.save(out, "PNG")
                blob = out.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None
    return store.put(blob, fmt)


def check_zip(data, limits):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(i.file_size for i in archive.infolist()) > limits.max_unzipped:
                raise ConversionError("archive expands beyond the size cap")
    except zipfile.BadZipFile:
        raise ConversionError("damaged archive") from None


def page_needs_image(chars, coverage, paths):
    """Keep a page image for figures, scans, photos and diagrams.

    coverage is the page fraction under raster images; paths counts vector
    drawing objects. A text page with a small logo stays text only, and so
    does a text-heavy page whose drawing objects are table rules, underlines
    or cell shading: the bar for vector drawings rises with the extractable
    text (one more object per 10 characters), so a chart or diagram with
    short labels still qualifies.
    """
    if coverage >= 0.25 or paths >= 40 + chars // 10:
        return True
    return chars < 80 and (coverage >= 0.02 or paths >= 8)


def pdf(name, data, store, limits):
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    try:
        document = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as error:
        if "password" in str(error).lower():
            raise Encrypted("password protected") from None
        raise ConversionError("unreadable PDF") from None
    parts, assets = [], []
    try:
        total = len(document)
        for index in range(min(total, limits.max_pages)):
            page = document[index]
            try:
                textpage = page.get_textpage()
                text = collapse(clean(textpage.get_text_range()))
                textpage.close()
                width, height = page.get_size()
                area = max(width * height, 1.0)
                covered, paths = 0.0, 0
                for count, obj in enumerate(
                    page.get_objects(
                        filter=[raw.FPDF_PAGEOBJ_IMAGE, raw.FPDF_PAGEOBJ_PATH],
                        max_depth=4,
                    )
                ):
                    if count >= 2000:
                        break
                    if obj.type == raw.FPDF_PAGEOBJ_IMAGE:
                        left, bottom, right, top = obj.get_bounds()
                        covered += max(right - left, 0) * max(top - bottom, 0)
                    else:
                        paths += 1
                number = index + 1
                if total > 1:
                    parts.append(["text", f"### Page {number}"])
                if text:
                    parts.append(["text", text])
                if (
                    page_needs_image(len(text), min(covered / area, 1.0), paths)
                    and len(assets) < limits.max_image_pages
                ):
                    scale = min(2.0, limits.render_long_side / max(width, height, 1))
                    image = page.render(scale=scale).to_pil().convert("RGB")
                    out = io.BytesIO()
                    image.save(out, "JPEG", quality=80)
                    asset = store.put(out.getvalue(), "jpg")
                    assets.append(
                        {"role": "page_image", "page": number, **link_of(asset)}
                    )
                    parts.append(
                        [
                            "link",
                            f"attachment {name} page {number} image: {asset['link']}",
                        ]
                    )
                elif not text:
                    parts.append(["text", "(no extractable text)"])
            finally:
                page.close()
        if total > limits.max_pages:
            parts.append(
                ["text", f"[{total - limits.max_pages} more pages not converted]"]
            )
    finally:
        document.close()
    return {
        "sections": [markdown_section(name, None, parts or [["text", "(empty PDF)"]])],
        "assets": assets,
        "pages": total,
    }


def link_of(asset):
    return {"link": asset["link"], "sha256": asset["sha256"]}


def soffice():
    return shutil.which("soffice") or shutil.which("libreoffice")


def docx_via_pdf(name, data, store, limits, binary):
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "document.docx"
        source.write_bytes(data)
        try:
            subprocess.run(
                [
                    binary,
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    tmp,
                    str(source),
                ],
                check=True,
                capture_output=True,
                timeout=300,
            )
            converted = (Path(tmp) / "document.pdf").read_bytes()
        except (subprocess.SubprocessError, OSError):
            return None
    result = pdf(name, converted, store, limits)
    result["renderer"] = "soffice"
    return result


def docx(name, data, store, limits):
    check_zip(data, limits)
    binary = soffice()
    if binary:
        result = docx_via_pdf(name, data, store, limits, binary)
        if result is not None:
            return result
    import mammoth

    assets, by_source = [], {}

    def image(element):
        try:
            with element.open() as handle:
                blob = handle.read()
        except Exception:
            return {"src": ""}
        if len(assets) >= limits.max_images:
            return {"src": ""}
        asset = store_image(blob, store, limits)
        if asset is None:
            return {"src": ""}
        if asset["link"] not in by_source:
            number = len(assets) + 1
            by_source[asset["link"]] = (
                f"attachment {name} image {number}: {asset['link']}"
            )
            assets.append({"role": "embedded_image", "index": number, **link_of(asset)})
        return {"src": asset["link"]}

    try:
        html = mammoth.convert_to_html(
            io.BytesIO(data), convert_image=mammoth.images.img_element(image)
        ).value
    except StoreError:
        raise
    except Exception:
        raise ConversionError("unreadable Word document") from None
    text = html_to_markdown(html)

    def replace(match):
        return by_source.get(match.group(1), "")

    text = re.sub(r"!\[[^\]]*\]\(([^)\s]*)(?:\s+\"[^\"]*\")?\)", replace, text)
    return {
        "sections": [
            markdown_section(name, None, split_links(text, set(by_source.values())))
        ],
        "assets": assets,
        "renderer": "embedded_images",
    }


def cell(value):
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def xlsx(name, data, store, limits):
    check_zip(data, limits)
    import openpyxl

    try:
        workbook = openpyxl.load_workbook(
            io.BytesIO(data), read_only=True, data_only=True
        )
    except Exception:
        raise ConversionError("unreadable workbook") from None
    sections, cells = [], 0
    try:
        for sheet in workbook.worksheets:
            rows, stopped = [], False
            for values in sheet.iter_rows(values_only=True):
                row = [clean(cell(v)) for v in values]
                while row and row[-1] == "":
                    row.pop()
                cells += len(row)
                if cells > limits.max_cells:
                    stopped = True
                    break
                rows.append(row)
            while rows and not rows[-1]:
                rows.pop()
            out = io.StringIO()
            csv.writer(out, lineterminator="\n").writerows(rows)
            text = out.getvalue().rstrip("\n") or "(empty sheet)"
            if stopped:
                text += "\n[workbook cell limit reached; remaining rows not converted]"
            sections.append(csv_section(name, text, sheet.title))
            if stopped:
                break
    finally:
        workbook.close()
    return {"sections": sections or [csv_section(name, "(no sheets)")], "assets": []}


def tabular(name, data):
    text = decode(data)
    if name.lower().endswith(".tsv"):
        out = io.StringIO()
        csv.writer(out, lineterminator="\n").writerows(
            csv.reader(io.StringIO(text), delimiter="\t")
        )
        text = out.getvalue()
    return {
        "sections": [csv_section(name, text.strip("\n") or "(empty)")],
        "assets": [],
    }


def zipped_xml(data, member, limits):
    check_zip(data, limits)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        try:
            return ElementTree.fromstring(archive.read(member))
        except (KeyError, ElementTree.ParseError):
            raise ConversionError("unreadable document") from None


def odt(name, data, store, limits):
    ns = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
    root = zipped_xml(data, "content.xml", limits)
    lines = []
    for element in root.iter():
        if element.tag in (ns + "h", ns + "p"):
            text = clean("".join(element.itertext())).strip()
            if text:
                level = element.get(ns + "outline-level")
                if element.tag == ns + "h":
                    lines.append("#" * min(int(level or 1), 6) + " " + text)
                else:
                    lines.append(text)
    return {
        "sections": [markdown_section(name, "\n\n".join(lines) or "(no text)")],
        "assets": [],
    }


def pptx(name, data, store, limits):
    check_zip(data, limits)
    ns = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        slides = sorted(
            (int(m.group(1)), n)
            for n in archive.namelist()
            if (m := re.fullmatch(r"ppt/slides/slide(\d+)\.xml", n))
        )
        blocks = []
        for number, member in slides:
            try:
                root = ElementTree.fromstring(archive.read(member))
            except ElementTree.ParseError:
                continue
            paragraphs = [
                clean("".join(t.text or "" for t in p.iter(ns + "t"))).strip()
                for p in root.iter(ns + "p")
            ]
            body = "\n".join(p for p in paragraphs if p)
            blocks.append(f"### Slide {number}" + ("\n\n" + body if body else ""))
    return {
        "sections": [markdown_section(name, "\n\n".join(blocks) or "(no slides)")],
        "assets": [],
    }


def convert(name, kind, data, store, limits=None):
    """{sections: [{header, parts}], assets: [...], ...} for one file."""
    limits = limits or Limits()
    name = safe_name(name)
    if kind in ("text", "markdown"):
        text = decode(data)
        return {
            "sections": [markdown_section(name, text.strip("\n") or "(empty)")],
            "assets": [],
        }
    if kind == "csv":
        return tabular(name, data)
    if kind == "html":
        return {
            "sections": [
                markdown_section(name, html_to_markdown(decode(data)) or "(no text)")
            ],
            "assets": [],
        }
    converters = {"pdf": pdf, "docx": docx, "xlsx": xlsx, "odt": odt, "pptx": pptx}
    if kind not in converters:
        raise ConversionError("unsupported kind")
    return converters[kind](name, data, store, limits)
