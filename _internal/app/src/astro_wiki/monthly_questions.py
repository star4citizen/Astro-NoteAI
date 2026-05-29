from __future__ import annotations

import hashlib
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import project_path
from .graphify import markdown_section, slug
from .semantic_wiki import graph_indexes, load_graph, paper_arxiv_id

PAPER_PREFIX = "wiki/papers/"
PAPER_SUFFIX = ".md"

DEFAULT_EXCLUDED_TOPIC_SLUGS = {
    "active-galactic",
    "active-galactic-nuclei",
    "energy-distribution",
    "formation-rate",
    "galactic-nuclei",
    "galaxy-evolution",
    "galaxy-property",
    "observational-data",
    "physical-property",
    "research-seeks",
    "star-formation",
    "stellar-masses",
}

SECTION_LINKS = {
    "Scientific Question": "scientific-question",
    "Data": "data",
    "Method": "method",
    "Main Results": "main-results",
    "Limitations": "limitations",
    "Follow-up Questions": "follow-up-questions",
}


@dataclass(frozen=True)
class MonthWindow:
    label: str
    start: date
    end: date


@dataclass(frozen=True)
class RelatedPaper:
    path: str
    arxiv_id: str
    title: str
    score: int
    shared_target_ids: tuple[str, ...]
    shared_labels: tuple[str, ...]


@dataclass(frozen=True)
class QuestionDraft:
    question: str
    related: RelatedPaper
    target_section: str
    related_section: str
    fingerprint: str


@dataclass(frozen=True)
class QuestionPageDraft:
    arxiv_id: str
    title: str
    path: Path
    content: str
    question_count: int
    related_count: int


def month_window(value: str, *, today: date | None = None) -> MonthWindow:
    today = today or date.today()
    if value == "previous":
        first_this_month = today.replace(day=1)
        end = first_this_month - timedelta(days=1)
        start = end.replace(day=1)
        return MonthWindow(end.strftime("%Y-%m"), start, end)
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise ValueError("month must be 'previous' or YYYY-MM")
    year, month = [int(part) for part in value.split("-")]
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return MonthWindow(value, start, end)


def kst_today() -> date:
    return datetime.now(ZoneInfo("Asia/Seoul")).date()


def paper_path_for_arxiv(arxiv_id: str) -> str:
    return f"{PAPER_PREFIX}{arxiv_id.replace('/', '_')}{PAPER_SUFFIX}"


def is_paper_path(path: str) -> bool:
    return path.startswith(PAPER_PREFIX) and path.endswith(PAPER_SUFFIX) and not path.endswith("-deep-summary.md")


def arxiv_sort_tuple(path: str) -> tuple[int, int]:
    arxiv_id = paper_arxiv_id(path)
    match = re.fullmatch(r"(\d{4})\.(\d{4,5})", arxiv_id)
    if not match:
        return (0, 0)
    return int(match.group(1)), int(match.group(2))


def changed_paper_paths(conn: sqlite3.Connection, start: date, end: date) -> list[str]:
    start_text = start.isoformat()
    end_text = end.isoformat()
    rows = conn.execute(
        """
        SELECT DISTINCT arxiv_id
        FROM papers
        WHERE text_path IS NOT NULL
          AND status IN ('ingested', 'digested', 'deep_summarized', 'curated', 'graphed')
          AND (
            announced_date BETWEEN ? AND ?
            OR date(published) BETWEEN ? AND ?
            OR date(updated) BETWEEN ? AND ?
            OR date(created_at) BETWEEN ? AND ?
            OR date(updated_at) BETWEEN ? AND ?
            OR date(created_at, '+9 hours') BETWEEN ? AND ?
            OR date(updated_at, '+9 hours') BETWEEN ? AND ?
          )
        ORDER BY arxiv_id DESC
        """,
        (
            start_text,
            end_text,
            start_text,
            end_text,
            start_text,
            end_text,
            start_text,
            end_text,
            start_text,
            end_text,
            start_text,
            end_text,
            start_text,
            end_text,
        ),
    )
    return [paper_path_for_arxiv(row["arxiv_id"]) for row in rows]


