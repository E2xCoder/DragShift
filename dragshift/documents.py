"""Markdown / HTML / Word conversions beyond plain text.

  .md    → PDF · DOCX · HTML          .html → PDF · DOCX · MD · TXT          .docx → MD · HTML
Markdown and HTML are rendered (headings, lists, tables, code, images next to the file), not dumped
as source text."""
from __future__ import annotations

import html
import re
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag
from markdown_it import MarkdownIt

from .convert import docx_to_html, html_to_pdf, read_text
from .util import UserError, sibling

MARKUP_EXTS = {".md", ".html", ".htm"}
PAGE_CSS = ("body{font-family:Segoe UI,Helvetica,Arial,sans-serif;max-width:46em;margin:2em auto;padding:0 1em;"
            "line-height:1.5}pre,code{font-family:Consolas,monospace;background:#f4f4f6}pre{padding:.8em;overflow:auto}"
            "table{border-collapse:collapse}td,th{border:1px solid #bbb;padding:.3em .6em}blockquote{color:#555;"
            "border-left:3px solid #ccc;margin-left:0;padding-left:1em}")


# ---------------------------------------------------------------- reading
def md_to_html(text: str) -> str:
    """CommonMark + tables + strikethrough (what GitHub renders); raw HTML in the text is escaped."""
    return MarkdownIt("commonmark").enable(["table", "strikethrough"]).render(text)


def _soup(src: Path) -> BeautifulSoup:
    """The page as a soup, with scripts and styles removed."""
    text = read_text(src)
    if src.suffix.lower() == ".md":
        text = md_to_html(text)
    soup = BeautifulSoup(text, "html.parser")
    for t in soup(["script", "style", "head", "noscript", "template"]):
        t.decompose()
    return soup


def _body(soup: BeautifulSoup) -> Tag:
    return soup.body or soup


# ---------------------------------------------------------------- → PDF
def markup_to_pdf(src: str | Path) -> list[Path]:
    import pymupdf as fitz
    src = Path(src)
    out = sibling(src, ext=".pdf")
    body = "".join(str(c) for c in _body(_soup(src)).children)
    html_to_pdf(body, out, archive=fitz.Archive(str(src.parent)))
    return [out]


# ---------------------------------------------------------------- → DOCX
_INLINE_SKIP = {"ul", "ol"}


def _runs(par, node, bold=False, italic=False, code=False, skip_lists=False):
    for ch in node.children:
        if isinstance(ch, NavigableString):
            text = re.sub(r"\s+", " ", str(ch)) if not code else str(ch)
            if text:
                r = par.add_run(text)
                r.bold, r.italic = bold or None, italic or None
                if code:
                    r.font.name = "Consolas"
        elif isinstance(ch, Tag):
            if skip_lists and ch.name in _INLINE_SKIP:
                continue
            if ch.name == "br":
                par.add_run().add_break()
            elif ch.name in ("b", "strong"):
                _runs(par, ch, True, italic, code, skip_lists)
            elif ch.name in ("i", "em"):
                _runs(par, ch, bold, True, code, skip_lists)
            elif ch.name == "code":
                _runs(par, ch, bold, italic, True, skip_lists)
            elif ch.name == "img":
                continue
            else:
                _runs(par, ch, bold, italic, code, skip_lists)


def _fill(par, node, **kw):
    """_runs, then drop the whitespace HTML indentation leaves at the edges of the paragraph."""
    _runs(par, node, **kw)
    runs = par.runs
    if runs:
        runs[0].text = runs[0].text.lstrip()
        runs[-1].text = runs[-1].text.rstrip()
    return par


def _add_table(doc, tbl: Tag):
    rows = [r for r in tbl.find_all("tr")]
    cols = max((len(r.find_all(["td", "th"])) for r in rows), default=0)
    if not rows or not cols:
        return
    t = doc.add_table(rows=len(rows), cols=cols, style="Table Grid")
    for i, r in enumerate(rows):
        for j, c in enumerate(r.find_all(["td", "th"])[:cols]):
            cell = t.cell(i, j)
            cell.text = ""
            run = cell.paragraphs[0].add_run(c.get_text(" ", strip=True))
            run.bold = c.name == "th" or None


def _add_image(doc, img: Tag, base: Path):
    from docx.shared import Inches
    src = img.get("src", "")
    if not src or re.match(r"^[a-z]+:", src, re.I):        # remote / data URLs are not fetched
        return
    p = (base / src.split("?")[0]).resolve()
    if p.is_file():
        try:
            doc.add_picture(str(p), width=Inches(5))
        except Exception:      # unsupported image type: skip it rather than fail the document
            pass


def _list(doc, lst: Tag, base: Path, depth: int = 0):
    ordered = lst.name == "ol"
    level = "" if depth == 0 else f" {min(depth, 2) + 1}"          # Word ships "List Bullet", "… 2", "… 3"
    for li in lst.find_all("li", recursive=False):
        par = doc.add_paragraph(style=("List Number" if ordered else "List Bullet") + level)
        _fill(par, li, skip_lists=True)
        for nested in li.find_all(["ul", "ol"], recursive=False):
            _list(doc, nested, base, depth + 1)


