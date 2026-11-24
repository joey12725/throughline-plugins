"""Attachments, Instagram links and reactions, on generated fixtures only.

Every document is built here from code, every download is a fake transport or
a local test server, and Instagram/transcription go through fake fetchers. No
real Instagram, Discord or owner content is used.
"""

import copy
import io
import json
import os
import shutil
import subprocess
import threading
import zipfile
import zlib
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from throughline_sdk import Checkpoint
from throughline_sdk.contract import check_plugin

from throughline_beeper import Beeper, enrichment_flags
from throughline_beeper import convert as converters
from throughline_beeper import media
from throughline_beeper.assets import LocalAssetStore
from throughline_beeper.enrich import Enricher, attachment_key, instagram_links
from throughline_beeper.mapping import CONTENT_LIMIT, archive_message, live_message
from throughline_beeper.reactions import line, normalize

RECORDINGS = Path(__file__).parent / "recorded"


# Generated documents ------------------------------------------------------------


def png(width=200, height=120, color=(30, 120, 200)):
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), color)
    ImageDraw.Draw(image).rectangle(
        [width // 8, height // 8, width // 2, height // 2], fill=(250, 200, 0)
    )
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def make_pdf(pages):
    """Tiny valid PDF; each page is {"text": str}, {"image": True}, {"paths": n}."""
    objects = {3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"}
    kids, number = [], 3
    for page in pages:
        page_id, content_id = number + 1, number + 2
        number += 2
        resources = b"/Font << /F1 3 0 R >>"
        stream = b""
        if page.get("text"):
            ops = ["BT /F1 12 Tf 72 720 Td 14 TL"]
            for text_line in page["text"].split("\n"):
                escaped = (
                    text_line.replace("\\", "\\\\")
                    .replace("(", "\\(")
                    .replace(")", "\\)")
                )
                ops.append(f"({escaped}) Tj T*")
            ops.append("ET")
            stream += "\n".join(ops).encode("latin-1")
        if page.get("paths"):
            # Separate stroked rectangles: table rules, bars or diagram boxes.
            stream += b"".join(
                b"\n%d %d 20 6 re S" % (60 + (i % 20) * 24, 60 + (i // 20) * 10)
                for i in range(page["paths"])
            )
        if page.get("image"):
            number += 1
            width = height = 32
            pixels = bytes(
                (x * 8 % 256, y * 8 % 256, 128)[c]
                for y in range(height)
                for x in range(width)
                for c in range(3)
            )
            data = zlib.compress(pixels)
            objects[number] = (
                (
                    f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height}"
                    f" /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode"
                    f" /Length {len(data)} >>\nstream\n"
                ).encode()
                + data
                + b"\nendstream"
            )
            resources += f" /XObject << /Im1 {number} 0 R >>".encode()
            stream += b"\nq 500 0 0 650 56 70 cm /Im1 Do Q"
        objects[content_id] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        )
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
            f" /Resources << {resources.decode()} >> /Contents {content_id} 0 R >>"
        ).encode()
        kids.append(page_id)
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>"
    ).encode()
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = {}
    for key in sorted(objects):
        offsets[key] = out.tell()
        out.write(f"{key} 0 obj\n".encode() + objects[key] + b"\nendobj\n")
    xref = out.tell()
    size = max(objects) + 1
    out.write(f"xref\n0 {size}\n0000000000 65535 f \n".encode())
    for key in range(1, size):
        out.write(f"{offsets.get(key, 0):010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NAMESPACES = (
    f'xmlns:w="{W}"'
    ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    ' xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"'
    ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    ' xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'
)


def drawing(rid):
    return (
        '<w:r><w:drawing><wp:inline><wp:extent cx="952500" cy="571500"/>'
        '<wp:docPr id="1" name="Picture"/><a:graphic><a:graphicData'
        ' uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic>'
        '<pic:nvPicPr><pic:cNvPr id="0" name="figure.png"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{rid}"/></pic:blipFill><pic:spPr/></pic:pic>'
        "</a:graphicData></a:graphic></wp:inline></w:drawing></w:r>"
    )


