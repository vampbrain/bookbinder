"""Bookbinder core: read many files -> one clean HTML intermediate -> export anywhere.

No Streamlit imports here, so this can be tested or reused from a CLI.
"""
from __future__ import annotations

import base64
import html as htmllib
import io
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import urllib.parse
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup

SUPPORTED = ["txt", "md", "markdown", "doc", "docx", "pdf", "pages"]

esc = htmllib.escape

LANGUAGES = [
    ("English", "en"), ("Tamil (தமிழ்)", "ta"), ("Hindi (हिन्दी)", "hi"), ("Telugu (తెలుగు)", "te"),
    ("Kannada (ಕನ್ನಡ)", "kn"), ("Malayalam (മലയാളം)", "ml"), ("Bengali (বাংলা)", "bn"),
    ("Marathi (मराठी)", "mr"), ("Gujarati (ગુજરાતી)", "gu"), ("Punjabi (ਪੰਜਾਬੀ)", "pa"),
    ("Sinhala (සිංහල)", "si"), ("French", "fr"), ("German", "de"), ("Spanish", "es"),
    ("Portuguese", "pt"), ("Italian", "it"),
]

# Preferred PDF fonts per language (Noto, from the fonts-noto-core package)
LANG_FONTS = {
    "ta": "'Noto Serif Tamil', 'Noto Sans Tamil'", "hi": "'Noto Serif Devanagari', 'Noto Sans Devanagari'",
    "mr": "'Noto Serif Devanagari', 'Noto Sans Devanagari'", "te": "'Noto Serif Telugu', 'Noto Sans Telugu'",
    "kn": "'Noto Serif Kannada', 'Noto Sans Kannada'", "ml": "'Noto Serif Malayalam', 'Noto Sans Malayalam'",
    "bn": "'Noto Serif Bengali', 'Noto Sans Bengali'", "gu": "'Noto Serif Gujarati', 'Noto Sans Gujarati'",
    "pa": "'Noto Sans Gurmukhi'", "si": "'Noto Serif Sinhala', 'Noto Sans Sinhala'",
}


def _in(w, h):
    return (round(w * 25.4, 1), round(h * 25.4, 1))


# region -> {label: (width_mm, height_mm)}; the first entry in each region is the default.
# India (Tamil) uses the Demy/Crown/Royal fractions common in Indian & Tamil publishing
# (approximate inches, per publisher listings, e.g. Navakarnataka); confirm with your printer.
SIZES = {
    "US": {
        "Trade 6 × 9 in": _in(6, 9),
        "Digest / trade 5.5 × 8.5 in": _in(5.5, 8.5),
        "Trade 5.25 × 8 in": _in(5.25, 8),
        "Trade 5 × 8 in": _in(5, 8),
        "Mass-market paperback 4.25 × 6.87 in": _in(4.25, 6.87),
        "US Letter 8.5 × 11 in": _in(8.5, 11),
    },
    "UK": {
        "B-format (trade paperback) 129 × 198 mm": (129.0, 198.0),
        "A-format (mass-market) 111 × 178 mm": (111.0, 178.0),
        "Demy 138 × 216 mm": (138.0, 216.0),
        "A5 148 × 210 mm": (148.0, 210.0),
        "Royal 156 × 234 mm": (156.0, 234.0),
        "Crown Quarto 189 × 246 mm": (189.0, 246.0),
        "A4 210 × 297 mm": (210.0, 297.0),
    },
    "India (Tamil)": {
        # 4-tuples are (page_w, page_h, text_w, text_h) in mm; margins are centred around the text area
        "Tamil press 140 × 215 mm (text 92 × 168.5 mm)": (140.0, 215.0, 92.0, 168.5),
        "1/8 Demy 5.5 × 8.5 in (most common)": _in(5.5, 8.5),
        "1/8 Crown 4.5 × 7 in (pocket)": _in(4.5, 7),
        "1/4 Royal 6.5 × 10 in": _in(6.5, 10),
        "1/4 Crown 7 × 9.5 in": _in(7, 9.5),
        "1/4 Demy 8.5 × 11 in": _in(8.5, 11),
        "A5 148 × 210 mm": (148.0, 210.0),
    },
    "International": {
        "A4 210 × 297 mm": (210.0, 297.0),
        "A5 148 × 210 mm": (148.0, 210.0),
        "A6 105 × 148 mm": (105.0, 148.0),
        "B5 176 × 250 mm": (176.0, 250.0),
        "Letter 8.5 × 11 in": _in(8.5, 11),
    },
}


def margins_mm(w: float, h: float, text=None):
    """(top/bottom, left/right) margins. With an explicit text area they centre it; otherwise they scale with the page."""
    if text:
        return round((h - text[1]) / 2, 2), round((w - text[0]) / 2, 2)
    return round(max(14.0, min(24.0, h * 0.095)), 1), round(max(12.0, min(22.0, w * 0.115)), 1)


