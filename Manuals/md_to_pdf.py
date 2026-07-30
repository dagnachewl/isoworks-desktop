#!/usr/bin/env python3
"""Convert a Markdown file to PDF via Chrome headless.

LaTeX math expressions ($...$ and $$...$$) are pre-converted to MathML
server-side so Chrome renders them natively without any JS dependency.

Mermaid diagrams (```mermaid blocks) are pre-rendered to SVG via mmdc
(the Mermaid CLI, invoked through npx) when --mermaid is passed.

Usage
-----
  python3 md_to_pdf.py [OPTIONS] [INPUT]

Arguments
---------
  INPUT       Path to the Markdown file to convert.
              Defaults to IsoWorks_Desktop_User_Manual.md in the same
              directory as this script.

Options
-------
  -o OUTPUT    Output PDF path (default: INPUT with .pdf extension).
  -t TITLE     Document title shown in PDF metadata
               (default: stem of the input filename).
  --mermaid    Pre-render ```mermaid blocks to SVG via mmdc / npx.
               Requires Node.js + npx on PATH.
  -h, --help   Show this help message and exit.

Examples
--------
  # Convert the default manual
  python3 md_to_pdf.py

  # Convert a specific file
  python3 md_to_pdf.py ../docs/ngam_ms_data_reduction.md

  # Convert a file that contains Mermaid diagrams
  python3 md_to_pdf.py ../docs/ngam_pipeline_page1.md --mermaid

  # Specify output path and title
  python3 md_to_pdf.py ../docs/ngam_ms_data_reduction.md \\
      -o ~/Desktop/ngam_reduction.pdf \\
      -t "NGAM Data Reduction Reference"
"""
from __future__ import annotations

import argparse
import re
import sys
import pathlib

import markdown
import latex2mathml.converter
from playwright.sync_api import sync_playwright
_DEFAULT_MD = pathlib.Path(__file__).parent / "IsoWorks_Desktop_User_Manual.md"

CSS = """
html, body {
  font-family: "Helvetica Neue", Arial, sans-serif;
  font-size: 11pt;
  line-height: 1.55;
  color: #1a1a1a;
  max-width: 860px;
  margin: 0 auto;
  padding: 8px 32px;
}
h1 { font-size: 20pt; color: #1a3a5c; border-bottom: 2px solid #1a3a5c; padding-bottom: 6px; margin-top: 24px; }
h2 { font-size: 15pt; color: #1a3a5c; border-bottom: 1px solid #bbb; margin-top: 22px; }
h3 { font-size: 12pt; color: #2a4a6c; margin-top: 16px; }
h4 { font-size: 10.5pt; color: #444; margin-top: 12px; }
code {
  font-family: "Courier New", monospace;
  font-size: 9pt;
  background: #f5f5f5;
  border-radius: 3px;
  padding: 1px 4px;
}
pre {
  font-family: "Courier New", monospace;
  font-size: 9pt;
  background: #f5f5f5;
  border-left: 3px solid #1a3a5c;
  padding: 8px 12px;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-word;
}
pre code { background: none; padding: 0; }
table {
  border-collapse: collapse;
  width: 100%;
  margin: 10px 0;
  font-size: 9.5pt;
}
th {
  background: #1a3a5c;
  color: #fff;
  padding: 5px 9px;
  text-align: left;
}
td {
  border: 1px solid #ccc;
  padding: 4px 9px;
  vertical-align: top;
}
tr:nth-child(even) td { background: #f9f9f9; }
blockquote {
  border-left: 4px solid #1a3a5c;
  margin: 10px 0;
  padding: 4px 14px;
  background: #f0f4f8;
  color: #444;
}
a { color: #1a3a5c; }
hr { border: none; border-top: 1px solid #ccc; margin: 18px 0; }
/* MathML */
math { font-size: 1.05em; }
.math-block { display: block; text-align: center; margin: 12px 0; overflow-x: auto; }
.math-inline { display: inline; }
/* Mermaid SVG */
.mermaid-diagram { display: flex; justify-content: center; margin: 16px 0; page-break-inside: avoid; }
.mermaid-diagram svg { max-width: 100%; height: auto; }
@media print {
  @page { size: A4; margin: 18mm 16mm 22mm 16mm; }
  h1, h2, h3 { page-break-after: avoid; }
  pre, table { page-break-inside: avoid; }
  a { color: #1a3a5c !important; text-decoration: none; }
}
"""


