from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from .config import project_path
from .graphify import markdown_section, slug


PAPER_PATH_RE = re.compile(r"^wiki/papers/(?P<arxiv_id>.+)\.md$")
DEFAULT_EXCLUDED_TOPIC_SLUGS = {
    "active-galactic",
    "distribution-sed",
    "energy-distribution",
    "energy-distribution-sed",
    "formation-rate",
    "formation-rates",
    "galactic-nuclei",
    "galactic-nucleus",
    "research-seeks",
    "section-section",
    "spectral-energy",
    "spectral-energy-distribution-sed",
    "stellar-masses",
    "supermassive-black",
}


@dataclass(frozen=True)
class SemanticTopicPage:
    topic_id: str
    label: str
    source: str
    path: Path
    content: str
    paper_count: int


def load_graph(path: Path | None = None) -> dict:
    graph_path = path or project_path("graphify-out", "graph.json")
    if not graph_path.exists():
        raise FileNotFoundError(f"Graph JSON not found: {graph_path}")
    return json.loads(graph_path.read_text(encoding="utf-8"))


def paper_arxiv_id(path: str) -> str:
    match = PAPER_PATH_RE.match(path)
    return match.group("arxiv_id") if match else Path(path).stem


def page_title(markdown: str, fallback: str) -> str:
    match = re.search(r"^# (?P<title>.+)$", markdown, flags=re.MULTILINE)
    return match.group("title").strip() if match else fallback


def compact_text(value: str, max_chars: int = 420) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= max_chars:
        return value
    clipped = value[:max_chars].rsplit(" ", 1)[0].rstrip()
    return f"{clipped}..."


def evidence_excerpt(markdown: str) -> str:
    for heading in ("Main Results", "Scientific Question", "Method", "Data", "Source Abstract"):
        section = markdown_section(markdown, heading)
        if section.strip():
            return compact_text(section)
    body = re.sub(r"^---\n.*?\n---\n", "", markdown, flags=re.DOTALL).strip()
    return compact_text(body)


def graph_indexes(graph: dict) -> tuple[dict[str, dict], dict[str, list[dict]], dict[str, list[dict]]]:
    nodes = {node["id"]: node for node in graph.get("nodes", []) if "id" in node}
    outgoing: dict[str, list[dict]] = defaultdict(list)
    incoming: dict[str, list[dict]] = defaultdict(list)
    for edge in graph.get("edges", []):
        source = edge.get("source")
        target = edge.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        outgoing[source].append(edge)
        incoming[target].append(edge)
    return nodes, outgoing, incoming


def topic_page_path(label: str) -> Path:
    return project_path("wiki", "topics", "semantic", f"{slug(label)}.md")


def relative_paper_link(_topic_path: Path, paper_path: str) -> str:
    return f"../../papers/{Path(paper_path).name}"


def paper_metadata(paper_path: str) -> dict:
    path = project_path(paper_path)
    arxiv_id = paper_arxiv_id(paper_path)
    if not path.exists():
        return {
            "path": paper_path,
            "arxiv_id": arxiv_id,
            "title": arxiv_id,
            "excerpt": "Paper wiki page is missing.",
        }
    markdown = path.read_text(encoding="utf-8", errors="ignore")
    return {
        "path": paper_path,
        "arxiv_id": arxiv_id,
        "title": page_title(markdown, arxiv_id),
        "excerpt": evidence_excerpt(markdown),
    }


def connected_papers(topic_id: str, incoming: dict[str, list[dict]], nodes: dict[str, dict]) -> list[str]:
    paths = []
    for edge in incoming.get(topic_id, []):
        source = edge.get("source")
        if edge.get("relation") == "has_topic" and isinstance(source, str) and nodes.get(source, {}).get("type") == "page":
            paths.append(source)
    return sorted(set(paths), reverse=True)


def cooccurring_facets(
    paper_paths: list[str],
    topic_id: str,
    outgoing: dict[str, list[dict]],
    nodes: dict[str, dict],
    facet_type: str,
    limit: int = 12,
    excluded_topic_slugs: set[str] | None = None,
) -> list[tuple[str, int]]:
    excluded = DEFAULT_EXCLUDED_TOPIC_SLUGS | (excluded_topic_slugs or set())
    counter: Counter[str] = Counter()
    for paper_path in paper_paths:
        for edge in outgoing.get(paper_path, []):
            target = edge.get("target")
            node = nodes.get(target, {}) if isinstance(target, str) else {}
            if target == topic_id or node.get("type") != facet_type:
                continue
            label = str(node.get("label") or target)
            if facet_type == "topic" and slug(label) in excluded:
                continue
            counter[label] += 1
    return counter.most_common(limit)