def make_docx(paragraphs, images=()):
    """paragraphs: str, "[image N]" for the N-th image, or a list of rows (table)."""
    body = []
    for item in paragraphs:
        if isinstance(item, list):
            rows = "".join(
                "<w:tr>"
                + "".join(
                    f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in row
                )
                + "</w:tr>"
                for row in item
            )
            body.append(f"<w:tbl>{rows}</w:tbl>")
        elif item.startswith("[image "):
            body.append(f"<w:p>{drawing('rIdImg' + item[7:-1])}</w:p>")
        else:
            body.append(f'<w:p><w:r><w:t xml:space="preserve">{item}</w:t></w:r></w:p>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        z.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        )
        rels = "".join(
            f'<Relationship Id="rIdImg{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image{i}.png"/>'
            for i in range(1, len(images) + 1)
        )
        z.writestr(
            "word/_rels/document.xml.rels",
            f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>',
        )
        for i, blob in enumerate(images, 1):
            z.writestr(f"word/media/image{i}.png", blob)
        z.writestr(
            "word/document.xml",
            f'<?xml version="1.0" encoding="UTF-8"?><w:document {NAMESPACES}><w:body>{"".join(body)}</w:body></w:document>',
        )
    return out.getvalue()


def make_xlsx():
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Invented budget"
    sheet.append(["item", "cost"])
    sheet.append(["fake apples", 3])
    sheet.append(["fake pears, ripe", 4.5])
    other = workbook.create_sheet("Notes")
    other.append(["synthetic note"])
    workbook.create_sheet("Empty")
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()


def make_odt():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        z.writestr(
            "content.xml",
            '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
            ' xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:text>'
            '<text:h text:outline-level="2">Invented heading</text:h><text:p>Synthetic <text:span>paragraph</text:span></text:p>'
            "</office:text></office:body></office:document-content>",
        )
    return out.getvalue()


def make_pptx():
    out = io.BytesIO()
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("ppt/presentation.xml", "<p:presentation xmlns:p='x'/>")
        for number, words in (
            (2, "Second invented slide"),
            (1, "First invented slide"),
        ):
            z.writestr(
                f"ppt/slides/slide{number}.xml",
                f'<p:sld xmlns:p="x" xmlns:a="{a}"><a:p><a:r><a:t>{words}</a:t></a:r></a:p></p:sld>',
            )
    return out.getvalue()


# Reactions -----------------------------------------------------------------------


def archive_row(**changes):
    row = json.loads((RECORDINGS / "archive.json").read_text())["messages"][1]
    row.update(changes)
    return row


def test_reactions_every_source_shape():
    discord = [
        {
            "emoji": {"id": "", "name": "\U0001f44d"},
            "count": 3,
            "users": [
                {"id": "fake-1", "name": "Invented Alex"},
                {"id": "fake-2", "nickname": "Sam"},
            ],
        },
        {"emoji": {"id": "123", "name": "fakeparrot"}, "count": 1},
    ]
    assert normalize(discord) == [
        {"emoji": "\U0001f44d", "sender_id": "fake-1", "sender_name": "Invented Alex"},
        {"emoji": "\U0001f44d", "sender_id": "fake-2", "sender_name": "Sam"},
        {"emoji": "\U0001f44d", "count": 1},
        {"emoji": ":fakeparrot:"},
    ]
    meta = [{"reaction": "❤️", "actor": "Invented Alex", "timestamp": 1577880000}]
    assert normalize(meta) == [
        {
            "emoji": "❤️",
            "sender_name": "Invented Alex",
            "at": "2020-01-01T12:00:00+00:00",
        }
    ]
    desktop = [
        {
            "id": "r1",
            "participantID": "@fake:beeper.invalid",
            "reactionKey": "\U0001f602",
            "emoji": True,
        }
    ]
    assert normalize(desktop) == [
        {"emoji": "\U0001f602", "sender_id": "@fake:beeper.invalid"}
    ]
    assert normalize([{"count": 2}, "x", 5, None, {"emoji": ""}]) == [{"emoji": "x"}]
    assert normalize("not a list") == []
    assert line(normalize(discord + meta)) == (
        "reactions: \U0001f44d Invented Alex, \U0001f44d Sam, \U0001f44d, :fakeparrot:, ❤️ Invented Alex"
    )
    assert line([{"emoji": "x", "count": 4}]) == "reactions: x ×4"
    assert line([]) == ""
    long = line([{"emoji": "\U0001f44d", "sender_name": "n" * 300}] * 80)
    assert len(long.encode()) <= 4000 and long.startswith("reactions: ")


def test_reactions_in_content_and_field_without_touching_unreacted_rows():
    plain = archive_message(archive_row())
    assert "reaction_details" not in plain["fields"]
    assert plain["content"] == "Invented orchard 1"
    row = archive_row(
        reactions_json=json.dumps(
            [
                {
                    "reaction": "❤️",
                    "actor": "Invented Sender",
                    "timestamp": "2020-01-02T03:04:05Z",
                }
            ]
        )
    )

    def resolve(rid, name):
        return rid or "fake-sender", name, "user_id"

    mapped = archive_message(row, resolve=resolve)
    assert mapped["content"] == "Invented orchard 1\n\nreactions: ❤️ Invented Sender"
    assert mapped["fields"]["reaction_details"] == [
        {
            "emoji": "❤️",
            "sender_id": "fake-sender",
            "sender_kind": "user_id",
            "sender_name": "Invented Sender",
            "at": "2020-01-02T03:04:05+00:00",
        }
    ]
    assert mapped["fields"]["reactions"] == json.loads(row["reactions_json"])
    bare = archive_message(archive_row(reactions_json=json.dumps("\U0001f525")))
    assert bare["content"].endswith("\n\nreactions: \U0001f525")


def test_archive_reactor_names_resolve_to_sender_ids(
    monkeypatch, tmp_path, synthetic_archive
):
    import psycopg

    data = json.loads((RECORDINGS / "archive.json").read_text())
    messages = [m for m in data["messages"] if m["platform"] == "instagram"]
    messages[1].update(sender_id="fake-other", sender_name="Invented Other")
    messages[3].update(
        reactions_json=json.dumps(
            [
                {"reaction": "\U0001f44d", "actor": "Invented Other"},
                {"reaction": "\U0001f44e", "actor": "Unknown Name"},
            ]
        )
    )
    data["messages"] = messages
    path = tmp_path / "reactions.json"
    path.write_text(json.dumps(data))
    monkeypatch.setenv("BEEPER_SOURCE", "archive")

    def check(records):
        got = {r["external_id"]: r for r in records if isinstance(r, dict)}
        details = got["instagram:fake-3"]["fields"]["reaction_details"]
        assert details == [
            {
                "emoji": "\U0001f44d",
                "sender_id": "fake-other",
                "sender_kind": "user_id",
                "sender_name": "Invented Other",
            },
            {"emoji": "\U0001f44e", "sender_name": "Unknown Name"},
        ]
        assert got["instagram:fake-3"]["content"].endswith(
            "reactions: \U0001f44d Invented Other, \U0001f44e Unknown Name"
        )

    monkeypatch.setenv("BEEPER_RECORDING", str(path))
    check(list(Beeper().records({}, "backfill")))
    with psycopg.connect(synthetic_archive) as conn:
        columns = [
            d.name
            for d in conn.execute("SELECT * FROM archive.messages LIMIT 0").description
        ]
        conn.execute("DELETE FROM archive.messages")
        for row in messages:
            conn.execute(
                "INSERT INTO archive.messages VALUES ("
                + ",".join(["%s"] * len(columns))
                + ")",
                [row.get(c) for c in columns],
            )
    monkeypatch.delenv("BEEPER_RECORDING")
    monkeypatch.setenv("BEEPER_ARCHIVE_DSN", synthetic_archive)
    check(list(Beeper().records({}, "backfill")))


def test_live_reactions_use_chat_participants():
    mapped = live_message(
        {
            "id": "1",
            "accountID": "fake-account",
            "chatID": "fake-chat",
            "timestamp": "2020-01-01T10:00:00+00:00",
            "text": "synthetic",
            "reactions": [
                {
                    "id": "r",
                    "participantID": "@fake-sam:beeper.invalid",
                    "reactionKey": "\U0001f44d",
                }
            ],
        },
        {
            "id": "fake-chat",
            "type": "group",
            "participants": {
                "items": [
                    {"id": "@fake-sam:beeper.invalid", "fullName": "Invented Sam"}
                ]
            },
        },
        {"network": "discord"},
    )
    assert mapped["content"] == "synthetic\n\nreactions: \U0001f44d Invented Sam"
    assert mapped["fields"]["reaction_details"] == [
        {
            "emoji": "\U0001f44d",
            "sender_id": "@fake-sam:beeper.invalid",
            "sender_kind": "beeper_user_id",
            "sender_name": "Invented Sam",
        }
    ]


# Converters ----------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return LocalAssetStore(tmp_path / "assets", "throughline-asset://beeper")


def text_of(result):
    return "\n".join(
        "\n".join([s["header"]] + [v for _, v in s["parts"]])
        for s in result["sections"]
    )


def test_detection_by_extension_mime_and_content():
    assert converters.candidate("Report.PDF", None) == "pdf"
    assert converters.candidate("notes", "text/markdown; charset=utf-8") == "markdown"
    assert converters.candidate("book.xlsm", None) == "xlsx"
    assert converters.candidate("photo.jpg", "image/jpeg") is None
    assert converters.candidate("blob", None) is None
    assert converters.sniff(make_pdf([{"text": "x"}]), "text") == "pdf"
    assert converters.sniff(make_docx(["x"]), None) == "docx"
    assert converters.sniff(make_xlsx(), "csv") == "xlsx"
    assert converters.sniff(make_odt(), None) == "odt"
    assert converters.sniff(make_pptx(), None) == "pptx"
    # An HTML error or login page served under a document name is not converted.
    assert converters.sniff(b"<!DOCTYPE html><p>expired</p>", "pdf") is None
    assert converters.sniff(b"<html><p>page</p></html>", "docx") is None
    assert converters.sniff(b"<!DOCTYPE html><p>saved page</p>", None) == "html"
    assert converters.sniff(b"<html><p>saved page</p>", "html") == "html"
    assert converters.sniff(b"\x89PNG\r\n\x1a\n\x00\x00", "text") is None
    assert converters.sniff(b"PK\x03\x04garbage", "docx") is None
    assert converters.sniff(b"plain words", "markdown") == "markdown"


def test_text_markdown_csv_and_html(store):
    result = converters.convert("a.txt", "text", b"caf\xe9 \x00line\r\nnext", store)
    assert (
        text_of(result)
        == "attachment a.txt: Markdown-converted content\ncafé line\nnext"
    )
    md = converters.convert(
        "b.md",
        "markdown",
        "﻿# Title\n\n\U0001f469‍\U0001f4bb ok".encode("utf-8-sig"),
        store,
    )
    assert "# Title" in text_of(md) and "\U0001f469‍\U0001f4bb" in text_of(md)
    csv_result = converters.convert("c.csv", "csv", b"a,b\n1,2\n", store)
    assert text_of(csv_result) == "attachment c.csv: CSV-converted content\na,b\n1,2"
    tsv = converters.convert("d.tsv", "csv", b"a\tb,c\n1\t2\n", store)
    assert text_of(tsv).endswith('a,"b,c"\n1,2')
    html = converters.convert(
        "e.html",
        "html",
        b"<html><head><title>t</title><style>p{}</style></head><body><h1>Invented</h1>"
        b"<script>alert(1)</script><p>Hello <b>fake</b> <img src='data:image/png;base64,AAAA' alt='pic'></p>"
        b"<table><tr><th>k</th></tr><tr><td>v</td></tr></table></body></html>",
        store,
    )
    out = text_of(html)
    assert out.startswith("attachment e.html: Markdown-converted content\n# Invented")
    assert (
        "alert" not in out
        and "base64" not in out
        and "**fake**" in out
        and "| k |" in out
    )


def test_xlsx_every_sheet_named_as_csv(store):
    out = text_of(converters.convert("budget.xlsx", "xlsx", make_xlsx(), store))
    assert out == (
        "attachment budget.xlsx sheet Invented budget: CSV-converted content\n"
        'item,cost\nfake apples,3\n"fake pears, ripe",4.5\n'
        "attachment budget.xlsx sheet Notes: CSV-converted content\nsynthetic note\n"
        "attachment budget.xlsx sheet Empty: CSV-converted content\n(empty sheet)"
    )
    limited = converters.convert(
        "budget.xlsx", "xlsx", make_xlsx(), store, converters.Limits(max_cells=3)
    )
    assert len(limited["sections"]) == 1
    assert "cell limit reached" in text_of(limited)


def test_docx_markdown_with_figure_links_and_tiny_images_skipped(store, monkeypatch):
    monkeypatch.setattr(converters, "soffice", lambda: None)
    blob = make_docx(
        [
            "Invented intro",
            "[image 1]",
            [["k", "v"], ["fake", "1"]],
            "[image 2]",
            "Outro",
        ],
        images=[png(), png(16, 16)],
    )
    result = converters.convert("plan.docx", "docx", blob, store)
    link = result["assets"][0]["link"]
    assert [a["role"] for a in result["assets"]] == ["embedded_image"]
    assert result["renderer"] == "embedded_images"
    parts = result["sections"][0]["parts"]
    assert ["link", f"attachment plan.docx image 1: {link}"] in parts
    out = text_of(result)
    assert out.startswith(
        "attachment plan.docx: Markdown-converted content\nInvented intro"
    )
    assert "| k | v |" in out and out.endswith("Outro")
    assert store.path(link).read_bytes()[:4] == b"\x89PNG"
    assert "![" not in out


def test_docx_uses_soffice_pdf_pages_when_available(store, monkeypatch):
    seen = []

    def fake(name, data, store_, limits, binary):
        seen.append(binary)
        result = converters.pdf(name, make_pdf([{"image": True}]), store_, limits)
        result["renderer"] = "soffice"
        return result

    monkeypatch.setattr(converters, "soffice", lambda: "soffice-fake")
    monkeypatch.setattr(converters, "docx_via_pdf", fake)
    result = converters.convert("plan.docx", "docx", make_docx(["x"]), store)
    assert seen == ["soffice-fake"] and result["renderer"] == "soffice"
    assert "attachment plan.docx page 1 image: throughline-asset://beeper/" in text_of(
        result
    )


def test_pdf_text_pages_stay_text_and_visual_pages_get_images(store):
    blob = make_pdf(
        [
            {"text": "Invented report\nwith several words of text " * 3},
            {"image": True},
            {"text": "Caption under a figure", "image": True},
            {},
        ]
    )
    result = converters.convert("report.pdf", "pdf", blob, store)
    assert result["pages"] == 4
    assert [(a["role"], a["page"]) for a in result["assets"]] == [
        ("page_image", 2),
        ("page_image", 3),
    ]
    out = text_of(result)
    assert out.startswith(
        "attachment report.pdf: Markdown-converted content\n### Page 1\nInvented report"
    )
    assert f"attachment report.pdf page 2 image: {result['assets'][0]['link']}" in out
    assert "Caption under a figure" in out
    assert out.endswith("### Page 4\n(no extractable text)")
    assert store.path(result["assets"][0]["link"]).read_bytes()[:2] == b"\xff\xd8"
    again = converters.convert("report.pdf", "pdf", blob, store)
    assert again == result  # Deterministic rendering: same names, same text.
    capped = converters.convert(
        "report.pdf",
        "pdf",
        blob,
        store,
        converters.Limits(max_pages=1, max_image_pages=0),
    )
    assert "[3 more pages not converted]" in text_of(capped) and not capped["assets"]
    with pytest.raises(converters.ConversionError):
        converters.convert("bad.pdf", "pdf", b"%PDF-1.4 broken", store)


def test_page_image_heuristic():
    assert converters.page_needs_image(5000, 0.5, 0)  # photo page with text
    assert converters.page_needs_image(300, 0.0, 120)  # vector chart, short labels
    assert converters.page_needs_image(3000, 0.0, 400)  # dense labelled diagram
    assert not converters.page_needs_image(3000, 0.0, 120)  # text page with a table
    assert not converters.page_needs_image(2000, 0.0, 60)  # underlines and rules
    assert converters.page_needs_image(10, 0.05, 0)  # scan or figure, no text layer
    assert not converters.page_needs_image(3000, 0.03, 4)  # text page with a logo
    assert not converters.page_needs_image(0, 0.0, 0)  # blank


def test_odt_and_pptx_text(store):
    assert text_of(converters.convert("a.odt", "odt", make_odt(), store)).endswith(
        "## Invented heading\n\nSynthetic paragraph"
    )
    out = text_of(converters.convert("s.pptx", "pptx", make_pptx(), store))
    assert out.index("Slide 1") < out.index("Slide 2")
    assert "First invented slide" in out


# Enrichment through the plugin -----------------------------------------------------


class Server:
    """Fake HTTP origin counting requests; maps URL path to (status, bytes)."""

    def __init__(self, files):
        self.files, self.requests = files, []

    def __call__(self, request):
        self.requests.append(str(request.url))
        status, body = self.files.get(request.url.path, (404, b"gone"))
        return httpx.Response(status, content=body)


def attachment_fixture(tmp_path, attachments_by_row, bodies=None):
    data = json.loads((RECORDINGS / "archive.json").read_text())
    base = data["messages"][1]
    data["messages"] = [
        dict(
            base,
            id=f"discord:fake-att-{i}",
            body=(bodies or {}).get(i, "Invented"),
            sent_at=f"2020-01-0{i + 1}T00:00:00Z",
            reply_to="",
            attachments_json=json.dumps(attachments),
        )
        for i, attachments in enumerate(attachments_by_row)
    ]
    path = tmp_path / "attachments.json"
    path.write_text(json.dumps(data))
    return path


def archive_plugin(monkeypatch, recording, server=None, **env):
    monkeypatch.setenv("BEEPER_SOURCE", "archive")
    monkeypatch.setenv("BEEPER_RECORDING", str(recording))
    for key in (
        "BEEPER_ATTACHMENTS",
        "BEEPER_INSTAGRAM_LINKS",
        "BEEPER_ATTACHMENT_LIMIT",
        "BEEPER_INSTAGRAM_LIMIT",
        "BEEPER_ATTACHMENT_MAX_BYTES",
    ):
        monkeypatch.delenv(key, raising=False)  # Each run states its own flags.
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    plugin = Beeper()
    if server is not None:
        plugin.enrich_options = {"transport": httpx.MockTransport(server)}
    return plugin


def messages(plugin):
    return {
        r["external_id"]: r
        for r in plugin.records({}, "backfill")
        if isinstance(r, dict)
    }


def cdn(name, att_id):
    return {
        "id": att_id,
        "fileName": name,
        "url": f"https://cdn.fake.invalid/attachments/1/{att_id}/{name}?ex=signed",
        "proxy_url": f"https://media.fake.invalid/attachments/1/{att_id}/{name}?ex=signed",
    }


def test_attachments_converted_cached_and_stable(
    monkeypatch, tmp_path, isolated_enrichment
):
    server = Server(
        {
            "/attachments/1/11/report.pdf": (
                200,
                make_pdf([{"text": "Invented findings"}, {"image": True}]),
            ),
            "/attachments/1/12/budget.xlsx": (200, make_xlsx()),
            "/attachments/1/13/old.txt": (404, b""),
            "/attachments/1/14/flaky.md": (503, b""),
            "/attachments/1/15/photo.jpg": (200, png()),
        }
    )
    recording = attachment_fixture(
        tmp_path,
        [
            [cdn("report.pdf", "11"), cdn("photo.jpg", "15")],
            [cdn("budget.xlsx", "12"), cdn("old.txt", "13")],
            [cdn("flaky.md", "14"), {"fileName": "ig-note.txt", "id": "16"}],
        ],
    )
    off = messages(archive_plugin(monkeypatch, recording, server))
    assert not server.requests  # Off by default: nothing fetched, nothing changed.
    assert all("attachment_files" not in m["fields"] for m in off.values())

    plugin = archive_plugin(monkeypatch, recording, server, BEEPER_ATTACHMENTS="1")
    first = messages(plugin)
    assert plugin.enrichment["attachments_converted"] == 2
    report = first["discord:fake-att-0"]
    files = report["fields"]["attachment_files"]
    assert [f["name"] for f in files] == ["report.pdf"]  # The photo is not a document.
    assert files[0]["status"] == "converted" and files[0]["pages"] == 2
    page_image = files[0]["assets"][0]["link"]
    assert report["content"].startswith(
        "Invented\n\nattachment report.pdf: Markdown-converted content\n### Page 1\nInvented findings"
    )
    assert f"attachment report.pdf page 2 image: {page_image}" in report["content"]
    assets = isolated_enrichment / "assets"
    assert (assets / page_image.rsplit("/", 1)[1]).exists()
    assert (assets / (files[0]["sha256"] + ".pdf")).exists()
    budget = first["discord:fake-att-1"]
    assert (
        "attachment budget.xlsx sheet Invented budget: CSV-converted content"
        in budget["content"]
    )
    assert [f["status"] for f in budget["fields"]["attachment_files"]] == [
        "converted",
        "unavailable",
    ]
    assert "HTTP 404" in budget["fields"]["attachment_files"][1]["error"]
    # A retryable failure is not rendered until final: no version per attempt.
    flaky = first["discord:fake-att-2"]["fields"]["attachment_files"]
    assert [(f["status"], f.get("attempts")) for f in flaky] == [("no_source", None)]
    # The signed CDN URL failed, then the proxy URL was tried too.
    assert sum("flaky.md" in u for u in server.requests) == 2

    # Rerun: nothing but the transient failure is fetched again; same output.
    server.requests.clear()
    second = messages(
        archive_plugin(monkeypatch, recording, server, BEEPER_ATTACHMENTS="1")
    )
    assert all("flaky.md" in u for u in server.requests)
    assert second["discord:fake-att-0"] == first["discord:fake-att-0"]
    assert second["discord:fake-att-1"] == first["discord:fake-att-1"]
    assert second["discord:fake-att-2"] == first["discord:fake-att-2"]

    # Switched off again (or dry-run): cached results still render, no fetches.
    server.requests.clear()
    cached = messages(archive_plugin(monkeypatch, recording, server))
    assert not server.requests
    assert cached["discord:fake-att-0"] == first["discord:fake-att-0"]
    dry = archive_plugin(monkeypatch, recording, server, BEEPER_ATTACHMENTS="1")
    dry.dry_run = True
    assert messages(dry)["discord:fake-att-1"] == first["discord:fake-att-1"]
    assert not server.requests

    # A third failure is final and shown once: no more retries.
    third = messages(
        archive_plugin(monkeypatch, recording, server, BEEPER_ATTACHMENTS="1")
    )
    assert [
        (f["status"], f.get("attempts"))
        for f in third["discord:fake-att-2"]["fields"]["attachment_files"]
    ] == [("error", 3), ("no_source", None)]
    server.requests.clear()
    messages(archive_plugin(monkeypatch, recording, server, BEEPER_ATTACHMENTS="1"))
    assert not server.requests


def test_per_run_limit_and_size_cap_resume(monkeypatch, tmp_path):
    server = Server(
        {f"/attachments/1/{i}/n{i}.txt": (200, b"synthetic %d" % i) for i in range(4)}
    )
    recording = attachment_fixture(
        tmp_path,
        [[cdn(f"n{i}.txt", str(i))] for i in range(3)]
        + [[dict(cdn("n3.txt", "3"), fileSizeBytes=10_000_000)]],
    )
    env = dict(
        BEEPER_ATTACHMENTS="1",
        BEEPER_ATTACHMENT_LIMIT="2",
        BEEPER_ATTACHMENT_MAX_BYTES="1000",
    )

    def converted(result):
        return sorted(
            k
            for k, m in result.items()
            if "attachment_files" in m["fields"]
            and m["fields"]["attachment_files"][0]["status"] == "converted"
        )

    first = messages(archive_plugin(monkeypatch, recording, server, **env))
    # Newest first: the per-run limit goes to the two most recent messages.
    assert converted(first) == ["discord:fake-att-1", "discord:fake-att-2"]
    # The declared size is above the cap: refused without any download, free.
    assert (
        first["discord:fake-att-3"]["fields"]["attachment_files"][0]["status"]
        == "too_large"
    )
    assert not any("n3.txt" in u for u in server.requests)
    second = messages(archive_plugin(monkeypatch, recording, server, **env))
    assert converted(second) == [f"discord:fake-att-{i}" for i in range(3)]
    assert (
        "attachment n2.txt: Markdown-converted content\nsynthetic 2"
        in second["discord:fake-att-2"]["content"]
    )


def test_long_conversions_truncate_with_marker_and_keep_full_text(
    monkeypatch, tmp_path
):
    words = " ".join(f"fakeword{i}" for i in range(30_000))  # ~300 kB
    server = Server(
        {
            "/attachments/1/1/long.txt": (200, words.encode()),
            "/attachments/1/2/short.csv": (200, b"a,b\n1,2\n"),
        }
    )
    recording = attachment_fixture(
        tmp_path,
        [[cdn("long.txt", "1"), cdn("short.csv", "2")]],
        bodies={0: "x" * 50_000},
    )
    result = messages(
        archive_plugin(monkeypatch, recording, server, BEEPER_ATTACHMENTS="1")
    )
    mapped = result["discord:fake-att-0"]
    assert len(mapped["content"].encode()) <= CONTENT_LIMIT
    assert "attachment short.csv: CSV-converted content\na,b\n1,2" in mapped["content"]
    assert "full text in field attachment_files]" in mapped["content"]
    long_field = mapped["fields"]["attachment_files"][0]
    assert long_field["truncated"] and long_field["text"].endswith("fakeword29999")
    assert "truncated" not in mapped["fields"]["attachment_files"][1]
    # A body that already fills the budget keeps the conversion in fields only.
    recording = attachment_fixture(
        tmp_path, [[cdn("short.csv", "2")]], bodies={0: "y" * 99_990}
    )
    full = messages(
        archive_plugin(monkeypatch, recording, server, BEEPER_ATTACHMENTS="1")
    )
    assert full["discord:fake-att-0"]["content"] == "y" * 99_990
    assert (
        full["discord:fake-att-0"]["fields"]["attachment_files"][0]["status"]
        == "converted"
    )


def test_conversion_crash_is_recorded_not_fatal(monkeypatch, tmp_path):
    server = Server({"/attachments/1/1/a.pdf": (200, make_pdf([{"text": "x"}]))})
    recording = attachment_fixture(tmp_path, [[cdn("a.pdf", "1")]])

    def boom(*args, **kwargs):
        raise MemoryError("invented")

    monkeypatch.setattr(converters, "pdf", boom)
    result = messages(
        archive_plugin(monkeypatch, recording, server, BEEPER_ATTACHMENTS="1")
    )
    field = result["discord:fake-att-0"]["fields"]["attachment_files"][0]
    assert field["status"] == "failed" and field["error"] == "MemoryError"
    assert result["discord:fake-att-0"]["content"] == "Invented"


def test_cache_keys_ignore_signed_query_and_are_scoped():
    a = cdn("r.pdf", "")
    b = dict(a, url=a["url"].replace("signed", "resigned"))
    assert attachment_key("discord", "m", 0, a) == attachment_key("discord", "m2", 3, b)
    assert attachment_key("discord", "m", 0, {"id": 5}) != attachment_key(
        "sms", "m", 0, {"id": 5}
    )
    assert attachment_key("sms", "m", 0, {"fileName": "x"}) != attachment_key(
        "sms", "m", 1, {"fileName": "x"}
    )


def test_live_desktop_assets_via_file_and_mxc(monkeypatch, tmp_path):
    folder = tmp_path / "desktop-media"
    folder.mkdir()
    (folder / "notes.md").write_bytes(b"# Invented notes")
    (folder / "sheet.csv").write_bytes(b"x,y\n")
    live = json.loads((RECORDINGS / "live.json").read_text())
    page = live["responses"][0]["body"]
    page["items"][0]["attachments"] = [
        {
            "id": "fake-a",
            "fileName": "notes.md",
            "srcURL": (folder / "notes.md").as_uri(),
        },
        {"id": "fake-b", "fileName": "sheet.csv", "srcURL": "mxc://fake.invalid/sheet"},
        {"id": "fake-c", "fileName": "lost.txt", "srcURL": "mxc://fake.invalid/lost"},
    ]
    live["responses"] += [
        {
            "path": "v1/assets/download",
            "params": {"url": "mxc://fake.invalid/sheet"},
            "body": {"srcURL": (folder / "sheet.csv").as_uri()},
        },
        {
            "path": "v1/assets/download",
            "params": {"url": "mxc://fake.invalid/lost"},
            "body": {"error": "not downloadable"},
        },
    ]
    path = tmp_path / "live.json"
    path.write_text(json.dumps(live))
    monkeypatch.setenv("BEEPER_SOURCE", "live")
    monkeypatch.setenv("BEEPER_RECORDING", str(path))
    monkeypatch.setenv("BEEPER_ATTACHMENTS", "1")
    result = messages(Beeper())
    mapped = next(m for m in result.values() if m["fields"].get("attachment_files"))
    assert (
        "attachment notes.md: Markdown-converted content\n# Invented notes"
        in mapped["content"]
    )
    assert "attachment sheet.csv: CSV-converted content\nx,y" in mapped["content"]
    assert [f["status"] for f in mapped["fields"]["attachment_files"]] == [
        "converted",
        "converted",
        "unavailable",
    ]


def test_archive_never_reads_local_paths(tmp_path):
    secret = tmp_path / "local.txt"
    secret.write_text("must not be read")
    enricher = Enricher({"attachments": "1", "state_dir": str(tmp_path / "s")})
    try:
        out = enricher(
            "discord",
            "m",
            "",
            [{"fileName": "local.txt", "url": secret.as_uri()}],
            50_000,
        )
    finally:
        enricher.close()
    assert out["fields"]["attachment_files"][0]["status"] == "no_source"
    assert "must not" not in json.dumps(out)


# Instagram ---------------------------------------------------------------------------


def test_instagram_link_detection():
    text = (
        "see https://www.instagram.com/reel/Fake_Reel1/?igsh=x and "
        "instagram.com/p/NOPE (no scheme), http://instagram.com/reels/FakeReel2 "
        "https://m.instagram.com/fake.user/p/FakePost3/ https://instagr.am/tv/FakeTv4 "
        "https://www.instagram.com/reel/Fake_Reel1/ again, https://www.instagram.com/stories/x/1/"
    )
    assert instagram_links(text) == [
        ("https://www.instagram.com/reel/Fake_Reel1/", "reel", "Fake_Reel1"),
        ("https://www.instagram.com/reel/FakeReel2/", "reel", "FakeReel2"),
        ("https://www.instagram.com/p/FakePost3/", "p", "FakePost3"),
        ("https://www.instagram.com/tv/FakeTv4/", "tv", "FakeTv4"),
    ]


needs_ffmpeg = pytest.mark.skipif(
    not shutil.which("ffmpeg"), reason="ffmpeg not on PATH"
)


def generated_video(path, seconds=7, sound=True):
    """A synthetic test pattern (and tone): never real content."""
    command = [
        shutil.which("ffmpeg"),
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size=160x120:rate=10:duration={seconds}",
    ]
    if sound:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    command += ["-pix_fmt", "yuv420p", "-c:v", "mpeg4", "-shortest", str(path)]
    subprocess.run(command, check=True, capture_output=True)
    return path


class FakeInstagram(media.InstagramFetcher):
    def __init__(self, outcomes):
        self.outcomes, self.calls = outcomes, []

    def fetch(self, url, workdir):
        self.calls.append(url)
        outcome = self.outcomes[url]
        if isinstance(outcome, Exception):
            raise outcome
        items = []
        for index, item in enumerate(outcome, 1):
            if item == "video":
                items.append(
                    {
                        "type": "video",
                        "duration": 7.0,
                        "path": generated_video(Path(workdir) / f"v{index}.mp4"),
                    }
                )
            elif item == "silent":
                items.append(
                    {
                        "type": "video",
                        "path": generated_video(
                            Path(workdir) / f"v{index}.mp4", 4, False
                        ),
                    }
                )
            else:
                path = Path(workdir) / f"i{index}.png"
                path.write_bytes(png(color=(index * 40, 10, 10)))
                items.append({"type": "image", "path": path})
        return items


class FakeTranscriber(media.Transcriber):
    name = "fake"

    def __init__(self):
        self.calls = 0

    def transcribe(self, audio_path):
        self.calls += 1
        assert Path(audio_path).stat().st_size > 0
        return "invented spoken words\x00"


@needs_ffmpeg
def test_instagram_video_frames_transcript_images_and_dead_links(monkeypatch, tmp_path):
    reel = "https://www.instagram.com/reel/FakeReel1/"
    post = "https://www.instagram.com/p/FakePost2/"
    mixed = "https://www.instagram.com/p/FakeMixed3/"
    private = "https://www.instagram.com/reel/FakePrivate4/"
    flaky = "https://www.instagram.com/reel/FakeFlaky5/"
    fetcher = FakeInstagram(
        {
            reel: ["video"],
            post: ["image", "image"],
            mixed: ["image", "silent"],
            private: media.MediaUnavailable("private", "private post or account"),
            flaky: media.MediaTransient("login required or rate limited"),
        }
    )
    recording = attachment_fixture(
        tmp_path,
        [[], [], []],
        bodies={0: f"watch {reel} and {post}", 1: f"{mixed} {private}", 2: f"{flaky}"},
    )

    def run(transcriber=None, **env):
        plugin = archive_plugin(monkeypatch, recording, None, **env)
        plugin.enrich_options = {
            "instagram": fetcher,
            "transcriber": transcriber or media.NullTranscriber(),
        }
        return messages(plugin)

    off = run()
    assert (
        not fetcher.calls
        and "instagram_links" not in off["discord:fake-att-0"]["fields"]
    )

    first = run(BEEPER_INSTAGRAM_LINKS="1")
    content = first["discord:fake-att-0"]["content"]
    links = first["discord:fake-att-0"]["fields"]["instagram_links"]
    assert [link["status"] for link in links] == ["video", "images"]
    video = links[0]["items"][0]
    assert [f["at_s"] for f in video["frames"]] == [0, 3, 6]
    assert video["transcript_status"] == "unavailable" and video["audio"].endswith(
        ".ogg"
    )
    assert (
        "attachment instagram reel FakeReel1: video transcript\ntranscript unavailable"
        in content
    )
    assert (
        f"attachment instagram reel FakeReel1 frame 0:03 image: {video['frames'][1]['link']}"
        in content
    )
    assert f"attachment instagram reel FakeReel1 audio: {video['audio']}" in content
    assert (
        "attachment instagram post FakePost2 image 2: throughline-asset://beeper/"
        in content
    )
    second = first["discord:fake-att-1"]["content"]
    assert "attachment instagram post FakeMixed3 image 1: " in second
    assert (
        "attachment instagram post FakeMixed3 item 2: video transcript\n(no audio)"
        in second
    )
    assert "attachment instagram reel FakePrivate4: unavailable (private)" in second
    # Retryable: neither content nor field shows it, so retries add no versions.
    assert first["discord:fake-att-2"]["content"] == flaky
    assert "instagram_links" not in first["discord:fake-att-2"]["fields"]

    # Rerun: no repeated downloads; the transient link is retried, then final.
    fetcher.calls.clear()
    again = run(BEEPER_INSTAGRAM_LINKS="1")
    assert fetcher.calls == [flaky]
    assert again["discord:fake-att-0"] == first["discord:fake-att-0"]
    final = run(BEEPER_INSTAGRAM_LINKS="1")
    assert final["discord:fake-att-2"]["content"].endswith(
        "attachment instagram reel FakeFlaky5: unavailable (private, removed or login required)"
    )
    fetcher.calls.clear()

    # A transcriber appears later: transcribed from the stored audio, no download.
    transcriber = FakeTranscriber()
    transcribed = run(transcriber, BEEPER_INSTAGRAM_LINKS="1")
    assert not fetcher.calls and transcriber.calls == 1
    assert (
        "attachment instagram reel FakeReel1: video transcript\ninvented spoken words"
        in transcribed["discord:fake-att-0"]["content"]
    )
    assert run(transcriber)["discord:fake-att-0"] == transcribed["discord:fake-att-0"]
    assert transcriber.calls == 1


def test_instagram_circuit_breaker_and_limit(monkeypatch, tmp_path):
    urls = [f"https://www.instagram.com/reel/FakeBusy{i}/" for i in range(8)]
    fetcher = FakeInstagram({u: media.MediaTransient("rate limited") for u in urls})
    recording = attachment_fixture(tmp_path, [[]], bodies={0: " ".join(urls)})
    plugin = archive_plugin(monkeypatch, recording, None, BEEPER_INSTAGRAM_LINKS="1")
    plugin.enrich_options = {"instagram": fetcher}
    messages(plugin)
    assert len(fetcher.calls) == 5
    fetcher.calls.clear()
    plugin = archive_plugin(
        monkeypatch,
        recording,
        None,
        BEEPER_INSTAGRAM_LINKS="1",
        BEEPER_INSTAGRAM_LIMIT="2",
    )
    plugin.enrich_options = {"instagram": fetcher}
    messages(plugin)
    assert len(fetcher.calls) == 2


def test_ytdlp_parses_posts_and_classifies_errors(monkeypatch, tmp_path):
    info = {
        "entries": [
            {
                "thumbnails": [
                    {
                        "url": "https://img.fake.invalid/s.jpg",
                        "width": 10,
                        "height": 10,
                    },
                    {
                        "url": "https://img.fake.invalid/l.jpg",
                        "width": 1080,
                        "height": 1080,
                    },
                ]
            },
            {"duration": 9999, "formats": [{"vcodec": "h264"}]},
        ]
    }
    calls = []

    def fake_run(command, capture_output, timeout):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(info).encode(), b"")

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    images = Server({"/l.jpg": (200, png())})
    fetcher = media.YtDlp(
        cookies="cookies.txt",
        binary="yt-dlp-fake",
        max_seconds=60,
        transport=httpx.MockTransport(images),
    )
    items = fetcher.fetch("https://www.instagram.com/p/FakePost/", tmp_path)
    assert (
        items[0]["type"] == "image" and items[0]["path"].read_bytes()[:4] == b"\x89PNG"
    )
    assert items[1] == {"type": "video", "skipped": "too_long", "duration": 9999}
    assert images.requests == ["https://img.fake.invalid/l.jpg"]
    assert calls[0][:6] == [
        "yt-dlp-fake",
        "--ignore-config",
        "--no-warnings",
        "--no-progress",
        "--cookies",
        "cookies.txt",
    ]
    for stderr, outcome in [
        (
            "ERROR: Requested content is not available, rate-limit reached or login required",
            media.MediaTransient,
        ),
        ("ERROR: This content is private", media.MediaUnavailable),
        ("ERROR: HTTP Error 404: Not Found", media.MediaUnavailable),
        ("ERROR: something new", media.MediaTransient),
    ]:
        with pytest.raises(outcome):
            media.classify(stderr)


