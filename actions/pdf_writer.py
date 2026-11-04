"""
actions/pdf_writer.py — turn a set of information into a real PDF.

WHY THIS IS NOT file_controller
    create_file writes text. Naming that file ".pdf" produces something no
    reader will open. The user asked for a document: a title, sections, a
    list or a table, arranged the way that material actually reads. That is
    a layout decision, and it belongs in one place so the speaking model
    only has to hand over the information.

WHY THE LAYOUT IS CHOSEN HERE
    A report, a letter, a list and a table are different documents. Asking
    the speaking model to emit PDF operators would throw that structure
    away, and one fixed template would make a letter look like a report.
    This module reads the shape of the content — headings, items, rows, a
    greeting — and picks the page that fits. A second model is asked only
    when the text arrives as a wall of prose with no shape of its own, and
    its answer is kept only when it still contains the information.
"""
from __future__ import annotations

import json
import os
import re
import traceback
from pathlib import Path

from core import outcome
from core.undo import push_undo


_KINDS = ("report", "letter", "list", "table", "notes", "summary")
_KIND_ALIAS = {
    "relatorio": "report", "relatório": "report",
    "carta": "letter",
    "lista": "list",
    "tabela": "table",
    "nota": "notes", "notas": "notes",
    "resumo": "summary",
    "auto": "", "automatic": "", "automatico": "", "automático": "",
}

# A document someone asked for, not a dump. Past this size we still write,
# but we stop so a pasted log cannot become a hundred-page accident.
_DOC_MAX = 80_000
# The structuring call sees this much. Longer texts stay on the local
# reader, which keeps the tail instead of summarising the head.
_MODEL_MAX = 24_000

_PAPER = "#F7F3EB"
_INK = "#2A241C"
_MUTED = "#6F6254"
_RULE = "#C4A46A"
_SAND = "#E8D7B6"
_BAND = "#EFE6D4"
_ALT = "#F3EEE4"

_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.+)$")
_HEAD_MD = re.compile(r"^\s*(#{1,3})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*```")
_GREET = re.compile(
    r"^\s*(prezad[oa]s?|caro|cara|querid[oa]|dear|hello|hi|olá|ola|oi|"
    r"bom dia|boa tarde|boa noite)\b",
    re.IGNORECASE,
)
_CLOSE = re.compile(
    r"\b(atenciosamente|cordialmente|respeitosamente|sincerely|"
    r"best regards|kind regards|abraços|abracos|com os melhores cumprimentos)\b",
    re.IGNORECASE,
)
_META_KEY = re.compile(
    r"^(data|date|local|lugar|para|to|de|from|assunto|subject)\s*:\s*(.+)$",
    re.IGNORECASE,
)
_MONTH = re.compile(
    r"\b(janeiro|fevereiro|março|marco|abril|maio|junho|julho|agosto|"
    r"setembro|outubro|novembro|dezembro|january|february|march|april|"
    r"may|june|july|august|september|october|november|december)\b",
    re.IGNORECASE,
)
_WHOLE_FENCE = re.compile(r"^\s*```[a-zA-Z0-9]*\n(.*)\n```\s*$", re.DOTALL)

_FONT_CANDIDATES = (
    ("C:/Windows/Fonts/georgia.ttf",
     "C:/Windows/Fonts/georgiab.ttf",
     "C:/Windows/Fonts/georgiai.ttf"),
    ("/Library/Fonts/Georgia.ttf",
     "/Library/Fonts/Georgia Bold.ttf",
     "/Library/Fonts/Georgia Italic.ttf"),
    ("/System/Library/Fonts/Supplemental/Georgia.ttf",
     "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
     "/System/Library/Fonts/Supplemental/Georgia Italic.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf"),
)
_MONO_CANDIDATES = (
    "C:/Windows/Fonts/consola.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/Library/Fonts/Consolas.ttf",
)

_FONTS: dict | None = None


def _kind_of(value: str) -> str:
    raw = str(value or "").strip().lower()
    mapped = _KIND_ALIAS.get(raw, raw)
    return mapped if mapped in _KINDS else ""


def _portuguese(text: str) -> bool:
    sample = text[:4000].lower()
    if re.search(r"[áàâãéêíóôõúç]", sample):
        return True
    words = set(re.findall(r"[a-záàâãéêíóôõúç]+", sample))
    return len(words & {"que", "não", "para", "uma", "com", "os", "das", "pelo"}) >= 3


def _fallback_title(kind: str, text: str) -> str:
    pt = _portuguese(text)
    names = {
        "report": ("Relatório", "Report"),
        "letter": ("Carta", "Letter"),
        "list": ("Lista", "List"),
        "table": ("Tabela", "Table"),
        "summary": ("Resumo", "Summary"),
        "notes": ("Notas", "Notes"),
    }
    pair = names.get(kind, names["notes"])
    return pair[0] if pt else pair[1]


