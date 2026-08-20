from __future__ import annotations

from pathlib import Path


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