def test_transcriber_selection_and_openai_compatible(tmp_path):
    assert isinstance(
        media.transcriber_from({"transcriber": "none"}, {}), media.NullTranscriber
    )
    if not media.FasterWhisper.available():
        assert isinstance(
            media.transcriber_from({"transcriber": "auto"}, {}), media.NullTranscriber
        )
        chosen = media.transcriber_from(
            {"transcriber": "auto", "transcribe_url": "https://stt.fake.invalid/v1"},
            {"transcribe_api_key": "k"},
        )
        assert isinstance(chosen, media.OpenAICompatible)
        with pytest.raises(ValueError):
            media.transcriber_from({"transcriber": "faster-whisper-missing"}, {})
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"text": " invented words "})

    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"fake-audio")
    client = media.OpenAICompatible(
        "https://stt.fake.invalid/v1/",
        "fake-key",
        transport=httpx.MockTransport(handler),
    )
    assert client.transcribe(audio) == "invented words"
    assert seen[0].url.path == "/v1/audio/transcriptions"
    assert seen[0].headers["Authorization"] == "Bearer fake-key"
    assert b"whisper-1" in seen[0].content


def test_cli_flags_map_to_settings():
    env, rest = enrichment_flags(
        [
            "backfill",
            "--attachments",
            "--instagram-links",
            "--cookies-from-browser",
            "firefox",
            "--attachment-limit",
            "50",
            "--dry-run",
        ]
    )
    assert rest == ["backfill", "--dry-run"]
    assert env == {
        "BEEPER_ATTACHMENTS": "1",
        "BEEPER_INSTAGRAM_LINKS": "1",
        "BEEPER_ATTACHMENT_LIMIT": "50",
        "BEEPER_YTDLP_COOKIES_FROM_BROWSER": "firefox",
    }
    assert enrichment_flags(["sync", "--once"]) == ({}, ["sync", "--once"])