def _inline(text: str, unicode_ok: bool) -> str:
    """Reportlab paragraphs are a small XML. Bold markers survive; the rest
    is escaped so a '<' in the user's text cannot break the page."""
    raw = str(text or "")
    if not unicode_ok:
        raw = raw.encode("latin-1", "replace").decode("latin-1")
    parts = re.split(r"(\*\*.+?\*\*)", raw)
    out = []
    for part in parts:
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            out.append("<b>" + _xml(part[2:-2]) + "</b>")
        else:
            out.append(_xml(part))
    return "".join(out).replace("\n", "<br/>")


def _xml(text: str) -> str:
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))


def _code_html(text: str, unicode_ok: bool) -> str:
    raw = str(text or "")
    if not unicode_ok:
        raw = raw.encode("latin-1", "replace").decode("latin-1")
    lines = []
    for line in raw.split("\n"):
        escaped = _xml(line).replace(" ", "&nbsp;")
        lines.append(escaped or "&nbsp;")
    return "<br/>".join(lines)


def _slug(title: str) -> str:
    cleaned = re.sub(r"[^\w]+", "-", title, flags=re.UNICODE).strip("-_")
    return (cleaned[:60] or "documento")


def _same_title(left: str, right: str) -> bool:
    """The model often passes a title and then repeats it as the first line.
    That second copy would become a heading sitting under the real title."""
    def norm(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip().casefold().rstrip(".:-")
    a, b = norm(left), norm(right)
    return bool(a) and a == b


def _looks_like_dateline(line: str) -> bool:
    text = line.strip()
    if not text or len(text) > 90 or not re.search(r"\d", text):
        return False
    if _META_KEY.match(text):
        return True
    return bool(_MONTH.search(text) or re.search(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", text))


def _is_table_line(line: str) -> bool:
    text = line.strip()
    if text.count("|") < 2:
        return False
    if text.startswith("|"):
        return True
    return len(text) <= 180


def _split_row(line: str) -> list[str] | None:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    cells = [cell.strip() for cell in text.split("|")]
    if cells and all(re.fullmatch(r":?-{2,}:?", cell.replace(" ", "")) for cell in cells):
        return None
    return cells


def _next_nonempty(lines: list[str], index: int) -> str:
    for line in lines[index + 1:]:
        if line.strip():
            return line.strip()
    return ""


def _is_plain_heading(line: str, lines: list[str], index: int) -> bool:
    text = line.strip()
    if not text or len(text) > 72 or len(text) < 2:
        return False
    if text.endswith((".", "!", "?", ",", ";")):
        return False
    if _BULLET.match(text) or _is_table_line(text) or _HEAD_MD.match(text):
        return False
    nxt = _next_nonempty(lines, index)
    if not nxt:
        return False
    if _BULLET.match(nxt) or _is_table_line(nxt) or _HEAD_MD.match(nxt):
        return True
    if text.endswith(":") and len(text) <= 48:
        return True
    return len(text) <= 42 and len(nxt) > len(text) + 20


def _empty_doc(kind: str, title: str) -> dict:
    return {
        "kind": kind if kind in _KINDS else "notes",
        "title": title.strip(),
        "subtitle": "",
        "meta": "",
        "lead": "",
        "sections": [],
        "title_is_fallback": False,
    }


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _section_from_parts(heading: str, paragraphs, bullets, table) -> dict:
    blocks = []
    for paragraph in _as_list(paragraphs):
        text = str(paragraph).strip()
        if text:
            blocks.append({"type": "p", "text": text})
    items = [str(item).strip() for item in _as_list(bullets) if str(item).strip()]
    if items:
        blocks.append({"type": "bullets", "items": items})
    if isinstance(table, dict):
        headers = [str(cell).strip() for cell in _as_list(table.get("headers"))]
        rows = []
        for row in _as_list(table.get("rows")):
            if isinstance(row, (list, tuple)):
                rows.append([str(cell).strip() for cell in row])
            elif str(row).strip():
                rows.append([str(row).strip()])
        if headers or rows:
            blocks.append({"type": "table", "headers": headers, "rows": rows})
    return {"heading": str(heading or "").strip(), "blocks": blocks}


def _has_body(sections: list[dict]) -> bool:
    for section in sections:
        if section.get("heading") or section.get("blocks"):
            return True
    return False


def _plain_length(doc: dict) -> int:
    chunks = [doc.get("title") or "", doc.get("subtitle") or "",
              doc.get("meta") or "", doc.get("lead") or ""]
    for section in doc.get("sections") or []:
        chunks.append(section.get("heading") or "")
        for block in section.get("blocks") or []:
            if block["type"] == "p":
                chunks.append(block["text"])
            elif block["type"] == "bullets":
                chunks.extend(block["items"])
            elif block["type"] == "code":
                chunks.append(block["text"])
            elif block["type"] == "table":
                chunks.extend(block.get("headers") or [])
                for row in block.get("rows") or []:
                    chunks.extend(row)
    return len(re.sub(r"\s+", "", " ".join(chunks)))


def _normalize(data: dict, title: str, kind_hint: str) -> dict:
    kind = _kind_of(kind_hint) or _kind_of(str(data.get("kind") or "")) or "notes"
    doc = _empty_doc(kind, title or str(data.get("title") or ""))
    doc["subtitle"] = str(data.get("subtitle") or "").strip()
    doc["meta"] = str(data.get("meta") or "").strip()
    doc["lead"] = str(data.get("lead") or "").strip()
    if not doc["title"]:
        doc["title"] = str(data.get("title") or "").strip()
    sections = []
    for raw in _as_list(data.get("sections"))[:80]:
        if isinstance(raw, str):
            section = _section_from_parts("", [raw], [], None)
        elif isinstance(raw, dict):
            section = _section_from_parts(
                raw.get("heading") or raw.get("title") or "",
                raw.get("paragraphs") or raw.get("body") or [],
                raw.get("bullets") or raw.get("items") or [],
                raw.get("table"),
            )
            for block in _as_list(raw.get("blocks")):
                if isinstance(block, dict) and block.get("type") in ("p", "bullets", "table", "code"):
                    section["blocks"].append(block)
        else:
            continue
        if section["heading"] or section["blocks"]:
            sections.append(section)
    doc["sections"] = sections
    return _finish(doc, title)


def _finish(doc: dict, requested_title: str) -> dict:
    """Give the page a real title, and let a summary open on its lead."""
    given = requested_title.strip()
    if given:
        doc["title"] = given
        doc["title_is_fallback"] = False
    elif not doc["title"]:
        for section in doc["sections"]:
            if section["heading"]:
                doc["title"] = section["heading"]
                section["heading"] = ""
                break
    if not doc["title"]:
        source = _source_of(doc)
        doc["title"] = _fallback_title(doc["kind"], source)
        doc["title_is_fallback"] = True
    else:
        doc["title_is_fallback"] = doc["title"] in {
            "Notas", "Notes", "Carta", "Letter", "Relatório", "Report",
            "Lista", "List", "Tabela", "Table", "Resumo", "Summary",
        } and not given

    if doc["kind"] == "summary" and not doc["lead"]:
        for section in doc["sections"]:
            for index, block in enumerate(section["blocks"]):
                if block["type"] == "p":
                    doc["lead"] = block["text"]
                    del section["blocks"][index]
                    break
            if doc["lead"]:
                break
    doc["sections"] = [s for s in doc["sections"] if s["heading"] or s["blocks"]]
    return doc


def _source_of(doc: dict) -> str:
    parts = [doc.get("lead") or ""]
    for section in doc.get("sections") or []:
        for block in section.get("blocks") or []:
            if block["type"] == "p":
                parts.append(block["text"])
            elif block["type"] == "bullets":
                parts.extend(block["items"])
    return " ".join(parts)


def _extract_json(text: str) -> dict | None:
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    sections = data.get("sections")
    if not isinstance(sections, list):
        return None
    return data


def _new_section(heading: str = "") -> dict:
    return {"heading": heading.strip(), "blocks": []}


def _organize_locally(content: str, title: str, kind_hint: str) -> dict:
    text = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    fenced = _WHOLE_FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    if len(text) > _DOC_MAX:
        text = text[:_DOC_MAX].rstrip() + "\n\n…"

    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)

    meta = ""
    if lines and _looks_like_dateline(lines[0]) and not _is_table_line(lines[0]):
        meta = lines.pop(0).strip()
        while lines and not lines[0].strip():
            lines.pop(0)

    chosen = title.strip()
    if not chosen and lines:
        first = lines[0].strip()
        more = any(line.strip() for line in lines[1:])
        if (more and len(first) <= 80 and not first.endswith((".", "!", "?"))
                and not _BULLET.match(first) and not _is_table_line(first)
                and not _META_KEY.match(first) and not _GREET.match(first)
                and not _HEAD_MD.match(first) and not _looks_like_dateline(first)):
            chosen = first
            lines = lines[1:]
            while lines and not lines[0].strip():
                lines.pop(0)
    if chosen and lines and _same_title(lines[0], chosen):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)

    sections: list[dict] = []
    current: dict | None = None
    paragraph: list[str] = []
    bullets: list[str] = []
    table: list[list[str]] = []
    code: list[str] = []
    in_code = False
    subtitle = ""

    def ensure() -> dict:
        nonlocal current
        if current is None:
            current = _new_section()
            sections.append(current)
        return current

    def flush_paragraph():
        joined = " ".join(part.strip() for part in paragraph if part.strip())
        paragraph.clear()
        if not joined:
            return
        for piece in _split_long(joined):
            ensure()["blocks"].append({"type": "p", "text": piece})

    def flush_bullets():
        if bullets:
            ensure()["blocks"].append({"type": "bullets", "items": list(bullets)})
            bullets.clear()

    def flush_table():
        if not table:
            return
        rows = list(table)
        table.clear()
        if len(rows) >= 2:
            ensure()["blocks"].append({
                "type": "table", "headers": rows[0], "rows": rows[1:],
            })
        else:
            ensure()["blocks"].append({"type": "p", "text": " | ".join(rows[0])})

    def flush_code():
        if code:
            ensure()["blocks"].append({"type": "code", "text": "\n".join(code).rstrip("\n")})
            code.clear()

    def flush_all():
        flush_paragraph()
        flush_bullets()
        flush_table()
        flush_code()

    for index, line in enumerate(lines):
        if in_code:
            if _FENCE.match(line):
                in_code = False
                flush_code()
            else:
                code.append(line)
            continue
        if _FENCE.match(line):
            flush_paragraph()
            flush_bullets()
            flush_table()
            in_code = True
            continue
        if not line.strip():
            flush_paragraph()
            flush_bullets()
            flush_table()
            continue
        if _is_table_line(line):
            cells = _split_row(line)
            if cells is None:
                continue
            flush_paragraph()
            flush_bullets()
            table.append(cells)
            continue
        flush_table()

        bullet = _BULLET.match(line)
        if bullet:
            flush_paragraph()
            bullets.append(bullet.group(1).strip())
            continue
        flush_bullets()

        meta_line = _META_KEY.match(line.strip())
        if meta_line and not _has_body(sections) and not paragraph and not bullets:
            key, value = meta_line.group(1).lower(), meta_line.group(2).strip()
            if key in ("assunto", "subject"):
                subtitle = subtitle or value
            else:
                shown = f"{meta_line.group(1)}: {value}"
                meta = f"{meta}\n{shown}".strip() if meta else shown
            continue

        heading = _HEAD_MD.match(line)
        if heading:
            flush_all()
            level, label = len(heading.group(1)), heading.group(2).strip()
            if level == 1 and not chosen and not _has_body(sections):
                chosen = label
                continue
            if _same_title(label, chosen) and not _has_body(sections):
                continue
            current = _new_section(label)
            sections.append(current)
            continue

        if _is_plain_heading(line, lines, index):
            flush_all()
            label = line.strip().rstrip(":").strip()
            if _same_title(label, chosen) and not _has_body(sections):
                continue
            current = _new_section(label)
            sections.append(current)
            continue

        paragraph.append(line.strip())

    flush_all()
    if in_code:
        flush_code()

    kind = _detect_kind(text, sections, kind_hint)
    doc = _empty_doc(kind, chosen)
    doc["subtitle"] = subtitle
    doc["meta"] = meta
    doc["sections"] = sections
    return _finish(doc, title)


def _split_long(text: str) -> list[str]:
    if len(text) < 900:
        return [text]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    pieces: list[str] = []
    buf = ""
    for sentence in sentences:
        if buf and len(buf) + len(sentence) > 520:
            pieces.append(buf)
            buf = sentence
        else:
            buf = f"{buf} {sentence}".strip()
    if buf:
        pieces.append(buf)
    return pieces or [text]


def _opens_as_letter(text: str) -> bool:
    """A date line in front of 'Prezado' is still a letter. The greeting is
    the first line that is not itself the date or a 'Para:' field."""
    skipped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if _looks_like_dateline(line) or _META_KEY.match(line):
            skipped += 1
            if skipped > 4:
                return False
            continue
        return bool(_GREET.match(line))
    return False


def _detect_kind(text: str, sections: list[dict], kind_hint: str) -> str:
    hinted = _kind_of(kind_hint)
    if hinted:
        return hinted
    headings = sum(1 for section in sections if section["heading"])
    bullets = 0
    paragraphs = 0
    tables = 0
    cells = 0
    for section in sections:
        for block in section["blocks"]:
            if block["type"] == "bullets":
                bullets += len(block["items"])
            elif block["type"] == "p":
                paragraphs += 1
            elif block["type"] == "table":
                tables += 1
                cells += len(block.get("headers") or [])
                cells += sum(len(row) for row in block.get("rows") or [])
    words = len(text.split())
    # A list of groups is not a report, and a short table is still a table.
    # Prose under several headings is what makes a report.
    if _opens_as_letter(text) and headings < 2 and tables == 0:
        return "letter"
    if tables and bullets < 3 and paragraphs <= 2 and cells >= 4:
        return "table"
    if bullets >= 3 and paragraphs == 0 and tables == 0:
        return "list"
    if headings >= 2:
        return "report"
    if words <= 220 and bullets >= 2 and paragraphs <= 3:
        return "summary"
    return "notes"


def _shaped(text: str) -> bool:
    """The local reader already knows what to do with this. A model call
    would only be a chance to drop a sentence."""
    if len(text) < 700:
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if any(_HEAD_MD.match(line) for line in lines):
        return True
    if sum(1 for line in lines if _BULLET.match(line)) >= 2:
        return True
    if sum(1 for line in lines if _is_table_line(line)) >= 2:
        return True
    if _opens_as_letter(text) or (lines and _looks_like_dateline(lines[0])):
        return True
    return False


_STRUCTURE_PROMPT = """You structure information into a PDF. Reply with one JSON object and nothing else.

Pick "kind" from: report, letter, list, table, notes, summary.
- report: several sections of an account, a study, a proposal or minutes
- letter: a message to a person, with a greeting and a close
- list: the content is items, grouped under short headings
- table: the content is rows of data
- notes: loose remarks or a few paragraphs
- summary: a short briefing, with one lead and the key points

Rules:
- Keep the language of the information. Do not translate.
- Do not invent facts, numbers, names or sections that are not in the information.
- Do not drop information to make it shorter.
- title hint: <<<TITLE>>>
- kind hint (empty means you choose): <<<KIND>>>

JSON shape:
{"kind":"notes","title":"...","subtitle":"","meta":"","lead":"","sections":[{"heading":"","paragraphs":["..."],"bullets":["..."],"table":null}]}

Use "table": {"headers":["..."],"rows":[["..."]]} only when there is a real table, otherwise null.

INFORMATION:
<<<CONTENT>>>
"""


def _structure_with_model(content: str, title: str, kind_hint: str) -> dict | None:
    try:
        from core import gemini
        prompt = (_STRUCTURE_PROMPT
                  .replace("<<<TITLE>>>", title.strip() or "(none)")
                  .replace("<<<KIND>>>", kind_hint.strip() or "(choose)")
                  .replace("<<<CONTENT>>>", content[:_MODEL_MAX]))
        reply = gemini.call(prompt, tier=gemini.FAST, timeout_ms=25_000)
        raw = getattr(reply, "text", "") if reply else ""
        data = _extract_json(raw or "")
        if not data:
            print("[pdf] the structuring call did not return a document")
            return None
        doc = _normalize(data, title, kind_hint)
        source_len = len(re.sub(r"\s+", "", content[:_MODEL_MAX]))
        if source_len and _plain_length(doc) < source_len * 0.5:
            print("[pdf] the structuring call dropped information; using the local layout")
            return None
        return doc
    except Exception as exc:
        print(f"[pdf] structuring call failed ({exc}); using the local layout")
        return None


def _structure(content, title: str, kind_hint: str) -> dict:
    if isinstance(content, dict):
        return _normalize(content, title, kind_hint)
    text = str(content or "").strip()
    ready = _extract_json(text) if text.startswith("{") else None
    if ready:
        return _normalize(ready, title, kind_hint)
    if text and not _shaped(text) and len(text) <= _MODEL_MAX:
        modeled = _structure_with_model(text, title, kind_hint)
        if modeled and (_has_body(modeled["sections"]) or modeled.get("lead")):
            return modeled
    return _organize_locally(text, title, kind_hint)


def _safe_font(pdfmetrics, ttfont, name: str, path: str):
    if name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(ttfont(name, path))


def _register_fonts() -> dict:
    global _FONTS
    if _FONTS is not None:
        return _FONTS
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    chosen = {"reg": "Times-Roman", "bold": "Times-Bold",
              "italic": "Times-Italic", "mono": "Courier", "unicode": False}
    for regular, bold, italic in _FONT_CANDIDATES:
        if not os.path.isfile(regular):
            continue
        try:
            _safe_font(pdfmetrics, TTFont, "DocSerif", regular)
            _safe_font(pdfmetrics, TTFont, "DocSerif-Bold",
                       bold if os.path.isfile(bold) else regular)
            _safe_font(pdfmetrics, TTFont, "DocSerif-Italic",
                       italic if os.path.isfile(italic) else regular)
            chosen = {"reg": "DocSerif", "bold": "DocSerif-Bold",
                      "italic": "DocSerif-Italic", "mono": "Courier", "unicode": True}
            break
        except Exception as exc:
            print(f"[pdf] font {regular} could not be registered ({exc})")
    for path in _MONO_CANDIDATES:
        if not os.path.isfile(path):
            continue
        try:
            _safe_font(pdfmetrics, TTFont, "DocMono", path)
            chosen["mono"] = "DocMono"
            break
        except Exception:
            continue
    _FONTS = chosen
    return chosen


def _styles(fonts: dict, kind: str) -> dict:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT, TA_RIGHT
    from reportlab.lib.styles import ParagraphStyle

    ink = HexColor(_INK)
    muted = HexColor(_MUTED)
    body_align = TA_JUSTIFY if kind == "report" else TA_LEFT
    body_size = 11 if kind != "letter" else 11.5
    return {
        "title": ParagraphStyle(
            "pdfTitle", fontName=fonts["bold"], fontSize=22, leading=26,
            textColor=ink, spaceAfter=3, alignment=TA_LEFT,
        ),
        "subject": ParagraphStyle(
            "pdfSubject", fontName=fonts["bold"], fontSize=13, leading=17,
            textColor=ink, spaceBefore=2, spaceAfter=8, alignment=TA_LEFT,
        ),
        "subtitle": ParagraphStyle(
            "pdfSubtitle", fontName=fonts["italic"], fontSize=11, leading=14,
            textColor=muted, spaceAfter=2,
        ),
        "meta": ParagraphStyle(
            "pdfMeta", fontName=fonts["reg"], fontSize=9.5, leading=13,
            textColor=muted, spaceAfter=2,
        ),
        "date": ParagraphStyle(
            "pdfDate", fontName=fonts["italic"], fontSize=10.5, leading=14,
            textColor=muted, alignment=TA_RIGHT, spaceAfter=8,
        ),
        "h1": ParagraphStyle(
            "pdfH1", fontName=fonts["bold"], fontSize=13, leading=16,
            textColor=HexColor("#3C3126"), spaceBefore=2, spaceAfter=4,
            keepWithNext=True,
        ),
        "body": ParagraphStyle(
            "pdfBody", fontName=fonts["reg"], fontSize=body_size, leading=16,
            textColor=ink, alignment=body_align, spaceAfter=7,
        ),
        "bullet": ParagraphStyle(
            "pdfBullet", fontName=fonts["reg"], fontSize=11, leading=15,
            textColor=ink, leftIndent=14, bulletIndent=2,
            bulletFontName=fonts["reg"], bulletFontSize=11, spaceAfter=2,
        ),
        "lead": ParagraphStyle(
            "pdfLead", fontName=fonts["italic"], fontSize=11.5, leading=16,
            textColor=ink,
        ),
        "sign": ParagraphStyle(
            "pdfSign", fontName=fonts["italic"], fontSize=11.5, leading=15,
            textColor=ink, spaceBefore=12, spaceAfter=2,
        ),
        "th": ParagraphStyle(
            "pdfTh", fontName=fonts["bold"], fontSize=9, leading=12, textColor=ink,
        ),
        "td": ParagraphStyle(
            "pdfTd", fontName=fonts["reg"], fontSize=9, leading=12, textColor=ink,
        ),
        "code": ParagraphStyle(
            "pdfCode", fontName=fonts["mono"], fontSize=8.5, leading=11, textColor=ink,
        ),
        "footer": ParagraphStyle(
            "pdfFooter", fontName=fonts["italic"], fontSize=8, leading=10, textColor=muted,
        ),
    }


def _table_flowable(block: dict, styles: dict, width: float, unicode_ok: bool):
    from reportlab.lib.colors import HexColor
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Table, TableStyle

    headers = [str(cell) for cell in block.get("headers") or []]
    rows = [[str(cell) for cell in row] for row in block.get("rows") or []]
    cols = max([len(headers), *[len(row) for row in rows]] or [1])
    if not headers:
        headers = [""] * cols
    headers = (headers + [""] * cols)[:cols]
    padded = [(row + [""] * cols)[:cols] for row in rows]

    data = [[Paragraph(_inline(cell, unicode_ok), styles["th"]) for cell in headers]]
    for row in padded:
        data.append([Paragraph(_inline(cell, unicode_ok), styles["td"]) for cell in row])
    widths = _column_widths(headers, padded, width, mm)
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HexColor(_SAND)),
        ("TEXTCOLOR", (0, 0), (-1, -1), HexColor(_INK)),
        ("GRID", (0, 0), (-1, -1), 0.4, HexColor(_RULE)),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [HexColor(_PAPER), HexColor(_ALT)]),
    ]))
    return table


