import hashlib
import hmac
import io
import os
import re
import unicodedata
import zipfile
from dataclasses import replace

import pandas as pd
import streamlit as st

import converters as cv

st.set_page_config(page_title="Bookbinder", page_icon="📚", layout="wide")


# ---- optional password gate (set APP_PASSWORD as env var or in Streamlit secrets) ----
def password_gate():
    pw = os.environ.get("APP_PASSWORD")
    if not pw:
        try:
            pw = st.secrets.get("APP_PASSWORD")
        except Exception:
            pw = None
    if not pw or st.session_state.get("authed"):
        return
    st.title("📚 Bookbinder")
    entered = st.text_input("Password", type="password")
    if entered:
        if hmac.compare_digest(entered, pw):
            st.session_state["authed"] = True
            st.rerun()
        st.error("Wrong password.")
    st.stop()


password_gate()


@st.cache_data(show_spinner=False, max_entries=64)
def parse_cached(name: str, data: bytes, reflow: bool):
    return cv.parse_file(name, data, reflow)


@st.cache_data(show_spinner=False, max_entries=8)
def parse_zip_cached(name: str, data: bytes, reflow: bool):
    return cv.parse_zip(name, data, reflow)


@st.cache_data(show_spinner=False, max_entries=8)
def font_cached(data: bytes, name: str):
    return cv.font_info(data, name)


@st.cache_data(show_spinner=False, ttl=5)
def folder_fonts():
    return cv.list_fonts()


def slug(s: str) -> str:
    # keep letters, digits AND combining marks (Tamil vowel signs are marks, which \w would strip)
    keep = "".join(c if (c.isalnum() or unicodedata.category(c).startswith("M") or c in "-_") else "_" for c in s)
    return re.sub(r"_+", "_", keep).strip("_")[:80] or "book"


# ---- sidebar ---------------------------------------------------------------
with st.sidebar:
    st.header("Book details")
    book_title = st.text_input("Title", "My Combined Book")
    author = st.text_input("Author", "")
    lang_names = [n for n, _ in cv.LANGUAGES] + ["Other..."]
    lang_name = st.selectbox("Language", lang_names, index=0,
                             help="Sets the language tag in EPUB/DOCX/PDF and picks the right PDF font.")
    if lang_name == "Other...":
        language = st.text_input("Language code", "en", help="BCP-47 code, e.g. ur, ar, ja.")
    else:
        language = dict(cv.LANGUAGES)[lang_name]
    st.header("Options")
    reflow = st.checkbox("Re-flow hard-wrapped .txt lines", True,
                         help="Joins lines inside a paragraph. Turn off to keep original line breaks (poetry, scripts).")
    toc = st.checkbox("Table of contents", True,
                      help="DOCX: Word asks to update the TOC on first open. If you upload the DOCX straight to "
                           "Kindle, untick this; Kindle builds its own chapter list from the headings.")
    regions = list(cv.SIZES)
    region = st.selectbox("Book size for", regions, index=regions.index("India (Tamil)") if language == "ta" else 0,
                          help="Switching the language to Tamil preselects India (Tamil).")
    size_label = st.selectbox("Trim size", list(cv.SIZES[region]))
    dims = cv.SIZES[region][size_label]
    page_mm, text_mm = dims[:2], (dims[2:] or None)
    st.caption(f"Page {page_mm[0]:g} × {page_mm[1]:g} mm"
               + (f", text area {text_mm[0]:g} × {text_mm[1]:g} mm" if text_mm else "")
               + ". Applies to PDF and DOCX; EPUB reflows to the reader's screen.")

    st.header("Font")
    font_upload = st.file_uploader("Custom font file (optional)", type=["ttf", "otf"],
                                   help="Used in PDF and EPUB (embedded) and DOCX (by name; the font must be "
                                        "installed on the computer that opens it). Or drop files into the fonts/ folder.")
    font_options = {"Default (Noto Serif / system Tamil font)": None}
    notes_font = []
    if font_upload:
        try:
            fi = font_cached(font_upload.getvalue(), font_upload.name)
            font_options[f"Uploaded: {fi.family}"] = fi
        except Exception as e:
            st.error(f"Couldn't read that font: {type(e).__name__}: {e}")
    font_options.update(folder_fonts())
    font_label = st.selectbox("Book font", list(font_options), index=1 if len(font_options) > 1 and font_upload else 0)
    font = font_options[font_label]
    if font and language == "ta" and not font.has_tamil:
        st.warning("This font has no Unicode Tamil letters (it may be an old non-Unicode Tamil font). "
                   "Tamil text will look wrong.")
    elif font:
        st.caption(f"Family name: {font.family}" + ("  ·  Tamil ✔" if font.has_tamil else ""))
    cover_file = st.file_uploader("EPUB cover (optional)", type=["jpg", "jpeg", "png"])