def test_invalid_limits_rejected(monkeypatch):
    monkeypatch.setenv("BEEPER_SOURCE", "archive")
    monkeypatch.setenv("BEEPER_RECORDING", str(RECORDINGS / "archive.json"))
    monkeypatch.setenv("BEEPER_ATTACHMENT_LIMIT", "-1")
    with pytest.raises(ValueError, match="BEEPER_ATTACHMENT_LIMIT"):
        Beeper()
    monkeypatch.setenv("BEEPER_ATTACHMENT_LIMIT", "5")
    monkeypatch.setenv("BEEPER_TRANSCRIBER", "whisperish")
    with pytest.raises(ValueError, match="BEEPER_TRANSCRIBER"):
        Beeper()
    monkeypatch.setenv("BEEPER_TRANSCRIBER", "openai")
    Beeper()  # Instagram links are off: the transcriber is never needed.
    monkeypatch.setenv("BEEPER_INSTAGRAM_LINKS", "1")
    with pytest.raises(ValueError, match="BEEPER_TRANSCRIBE_URL"):
        Beeper()


# Contract kit with enrichment on --------------------------------------------------------


@pytest.fixture
def local_origin(tmp_path):
    """A loopback HTTP server the contract kit's subprocesses can download from."""
    root = tmp_path / "origin"
    root.mkdir()
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(QuietHandler, directory=str(root))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield root, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def test_plg_6_contract_with_enrichment_is_idempotent(
    monkeypatch, tmp_path, local_origin, retry_core_role_race, isolated_enrichment
):
    root, origin = local_origin
    (root / "report.pdf").write_bytes(
        make_pdf([{"text": "Invented contract text"}, {"image": True}])
    )
    (root / "budget.xlsx").write_bytes(make_xlsx())
    data = json.loads((RECORDINGS / "archive.json").read_text())
    data["messages"][1]["attachments_json"] = json.dumps(
        [{"id": "fake-c1", "fileName": "report.pdf", "url": origin + "/report.pdf"}]
    )
    data["messages"][3]["attachments_json"] = json.dumps(
        [
            {
                "id": "fake-c2",
                "fileName": "budget.xlsx",
                "url": origin + "/budget.xlsx",
            },
            {"id": "fake-c3", "fileName": "gone.txt", "url": origin + "/gone.txt"},
        ]
    )
    data["messages"][3]["reactions_json"] = json.dumps(
        [
            {
                "emoji": {"id": "", "name": "\U0001f44d"},
                "count": 2,
                "users": [{"id": "fake-sender", "name": "Invented Sender"}],
            }
        ]
    )
    recording = tmp_path / "contract-archive.json"
    recording.write_text(json.dumps(data))
    try:
        check_plugin(
            "throughline_beeper:Beeper",
            {
                "BEEPER_SOURCE": "archive",
                "BEEPER_RECORDING": str(recording),
                "BEEPER_BATCH_SIZE": "4",
                "BEEPER_ATTACHMENTS": "1",
                "BEEPER_INSTAGRAM_LINKS": "1",
                "BEEPER_TRANSCRIBE_API_KEY": "fake-contract-canary-transcribe",
            },
            secrets=["fake-contract-canary-transcribe"],
        )
    except subprocess.CalledProcessError as error:
        stderr = error.stderr or b""
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        pytest.fail(
            "disposable core subprocess failed: " + stderr[-2000:], pytrace=False
        )
    assets = sorted(p.suffix for p in (isolated_enrichment / "assets").iterdir())
    assert assets == [".jpg", ".pdf", ".xlsx"]