def _column_widths(headers: list[str], rows: list[list[str]], total: float, mm: float) -> list[float]:
    weights = []
    cols = len(headers)
    for index in range(cols):
        longest = len(headers[index])
        for row in rows[:40]:
            if index < len(row):
                longest = max(longest, min(len(row[index]), 48))
        weights.append(max(longest, 4))
    scale = total / float(sum(weights))
    widths = [max(16 * mm, weight * scale) for weight in weights]
    used = sum(widths)
    if used > total:
        widths = [width * total / used for width in widths]
    return widths


def _callout(text: str, styles: dict, width: float, unicode_ok: bool):
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import Paragraph, Table, TableStyle

    cell = Paragraph(_inline(text, unicode_ok), styles["lead"])
    table = Table([[cell]], colWidths=[width])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), HexColor(_BAND)),
        ("LINEBEFORE", (0, 0), (0, -1), 3, HexColor(_RULE)),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return table


def _code_flowable(text: str, styles: dict, width: float, unicode_ok: bool):
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import Paragraph, Table, TableStyle

    cell = Paragraph(_code_html(text, unicode_ok), styles["code"])
    table = Table([[cell]], colWidths=[width])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), HexColor(_BAND)),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return table


def _max_columns(doc: dict) -> int:
    widest = 0
    for section in doc["sections"]:
        for block in section["blocks"]:
            if block["type"] != "table":
                continue
            widest = max(widest, len(block.get("headers") or []))
            for row in block.get("rows") or []:
                widest = max(widest, len(row))
    return widest