def body_pt(w: float) -> float:
    return 10 if w < 125 else 10.5 if w < 150 else 11


@dataclass
class Parsed:
    name: str
    title: str = ""
    body: str = ""
    words: int = 0
    warnings: list = field(default_factory=list)
    error: str | None = None


@dataclass
class FontInfo:
    family: str          # real family name read from the font file (what Word needs)
    data: bytes
    ext: str = ".ttf"
    has_tamil: bool = False


def font_info(data: bytes, filename: str = "") -> FontInfo:
    """Read the family name and Tamil coverage from a font file, whatever it is called on disk."""
    from fontTools.ttLib import TTFont

    f = TTFont(io.BytesIO(data), fontNumber=0, lazy=True)
    names = f["name"]
    family = names.getDebugName(16) or names.getDebugName(1) or Path(filename).stem
    cmap = f.getBestCmap() or {}
    ext = Path(filename).suffix.lower()
    if ext not in (".ttf", ".otf"):
        ext = ".otf" if data[:4] == b"OTTO" else ".ttf"
    return FontInfo(family, data, ext, 0x0B95 in cmap and 0x0BCD in cmap)


def list_fonts() -> dict:
    """Fonts dropped into the ./fonts folder next to this file: {label: FontInfo}."""
    out = {}
    d = Path(__file__).parent / "fonts"
    for f in sorted(d.glob("*")) if d.is_dir() else []:
        if f.suffix.lower() in (".ttf", ".otf"):
            try:
                fi = font_info(f.read_bytes(), f.name)
                out[f"{fi.family} ({f.name})"] = fi
            except Exception:
                continue
    return out


@dataclass
class Book:
    title: str = "Combined Book"
    author: str = ""
    language: str = "en"
    toc: bool = True
    page_mm: tuple = (210.0, 297.0)  # (width, height) in mm; used by PDF and DOCX
    text_mm: tuple | None = None     # optional text-area (width, height) in mm; margins are centred around it
    font: "FontInfo | None" = None   # optional custom font used in PDF, DOCX and EPUB
    cover: bytes | None = None


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def nice_title(filename: str) -> str:
    stem = Path(filename).stem
    return re.sub(r"\s+", " ", re.sub(r"[_\-]+", " ", stem)).strip() or "Untitled"