# ── Math pre-processing ───────────────────────────────────────────────────────

_PLACEHOLDER = "XXMATHXX{}XXENDXX"
_DISPLAY_RE  = re.compile(r'\$\$(.+?)\$\$', re.DOTALL)
_INLINE_RE   = re.compile(r'\$(?!\$)(.+?)(?<!\$)\$')


def _to_mathml(latex: str, display: str) -> str:
    try:
        return latex2mathml.converter.convert(latex.strip(), display=display)
    except Exception as exc:
        # Fall back to monospace if conversion fails
        return f"<code>{latex.strip()}</code>"


def _extract_math(src: str) -> tuple[str, dict[str, str]]:
    """Replace LaTeX math spans with opaque placeholders safe for the markdown parser."""
    store: dict[str, str] = {}
    idx = 0

    def sub_display(m: re.Match) -> str:
        nonlocal idx
        key = _PLACEHOLDER.format(idx); idx += 1
        store[key] = f'<div class="math-block">{_to_mathml(m.group(1), "block")}</div>'
        return key

    def sub_inline(m: re.Match) -> str:
        nonlocal idx
        key = _PLACEHOLDER.format(idx); idx += 1
        store[key] = f'<span class="math-inline">{_to_mathml(m.group(1), "inline")}</span>'
        return key

    src = _DISPLAY_RE.sub(sub_display, src)
    src = _INLINE_RE.sub(sub_inline, src)
    return src, store


def _restore_math(html: str, store: dict[str, str]) -> str:
    for key, mml in store.items():
        html = html.replace(key, mml)
    return html


# ── Mermaid pre-rendering ─────────────────────────────────────────────────────

_MERMAID_RE  = re.compile(r'`{3}mermaid[^\n]*\n(.*?)`{3}', re.DOTALL)
_MERMAID_KEY = "XXMERMAIDXX{}XXENDXX"


def _extract_mermaid(src: str) -> tuple[str, dict[str, str]]:
    """Replace ```mermaid blocks with opaque placeholders."""
    store: dict[str, str] = {}
    idx = 0

    def _sub(m: re.Match) -> str:
        nonlocal idx
        key = _MERMAID_KEY.format(idx); idx += 1
        store[key] = m.group(1)
        return f"\n\n{key}\n\n"  # blank lines → own <p> after markdown parse

    return _MERMAID_RE.sub(_sub, src), store


def _render_mermaid(store: dict[str, str]) -> dict[str, str]:
    """Render each stored mermaid block to an inline SVG via mmdc / npx."""
    import subprocess, tempfile, os
    result: dict[str, str] = {}
    for key, code in store.items():
        tmp_mmd = tempfile.NamedTemporaryFile(suffix='.mmd', mode='w',
                                              encoding='utf-8', delete=False)
        tmp_mmd.write(code); tmp_mmd.close()
        svg_path = tmp_mmd.name.replace('.mmd', '.svg')
        try:
            subprocess.run(
                ['npx', '-y', '@mermaid-js/mermaid-cli',
                 '-i', tmp_mmd.name, '-o', svg_path, '-b', 'white', '-q'],
                check=True, capture_output=True,
            )
            svg = pathlib.Path(svg_path).read_text(encoding='utf-8')
            svg = re.sub(r'<\?xml[^?]*\?>', '', svg).strip()  # strip XML decl
            result[key] = f'<div class="mermaid-diagram">{svg}</div>'
        except Exception as exc:
            result[key] = f'<pre>[Mermaid render failed: {exc}]</pre>'
        finally:
            os.unlink(tmp_mmd.name)
            if os.path.exists(svg_path):
                os.unlink(svg_path)
    return result