def _story(doc: dict, styles: dict, width: float, unicode_ok: bool) -> list:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.units import mm
    from reportlab.platypus import CondPageBreak, HRFlowable, Paragraph, Spacer

    kind = doc["kind"]
    story = []
    show_title = not (kind == "letter" and doc.get("title_is_fallback"))

    if kind == "letter":
        if doc.get("meta"):
            story.append(Paragraph(_inline(doc["meta"], unicode_ok), styles["date"]))
        if show_title:
            story.append(Paragraph(_inline(doc["title"], unicode_ok), styles["subject"]))
        if doc.get("subtitle"):
            story.append(Paragraph(_inline(doc["subtitle"], unicode_ok), styles["subtitle"]))
    else:
        if show_title:
            story.append(Paragraph(_inline(doc["title"], unicode_ok), styles["title"]))
        if doc.get("subtitle"):
            story.append(Paragraph(_inline(doc["subtitle"], unicode_ok), styles["subtitle"]))
        if doc.get("meta"):
            story.append(Paragraph(_inline(doc["meta"], unicode_ok), styles["meta"]))
        story.append(Spacer(1, 1.5 * mm))
        story.append(HRFlowable(
            width="100%", thickness=0.8, color=HexColor(_RULE), spaceAfter=5 * mm,
        ))

    if doc.get("lead"):
        story.append(_callout(doc["lead"], styles, width, unicode_ok))
        story.append(Spacer(1, 4 * mm))

    number = 0
    first_section = True
    for section in doc["sections"]:
        if not first_section and kind == "notes" and section["heading"]:
            story.append(HRFlowable(
                width="100%", thickness=0.3, color=HexColor(_RULE),
                spaceBefore=2 * mm, spaceAfter=2 * mm,
            ))
        if section["heading"]:
            story.append(CondPageBreak(16 * mm))
            if kind == "list" and not first_section:
                story.append(HRFlowable(
                    width=22, thickness=1.6, color=HexColor(_RULE),
                    spaceBefore=1 * mm, spaceAfter=1.5 * mm, hAlign="LEFT",
                ))
            label = section["heading"]
            if kind == "report":
                number += 1
                label = f"{number}.  {label}"
            story.append(Paragraph(_inline(label, unicode_ok), styles["h1"]))
        for block in section["blocks"]:
            if block["type"] == "p":
                closing = (kind == "letter" and _CLOSE.search(block["text"])
                           and len(block["text"]) < 180)
                style = styles["sign"] if closing else styles["body"]
                story.append(Paragraph(_inline(block["text"], unicode_ok), style))
            elif block["type"] == "bullets":
                for item in block["items"]:
                    story.append(Paragraph(
                        _inline(item, unicode_ok), styles["bullet"], bulletText="•",
                    ))
                story.append(Spacer(1, 1.5 * mm))
            elif block["type"] == "table":
                story.append(Spacer(1, 1 * mm))
                story.append(_table_flowable(block, styles, width, unicode_ok))
                story.append(Spacer(1, 3 * mm))
            elif block["type"] == "code":
                story.append(Spacer(1, 1 * mm))
                story.append(_code_flowable(block["text"], styles, width, unicode_ok))
                story.append(Spacer(1, 3 * mm))
        first_section = False
    if not story:
        story.append(Paragraph(_inline(doc["title"], unicode_ok), styles["title"]))
    return story