def decode_text(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            from charset_normalizer import from_bytes

            best = from_bytes(data).best()
            if best is not None:
                return str(best)
        except Exception:
            pass
        return data.decode("cp1252", "replace")


# --------------------------------------------------------------------------
# readers: each returns an HTML string
# --------------------------------------------------------------------------
def txt_to_html(text: str, reflow: bool = True) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    paras = re.split(r"\n\s*\n", text)
    if len(paras) == 1 and "\n" in paras[0]:
        paras = paras[0].split("\n")  # no blank lines at all: one paragraph per line
    out = []
    for p in paras:
        lines = [ln.strip() for ln in p.split("\n") if ln.strip()]
        if not lines:
            continue
        if reflow:
            out.append(f"<p>{esc(' '.join(lines))}</p>")
        else:
            out.append(f"<p>{'<br/>'.join(esc(ln) for ln in lines)}</p>")
    return "\n".join(out)


def md_to_html(text: str) -> str:
    import markdown

    text = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", text, flags=re.S)  # YAML front matter
    return markdown.markdown(text, extensions=["extra", "sane_lists"])


def docx_to_html(data: bytes) -> str:
    import mammoth

    style_map = "p[style-name='Title'] => h1:fresh"
    res = mammoth.convert_to_html(io.BytesIO(data), style_map=style_map)
    return res.value


def doc_to_html(data: bytes, warnings: list) -> str:
    # Many ".doc" files are really docx / rtf in disguise.
    if data[:4] == b"PK\x03\x04":
        return docx_to_html(data)
    if data.lstrip()[:5] == b"{\\rtf":
        import pypandoc

        return pypandoc.convert_text(data.decode("latin-1"), "html", format="rtf")

    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    antiword = shutil.which("antiword")
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.doc"
        src.write_bytes(data)
        if soffice:
            try:
                subprocess.run(
                    [soffice, "--headless", "--convert-to", "docx", "--outdir", td, str(src)],
                    check=True, timeout=180, capture_output=True,
                    env={**os.environ, "HOME": td},
                )
                return docx_to_html((Path(td) / "in.docx").read_bytes())
            except Exception:
                pass
        if antiword:
            out = subprocess.run(
                [antiword, "-w", "0", "-m", "UTF-8.txt", str(src)],
                check=True, timeout=90, capture_output=True,
            ).stdout
            warnings.append("Legacy .doc read as plain text (formatting/images lost). "
                            "Install LibreOffice on the server, or save as .docx, for full fidelity.")
            return txt_to_html(out.decode("utf-8", "replace"), reflow=True)
    raise RuntimeError("Can't read legacy .doc here: install `antiword` or LibreOffice, or save the file as .docx.")


_PAGE_NO = re.compile(r"^(page\s*)?\d{1,4}(\s*(of|/)\s*\d{1,4})?$", re.I)


def _join_lines(lines):
    out = ""
    for ln in lines:
        if not out:
            out = ln
        elif out.endswith("-") and ln[:1].islower():
            out = out[:-1] + ln
        else:
            out += " " + ln
    return out


def pdf_to_html(data: bytes, warnings: list) -> str:
    import pymupdf as fitz  # PyMuPDF

    doc = fitz.open(stream=data, filetype="pdf")
    if doc.needs_pass:
        raise ValueError("PDF is password-protected.")
    sizes: Counter = Counter()
    pages = []
    for page in doc:
        blocks = []
        for b in page.get_text("dict", sort=True)["blocks"]:
            if b.get("type") != 0:
                continue
            lines, wsum, n = [], 0.0, 0
            for ln in b["lines"]:
                txt = "".join(s["text"] for s in ln["spans"]).strip()
                if txt:
                    lines.append(txt)
                for s in ln["spans"]:
                    c = len(s["text"].strip())
                    if c:
                        sizes[round(s["size"])] += c
                        wsum += s["size"] * c
                        n += c
            if lines and n:
                blocks.append((_join_lines(lines), wsum / n))
        pages.append(blocks)
    if not sizes:
        raise ValueError("No selectable text found (scanned/image-only PDF? This app does not do OCR).")
    body_size = sizes.most_common(1)[0][0] or 1

    # drop running headers/footers: first/last block text repeated on many pages
    def sig(t):
        return re.sub(r"\d+", "#", t.lower())[:80]

    edge: Counter = Counter()
    for blocks in pages:
        if blocks:
            for t in {sig(blocks[0][0]), sig(blocks[-1][0])}:
                edge[t] += 1
    cutoff = max(3, int(len(pages) * 0.4))
    repeated = {t for t, c in edge.items() if c >= cutoff}

    items: list = []
    for blocks in pages:
        page_start = True
        for i, (text, size) in enumerate(blocks):
            if _PAGE_NO.match(text.strip()):
                continue
            if i in (0, len(blocks) - 1) and sig(text) in repeated:
                continue
            ratio = size / body_size
            tag = "p"
            if len(text) < 160:
                tag = "h1" if ratio >= 1.5 else "h2" if ratio >= 1.2 else "p"
            if (page_start and tag == "p" and items and items[-1][0] == "p"
                    and not re.search(r"[.!?:;\"”’)\]]$", items[-1][1]) and text[:1].islower()):
                items[-1] = ("p", _join_lines([items[-1][1], text]))
            else:
                items.append((tag, text))
            page_start = False
    html = "\n".join(f"<{t}>{esc(x)}</{t}>" for t, x in items)
    if len(re.sub(r"<[^>]+>", " ", html).split()) < 5:
        raise ValueError("No usable text found in this PDF.")
    html = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", html)  # broken-glyph control chars
    warnings.append("PDF text was re-flowed; headings are guessed from font size and columns/tables may be imperfect.")
    if re.search(r"[\u0B80-\u0BFF]", html):
        warnings.append("Tamil text in PDFs often extracts with missing or misplaced vowel signs (ொ ோ ௌ). "
                        "Proofread the result, or use the .docx/.txt source if you have it.")
    return html


# ---- .pages (Apple iWork): zip of Snappy-compressed protobuf (.iwa) ---------
def _varint(buf, i):
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def _snappy(buf: bytes) -> bytes:
    _, i = _varint(buf, 0)
    out = bytearray()
    while i < len(buf):
        tag = buf[i]
        i += 1
        t = tag & 3
        if t == 0:
            ln = tag >> 2
            if ln < 60:
                ln += 1
            else:
                nb = ln - 59
                ln = int.from_bytes(buf[i:i + nb], "little") + 1
                i += nb
            out += buf[i:i + ln]
            i += ln
        else:
            if t == 1:
                ln = 4 + ((tag >> 2) & 7)
                off = ((tag >> 5) << 8) | buf[i]
                i += 1
            elif t == 2:
                ln = 1 + (tag >> 2)
                off = int.from_bytes(buf[i:i + 2], "little")
                i += 2
            else:
                ln = 1 + (tag >> 2)
                off = int.from_bytes(buf[i:i + 4], "little")
                i += 4
            start = len(out) - off
            if off >= ln:
                out += out[start:start + ln]
            else:
                for _ in range(ln):
                    out.append(out[-off])
    return bytes(out)


def _iwa_decompress(raw: bytes) -> bytes:
    i, out = 0, bytearray()
    while i < len(raw):
        if raw[i] != 0:
            raise ValueError("bad IWA chunk")
        ln = int.from_bytes(raw[i + 1:i + 4], "little")
        i += 4
        out += _snappy(raw[i:i + ln])
        i += ln
    return bytes(out)


def _pb(buf: bytes):
    i, fields = 0, []
    while i < len(buf):
        key, i = _varint(buf, i)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(buf, i)
        elif wt == 1:
            v, i = buf[i:i + 8], i + 8
        elif wt == 2:
            ln, i = _varint(buf, i)
            v, i = buf[i:i + ln], i + ln
        elif wt == 5:
            v, i = buf[i:i + 4], i + 4
        else:
            raise ValueError("bad wire type")
        fields.append((fn, wt, v))
    return fields


def _iwa_messages(stream: bytes):
    i = 0
    while i < len(stream):
        n, i = _varint(stream, i)
        info = _pb(stream[i:i + n])
        i += n
        for fn, wt, v in info:
            if fn == 2 and wt == 2:
                mi = _pb(v)
                typ = next((x for f, _, x in mi if f == 1), None)
                ln = next((x for f, _, x in mi if f == 3), 0)
                yield typ, stream[i:i + ln]
                i += ln


def _utf8(b):
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _iwa_blobs(z: zipfile.ZipFile):
    names = z.namelist()
    for n in names:
        if n.lower().endswith(".iwa") and "document" in n.lower():
            yield z.read(n)
    if "Index.zip" in names:
        yield from _iwa_blobs(zipfile.ZipFile(io.BytesIO(z.read("Index.zip"))))


def pages_to_html(data: bytes, warnings: list) -> str:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("Not a readable .pages package. In Pages: File > Export To > Word or PDF, then upload that.")
    best, fallback = "", ""
    for blob in _iwa_blobs(z):
        try:
            for typ, payload in _iwa_messages(_iwa_decompress(blob)):
                strs = [s for fn, wt, v in _pb(payload) if fn == 3 and wt == 2 and (s := _utf8(v))]
                text = "\n".join(strs)
                if typ == 2001:  # TSWP.StorageArchive = body/footnote/textbox text
                    if len(text) > len(best):
                        best = text
                elif len(text) > len(fallback):
                    fallback = text
        except Exception:
            continue
    text = best or fallback
    for ch in ("\u2028", "\u2029", "\x0b", "\x0c"):
        text = text.replace(ch, "\n")
    text = text.replace("\ufffc", "")
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    if not paras:
        raise ValueError("Couldn't extract text from this .pages file. In Pages: File > Export To > Word or PDF, then upload that.")
    warnings.append(".pages is read as plain text only (styles, images, tables are dropped). Export to Word from Pages for full fidelity.")
    return "\n".join(f"<p>{esc(p)}</p>" for p in paras)


# --------------------------------------------------------------------------
# sanitising + normalising the HTML intermediate
# --------------------------------------------------------------------------
ALLOWED = {
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "br", "hr", "strong", "b", "em", "i", "u", "s", "del",
    "sub", "sup", "blockquote", "ul", "ol", "li", "pre", "code", "a", "img", "table", "thead",
    "tbody", "tfoot", "tr", "th", "td", "figure", "figcaption",
}
DROP = ["script", "style", "iframe", "object", "embed", "form", "input", "button", "svg",
        "head", "title", "meta", "link", "noscript", "template"]
SAFE_HREF = re.compile(r"^(https?://|mailto:)", re.I)


def clean_html(raw: str):
    soup = BeautifulSoup(raw, "lxml")
    for t in soup.find_all(DROP):
        t.decompose()
    body = soup.body or soup
    for t in list(body.find_all(True)):
        if t.name not in ALLOWED:
            t.unwrap()
            continue
        keep = {}
        if t.name == "a" and SAFE_HREF.match(t.get("href", "")):
            keep["href"] = t["href"]
        elif t.name == "img":
            src = t.get("src", "")
            if src.startswith("data:image/"):  # only embedded images; never fetch remote/local files
                keep = {"src": src, "alt": t.get("alt", "")}
            else:
                t.decompose()
                continue
        elif t.name in ("td", "th"):
            keep = {k: t[k] for k in ("colspan", "rowspan") if t.has_attr(k)}
        t.attrs = keep
    for t in list(body.find_all(["p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"])):
        if not t.get_text(strip=True) and not t.find("img"):
            t.decompose()
    return body


def finalize(raw_html: str, fallback_title: str):
    body = clean_html(raw_html)
    title = fallback_title
    h1s = body.find_all("h1")
    first = next((c for c in body.children if getattr(c, "name", None)), None)
    if first is not None and first.name == "h1" and len(h1s) == 1:
        t = first.get_text(" ", strip=True)
        if t:
            title = t[:200]
        first.decompose()
    if body.find("h1"):  # file has its own top-level headings -> nest them under the chapter title
        for h in body.find_all(re.compile(r"^h[1-6]$")):
            h.name = f"h{min(int(h.name[1]) + 1, 6)}"
    words = len(body.get_text(" ").split())
    # canonical Unicode form: joins split Tamil vowel signs (ெ+ா -> ொ) so every font/engine shapes them alike
    return title, unicodedata.normalize("NFC", body.decode_contents()), words


def parse_file(name: str, data: bytes, reflow: bool = True) -> Parsed:
    ext = Path(name).suffix.lower().lstrip(".")
    res = Parsed(name=name, title=nice_title(name))
    try:
        if ext == "txt":
            raw = txt_to_html(decode_text(data), reflow)
        elif ext in ("md", "markdown"):
            raw = md_to_html(decode_text(data))
        elif ext == "docx":
            raw = docx_to_html(data)
        elif ext == "doc":
            raw = doc_to_html(data, res.warnings)
        elif ext == "pdf":
            raw = pdf_to_html(data, res.warnings)
        elif ext == "pages":
            raw = pages_to_html(data, res.warnings)
        else:
            raise ValueError(f"Unsupported file type: .{ext}")
        res.title, res.body, res.words = finalize(raw, res.title)
        if res.words == 0:
            res.error = "No text found in this file."
    except Exception as e:  # surfaced in the UI, never crashes the batch
        res.error = f"{type(e).__name__}: {e}"
    return res


# ---- zip archives ----------------------------------------------------------
MAX_ZIP_ENTRIES = 3000
MAX_ZIP_ENTRY = 300 * 1024**2
MAX_ZIP_TOTAL = 1024**3


def _zip_entry_name(zi: zipfile.ZipInfo) -> str:
    n = zi.filename
    if not zi.flag_bits & 0x800:  # no UTF-8 flag: zipfile guessed cp437; Tamil/Unicode names are often UTF-8 anyway
        try:
            n = n.encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return n.replace("\\", "/")


def parse_zip(zip_name: str, data: bytes, reflow: bool = True, _depth: int = 0, _prefix: str | None = None):
    """Parse every supported file inside a zip. Returns (list[Parsed], list[str] notes)."""
    prefix = _prefix or Path(zip_name).stem
    out, notes = [], []
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return [], [f"{zip_name}: not a valid zip file."]
    infos = z.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        return [], [f"{zip_name}: too many entries (limit {MAX_ZIP_ENTRIES})."]
    if sum(i.file_size for i in infos) > MAX_ZIP_TOTAL:
        return [], [f"{zip_name}: too large once unpacked (limit 1 GB)."]
    skipped = 0
    for zi in infos:
        if zi.is_dir():
            continue
        name = _zip_entry_name(zi)
        parts = name.split("/")
        base = parts[-1]
        if (base.startswith((".", "~$")) or "__MACOSX" in parts
                or base.lower() in ("thumbs.db", "desktop.ini")):
            continue
        ext = Path(base).suffix.lower().lstrip(".")
        if zi.flag_bits & 1:
            notes.append(f"{zip_name}: '{name}' is encrypted and was skipped.")
        elif zi.file_size > MAX_ZIP_ENTRY:
            notes.append(f"{zip_name}: '{name}' is over 300 MB and was skipped.")
        elif ext == "zip" and _depth < 1:
            sub, sub_notes = parse_zip(name, z.read(zi), reflow, _depth + 1, f"{prefix}/{name[:-4]}")
            out += sub
            notes += sub_notes
        elif ext in SUPPORTED:
            out.append(parse_file(f"{prefix}/{name}", z.read(zi), reflow))
        else:
            skipped += 1
    if skipped:
        notes.append(f"{zip_name}: skipped {skipped} file(s) of unsupported types.")
    if not out and not notes:
        notes.append(f"{zip_name}: no supported files inside.")
    return out, notes


# --------------------------------------------------------------------------
# exporters
# --------------------------------------------------------------------------
def chapters_html(docs) -> str:
    return "\n".join(f'<h1 id="ch{i}">{esc(d.title)}</h1>\n{d.body}' for i, d in enumerate(docs, 1))


# key -> (pandoc writer, extension, label, mime)
FORMATS = {
    "docx": ("docx", "docx", "DOCX (Word / Kindle)", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "epub": ("epub3", "epub", "EPUB (Kindle / e-readers)", "application/epub+zip"),
    "pdf": (None, "pdf", "PDF", "application/pdf"),
    "odt": ("odt", "odt", "ODT (LibreOffice)", "application/vnd.oasis.opendocument.text"),
    "rtf": ("rtf", "rtf", "RTF", "application/rtf"),
    "md": ("gfm", "md", "Markdown", "text/markdown"),
    "txt": ("plain", "txt", "Plain text", "text/plain"),
    "html": ("html5", "html", "HTML (single file)", "text/html"),
}


def _docx_set_font(d, family: str):
    """Point every paragraph style (and the document defaults) at one family, incl. the complex-script slot Tamil uses."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    def apply(rpr):
        rf = rpr.find(qn("w:rFonts"))
        if rf is None:
            rf = OxmlElement("w:rFonts")
            rpr.insert(0, rf)
        for a in ("w:asciiTheme", "w:hAnsiTheme", "w:cstheme", "w:eastAsiaTheme"):
            rf.attrib.pop(qn(a), None)
        for a in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
            rf.set(qn(a), family)

    defaults = d.styles.element.find(qn("w:docDefaults"))
    if defaults is not None:
        rpr = defaults.find(qn("w:rPrDefault") + "/" + qn("w:rPr"))
        if rpr is not None:
            apply(rpr)
    for st in d.styles:
        if st.type == 1 and st.name not in ("Source Code", "Verbatim Char"):  # paragraph styles
            apply(st.element.get_or_add_rPr())


def export_pandoc(fmt: str, docs, book: Book) -> bytes:
    import pypandoc

    writer, ext, _, _ = FORMATS[fmt]
    html = chapters_html(docs)
    if fmt in ("md", "txt"):
        html = re.sub(r"<img\b[^>]*>", "", html, flags=re.I)
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, f"book.{ext}")
        args = ["--metadata", f"title={book.title}", "--metadata", f"lang={book.language or 'en'}"]
        if book.author:
            args += ["--metadata", f"author={book.author}"]
        if book.toc and fmt not in ("md", "txt", "rtf"):
            args += ["--toc", "--toc-depth=2"]
        if fmt in ("rtf", "html"):
            args.append("-s")
        if fmt in ("md", "txt"):
            args.append("--wrap=none")
        if fmt == "epub" and book.cover:
            cp = os.path.join(td, "cover." + ("png" if book.cover[:4] == b"\x89PNG" else "jpg"))
            Path(cp).write_bytes(book.cover)
            args.append(f"--epub-cover-image={cp}")
        if fmt == "epub" and book.font:
            fp = os.path.join(td, "bookfont" + book.font.ext)
            Path(fp).write_bytes(book.font.data)
            cp = os.path.join(td, "bookfont.css")
            Path(cp).write_text(
                '@font-face { font-family: "BookFont"; font-weight: normal; font-style: normal; '
                f'src: url("../fonts/bookfont{book.font.ext}"); }}\n'
                'body, p, li, td, th, blockquote, h1, h2, h3, h4, h5, h6 { font-family: "BookFont", serif; }\n',
                encoding="utf-8")
            args += [f"--epub-embed-font={fp}", f"--css={cp}"]
        pypandoc.convert_text(html, writer, format="html", outputfile=out, extra_args=args)
        if fmt == "docx":  # every file starts on a new page (Kindle also builds its chapter list from Heading 1)
            from docx import Document

            d = Document(out)
            from docx.shared import Mm

            d.styles["Heading 1"].paragraph_format.page_break_before = True
            w, h = book.page_mm
            tb, lr = margins_mm(w, h, book.text_mm)
            if book.font:
                _docx_set_font(d, book.font.family)
            for sec in d.sections:
                sec.page_width, sec.page_height = Mm(w), Mm(h)
                sec.top_margin = sec.bottom_margin = Mm(tb)
                sec.left_margin = sec.right_margin = Mm(lr)
            d.save(out)
        return Path(out).read_bytes()


_PDF_CSS = """
@page { size: __W__mm __H__mm; margin: __TB__mm __LR__mm; @bottom-center { content: counter(page); font-size: 9pt; color: #666; } }
body { margin: 0; font-family: __FONTS__; font-size: __PT__pt; line-height: 1.55; }
h1 { break-before: page; font-size: 2em; margin: 0 0 1em; line-height: 1.25; }
h2 { font-size: 1.45em; margin: 1.4em 0 .5em; line-height: 1.3; } h3 { font-size: 1.2em; margin: 1.2em 0 .4em; }
h2, h3, h4 { break-after: avoid; }
p { margin: 0 0 .7em; orphans: 3; widows: 3; }
img { max-width: 100%; height: auto; }
pre, code { font-family: 'DejaVu Sans Mono', monospace; font-size: 9pt; } pre { white-space: pre-wrap; }
blockquote { margin: 1em 1.5em; color: #444; font-style: italic; }
table { border-collapse: collapse; margin: 1em 0; } td, th { border: 1px solid #bbb; padding: 3px 6px; }
.titlepage { text-align: center; padding-top: 35%; }
.book-title { font-size: 2.7em; font-weight: bold; margin: 0 0 .5em; } .author { font-size: 1.3em; color: #444; }
.toc { break-before: page; } .toc h2 { margin-top: 0; }
.toc ol { list-style: none; padding: 0; } .toc li { margin: .35em 0; }
.toc a { color: inherit; text-decoration: none; }
.toc a::after { content: leader('.') target-counter(attr(href), page); }
"""


def _data_url_fetcher(url, *a, **k):
    if not url.startswith("data:"):
        raise ValueError("external resources are blocked")
    header, _, payload = url[5:].partition(",")
    raw = base64.b64decode(payload) if ";base64" in header else urllib.parse.unquote_to_bytes(payload)
    return {"string": raw, "mime_type": header.split(";")[0] or "text/plain"}


# ---- PDF fallback: PyMuPDF Story (pure pip, no GTK/Pango needed; handy on Windows) ----------
_SCRIPT_FONTS = {  # lang -> (Noto script name, classic Windows font file)
    "ta": ("Tamil", "latha.ttf"), "hi": ("Devanagari", "mangal.ttf"), "mr": ("Devanagari", "mangal.ttf"),
    "te": ("Telugu", "gautami.ttf"), "kn": ("Kannada", "tunga.ttf"), "ml": ("Malayalam", "kartika.ttf"),
    "bn": ("Bengali", "vrinda.ttf"), "gu": ("Gujarati", "shruti.ttf"), "pa": ("Gurmukhi", "raavi.ttf"),
    "si": ("Sinhala", "iskpota.ttf"),
}


def _find_script_font(lang: str):
    """Find a TTF covering the language's script: ./fonts first, then system font folders."""
    script = _SCRIPT_FONTS.get(lang)
    if not script:
        return None
    names = [f"NotoSerif{script[0]}-Regular.ttf", f"NotoSans{script[0]}-Regular.ttf", "Nirmala.ttf", script[1]]
    dirs = [Path(__file__).parent / "fonts", Path("C:/Windows/Fonts"),
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts",
            Path("/usr/share/fonts"), Path("/Library/Fonts"), Path("/System/Library/Fonts"),
            Path.home() / ".fonts", Path.home() / "Library" / "Fonts"]
    for n in names:
        for d in dirs:
            try:
                if d.is_dir():
                    hit = next(d.rglob(n), None)
                    if hit:
                        return hit
            except OSError:
                continue
    return None


_STORY_CSS = """
body { font-family: __FONT__serif; font-size: __PT__pt; line-height: 1.5; }
h1 { font-size: 2em; page-break-before: always; margin: 0 0 1em; }
h2 { font-size: 1.45em; margin: 1.3em 0 .5em; } h3 { font-size: 1.2em; margin: 1.1em 0 .4em; }
p { margin: 0 0 .7em; } img { max-width: 100%; }
pre { white-space: pre-wrap; font-family: monospace; font-size: .9em; }
blockquote { margin: 1em 1.5em; font-style: italic; }
table { border-collapse: collapse; } td, th { border: 1px solid #bbb; padding: 3px 6px; }
"""


def export_pdf_pymupdf(docs, book: Book) -> bytes:
    import pymupdf

    lang = (book.language or "en").split("-")[0].lower()
    w, h = book.page_mm
    tb, lr = margins_mm(w, h, book.text_mm)
    W, H = w / 25.4 * 72, h / 25.4 * 72
    mtb, mlr = tb / 25.4 * 72, lr / 25.4 * 72
    mediabox = pymupdf.Rect(0, 0, W, H)
    where = mediabox + (mlr, mtb, -mlr, -mtb)

    archive = pymupdf.Archive()
    font_face, font_name = "", ""
    if book.font:
        archive.add(book.font.data, "bookfont" + book.font.ext)
        font_face = f"@font-face {{ font-family: BookFont; src: url(bookfont{book.font.ext}); }}"
        font_name = "BookFont, "
    elif lang in _SCRIPT_FONTS:
        ff = _find_script_font(lang)
        if ff is None:
            raise RuntimeError(
                f"No font for '{lang}' found for the built-in PDF engine. Put a font such as "
                f"NotoSerif{_SCRIPT_FONTS[lang][0]}-Regular.ttf in a 'fonts' folder next to app.py "
                "(or install the WeasyPrint/GTK libraries).")
        archive.add(ff.read_bytes(), "bookfont" + ff.suffix)
        font_face = f"@font-face {{ font-family: BookFont; src: url(bookfont{ff.suffix}); }}"
        font_name = "BookFont, "
    css = font_face + _STORY_CSS.replace("__FONT__", font_name).replace("__PT__", str(body_pt(w)))

    body = chapters_html(docs)
    imgs = []

    def grab(m):  # data-URI images -> files in the Story archive
        name = f"img{len(imgs)}.{m.group(1).split('+')[0].replace('jpeg', 'jpg')}"
        archive.add(base64.b64decode(m.group(2)), name)
        imgs.append(name)
        return f'<img src="{name}"/>'

    body = re.sub(r'<img\b[^>]*?src="data:image/([a-z0-9.+-]+);base64,([^"]+)"[^>]*>', grab, body, flags=re.I)

    def build(pages):
        author = f'<p style="font-size:1.3em">{esc(book.author)}</p>' if book.author else ""
        title = (f'<table style="width:100%;margin-top:{int(H * 0.28)}pt"><tr><td style="border:none;text-align:center">'
                 f'<p style="font-size:2.6em;font-weight:bold">{esc(book.title)}</p>{author}</td></tr></table>')
        toc = ""
        if book.toc:
            rows = "".join(
                f'<tr><td style="border:none">{esc(d.title)}</td>'
                f'<td style="border:none;text-align:right">{pages[i] if pages else "00"}</td></tr>'
                for i, d in enumerate(docs))
            toc = (f'<p style="page-break-before:always;font-size:1.5em;font-weight:bold">Contents</p>'
                   f'<table style="width:100%">{rows}</table>')
        return title + toc + body

    def render(html):
        story = pymupdf.Story(html=html, user_css=css, archive=archive)
        buf = io.BytesIO()
        writer = pymupdf.DocumentWriter(buf)
        heads, pno, more = [], 0, 1

        def rec(pos):
            if pos.heading and pos.open_close & 1:
                heads.append((pos.heading, (pos.text or "").strip(), pos.page))

        while more:
            dev = writer.begin_page(mediabox)
            more, _ = story.place(where)
            story.element_positions(rec, {"page": pno})
            story.draw(dev)
            writer.end_page()
            pno += 1
        writer.close()
        return buf.getvalue(), heads

    _, heads = render(build(None))
    chapter_pages = [pg + 1 for lvl, _, pg in heads if lvl == 1]
    data, heads = render(build(chapter_pages if len(chapter_pages) == len(docs) else None))

    doc = pymupdf.open(stream=data, filetype="pdf")
    toc_entries, prev = [], 0
    for lvl, text, pg in heads:
        lvl = min(lvl, prev + 1)
        toc_entries.append([lvl, text or "-", pg + 1])
        prev = lvl
    if toc_entries:
        doc.set_toc(toc_entries)
    for i, page in enumerate(doc):
        if i:  # page numbers, skipping the title page
            label = str(i + 1)
            x = (W - pymupdf.get_text_length(label, fontname="helv", fontsize=9)) / 2
            page.insert_text((x, H - mtb / 2), label, fontname="helv", fontsize=9, color=(0.4, 0.4, 0.4))
    doc.set_metadata({"title": book.title, "author": book.author})
    return doc.tobytes(garbage=3, deflate=True)


def pdf_engine() -> str:
    """'weasyprint' if its system libraries load here, otherwise 'pymupdf' (built-in, simpler layout)."""
    try:
        import weasyprint  # noqa: F401
        return "weasyprint"
    except (OSError, ImportError):
        return "pymupdf"


def _safe_fetcher():
    """Only data: URLs may be fetched (embedded images/fonts). Works across WeasyPrint's old and new fetcher APIs."""
    try:
        from weasyprint import URLFetcher

        return URLFetcher(allowed_protocols={"data"})
    except ImportError:
        return _data_url_fetcher


def export_pdf(docs, book: Book) -> bytes:
    if pdf_engine() == "pymupdf":
        return export_pdf_pymupdf(docs, book)
    from weasyprint import HTML

    toc = ""
    if book.toc:
        items = "".join(f'<li><a href="#ch{i}">{esc(d.title)}</a></li>' for i, d in enumerate(docs, 1))
        toc = f'<nav class="toc"><h2>Contents</h2><ol>{items}</ol></nav>'
    author = f'<p class="author">{esc(book.author)}</p>' if book.author else ""
    from weasyprint.text.fonts import FontConfiguration

    w, h = book.page_mm
    tb, lr = margins_mm(w, h, book.text_mm)
    lang_font = LANG_FONTS.get((book.language or "en").split("-")[0].lower())
    face = ""
    if book.font:  # embedded as a data: URI so no files or network are touched
        mime = "font/otf" if book.font.ext == ".otf" else "font/ttf"
        face = ("@font-face { font-family: BookFont; src: url(data:%s;base64,%s); }\n"
                % (mime, base64.b64encode(book.font.data).decode()))
    fonts = ", ".join(x for x in ("BookFont" if book.font else None, "'Noto Serif'", lang_font, "'DejaVu Serif'",
                                  "'Times New Roman'", "serif") if x)
    css = face + (_PDF_CSS.replace("__W__", str(w)).replace("__H__", str(h)).replace("__TB__", str(tb))
           .replace("__LR__", str(lr)).replace("__PT__", str(body_pt(w))).replace("__FONTS__", fonts))
    html = (
        f'<!doctype html><html lang="{esc(book.language or "en")}"><head><meta charset="utf-8">'
        f"<title>{esc(book.title)}</title><style>{css}</style></head><body>"
        f'<section class="titlepage"><p class="book-title">{esc(book.title)}</p>{author}</section>'
        f"{toc}{chapters_html(docs)}</body></html>"
    )
    fc = FontConfiguration()
    return HTML(string=html, url_fetcher=_safe_fetcher()).write_pdf(font_config=fc)


def export(fmt: str, docs, book: Book) -> bytes:
    return export_pdf(docs, book) if fmt == "pdf" else export_pandoc(fmt, docs, book)