def _blocks(doc, node: Tag, base: Path):
    for ch in node.children:
        if isinstance(ch, NavigableString):
            text = re.sub(r"\s+", " ", str(ch)).strip()
            if text:
                doc.add_paragraph(text)
            continue
        name = ch.name
        if re.fullmatch(r"h[1-6]", name):
            _fill(doc.add_heading("", level=int(name[1])), ch)
        elif name in ("ul", "ol"):
            _list(doc, ch, base)
        elif name == "pre":
            doc.add_paragraph().add_run(ch.get_text()).font.name = "Consolas"
        elif name == "blockquote":
            for q in ch.find_all("p", recursive=False) or [ch]:     # markdown wraps quoted text in <p>
                _fill(doc.add_paragraph(style="Quote"), q)
        elif name == "table":
            _add_table(doc, ch)
        elif name == "img":
            _add_image(doc, ch, base)
        elif name == "p":
            _fill(doc.add_paragraph(), ch)
            for im in ch.find_all("img"):
                _add_image(doc, im, base)
        elif name == "hr":
            doc.add_paragraph("―" * 20)
        else:                          # div, section, article, body…: look inside
            _blocks(doc, ch, base)


def markup_to_docx(src: str | Path) -> list[Path]:
    from docx import Document
    src = Path(src)
    doc = Document()
    _blocks(doc, _body(_soup(src)), src.parent)
    out = sibling(src, ext=".docx")
    doc.save(out)
    return [out]


# ---------------------------------------------------------------- → MD / TXT / HTML
def html_to_md(src: str | Path) -> list[Path]:
    from markdownify import markdownify
    src = Path(src)
    text = markdownify(str(_body(_soup(src))), heading_style="ATX", bullets="-")
    out = sibling(src, ext=".md")
    out.write_text(re.sub(r"\n{3,}", "\n\n", text).strip() + "\n", encoding="utf-8")
    return [out]


def html_to_txt(src: str | Path) -> list[Path]:
    src = Path(src)
    text = _body(_soup(src)).get_text("\n")
    out = sibling(src, ext=".txt")
    out.write_text(re.sub(r"\n\s*\n+", "\n\n", text).strip() + "\n", encoding="utf-8")
    return [out]


def md_to_html_file(src: str | Path) -> list[Path]:
    src = Path(src)
    out = sibling(src, ext=".html")
    out.write_text(_page(src.stem, md_to_html(read_text(src))), encoding="utf-8")
    return [out]


def _page(title: str, body: str) -> str:
    return (f'<!doctype html>\n<html><head><meta charset="utf-8"><title>{html.escape(title)}</title>'
            f"<style>{PAGE_CSS}</style></head>\n<body>\n{body}\n</body></html>\n")


def docx_to_html_file(src: str | Path) -> list[Path]:
    src = Path(src)
    out = sibling(src, ext=".html")
    out.write_text(_page(src.stem, docx_to_html(src)), encoding="utf-8")
    return [out]


def docx_to_md(src: str | Path) -> list[Path]:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    src = Path(src)
    d = Document(src)
    blocks: list[tuple[str, str]] = []     # (kind, markdown); items of one list stay together, the rest is spaced

    def inline(p: Paragraph) -> str:
        runs = [r for r in p.runs if r.text]
        if "".join(r.text for r in runs) != p.text:      # hyperlinks etc.: keep the text, drop formatting
            return p.text.strip()
        out = []
        for r in runs:
            t = r.text
            if t.strip():
                lead, core, tail = t[:len(t) - len(t.lstrip())], t.strip(), t[len(t.rstrip()):]
                if r.bold and r.italic:
                    core = f"***{core}***"
                elif r.bold:
                    core = f"**{core}**"
                elif r.italic:
                    core = f"*{core}*"
                t = lead + core + tail
            out.append(t)
        return "".join(out).strip()

    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, d)
            text = inline(p)
            style = (p.style.name or "").lower() if p.style is not None else ""
            if not text:
                continue
            if style == "title":
                blocks.append(("h", f"# {text}"))
            elif style.startswith("heading"):
                lvl = "".join(c for c in style if c.isdigit()) or "1"
                blocks.append(("h", f"{'#' * min(int(lvl), 6)} {text}"))
            elif "list number" in style:
                blocks.append(("ol", f"1. {text}"))
            elif "list" in style:
                indent = "  " * (int(style[-1]) - 1 if style[-1].isdigit() else 0)
                blocks.append(("ul", f"{indent}- {text}"))
            elif style == "quote":
                blocks.append(("p", f"> {text}"))
            else:
                blocks.append(("p", text))
        elif tag == "tbl":
            t = Table(child, d)
            rows = [[c.text.replace("\n", " ").replace("|", "\\|").strip() for c in r.cells] for r in t.rows]
            if rows:
                md = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * len(rows[0])]
                md += ["| " + " | ".join(r) + " |" for r in rows[1:]]
                blocks.append(("t", "\n".join(md)))
    text = ""
    for i, (kind, md) in enumerate(blocks):
        if i:
            text += "\n" if kind in ("ul", "ol") and blocks[i - 1][0] == kind else "\n\n"
        text += md
    out = sibling(src, ext=".md")
    out.write_text(text.strip() + "\n", encoding="utf-8")
    return [out]


# ---------------------------------------------------------------- dispatch
def convert_markup(src: str | Path, target: str) -> list[Path]:
    src, target = Path(src), target.upper()
    is_md = src.suffix.lower() == ".md"
    table = {"PDF": markup_to_pdf, "DOCX": markup_to_docx,
             "HTML": md_to_html_file if is_md else None,
             "MD": None if is_md else html_to_md, "TXT": None if is_md else html_to_txt}
    fn = table.get(target)
    if fn is None:
        raise UserError(f"Can't convert {src.suffix} to {target}.")
    return fn(src)