def _page_painter(title: str, fonts: dict):
    from reportlab.lib.colors import HexColor
    from reportlab.lib.units import mm

    label = title.strip() or ""

    def paint(canvas, _doc):
        canvas.saveState()
        width, height = canvas._pagesize
        canvas.setFillColor(HexColor(_PAPER))
        canvas.rect(0, 0, width, height, fill=1, stroke=0)
        canvas.setStrokeColor(HexColor(_RULE))
        canvas.setLineWidth(0.8)
        canvas.line(18 * mm, height - 12 * mm, width - 18 * mm, height - 12 * mm)
        canvas.setFillColor(HexColor(_MUTED))
        canvas.setFont(fonts["italic"], 8)
        canvas.drawString(18 * mm, 10 * mm, label[:90])
        canvas.setFont(fonts["reg"], 8)
        canvas.drawRightString(width - 18 * mm, 10 * mm, str(canvas.getPageNumber()))
        canvas.restoreState()

    return paint


def _write_pdf(doc: dict, dest: Path) -> int:
    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate
    except ImportError as exc:
        raise RuntimeError("reportlab is not installed. Run: pip install reportlab") from exc

    fonts = _register_fonts()
    pagesize = landscape(A4) if _max_columns(doc) >= 6 else A4
    footer = doc["title"]
    if doc.get("title_is_fallback") and doc["kind"] == "letter":
        footer = (doc.get("meta") or "").split("\n")[0]
    paint = _page_painter(footer, fonts)
    document = SimpleDocTemplate(
        str(dest), pagesize=pagesize,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=16 * mm,
        title=doc["title"], author="MARK LVI",
    )
    styles = _styles(fonts, doc["kind"])
    width = pagesize[0] - 36 * mm
    # Rebuild table/code paragraphs with the font's encoding. Styles already
    # carry the font; the inline helper only needs the flag for the glyphs.
    story = _story(doc, styles, width, fonts["unicode"])
    document.build(story, onFirstPage=paint, onLaterPages=paint)
    return _page_count(dest)