def _restore_mermaid(html: str, rendered: dict[str, str]) -> str:
    for key, svg_html in rendered.items():
        html = html.replace(f'<p>{key}</p>', svg_html)
        html = html.replace(key, svg_html)  # fallback if wrapped differently
    return html


# ── Core conversion ───────────────────────────────────────────────────────────

def convert(md_file: pathlib.Path, pdf_file: pathlib.Path, title: str,
            render_mermaid: bool = False) -> None:
    if not md_file.exists():
        sys.exit(f"Error: file not found: {md_file}")

    src = md_file.read_text(encoding="utf-8")

    mermaid_store: dict[str, str] = {}
    rendered_mermaid: dict[str, str] = {}
    if render_mermaid:
        print("  Rendering Mermaid diagrams via mmdc …")
        src, mermaid_store = _extract_mermaid(src)
        rendered_mermaid = _render_mermaid(mermaid_store)
        print(f"  {len(mermaid_store)} diagram(s) rendered.")

    src, math_store = _extract_math(src)

    body = markdown.markdown(
        src,
        extensions=["tables", "fenced_code", "toc", "attr_list", "def_list"],
    )
    body = _restore_math(body, math_store)
    if render_mermaid:
        body = _restore_mermaid(body, rendered_mermaid)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>{CSS}</style>
</head>
<body>
{body}
</body>
</html>"""

    header_tmpl = (
        '<div style="font-size:8pt;font-family:\'Helvetica Neue\',Arial,sans-serif;'
        'color:#555;width:100%;padding:0 16mm;box-sizing:border-box;'
        'display:flex;justify-content:space-between;border-bottom:1px solid #ccc;">'
        '<span class="title"></span>'
        '<span class="date"></span>'
        '</div>'
    )
    footer_tmpl = (
        '<div style="font-size:8pt;font-family:\'Helvetica Neue\',Arial,sans-serif;'
        'color:#555;width:100%;padding:0 16mm;box-sizing:border-box;'
        'text-align:center;border-top:1px solid #ccc;">'
        'Page <span class="pageNumber"></span> of <span class="totalPages"></span>'
        '</div>'
    )

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        page.pdf(
            path=str(pdf_file),
            format="A4",
            display_header_footer=True,
            header_template=header_tmpl,
            footer_template=footer_tmpl,
            print_background=True,
            margin={"top": "22mm", "right": "16mm", "bottom": "18mm", "left": "16mm"},
        )
        browser.close()
    print(f"PDF written → {pdf_file}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="md_to_pdf.py",
        description="Convert a Markdown file to PDF via Chrome headless.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 md_to_pdf.py\n"
            "  python3 md_to_pdf.py ../docs/ngam_ms_data_reduction.md\n"
            "  python3 md_to_pdf.py input.md -o ~/Desktop/out.pdf -t 'My Title'"
        ),
    )
    parser.add_argument(
        "input",
        nargs="?",
        metavar="INPUT",
        default=str(_DEFAULT_MD),
        help=f"Markdown file to convert (default: {_DEFAULT_MD.name})",
    )
    parser.add_argument(
        "-o", "--output",
        metavar="OUTPUT",
        help="Output PDF path (default: INPUT with .pdf extension)",
    )
    parser.add_argument(
        "-t", "--title",
        metavar="TITLE",
        help="Document title for PDF metadata (default: filename stem)",
    )
    parser.add_argument(
        "--mermaid",
        action="store_true",
        help="Pre-render ```mermaid blocks to SVG via mmdc (requires npx)",
    )
    return parser.parse_args()


def main() -> None:
    args     = parse_args()
    md_file  = pathlib.Path(args.input).expanduser().resolve()
    pdf_file = pathlib.Path(args.output).expanduser().resolve() if args.output \
               else md_file.with_suffix(".pdf")
    title    = args.title or md_file.stem.replace("_", " ")
    convert(md_file, pdf_file, title, render_mermaid=args.mermaid)


if __name__ == "__main__":
    main()
