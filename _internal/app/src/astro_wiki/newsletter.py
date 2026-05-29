from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import project_path
from .graphify import markdown_section


@dataclass(frozen=True)
class PaperNewsletterBrief:
    arxiv_id: str
    title: str
    sections: dict[str, str]

    @property
    def link(self) -> str:
        return f"[{self.arxiv_id}](../papers/{self.arxiv_id.replace('/', '_')}.md)"


def kst_today() -> date:
    return datetime.now(ZoneInfo("Asia/Seoul")).date()


def parse_target_date(value: str | None) -> str:
    if not value or value == "today":
        return kst_today().isoformat()
    return datetime.fromisoformat(value).date().isoformat()


def newsletter_path(newsletter_date: str) -> Path:
    return project_path("wiki", "newsletters", f"{newsletter_date}.md")


def source_digest_path(newsletter_date: str) -> Path:
    return project_path("wiki", "daily", f"{newsletter_date}.md")


def paper_ids_from_digest(markdown: str, *, limit: int | None = None) -> list[str]:
    seen: set[str] = set()
    arxiv_ids: list[str] = []
    for match in re.finditer(r"\.\./papers/(\d{4}\.\d{4,5})\.md", markdown):
        arxiv_id = match.group(1)
        if arxiv_id in seen:
            continue
        seen.add(arxiv_id)
        arxiv_ids.append(arxiv_id)
        if limit and len(arxiv_ids) >= limit:
            break
    return arxiv_ids


def arxiv_sort_key(arxiv_id: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in arxiv_id.split("."))
    except ValueError:
        return (0,)


def paper_ids_from_db(conn: sqlite3.Connection, newsletter_date: str, *, limit: int | None = None) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT arxiv_id
        FROM papers
        WHERE status IN ('ingested', 'digested', 'deep_summarized', 'curated', 'graphed')
          AND (
            announced_date = ?
            OR date(published) = ?
            OR date(updated) = ?
            OR date(created_at) = ?
            OR date(updated_at) = ?
            OR date(created_at, '+9 hours') = ?
            OR date(updated_at, '+9 hours') = ?
          )
        """,
        (newsletter_date,) * 7,
    ).fetchall()
    arxiv_ids = sorted({row["arxiv_id"] for row in rows}, key=arxiv_sort_key, reverse=True)
    return arxiv_ids[:limit] if limit else arxiv_ids


def title_from_paper_markdown(markdown: str, arxiv_id: str) -> str:
    yaml_title = re.search(r"^title:\s*[\"']?(.*?)[\"']?\s*$", markdown, flags=re.MULTILINE)
    if yaml_title:
        return yaml_title.group(1).strip()
    heading = re.search(r"^#\s+(.+)$", markdown, flags=re.MULTILINE)
    return heading.group(1).strip() if heading else arxiv_id


def compact_text(text: str, *, max_chars: int) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= max_chars:
        return compact
    return compact[:max_chars].rsplit(" ", 1)[0].rstrip() + "..."


def load_paper_briefs(arxiv_ids: list[str], *, section_max_chars: int = 1600) -> list[PaperNewsletterBrief]:
    briefs: list[PaperNewsletterBrief] = []
    headings = ["Source Abstract", "Scientific Question", "Data", "Method", "Main Results", "Limitations"]
    for arxiv_id in arxiv_ids:
        path = project_path("wiki", "papers", f"{arxiv_id.replace('/', '_')}.md")
        if not path.exists():
            continue
        markdown = path.read_text(encoding="utf-8", errors="ignore")
        sections = {
            heading: compact_text(markdown_section(markdown, heading), max_chars=section_max_chars)
            for heading in headings
            if markdown_section(markdown, heading).strip()
        }
        briefs.append(PaperNewsletterBrief(arxiv_id=arxiv_id, title=title_from_paper_markdown(markdown, arxiv_id), sections=sections))
    return briefs


def build_llm_context(digest_markdown: str, briefs: list[PaperNewsletterBrief], *, max_digest_chars: int = 5000) -> str:
    paper_blocks: list[str] = []
    for brief in briefs:
        sections = "\n\n".join(
            f"### {heading}\n{text}" for heading, text in brief.sections.items() if text
        )
        paper_blocks.append(f"## {brief.arxiv_id} - {brief.title}\n{sections}")
    return (
        "Daily digest excerpt:\n"
        f"{digest_markdown[:max_digest_chars]}\n\n"
        "Paper wiki excerpts:\n"
        + "\n\n---\n\n".join(paper_blocks)
    )


def render_fallback_newsletter(newsletter_date: str, briefs: list[PaperNewsletterBrief]) -> str:
    editor_picks = briefs[:3]
    paper_map = "\n".join(f"- {brief.link}: {brief.title}" for brief in briefs) or "- 오늘 처리된 paper wiki가 없습니다."
    picks = []
    for brief in editor_picks:
        result = brief.sections.get("Main Results") or brief.sections.get("Scientific Question") or brief.sections.get("Source Abstract") or ""
        picks.append(f"### {brief.arxiv_id}\n{brief.link} - {compact_text(result, max_chars=520)}")
    methods = []
    for brief in briefs:
        method = brief.sections.get("Method") or brief.sections.get("Data")
        if method:
            methods.append(f"- {brief.link}: {compact_text(method, max_chars=320)}")
    return (
        f"# Astro-ph Research Brief - {newsletter_date}\n\n"
        "## 오늘의 주요 뉴스\n\n"
        f"{len(briefs)}편의 paper wiki를 바탕으로 오늘의 astro-ph 업데이트를 정리했습니다. "
        "이 버전은 LLM을 사용하지 않은 deterministic fallback이므로, 논문 간 큰 흐름의 해석은 보수적으로 제한했습니다.\n\n"
        "## Editor's Picks\n\n"
        + ("\n\n".join(picks) if picks else "선정할 paper wiki가 없습니다.")
        + "\n\n## 한눈에 보는 오늘의 논문 지도\n\n"
        + paper_map
        + "\n\n## Methods / Data Watch\n\n"
        + ("\n".join(methods[:8]) if methods else "Method 또는 Data section을 가진 paper wiki가 없습니다.")
        + "\n\n## 고민해 볼 만한 질문들\n\n"
        "- 오늘 추가된 paper wiki가 기존 semantic topic page를 어떻게 바꾸는지 확인합니다.\n"
        "- citation trace에서 주요 claim의 paper section 근거가 충분한지 확인합니다.\n"
    )


def wrap_newsletter_frontmatter(
    content: str,
    *,
    newsletter_date: str,
    paper_count: int,
    llm_generated: bool,
    model: str,
    created_at: str,
) -> str:
    body = content.strip()
    if body.startswith("---"):
        body = re.sub(r"^---.*?---\s*", "", body, count=1, flags=re.DOTALL)
    return (
        "---\n"
        f"date: {newsletter_date}\n"
        "page_type: newsletter\n"
        f"paper_count: {paper_count}\n"
        f"llm_generated: {str(llm_generated).lower()}\n"
        f"model: \"{model}\"\n"
        f"source_digest: \"daily/{newsletter_date}.md\"\n"
        f"created_at: \"{created_at}\"\n"
        "---\n\n"
        f"{body}\n"
    )