def node_label(nodes: dict[str, dict], target_id: str) -> str:
    node = nodes.get(target_id, {})
    return str(node.get("label") or target_id.split(":", 1)[-1])


def is_specific_target(target_id: str, nodes: dict[str, dict], excluded_topic_slugs: set[str]) -> bool:
    node = nodes.get(target_id, {})
    node_type = node.get("type")
    if node_type not in {"topic", "method", "observation"}:
        return False
    label = node_label(nodes, target_id)
    if node_type == "topic" and slug(label) in excluded_topic_slugs:
        return False
    return True


def paper_targets(
    graph: dict[str, Any],
    *,
    excluded_topic_slugs: set[str] | None = None,
) -> dict[str, set[str]]:
    nodes, outgoing, _incoming = graph_indexes(graph)
    excluded = DEFAULT_EXCLUDED_TOPIC_SLUGS | (excluded_topic_slugs or set())
    result: dict[str, set[str]] = {}
    for source, edges in outgoing.items():
        if not is_paper_path(source):
            continue
        targets = {
            edge["target"]
            for edge in edges
            if isinstance(edge.get("target"), str) and is_specific_target(edge["target"], nodes, excluded)
        }
        if targets:
            result[source] = targets
    return result


def paper_title(path: str) -> str:
    full_path = project_path(path)
    if not full_path.exists():
        return paper_arxiv_id(path)
    text = full_path.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r"^#\s+(.+)$", text, flags=re.MULTILINE)
    return match.group(1).strip() if match else paper_arxiv_id(path)


def related_papers_for(
    paper_path: str,
    graph: dict[str, Any],
    *,
    max_related: int = 5,
    min_shared_facets: int = 2,
    excluded_topic_slugs: set[str] | None = None,
) -> list[RelatedPaper]:
    nodes, _outgoing, _incoming = graph_indexes(graph)
    targets_by_paper = paper_targets(graph, excluded_topic_slugs=excluded_topic_slugs)
    target_targets = targets_by_paper.get(paper_path, set())
    if not target_targets:
        return []
    related: list[RelatedPaper] = []
    for other_path, other_targets in targets_by_paper.items():
        if other_path == paper_path:
            continue
        shared = tuple(sorted(target_targets & other_targets))
        if len(shared) < min_shared_facets:
            continue
        labels = tuple(node_label(nodes, target_id) for target_id in shared)
        related.append(
            RelatedPaper(
                path=other_path,
                arxiv_id=paper_arxiv_id(other_path),
                title=paper_title(other_path),
                score=len(shared),
                shared_target_ids=shared,
                shared_labels=labels,
            )
        )
    related.sort(key=lambda item: (-item.score, -arxiv_sort_tuple(item.path)[0], -arxiv_sort_tuple(item.path)[1]))
    return related[:max_related]


def affected_paper_paths(
    changed_paths: list[str],
    graph: dict[str, Any],
    *,
    max_pages: int = 100,
    max_related_per_changed: int = 5,
    min_shared_facets: int = 2,
    excluded_topic_slugs: set[str] | None = None,
) -> list[str]:
    changed_set = {path for path in changed_paths if is_paper_path(path)}
    changed_sorted = sorted(changed_set, key=lambda path: arxiv_sort_tuple(path), reverse=True)
    if len(changed_sorted) >= max_pages:
        return changed_sorted[:max_pages]

    priority: Counter[str] = Counter()
    for path in changed_paths:
        if not is_paper_path(path):
            continue
        for related in related_papers_for(
            path,
            graph,
            max_related=max_related_per_changed,
            min_shared_facets=min_shared_facets,
            excluded_topic_slugs=excluded_topic_slugs,
        ):
            priority[related.path] += related.score
    paths = [path for path in priority if path not in changed_set]
    paths.sort(key=lambda path: (-priority[path], -arxiv_sort_tuple(path)[0], -arxiv_sort_tuple(path)[1]))
    return [*changed_sorted, *paths][:max_pages]