def test_contract_recordings_have_reactions():
    archive = json.loads((RECORDINGS / "archive.json").read_text())
    assert any(json.loads(m["reactions_json"]) for m in archive["messages"])
    live = json.loads((RECORDINGS / "live.json").read_text())
    assert any(
        item.get("reactions")
        for r in live["responses"]
        if r["path"].endswith("/messages")
        for item in r["body"]["items"]
    )


def test_checkpoints_unchanged_by_enrichment(monkeypatch, tmp_path):
    server = Server({"/attachments/1/1/a.txt": (200, b"synthetic")})
    recording = attachment_fixture(tmp_path, [[cdn("a.txt", "1")], [], []])
    plain = [
        copy.deepcopy(r.cursor)
        for r in archive_plugin(monkeypatch, recording, server).records({}, "backfill")
        if isinstance(r, Checkpoint)
    ]
    enriched = [
        r.cursor
        for r in archive_plugin(
            monkeypatch, recording, server, BEEPER_ATTACHMENTS="1"
        ).records({}, "backfill")
        if isinstance(r, Checkpoint)
    ]
    assert plain == enriched


def test_core_sees_enriched_content_and_reruns_add_no_versions(
    monkeypatch, tmp_path, retry_core_role_race
):
    from throughline_sdk import Client
    from throughline_sdk._server import core_server

    server = Server({"/attachments/1/1/notes.csv": (200, b"fake,1\n")})
    recording = attachment_fixture(tmp_path, [[cdn("notes.csv", "1")], [], []])
    data = json.loads(recording.read_text())
    data["messages"][1]["reactions_json"] = json.dumps(
        [
            {
                "reaction": "\U0001f44d",
                "actor": "Invented Sender",
                "timestamp": 1577880000,
            }
        ]
    )
    recording.write_text(json.dumps(data))
    with core_server() as (_, env):
        client = Client(env["THROUGHLINE_URL"], env["THROUGHLINE_TOKEN"], "beeper")

        def rows():
            statement = (
                "SELECT external_id, version, content,"
                " fields->'reaction_details'->0->>'sender_id' AS reactor,"
                " fields->'attachment_files'->0->>'status' AS status"
                " FROM api.messages ORDER BY external_id"
            )
            return client.request("POST", "sql", {"statement": statement})["rows"]

        try:
            for flags in ({"BEEPER_ATTACHMENTS": "1"}, {"BEEPER_ATTACHMENTS": "1"}, {}):
                plugin = archive_plugin(monkeypatch, recording, server, **flags)
                plugin.execute(client, "backfill")
                client.request("PUT", "plugins/beeper/cursor/default", {})
            got = {r["external_id"]: r for r in rows()}
            first, second = got["discord:fake-att-0"], got["discord:fake-att-1"]
            assert {r["version"] for r in got.values()} == {1}
            assert first["content"] == (
                "Invented\n\nattachment notes.csv: CSV-converted content\nfake,1"
            )
            assert first["status"] == "converted"
            assert second["content"].endswith("reactions: \U0001f44d Invented Sender")
            assert second["reactor"] == "fake-sender"
        finally:
            client.close()


