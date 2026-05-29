from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .config import project_path


def safe_arxiv_filename(arxiv_id: str) -> str:
    return arxiv_id.replace("/", "_")


def safe_filename_part(value: object, *, max_chars: int = 90) -> str:
    text = re.sub(r"[*_`]+", "", str(value or ""))
    text = re.sub(r"[\\/:*?\"<>|]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-_")
    text = text[:max_chars].rstrip(" .-_")
    return text


def first_author_surname(authors: object) -> str:
    if isinstance(authors, str):
        try:
            parsed = json.loads(authors)
            authors = parsed
        except json.JSONDecodeError:
            authors = [authors]
    if not isinstance(authors, list) or not authors:
        return ""
    first = safe_filename_part(authors[0], max_chars=48)
    if not first:
        return ""
    if "," in first:
        return safe_filename_part(first.split(",", 1)[0], max_chars=48)
    parts = first.split()
    return safe_filename_part(parts[-1] if parts else first, max_chars=48)


def paper_year(*values: object) -> str:
    for value in values:
        match = re.search(r"\b(19|20)\d{2}\b", str(value or ""))
        if match:
            return match.group(0)
    return ""


def paper_filename(
    arxiv_id: str,
    *,
    title: object = "",
    authors: object = None,
    year: object = "",
) -> str:
    title_part = safe_filename_part(title, max_chars=96)
    author_part = first_author_surname(authors)
    year_part = paper_year(year)
    if title_part and author_part and year_part:
        return f"{title_part} - {author_part} - {year_part}.md"
    return f"{safe_arxiv_filename(arxiv_id)}.md"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def write_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def paper_page_path(
    arxiv_id: str,
    *,
    title: object = "",
    authors: object = None,
    year: object = "",
) -> Path:
    return project_path("wiki", "papers", paper_filename(arxiv_id, title=title, authors=authors, year=year))


def wiki_rel(path: Path) -> str:
    return str(path.relative_to(project_path())).replace("\\", "/")


def markdown_section_bounds(text: str, header: str) -> tuple[int, int] | None:
    marker = f"{header}\n"
    start = text.find(marker)
    if start < 0:
        return None
    body_start = start + len(marker)
    next_header = re.search(r"\n##\s+", text[body_start:])
    body_end = body_start + next_header.start() if next_header else len(text)
    return body_start, body_end


def arxiv_sort_key(line: str) -> tuple[int, ...]:
    match = re.search(r"\((\d{4}\.\d{4,5})\)", line)
    if not match:
        match = re.search(r"papers/(\d{4}\.\d{4,5})\.md", line)
    if not match:
        return (0,)
    return tuple(int(part) for part in match.group(1).split("."))


def date_sort_key(line: str) -> tuple[int, int, int]:
    match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", line)
    if not match:
        return (0, 0, 0)
    return tuple(int(part) for part in match.groups())


def sort_markdown_section_lines(text: str, header: str, sort: str | None) -> str:
    if not sort:
        return text
    bounds = markdown_section_bounds(text, header)
    if not bounds:
        return text
    body_start, body_end = bounds
    body = text[body_start:body_end]
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    sortable = [line for line in lines if line.startswith("- ")]
    other = [line for line in lines if not line.startswith("- ")]
    if sortable:
        other = [line for line in other if not re.match(r"^No .*(?:yet|created|added|ingested)", line, flags=re.IGNORECASE)]
    if sort == "arxiv_desc":
        sortable = sorted(dict.fromkeys(sortable), key=arxiv_sort_key, reverse=True)
    elif sort == "date_desc":
        sortable = sorted(dict.fromkeys(sortable), key=date_sort_key, reverse=True)
    else:
        return text
    section_body = "\n".join([*sortable, *other])
    replacement = f"\n\n{section_body}\n\n" if section_body else "\n\n"
    return text[:body_start] + replacement + text[body_end:].lstrip("\n")


def append_unique_line(path: Path, header: str, line: str, *, sort: str | None = None) -> bool:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {path.stem}\n\n{header}\n\n", encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    added = line not in text
    marker = f"{header}\n"
    if added and marker in text:
        text = text.replace(marker, marker + f"\n{line}\n", 1)
    elif added:
        text = text.rstrip() + f"\n\n{header}\n\n{line}\n"
    text = sort_markdown_section_lines(text, header, sort)
    path.write_text(text, encoding="utf-8")
    return added


def extract_markdown_links(text: str) -> list[str]:
    return re.findall(r"\[[^\]]+\]\(([^)]+)\)", text)