def compact_section(path: str, section: str, max_chars: int = 520) -> str:
    full_path = project_path(path)
    if not full_path.exists():
        return ""
    text = full_path.read_text(encoding="utf-8", errors="ignore")
    body = re.sub(r"\s+", " ", markdown_section(text, section)).strip()
    if len(body) <= max_chars:
        return body
    return body[:max_chars].rsplit(" ", 1)[0].rstrip() + "..."


def question_fingerprint(question: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", question.lower()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def citation_link(base: str, paper_path: str, section: str) -> str:
    anchor = SECTION_LINKS.get(section, slug(section))
    return f"[{paper_arxiv_id(paper_path)} {section}]({base}/{Path(paper_path).name}#{anchor})"


def shared_phrase(labels: tuple[str, ...], limit: int = 3) -> str:
    selected = [label for label in labels if label][:limit]
    return ", ".join(selected) if selected else "the shared graph context"


def template_question(target_id: str, related: RelatedPaper) -> tuple[str, str, str]:
    labels = {slug(label) for label in related.shared_labels}
    phrase = shared_phrase(related.shared_labels)
    if {"feedback", "black-hole", "accretion-rate"} & labels:
        return (
            "Main Results",
            "Main Results",
            f"Do the feedback or black-hole regulation results in `{target_id}` remain consistent with `{related.arxiv_id}` when comparing {phrase}, or do the papers point to different coupling channels?",
        )
    if {"gas-evolution", "interstellar-medium"} & labels:
        return (
            "Main Results",
            "Main Results",
            f"Can the gas inflow/outflow interpretation in `{target_id}` be tested against the gas-evolution diagnostics in `{related.arxiv_id}` for {phrase}?",
        )
    if {"cosmological-simulation", "simulation"} & labels:
        return (
            "Method",
            "Method",
            f"Which assumptions change most when comparing the simulation setup in `{target_id}` with `{related.arxiv_id}` for {phrase}?",
        )
    if {"quenching", "massive-galaxy"} & labels:
        return (
            "Main Results",
            "Limitations",
            f"Do `{target_id}` and `{related.arxiv_id}` imply the same quenching route for massive galaxies, or are their conclusions limited by different model assumptions?",
        )
    return (
        "Limitations",
        "Limitations",
        f"What assumption or observable should be compared first between `{target_id}` and `{related.arxiv_id}` for {phrase}?",
    )


def question_drafts(
    paper_path: str,
    related: list[RelatedPaper],
    *,
    max_questions: int = 5,
) -> list[QuestionDraft]:
    target_id = paper_arxiv_id(paper_path)
    drafts: list[QuestionDraft] = []
    seen: set[str] = set()
    for item in related:
        target_section, related_section, question = template_question(target_id, item)
        fingerprint = question_fingerprint(question)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        drafts.append(
            QuestionDraft(
                question=question,
                related=item,
                target_section=target_section,
                related_section=related_section,
                fingerprint=fingerprint,
            )
        )
        if len(drafts) >= max_questions:
            break
    return drafts


def render_question_page(
    paper_path: str,
    related: list[RelatedPaper],
    *,
    month_label: str,
    max_questions: int = 5,
) -> QuestionPageDraft | None:
    arxiv_id = paper_arxiv_id(paper_path)
    title = paper_title(paper_path)
    drafts = question_drafts(paper_path, related, max_questions=max_questions)
    if not drafts:
        return None
    output_path = project_path("wiki", "questions", "papers", f"{arxiv_id}.md")
    paper_base = "../../papers"
    related_lines = [
        f"- [{item.title}]({paper_base}/{Path(item.path).name}) ({item.arxiv_id}; shared: {shared_phrase(item.shared_labels, 5)})"
        for item in related
    ]
    question_lines = []
    fingerprints = []
    for index, draft in enumerate(drafts, start=1):
        fingerprints.append(draft.fingerprint)
        target_link = citation_link(paper_base, paper_path, draft.target_section)
        related_link = citation_link(paper_base, draft.related.path, draft.related_section)
        question_lines.extend(
            [
                f"{index}. {draft.question}",
                f"   - Evidence: {target_link}; {related_link}.",
                f"   - Shared graph facets: {shared_phrase(draft.related.shared_labels, 6)}.",
            ]
        )
    content = (
        "---\n"
        "page_type: cross_paper_questions\n"
        f"arxiv_id: \"{arxiv_id}\"\n"
        f"generated_for_month: \"{month_label}\"\n"
        "generated_by: monthly_question_curator\n"
        f"related_paper_count: {len(related)}\n"
        f"question_count: {len(drafts)}\n"
        "question_fingerprints:\n"
        + "".join(f"  - \"{fingerprint}\"\n" for fingerprint in fingerprints)
        + "---\n\n"
        f"# Cross-paper Questions: {title}\n\n"
        "This page is maintained by the monthly incremental question curator. "
        "It does not rewrite the source paper wiki; it stores cross-paper research questions separately.\n\n"
        "## Source Paper\n\n"
        f"- [{title}]({paper_base}/{Path(paper_path).name}) ({arxiv_id})\n\n"
        "## Policy\n\n"
        "- Each question must cite at least two paper wiki sections.\n"
        "- Questions are synthesis prompts, not claims.\n"
        "- Monthly runs update only changed papers and their bounded graph neighborhood.\n\n"
        "## Related Papers Used\n\n"
        + "\n".join(related_lines)
        + "\n\n## Cross-paper Questions\n\n"
        + "\n".join(question_lines)
        + "\n"
    )
    return QuestionPageDraft(arxiv_id, title, output_path, content, len(drafts), len(related))


def write_if_changed(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = content.rstrip() + "\n"
    if path.exists() and path.read_text(encoding="utf-8", errors="ignore") == normalized:
        return False
    path.write_text(normalized, encoding="utf-8")
    return True


def render_index(pages: list[QuestionPageDraft]) -> str:
    lines = [
        "# Cross-paper Research Questions",
        "",
        "Automatically maintained by `scripts/monthly_question_curator.py`.",
        "",
        "## Paper Question Pages",
        "",
    ]
    for page in sorted(pages, key=lambda item: arxiv_sort_tuple(paper_path_for_arxiv(item.arxiv_id)), reverse=True):
        rel = f"papers/{page.arxiv_id}.md"
        lines.append(f"- [{page.title}]({rel}) ({page.arxiv_id}; {page.question_count} questions; {page.related_count} related papers)")
    return "\n".join(lines).rstrip() + "\n"


def parse_frontmatter_int(content: str, key: str) -> int:
    match = re.search(rf"^{re.escape(key)}:\s*(\d+)\s*$", content, flags=re.MULTILINE)
    return int(match.group(1)) if match else 0


def existing_question_pages(root: Path | None = None) -> list[QuestionPageDraft]:
    root = root or project_path("wiki", "questions", "papers")
    if not root.exists():
        return []
    pages: list[QuestionPageDraft] = []
    for path in sorted(root.glob("*.md")):
        content = path.read_text(encoding="utf-8", errors="ignore")
        arxiv_id = path.stem
        title_match = re.search(r"^# Cross-paper Questions:\s*(.+)$", content, flags=re.MULTILINE)
        title = title_match.group(1).strip() if title_match else arxiv_id
        pages.append(
            QuestionPageDraft(
                arxiv_id=arxiv_id,
                title=title,
                path=path,
                content=content,
                question_count=parse_frontmatter_int(content, "question_count"),
                related_count=parse_frontmatter_int(content, "related_paper_count"),
            )
        )
    return pages


def build_question_pages(
    graph: dict[str, Any],
    paper_paths: list[str],
    *,
    month_label: str,
    max_related: int = 5,
    max_questions: int = 5,
    min_shared_facets: int = 2,
    excluded_topic_slugs: set[str] | None = None,
) -> list[QuestionPageDraft]:
    pages: list[QuestionPageDraft] = []
    for paper_path in paper_paths:
        related = related_papers_for(
            paper_path,
            graph,
            max_related=max_related,
            min_shared_facets=min_shared_facets,
            excluded_topic_slugs=excluded_topic_slugs,
        )
        page = render_question_page(paper_path, related, month_label=month_label, max_questions=max_questions)
        if page:
            pages.append(page)
    return pages


def load_default_graph() -> dict[str, Any]:
    return load_graph(project_path("graphify-out", "graph.json"))