# Checker findings -----------------------------------------------------------------


def test_pdf_tables_stay_text_but_charts_get_page_images(store):
    """Real pdfium objects: table rules on a text page are not a figure."""
    prose = "\n".join(f"Invented row {i} with several cells of text" for i in range(45))
    blob = make_pdf(
        [
            {"text": prose, "paths": 120},  # a ruled table: lots of text
            {"text": "Figure 1\nInvented axis", "paths": 120},  # a vector chart
            {"text": prose},  # plain prose
        ]
    )
    result = converters.convert("tables.pdf", "pdf", blob, store)
    assert [a["page"] for a in result["assets"]] == [2]
    out = text_of(result)
    assert "attachment tables.pdf page 2 image: " in out
    assert "page 1 image" not in out and "page 3 image" not in out


def test_instagram_page_links_are_not_posts():
    assert instagram_links(
        "https://www.instagram.com/reels/audio/1234567890/ "
        "https://notinstagram.com/p/FakePost1/ "
        "https://fake.invalid/instagram.com/p/FakePost2/ "
        "https://www.instagram.com/p/FakePost3/embed/ "
        "https://www.instagram.com/p/embed/"
    ) == [("https://www.instagram.com/p/FakePost3/", "p", "FakePost3")]
    # Bare links count when they stand on their own.
    assert instagram_links("watch instagram.com/reel/FakeBare1 now") == [
        ("https://www.instagram.com/reel/FakeBare1/", "reel", "FakeBare1")
    ]