def render_count_list(items: list[tuple[str, int]]) -> str:
    if not items:
        return "No graph co-occurrences above the current threshold."
    return "\n".join(f"- {label} ({count} papers)" for label, count in items)


def render_topic_page(
    topic_node: dict,
    paper_paths: list[str],
    outgoing: dict[str, list[dict]],
    nodes: dict[str, dict],
    max_papers: int,
    excluded_topic_slugs: set[str] | None = None,
) -> SemanticTopicPage:
    label = str(topic_node.get("label") or topic_node["id"].split(":", 1)[-1])
    source = str(topic_node.get("source") or "configured")
    path = topic_page_path(label)
    papers = [paper_metadata(paper_path) for paper_path in paper_paths[:max_papers]]
    omitted_count = max(0, len(paper_paths) - len(papers))
    paper_lines = []
    for paper in papers:
        link = relative_paper_link(path, paper["path"])
        paper_lines.append(
            f"- [{paper['title']}]({link}) ({paper['arxiv_id']})\n"
            f"  - Evidence: {paper['excerpt']}"
        )
    if omitted_count:
        paper_lines.append(f"- {omitted_count} additional connected papers omitted by display limit.")

    related_topics = cooccurring_facets(
        paper_paths,
        topic_node["id"],
        outgoing,
        nodes,
        "topic",
        excluded_topic_slugs=excluded_topic_slugs,
    )
    methods = cooccurring_facets(paper_paths, topic_node["id"], outgoing, nodes, "method")
    observations = cooccurring_facets(paper_paths, topic_node["id"], outgoing, nodes, "observation")
    content = (
        "---\n"
        "page_type: semantic_topic\n"
        f"topic_id: \"{topic_node['id']}\"\n"
        f"topic_label: \"{label.replace(chr(34), chr(39))}\"\n"
        f"topic_source: \"{source}\"\n"
        f"paper_count: {len(paper_paths)}\n"
        "generated_by: semantic_wiki_agent\n"
        "---\n\n"
        f"# Topic: {label}\n\n"
        "## Scope\n\n"
        "This page is automatically maintained from `graphify-out/graph.json`. "
        "It lists papers whose source-grounded wiki pages are connected to this graph topic. "
        "It does not make broad synthesis claims without human review.\n\n"
        "## Connected Papers\n\n"
        + ("\n".join(paper_lines) if paper_lines else "No connected papers.")
        + "\n\n## Related Topics\n\n"
        + render_count_list(related_topics)
        + "\n\n## Methods\n\n"
        + render_count_list(methods)
        + "\n\n## Observations / Surveys / Simulations\n\n"
        + render_count_list(observations)
        + "\n\n## Maintenance Notes\n\n"
        f"- Graph topic id: `{topic_node['id']}`\n"
        f"- Topic source: `{source}`\n"
        f"- Connected paper count: {len(paper_paths)}\n"
        "- Major synthesis edits should be reviewed by a human before replacing this generated page.\n"
    )
    return SemanticTopicPage(topic_node["id"], label, source, path, content, len(paper_paths))


def semantic_topic_pages(
    graph: dict,
    *,
    min_papers: int = 2,
    max_topics: int | None = 80,
    max_papers_per_topic: int = 25,
    excluded_topic_slugs: set[str] | None = None,
) -> list[SemanticTopicPage]:
    nodes, outgoing, incoming = graph_indexes(graph)
    topics = [node for node in nodes.values() if node.get("type") == "topic"]
    excluded = DEFAULT_EXCLUDED_TOPIC_SLUGS | (excluded_topic_slugs or set())
    pages: list[SemanticTopicPage] = []
    for topic in topics:
        label = str(topic.get("label") or topic["id"].split(":", 1)[-1])
        if slug(label) in excluded:
            continue
        papers = connected_papers(topic["id"], incoming, nodes)
        if len(papers) < min_papers:
            continue
        pages.append(render_topic_page(topic, papers, outgoing, nodes, max_papers_per_topic, excluded))
    pages.sort(key=lambda page: (-page.paper_count, page.label.lower()))
    return pages[:max_topics] if max_topics else pages


def semantic_index_content(pages: list[SemanticTopicPage]) -> str:
    lines = [
        "# Semantic Topic Index",
        "",
        "Automatically maintained from graph topic nodes.",
        "",
        "## Topics",
        "",
    ]
    for page in pages:
        rel = f"semantic/{page.path.name}"
        lines.append(f"- [{page.label}]({rel}) ({page.paper_count} papers; {page.source})")
    return "\n".join(lines).rstrip() + "\n"