# ---- main ------------------------------------------------------------------
st.title("📚 Bookbinder")
st.caption("Merge many files into one book and export to DOCX, EPUB, PDF and more. "
           "Files are processed in memory and not stored.")

files = st.file_uploader("Add your files", type=cv.SUPPORTED + ["zip"], accept_multiple_files=True,
                         help=".txt  .md  .doc  .docx  .pdf  .pages, or .zip files containing them")
if not files:
    st.info("Upload two or more files to begin. They're ordered by name (1, 2, 10 sorts naturally); you can reorder next.")
    st.stop()

parsed, zip_notes = [], []
for f in files:
    if f.name.lower().endswith(".zip"):
        ps, ns = parse_zip_cached(f.name, f.getvalue(), reflow)
        parsed += ps
        zip_notes += ns
    else:
        parsed.append(parse_cached(f.name, f.getvalue(), reflow))
parsed.sort(key=lambda p: cv.natural_key(p.name))
ok = [p for p in parsed if not p.error]

for p in parsed:
    if p.error:
        st.error(f"**{p.name}** was skipped: {p.error}")
notes = [(p.name, w) for p in ok for w in p.warnings] + [("", n) for n in zip_notes]
if zip_notes:
    st.warning("\n\n".join(zip_notes))
file_notes = [(n, w) for n, w in notes if n]
if file_notes:
    with st.expander(f"{len(file_notes)} note(s) about how some files were read"):
        for name, w in file_notes:
            st.write(f"**{name}**: {w}")

if not ok:
    st.stop()

st.subheader("Order and titles")
st.caption("Edit **Order** to rearrange, **Title** to rename a chapter, untick **Include** to leave a file out.")
df = pd.DataFrame({
    "Include": True,
    "Order": range(1, len(ok) + 1),
    "Title": [p.title for p in ok],
    "File": [p.name for p in ok],
    "Words": [p.words for p in ok],
})
sig = hashlib.md5("|".join(f"{p.name}:{p.words}" for p in ok).encode()).hexdigest()
edited = st.data_editor(
    df, key=f"editor-{sig}", hide_index=True, width="stretch", disabled=["File", "Words"],
    column_config={"Order": st.column_config.NumberColumn(min_value=1, step=1)},
)
edited = edited.assign(_i=range(len(edited)))
chosen = edited[edited["Include"]].sort_values(["Order", "_i"])
docs = [replace(ok[i], title=(row["Title"] or ok[i].title).strip()) for i, row in chosen.iterrows()]

st.metric("Combined length", f"{sum(d.words for d in docs):,} words in {len(docs)} chapter(s)")

st.subheader("Export")
labels = {k: v[2] for k, v in cv.FORMATS.items()}
picked = st.multiselect("Formats", list(labels), default=["docx", "epub"], format_func=labels.get)
if "pdf" in picked and cv.pdf_engine() == "pymupdf":
    st.info("PDF will use the built-in engine because WeasyPrint's system libraries (GTK/Pango) aren't "
            "available here. It has a simpler layout. Install them for the full version (see the README note).")
st.caption("Kindle: upload the **EPUB** (or DOCX) via Send to Kindle or KDP. Amazon no longer accepts MOBI.")

if st.button("Build book", type="primary", disabled=not (docs and picked)):
    book = cv.Book(
        title=book_title.strip() or "Combined Book", author=author.strip(),
        language=language.strip() or "en", toc=toc, page_mm=page_mm, text_mm=text_mm, font=font,
        cover=cover_file.getvalue() if cover_file else None,
    )
    results, bar = {}, st.progress(0.0)
    for n, fmt in enumerate(picked, 1):
        try:
            with st.spinner(f"Building {labels[fmt]}..."):
                results[fmt] = cv.export(fmt, docs, book)
        except Exception as e:
            st.error(f"{labels[fmt]} failed: {type(e).__name__}: {e}")
        bar.progress(n / len(picked))
    st.session_state["results"] = (slug(book.title), results)

if "results" in st.session_state:
    base, results = st.session_state["results"]
    if results:
        cols = st.columns(min(4, len(results)))
        for n, (fmt, blob) in enumerate(results.items()):
            _, ext, label, mime = cv.FORMATS[fmt]
            cols[n % len(cols)].download_button(
                f"⬇ {label}", blob, file_name=f"{base}.{ext}", mime=mime,
                key=f"dl-{fmt}", width="stretch",
            )
        if len(results) > 1:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for fmt, blob in results.items():
                    z.writestr(f"{base}.{cv.FORMATS[fmt][1]}", blob)
            st.download_button("⬇ Everything (.zip)", buf.getvalue(), file_name=f"{base}.zip",
                               mime="application/zip", key="dl-zip")