def test_ytdlp_image_must_be_an_image_and_is_size_capped(monkeypatch, tmp_path):
    info = {"thumbnails": [{"url": "https://img.fake.invalid/a.jpg"}]}

    def fake_run(command, capture_output, timeout):
        return subprocess.CompletedProcess(command, 0, json.dumps(info).encode(), b"")

    monkeypatch.setattr(media.subprocess, "run", fake_run)

    def fetcher(body, max_bytes=10_000_000):
        return media.YtDlp(
            binary="yt-dlp-fake",
            max_bytes=max_bytes,
            transport=httpx.MockTransport(Server({"/a.jpg": (200, body)})),
        )

    login = b"<!DOCTYPE html><title>Log in</title>"
    with pytest.raises(media.MediaTransient):
        fetcher(login).fetch("https://www.instagram.com/p/FakePost/", tmp_path)
    assert not list(tmp_path.iterdir())  # Nothing stored from a login wall.
    big = png(400, 400)
    items = fetcher(big, max_bytes=len(big) - 1).fetch(
        "https://www.instagram.com/p/FakePost/", tmp_path
    )
    assert items == [{"type": "image", "skipped": "too_large"}]
    items = fetcher(big).fetch("https://www.instagram.com/p/FakePost/", tmp_path)
    assert items[0]["path"].suffix == ".png"