def _page_count(dest: Path) -> int:
    try:
        import pdfplumber
    except ImportError:
        return 0
    with pdfplumber.open(str(dest)) as pdf:
        count = len(pdf.pages)
        text = "\n".join((page.extract_text() or "") for page in pdf.pages[:4])
    if len(text.strip()) < 8:
        raise RuntimeError("the PDF was written but no readable text came back out of it")
    return count


def _destination(path: str, filename: str, title: str) -> Path:
    from actions.file_controller import _get_desktop, _is_safe_path, _resolve_path

    name = _filename(filename, title)
    raw = (path or "").strip()
    if raw:
        target = _resolve_path(raw)
        if target.suffix.lower() == ".pdf":
            dest = target
        else:
            dest = target / name
    else:
        dest = _get_desktop() / name
    dest = _unique(dest)
    if not _is_safe_path(dest):
        raise PermissionError(f"that folder is outside the home directory: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


def _filename(filename: str, title: str) -> str:
    raw = (filename or "").strip() or _slug(title)
    raw = Path(raw.replace("\\", "/")).name
    raw = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", raw).strip().rstrip(".")
    if not raw:
        raw = "documento"
    if not raw.lower().endswith(".pdf"):
        raw += ".pdf"
    if len(raw) > 120:
        raw = raw[:110].rstrip(" .-") + ".pdf"
    return raw


def _unique(dest: Path) -> Path:
    if not dest.exists():
        return dest
    for number in range(2, 100):
        candidate = dest.with_name(f"{dest.stem}-{number}{dest.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"too many files already named {dest.name}")


def _undo_pdf(target: Path):
    def _fn():
        if target.exists() and target.is_file():
            target.unlink()
            return f"Removed '{target.name}'."
        return f"'{target.name}' is already gone."
    return _fn


def _param(parameters: dict, *keys):
    for key in keys:
        if key in parameters and parameters[key] not in (None, ""):
            return parameters[key]
    return ""


def create_pdf(parameters: dict, player=None) -> str:
    """Build a PDF whose layout follows the information it was given."""
    content = _param(parameters, "content", "text", "body")
    if isinstance(content, str):
        content = content.strip()
    if content in ("", None) or content == []:
        return outcome.failed(
            "create_pdf", "there is no information to put in the PDF")

    title = str(_param(parameters, "title") or "").strip()
    kind = str(_param(parameters, "kind") or "")
    path = str(_param(parameters, "path", "destination", "folder") or "")
    filename = str(_param(parameters, "filename", "name") or "")

    try:
        doc = _structure(content, title, kind)
        if not doc["sections"] and not doc.get("lead"):
            return outcome.failed(
                "create_pdf", "there is no information to put in the PDF")
        dest = _destination(path, filename, doc["title"])
        pages = _write_pdf(doc, dest)
        if not dest.exists() or dest.stat().st_size < 400:
            return outcome.failed(
                "create_pdf", "the PDF file was not written")
        if dest.read_bytes()[:5] != b"%PDF-":
            dest.unlink(missing_ok=True)
            return outcome.failed(
                "create_pdf", "the file that was written is not a PDF")
        push_undo(f"create {dest.name}", _undo_pdf(dest))
        if player is not None and hasattr(player, "write_log"):
            try:
                player.write_log(f"PDF  {dest.name}")
            except Exception:
                pass
        facts = {"path": str(dest), "kind": doc["kind"], "title": doc["title"]}
        if pages:
            facts["pages"] = pages
        return outcome.done("create_pdf", **facts)
    except PermissionError as exc:
        return outcome.failed("create_pdf", str(exc))
    except Exception as exc:
        traceback.print_exc()
        return outcome.failed(
            "create_pdf", str(exc) or "the PDF could not be written")


TOOL = {
    "name": "create_pdf",
    "description": (
        "Creates a real PDF from a set of information the user wants saved, "
        "printed or handed over. Use this whenever the user asks for a PDF, "
        "a printable document, or a formatted file of notes, a report, a "
        "letter, a list or a table — including when they ask to 'put this "
        "in a PDF'. Do not use file_controller or file_processor to create "
        "a PDF: one writes plain text, the other only reads a file that "
        "already exists. Pass the information in 'content', in the user's "
        "language, as completely as you have it. Write it as markdown when "
        "you can: a title if you did not pass one, ## headings for sections, "
        "- bullets for items, and a pipe table when the data is rows. The "
        "tool chooses the page layout from that shape (report, letter, list, "
        "table, notes or summary). Set 'kind' only when the user was explicit. "
        "'path' is a folder shortcut (desktop, documents, downloads) or a full "
        "path; the default is the Desktop. 'filename' is optional."
    ),
    "brief": (
        "Creates a formatted PDF from the information in 'content' and saves "
        "it. Default folder is the Desktop. Use when the user asks for a PDF."
    ),
    "tier": "core",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "content": {
                "type": "STRING",
                "description": (
                    "The information to put in the PDF, in the user's language, "
                    "complete. Markdown headings, bullets and pipe tables tell "
                    "the layout what the material is."
                ),
            },
            "title": {
                "type": "STRING",
                "description": "Document title. Omit it to let the tool take one from the content.",
            },
            "kind": {
                "type": "STRING",
                "description": (
                    "Optional. report, letter, list, table, notes or summary. "
                    "Leave empty unless the user asked for that kind of document."
                ),
            },
            "path": {
                "type": "STRING",
                "description": (
                    "Where to save it. A shortcut (desktop, documents, downloads, "
                    "home) or a full path to a folder or a .pdf file. Default: desktop."
                ),
            },
            "filename": {
                "type": "STRING",
                "description": "File name. '.pdf' is added if missing. Default: a name taken from the title.",
            },
        },
        "required": ["content"],
    },
    "handler": create_pdf,
}
