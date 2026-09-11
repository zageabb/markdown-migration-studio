from __future__ import annotations

import argparse
from pathlib import Path

from document_conversion import markdown_mermaid_to_pptx


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export Mermaid code from Markdown or a raw .mmd file to editable PowerPoint shapes."
    )
    parser.add_argument("source", type=Path, help="Markdown or Mermaid source file")
    parser.add_argument("destination", type=Path, nargs="?", help="Output .pptx file")
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    if not source.is_file():
        parser.error(f"Source file does not exist: {source}")
    destination = (args.destination or source.with_suffix(".pptx")).expanduser().resolve()
    if destination.suffix.lower() != ".pptx":
        destination = destination.with_suffix(".pptx")

    markdown = source.read_text(encoding="utf-8", errors="replace")
    count = markdown_mermaid_to_pptx(markdown, destination)
    print(f"Exported {count} Mermaid diagram(s) to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
