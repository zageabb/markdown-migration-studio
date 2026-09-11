from __future__ import annotations

from pathlib import Path
import re


MERMAID_STARTERS = (
    "flowchart", "graph", "sequencediagram", "classdiagram", "erdiagram", "statediagram",
    "journey", "gantt", "pie", "quadrantchart", "requirementdiagram", "gitgraph", "mindmap",
    "timeline", "zenuml", "sankey-beta", "xychart-beta", "block-beta", "packet-beta", "kanban",
    "architecture-beta", "radar-beta", "treemap-beta", "c4context", "c4container", "c4component",
    "c4dynamic", "c4deployment",
)


def _markdown_escape(value: object) -> str:
    return str(value or "").replace("\n", " ").strip().replace("|", "\\|")


def _rows_to_markdown(rows: list[list[str]]) -> str:
    cleaned = [[_markdown_escape(cell) for cell in row] for row in rows if any(str(cell).strip() for cell in row)]
    if not cleaned:
        return ""
    width = max(len(row) for row in cleaned)
    padded = [row + [""] * (width - len(row)) for row in cleaned]
    return "\n".join([
        "| " + " | ".join(padded[0]) + " |",
        "| " + " | ".join(["---"] * width) + " |",
        *["| " + " | ".join(row) + " |" for row in padded[1:]],
    ])


def docx_to_markdown(path: str | Path) -> str:
    """Convert a DOCX file using the shared converter behavior from the Ollama apps."""
    from docx import Document

    document = Document(str(path))
    lines: list[str] = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        style = (paragraph.style.name or "").lower()
        if style.startswith("heading"):
            digits = "".join(character for character in style if character.isdigit())
            level = min(max(int(digits or "1"), 1), 6)
            lines.append(f"{'#' * level} {text}")
        elif "list bullet" in style:
            lines.append(f"- {text}")
        elif "list number" in style:
            lines.append(f"1. {text}")
        else:
            lines.append(text)

    for table in document.tables:
        rendered = _rows_to_markdown([[cell.text.strip() for cell in row.cells] for row in table.rows])
        if rendered:
            lines.extend(["", rendered])

    markdown = "\n".join(lines).strip()
    if not markdown:
        raise ValueError("No readable text could be extracted from the Word document")
    return markdown + "\n"


def markdown_to_docx(markdown: str, destination: str | Path) -> None:
    """Write a readable Word copy of the Markdown produced by the app."""
    from docx import Document
    from docx.shared import Pt

    document = Document()
    lines = markdown.splitlines()
    index = 0
    in_code_block = False
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            index += 1
            continue
        if in_code_block:
            paragraph = document.add_paragraph()
            run = paragraph.add_run(line)
            run.font.name = "Courier New"
            run.font.size = Pt(9)
            index += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            document.add_heading(heading.group(2), level=len(heading.group(1)))
            index += 1
            continue
        if "|" in line and index + 1 < len(lines) and _is_table_separator(lines[index + 1]):
            rows = [_markdown_table_cells(line)]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(_markdown_table_cells(lines[index]))
                index += 1
            width = max(len(row) for row in rows)
            table = document.add_table(rows=len(rows), cols=width)
            table.style = "Table Grid"
            for row_number, row in enumerate(rows):
                for column_number, value in enumerate(row):
                    table.cell(row_number, column_number).text = value
            continue
        bullet = re.match(r"^[-*+]\s+(.+)$", stripped)
        numbered = re.match(r"^\d+[.)]\s+(.+)$", stripped)
        if bullet:
            document.add_paragraph(bullet.group(1), style="List Bullet")
        elif numbered:
            document.add_paragraph(numbered.group(1), style="List Number")
        elif stripped:
            document.add_paragraph(stripped)
        index += 1

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(destination))


def _looks_like_mermaid(value: str) -> bool:
    for raw in value.splitlines():
        line = raw.strip()
        if not line or line.startswith("%%") or line.startswith("---"):
            continue
        normalized = re.sub(r"\s+", "", line).lower()
        return any(normalized.startswith(starter) for starter in MERMAID_STARTERS)
    return False


def extract_mermaid_blocks(markdown: str) -> list[tuple[str, str]]:
    """Extract fenced Mermaid blocks and the closest preceding Markdown heading."""
    blocks: list[tuple[str, str]] = []
    lines = markdown.splitlines()
    heading = "Diagram"
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        heading_match = re.match(r"^#{1,6}\s+(.+)$", stripped)
        if heading_match:
            heading = heading_match.group(1).strip()
            index += 1
            continue
        fence = re.match(r"^```\s*(mermaid|mmd)\s*$", stripped, re.IGNORECASE)
        if not fence:
            index += 1
            continue
        index += 1
        source: list[str] = []
        while index < len(lines) and not lines[index].strip().startswith("```"):
            source.append(lines[index])
            index += 1
        if source and _looks_like_mermaid("\n".join(source)):
            blocks.append((heading, "\n".join(source).strip() + "\n"))
        index += 1

    if not blocks and _looks_like_mermaid(markdown):
        blocks.append(("Diagram", markdown.strip() + "\n"))
    return blocks


def markdown_mermaid_to_pptx(markdown: str, destination: str | Path) -> int:
    """Export Mermaid blocks as one editable 16:9 PowerPoint slide per diagram."""
    from diagram_pptx import render_mermaid
    from pptx import Presentation
    from pptx.util import Inches

    blocks = extract_mermaid_blocks(markdown)
    if not blocks:
        raise ValueError("No Mermaid diagram blocks were found in the Markdown")

    deck = Presentation()
    deck.slide_width = Inches(13.333)
    deck.slide_height = Inches(7.5)
    for _heading, source in blocks:
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        render_mermaid(source, slide=slide, position="full")

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    deck.save(str(destination))
    return len(blocks)


def _markdown_table_cells(line: str) -> list[str]:
    return [cell.strip().replace("\\|", "|") for cell in line.strip().strip("|").split("|")]


def _is_table_separator(line: str) -> bool:
    cells = _markdown_table_cells(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)