def test_attachment_served_as_html_error_page_is_not_converted(monkeypatch, tmp_path):
    server = Server(
        {"/attachments/1/1/report.pdf": (200, b"<!DOCTYPE html><p>Log in</p>")}
    )
    recording = attachment_fixture(tmp_path, [[cdn("report.pdf", "1")]])
    result = messages(
        archive_plugin(monkeypatch, recording, server, BEEPER_ATTACHMENTS="1")
    )
    field = result["discord:fake-att-0"]["fields"]["attachment_files"][0]
    assert field["status"] == "unsupported"
    assert result["discord:fake-att-0"]["content"] == "Invented"


def test_store_failure_stops_the_run_and_caches_nothing(
    monkeypatch, tmp_path, isolated_enrichment
):
    from throughline_beeper.assets import StoreError

    server = Server({"/attachments/1/1/a.txt": (200, b"synthetic text")})
    recording = attachment_fixture(tmp_path, [[cdn("a.txt", "1")]])

    def full(self, data, ext):
        raise StoreError("invented: disk full")

    put = LocalAssetStore.put
    monkeypatch.setattr(LocalAssetStore, "put", full)
    with pytest.raises(OSError):
        messages(archive_plugin(monkeypatch, recording, server, BEEPER_ATTACHMENTS="1"))
    # Disk space is back: the same file is converted, not "failed". Only the
    # store is restored; monkeypatch.undo() would also drop the isolated state
    # directory and write to the owner's real Beeper state.
    monkeypatch.setattr(LocalAssetStore, "put", put)
    assert os.environ["BEEPER_STATE_DIR"] == str(isolated_enrichment)
    result = messages(
        archive_plugin(monkeypatch, recording, server, BEEPER_ATTACHMENTS="1")
    )
    field = result["discord:fake-att-0"]["fields"]["attachment_files"][0]
    assert field["status"] == "converted"
    assert "synthetic text" in result["discord:fake-att-0"]["content"]


def test_transient_retries_add_no_versions_until_final(monkeypatch, tmp_path):
    server = Server({"/attachments/1/1/flaky.md": (503, b"")})
    recording = attachment_fixture(tmp_path, [[cdn("flaky.md", "1")]])
    runs = [
        messages(archive_plugin(monkeypatch, recording, server, BEEPER_ATTACHMENTS="1"))
        for _ in range(4)
    ]
    plain = messages(archive_plugin(monkeypatch, recording, server))
    assert runs[0] == runs[1] and runs[0]["discord:fake-att-0"]["fields"] == {
        k: v
        for k, v in plain["discord:fake-att-0"]["fields"].items()
        if k != "attachment_files"
    }
    assert runs[2] == runs[3] == plain  # One change only: the final outcome.
    field = plain["discord:fake-att-0"]["fields"]["attachment_files"][0]
    assert field["status"] == "error" and field["attempts"] == 3


def test_reactor_names_resolve_across_latin1_mojibake():
    from throughline_beeper.mapping import archive_resolver

    senders = {
        "by_name": {"José Fake": "fake-id-1", "Ana Fake": "fake-id-2"},
        "by_id": {"fake-id-1": "José Fake", "fake-id-2": "Ana Fake"},
    }
    resolve = archive_resolver(senders, lambda rid: "user_id")
    mangled = "José Fake".encode("utf-8").decode("latin-1")
    assert resolve(None, mangled) == ("fake-id-1", mangled, "user_id")
    assert resolve(None, "Ana Fake") == ("fake-id-2", "Ana Fake", "user_id")
    assert resolve(None, "Unknown Fake") == (None, "Unknown Fake", None)
    assert resolve("fake-id-2", None) == ("fake-id-2", "Ana Fake", "user_id")
