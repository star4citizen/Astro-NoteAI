from __future__ import annotations

from pathlib import Path

import fitz


def extract_pdf_text(pdf_path: Path) -> str:
    chunks: list[str] = []
    with fitz.open(pdf_path) as document:
        for index, page in enumerate(document, start=1):
            text = page.get_text("text")
            chunks.append(f"\n\n--- Page {index} ---\n\n{text.strip()}")
    return "\n".join(chunks).strip()
