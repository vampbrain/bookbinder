# bookbinder
your binder for digital world


# Bookbinder

    pip install -r requirements.txt
    streamlit run app.py

## PDF export on Windows

PDF uses WeasyPrint when its GTK/Pango libraries are present; otherwise Bookbinder
automatically falls back to a built-in PyMuPDF engine (simpler layout, same chapters,
contents, bookmarks, page numbers, Tamil/Indic scripts).

For the full-layout engine on Windows:
1. Install MSYS2 (msys2.org), then in the MSYS2 shell: `pacman -S mingw-w64-x86_64-pango`
2. Set the environment variable `WEASYPRINT_DLL_DIRECTORIES=C:\msys64\mingw64\bin`
3. Restart the terminal and run the app again.

The fallback engine needs a font file for Indic scripts. Windows ships Nirmala UI, so Tamil
works out of the box; otherwise drop `NotoSerifTamil-Regular.ttf` into a `fonts/` folder next to app.py.

On Linux hosting (Streamlit Cloud, Docker) `packages.txt` / `Dockerfile` already install what WeasyPrint needs.

## Fonts

Pick a font in the sidebar: upload a `.ttf`/`.otf` for one-off use, or drop files into the `fonts/` folder
so they always appear in the dropdown. The font is embedded in PDF and EPUB; for DOCX the font is referenced by
name, so it must be installed on the computer that opens the file (Word does not embed it automatically).
The app reads the real family name from the file, so odd filenames are fine, and warns if a font has no Unicode
Tamil letters (old non-Unicode Tamil fonts cannot be used with Unicode text).

## Book sizes

"Tamil press 140 x 215 mm (text 92 x 168.5 mm)" centres a 92 x 168.5 mm text block on the page. To add your own
size, edit `SIZES` in `converters.py`: `(page_w, page_h)` or `(page_w, page_h, text_w, text_h)` in millimetres.
