from document_conversion import extract_mermaid_blocks


def test_extracts_multiple_mermaid_blocks_with_headings():
    markdown = """# Report

## Local search
```mermaid
flowchart LR
    A --> B
```

## Internet search
```mmd
sequenceDiagram
    User->>Search: Find evidence
```
"""

    blocks = extract_mermaid_blocks(markdown)
    assert [title for title, _source in blocks] == ["Local search", "Internet search"]
    assert blocks[0][1].startswith("flowchart LR")
    assert blocks[1][1].startswith("sequenceDiagram")


def test_accepts_raw_mermaid_source():
    blocks = extract_mermaid_blocks("flowchart TD\nA[Start] --> B[Done]\n")
    assert len(blocks) == 1
    assert blocks[0][0] == "Diagram"
