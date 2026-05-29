from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

import yaml

from .config import project_path
from .wiki_io import extract_markdown_links


FACETS: dict[str, dict[str, list[str]]] = {
    "topic": {
        "quenching": ["quenching", "quenched", "passive", "star formation cessation"],
        "star formation": ["star formation", "star-forming", "sfr", "specific star formation"],
        "morphology": ["morphology", "morphological", "sersic", "early-type", "late-type", "disk-like"],
        "environment": ["environment", "environmental", "density", "cluster", "group", "void", "filament"],
        "mergers": ["merger", "post-merger", "dry merger", "dual agn"],
        "gas evolution": ["gas fraction", "h i", "hi ", "neutral hydrogen", "cold gas", "gas accretion"],
        "high-redshift galaxies": ["high-redshift", "high redshift", "z >", "jwst", "lyman", "reionization"],
        "stellar mass growth": ["stellar mass", "mass function", "stellar population"],
        "feedback": ["feedback", "agn feedback", "stellar feedback", "black hole"],
    },
    "method": {
        "machine learning": ["machine learning", "deep learning", "neural network", "random forest", "xgboost"],
        "vae": ["vae", "variational autoencoder"],
        "gcnn": ["gcnn", "graph convolution"],
        "stacking": ["stacking", "stacked"],
        "spectroscopy": ["spectroscopy", "spectroscopic"],
        "photometry": ["photometry", "photometric"],
        "photometric redshift": ["photometric redshift", "photo-z", "photo z"],
        "simulation": ["simulation", "simulations", "cosmological simulation", "hydrodynamical"],
        "semi-analytic model": ["semi-analytic", "semi analytic"],
        "nearest-neighbour density": ["nearest neighbour", "nearest-neighbor", "nth-nearest"],
    },
    "observation": {
        "JWST": ["jwst", "nircam", "nirspec"],
        "Euclid": ["euclid"],
        "ALMA": ["alma"],
        "DESI": ["desi"],
        "COSMOS": ["cosmos"],
        "CHILES": ["chiles"],
        "MUSE": ["muse"],
        "Roman": ["roman"],
        "IllustrisTNG": ["illustris", "tng100", "illustristng"],
        "FIRE": ["fire simulation", "fire simulations", "fire-"],
        "THESAN": ["thesan"],
        "MaNGA": ["manga"],
    },
}

STOPWORDS = {
    "and",
    "are",
    "but",
    "can",
    "for",
    "has",
    "may",
    "not",
    "our",
    "the",
    "was",
    "will",
    "about",
    "above",
    "after",
    "again",
    "against",
    "also",
    "among",
    "across",
    "because",
    "been",
    "before",
    "being",
    "between",
    "both",
    "different",
    "into",
    "could",
    "does",
    "each",
    "first",
    "from",
    "have",
    "having",
    "have",
    "here",
    "how",
    "however",
    "into",
    "only",
    "more",
    "most",
    "other",
    "over",
    "paper",
    "papers",
    "previous",
    "results",
    "same",
    "should",
    "show",
    "shows",
    "than",
    "that",
    "their",
    "these",
    "this",
    "through",
    "used",
    "using",
    "were",
    "what",
    "where",
    "which",
    "while",
    "with",
    "within",
}

GENERIC_TOPIC_TOKENS = {
    "abstract",
    "agn",
    "alma",
    "analysis",
    "authors",
    "compared",
    "desi",
    "determine",
    "during",
    "employed",
    "euclid",
    "evidence",
    "exhibit",
    "exhibits",
    "extracted",
    "fallback",
    "follow-up",
    "including",
    "investigate",
    "investigates",
    "ism",
    "james",
    "jwst",
    "level",
    "lrds",
    "magnitude",
    "model",
    "nircam",
    "nirspec",
    "observations",
    "odot",
    "omega",
    "opening",
    "order",
    "present",
    "question",
    "questions",
    "range",
    "researchers",
    "review",
    "scientific",
    "section",
    "sections",
    "sample",
    "samples",
    "sfr",
    "sfh",
    "sigma",
    "source",
    "specifically",
    "studies",
    "study",
    "summarizer",
    "telescope",
    "times",
    "utilizes",
    "webb",
}

GENERIC_TOPIC_PHRASES = {
    "arxiv",
    "arxiv id",
    "follow up",
    "follow up questions",
    "local path",
    "main results",
    "manual review",
    "paper text",
    "pdf local",
    "requires manual",
    "source abstract",
    "source text",
    "text excerpt",
    "text local",
}

IRREGULAR_TOPIC_SINGULARS = {
    "galaxies": "galaxy",
    "properties": "property",
}

TOPIC_SINGULAR_EXCEPTIONS = {
    "gas",
    "lens",
    "mass",
    "series",
}


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def singularize_topic_token(token: str) -> str:
    if token in IRREGULAR_TOPIC_SINGULARS:
        return IRREGULAR_TOPIC_SINGULARS[token]
    if token in TOPIC_SINGULAR_EXCEPTIONS or len(token) <= 3:
        return token
    if token.endswith(("ss", "us", "is")):
        return token
    if token.endswith("ies") and len(token) > 4:
        return f"{token[:-3]}y"
    if token.endswith("s"):
        return token[:-1]
    return token


def topic_canonical_key(label: str) -> str:
    normalized = re.sub(r"[-_/]+", " ", label.lower())
    tokens = re.findall(r"[a-z0-9]+", normalized)
    return " ".join(singularize_topic_token(token) for token in tokens)


def preferred_topic_label(labels: set[str], *, configured: set[str] | None = None, custom: set[str] | None = None) -> str:
    configured = configured or set()
    custom = custom or set()
    key = topic_canonical_key(next(iter(labels))) if labels else ""

    def rank(label: str) -> tuple[int, int, str]:
        if label in configured:
            source_rank = 0
        elif label in custom:
            source_rank = 1
        elif topic_canonical_key(label) == label.lower().replace("-", " "):
            source_rank = 2
        else:
            source_rank = 3
        exact_key_penalty = 0 if topic_canonical_key(label) == key and slug(label) == slug(key) else 1
        return (source_rank, exact_key_penalty, label.lower())

    return sorted(labels, key=rank)[0]


def canonical_topic_label_map(
    labels: set[str],
    *,
    configured: set[str] | None = None,
    custom: set[str] | None = None,
) -> dict[str, str]:
    by_key: dict[str, set[str]] = defaultdict(set)
    for label in labels:
        key = topic_canonical_key(label)
        if key:
            by_key[key].add(label)
    mapping: dict[str, str] = {}
    for grouped in by_key.values():
        preferred = preferred_topic_label(grouped, configured=configured, custom=custom)
        for label in grouped:
            mapping[label] = preferred
    return mapping


def normalize_custom_keywords(value: object) -> list[str]:
    if isinstance(value, dict):
        value = value.get("keywords", [])
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def custom_facets() -> dict[str, dict[str, list[str]]]:
    path = project_path("config", "graph_facets.yml")
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_facets = data.get("facets", {}) if isinstance(data, dict) else {}
    if not isinstance(raw_facets, dict):
        return {}
    facets: dict[str, dict[str, list[str]]] = {}
    for facet_type, labels in raw_facets.items():
        if facet_type not in {"topic", "method", "observation"} or not isinstance(labels, dict):
            continue
        for label, value in labels.items():
            keywords = normalize_custom_keywords(value)
            if not keywords:
                keywords = [str(label)]
            facets.setdefault(str(facet_type), {})[str(label)] = keywords
    return facets


def all_facets() -> dict[str, dict[str, list[str]]]:
    merged = {facet_type: {label: list(keywords) for label, keywords in labels.items()} for facet_type, labels in FACETS.items()}
    for facet_type, labels in custom_facets().items():
        merged.setdefault(facet_type, {})
        for label, keywords in labels.items():
            existing = merged[facet_type].get(label, [])
            merged[facet_type][label] = list(dict.fromkeys([*existing, *keywords]))
    return merged


def keyword_matches(lowered_text: str, keyword: str) -> bool:
    lowered_keyword = keyword.lower().strip()
    if not lowered_keyword:
        return False
    if re.fullmatch(r"[a-z0-9]{2,5}", lowered_keyword):
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(lowered_keyword)}(?![a-z0-9])", lowered_text))
    return lowered_keyword in lowered_text


def matching_facets(text: str, facets: dict[str, dict[str, list[str]]] | None = None) -> list[tuple[str, str]]:
    lowered = text.lower()
    matches: list[tuple[str, str]] = []
    for facet_type, labels in (facets or all_facets()).items():
        for label, keywords in labels.items():
            if any(keyword_matches(lowered, keyword) for keyword in keywords):
                matches.append((facet_type, label))
    return matches


def markdown_section(markdown: str, heading: str) -> str:
    pattern = re.compile(rf"^## {re.escape(heading)}\s*$\n(?P<body>.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
    match = pattern.search(markdown)
    return match.group("body") if match else ""


def strip_frontmatter(markdown: str) -> str:
    if markdown.startswith("---\n"):
        end = markdown.find("\n---", 4)
        if end >= 0:
            return markdown[end + 4 :].lstrip()
    return markdown


def facet_match_text(markdown: str) -> str:
    markdown = strip_frontmatter(markdown)
    title_match = re.search(r"^# (?P<title>.+)$", markdown, flags=re.MULTILINE)
    title = title_match.group("title") if title_match else ""
    sections = [
        markdown_section(markdown, "Source Abstract"),
        markdown_section(markdown, "Scientific Question"),
        markdown_section(markdown, "Data"),
        markdown_section(markdown, "Method"),
        markdown_section(markdown, "Main Results"),
        markdown_section(markdown, "Limitations"),
        markdown_section(markdown, "Follow-up Questions"),
        markdown_section(markdown, "Deep Dive Summary"),
    ]
    scientific_sections = [section for section in sections if section.strip()]
    if scientific_sections:
        return "\n".join([title, *scientific_sections])
    return markdown


def dynamic_topic_text(markdown: str) -> str:
    markdown = strip_frontmatter(markdown)
    title_match = re.search(r"^# (?P<title>.+)$", markdown, flags=re.MULTILINE)
    title = title_match.group("title") if title_match else ""
    sections = [
        markdown_section(markdown, "Source Abstract"),
        markdown_section(markdown, "Scientific Question"),
        markdown_section(markdown, "Data"),
        markdown_section(markdown, "Method"),
        markdown_section(markdown, "Main Results"),
    ]
    return "\n".join([title, *sections])


def candidate_topic_phrases(text: str) -> set[str]:
    text = re.sub(r"https?://\S+", " ", text.lower())
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"\[[^\]]+\]\([^)]+\)", " ", text)
    text = re.sub(r"[^a-z][a-z]_[a-z][^a-z]", " ", text)
    tokens = re.findall(r"[a-z][a-z-]{2,}", text)
    phrases: set[str] = set()
    for size in (2, 3, 4):
        for idx in range(0, max(0, len(tokens) - size + 1)):
            parts = tokens[idx : idx + size]
            if parts[0] in STOPWORDS or parts[-1] in STOPWORDS:
                continue
            if any(part in GENERIC_TOPIC_TOKENS for part in parts):
                continue
            if any(subpart in GENERIC_TOPIC_TOKENS for part in parts for subpart in re.findall(r"[a-z0-9]+", part)):
                continue
            if sum(part not in STOPWORDS for part in parts) < 2:
                continue
            phrase = " ".join(parts)
            if phrase in GENERIC_TOPIC_PHRASES:
                continue
            if any(generic in phrase for generic in GENERIC_TOPIC_PHRASES):
                continue
            if not any(len(part) >= 5 for part in parts):
                continue
            phrases.add(phrase)
    return phrases


def dynamic_topics_by_page(page_texts: dict[str, str], min_docs: int = 2, max_topics: int = 50) -> dict[str, set[str]]:
    page_phrases = {page: candidate_topic_phrases(dynamic_topic_text(text)) for page, text in page_texts.items()}
    page_topic_keys: dict[str, set[str]] = {}
    labels_by_key: dict[str, set[str]] = defaultdict(set)
    document_frequency: dict[str, int] = {}
    for phrases in page_phrases.values():
        keys = set()
        for phrase in phrases:
            key = topic_canonical_key(phrase)
            if not key:
                continue
            keys.add(key)
            labels_by_key[key].add(phrase)
        for key in keys:
            document_frequency[key] = document_frequency.get(key, 0) + 1
    for page, phrases in page_phrases.items():
        page_topic_keys[page] = {topic_canonical_key(phrase) for phrase in phrases if topic_canonical_key(phrase)}

    seed_topic_keys = {topic_canonical_key(label) for label in all_facets().get("topic", {})}
    page_count = max(1, len(page_texts))
    ranked = sorted(
        (
            (key, count)
            for key, count in document_frequency.items()
            if count >= min_docs and count <= max(2, int(page_count * 0.35)) and key not in seed_topic_keys
        ),
        key=lambda item: (item[1], len(item[0]), item[0]),
        reverse=True,
    )
    allowed = {key for key, _ in ranked[:max_topics]}
    label_by_key = {key: preferred_topic_label(labels_by_key[key]) for key in allowed}
    return {page: {label_by_key[key] for key in keys if key in allowed} for page, keys in page_topic_keys.items()}


def build_simple_graph(wiki_dir: Path | None = None, output_dir: Path | None = None) -> dict:
    wiki_dir = wiki_dir or project_path("wiki", "papers")
    output_dir = output_dir or project_path("graphify-out")
    output_dir.mkdir(parents=True, exist_ok=True)
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    edge_keys: set[tuple[str, str, str]] = set()
    arxiv_pattern = re.compile(r"\b\d{4}\.\d{4,5}(?:v\d+)?\b|\b[a-z-]+/\d{7}(?:v\d+)?\b")
    page_paths = set(wiki_dir.rglob("*.md")) if wiki_dir.exists() else set()
    paper_pages_by_arxiv_id = {path.stem: str(path.relative_to(project_path())).replace("\\", "/") for path in page_paths}
    page_texts = {
        str(path.relative_to(project_path())).replace("\\", "/"): path.read_text(encoding="utf-8", errors="ignore")
        for path in sorted(page_paths)
    }
    facets = all_facets()
    custom_facet_keys = {(facet_type, label) for facet_type, labels in custom_facets().items() for label in labels}
    dynamic_topics = dynamic_topics_by_page(page_texts)
    configured_topic_labels = set(FACETS.get("topic", {}))
    custom_topic_labels = {label for facet_type, label in custom_facet_keys if facet_type == "topic"}
    topic_labels = set(facets.get("topic", {}))
    for labels in dynamic_topics.values():
        topic_labels.update(labels)
    topic_label_map = canonical_topic_label_map(
        topic_labels,
        configured=configured_topic_labels,
        custom=custom_topic_labels,
    )

    def facet_node_metadata(facet_type: str, label: str) -> dict:
        canonical_label = topic_label_map.get(label, label) if facet_type == "topic" else label
        node_id = f"{facet_type}:{slug(canonical_label)}"
        metadata = {"id": node_id, "type": facet_type, "label": canonical_label}
        if facet_type == "topic":
            grouped_labels = {raw for raw, canonical in topic_label_map.items() if canonical == canonical_label}
            if grouped_labels & configured_topic_labels:
                return metadata
            if grouped_labels & custom_topic_labels:
                metadata["source"] = "custom"
            else:
                metadata["source"] = "dynamic"
        elif (facet_type, label) in custom_facet_keys:
            metadata["source"] = "custom"
        return metadata

    def add_edge(source: str, target: str, relation: str) -> None:
        key = (source, target, relation)
        if key in edge_keys:
            return
        edge_keys.add(key)
        edges.append({"source": source, "target": target, "relation": relation})

    for path in sorted(page_paths):
        rel = str(path.relative_to(project_path())).replace("\\", "/")
        text = page_texts[rel]
        nodes[rel] = {"id": rel, "type": "page", "label": path.stem}
        for facet_type, label in matching_facets(facet_match_text(text), facets):
            metadata = facet_node_metadata(facet_type, label)
            node_id = metadata["id"]
            nodes.setdefault(node_id, metadata)
            add_edge(rel, node_id, f"has_{facet_type}")
        for label in sorted(dynamic_topics.get(rel, set())):
            metadata = facet_node_metadata("topic", label)
            node_id = metadata["id"]
            nodes.setdefault(node_id, metadata)
            add_edge(rel, node_id, "has_topic")
        for link in extract_markdown_links(text):
            if link.startswith("http"):
                continue
            target_path = (path.parent / link).resolve()
            if target_path not in page_paths:
                continue
            target = str(target_path)
            try:
                target_rel = str(Path(target).relative_to(project_path())).replace("\\", "/")
            except ValueError:
                continue
            nodes.setdefault(target_rel, {"id": target_rel, "type": "page", "label": Path(target_rel).stem})
            add_edge(rel, target_rel, "markdown_link")
        for arxiv_id in arxiv_pattern.findall(text):
            base_arxiv_id = re.sub(r"v\d+$", "", arxiv_id)
            if paper_page := paper_pages_by_arxiv_id.get(base_arxiv_id):
                if paper_page != rel:
                    add_edge(rel, paper_page, "cites")
                continue
            node_id = f"arxiv:{arxiv_id}"
            nodes.setdefault(node_id, {"id": node_id, "type": "arxiv", "label": arxiv_id})
            add_edge(rel, node_id, "cites")

    graph = {"nodes": list(nodes.values()), "edges": edges}
    type_counts: dict[str, int] = {}
    relation_counts: dict[str, int] = {}
    for node in nodes.values():
        type_counts[node["type"]] = type_counts.get(node["type"], 0) + 1
    for edge in edges:
        relation_counts[edge["relation"]] = relation_counts.get(edge["relation"], 0) + 1
    dynamic_topic_count = sum(1 for node in nodes.values() if node["type"] == "topic" and node.get("source") == "dynamic")
    (output_dir / "graph.json").write_text(json.dumps(graph, indent=2, ensure_ascii=True), encoding="utf-8")
    (output_dir / "GRAPH_REPORT.md").write_text(
        "# Graph Report\n\n"
        f"- Scope: `{wiki_dir.relative_to(project_path())}`\n"
        f"- Nodes: {len(nodes)}\n"
        f"- Edges: {len(edges)}\n\n"
        f"- Dynamic topics: {dynamic_topic_count}\n\n"
        "## Node Types\n\n"
        + "\n".join(f"- {key}: {value}" for key, value in sorted(type_counts.items()))
        + "\n\n## Edge Relations\n\n"
        + "\n".join(f"- {key}: {value}" for key, value in sorted(relation_counts.items()))
        + "\n",
        encoding="utf-8",
    )
    (output_dir / "graph.html").write_text(render_graph_html(graph), encoding="utf-8")
    return graph


def render_graph_html(graph: dict) -> str:
    graph_json = json.dumps(graph, ensure_ascii=False).replace("</", "<\\/")
    node_count = len(graph.get("nodes", []))
    edge_count = len(graph.get("edges", []))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Astro Wiki Graph</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f7f4;
      --ink: #1d2528;
      --muted: #647174;
      --line: #c9d2cf;
      --panel: #ffffff;
      --page: #2a7f8f;
      --arxiv: #b65f34;
      --topic: #6f63b6;
      --method: #2d7c52;
      --observation: #9a6a18;
      --focus: #203f8f;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background: var(--bg);
      overflow: hidden;
    }}
    header {{
      height: 58px;
      display: flex;
      align-items: center;
      gap: 16px;
      padding: 0 18px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }}
    h1 {{
      margin: 0;
      font-size: 18px;
      font-weight: 700;
      letter-spacing: 0;
      white-space: nowrap;
    }}
    .stats {{
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }}
    .toolbar {{
      margin-left: auto;
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }}
    input, select, button, textarea {{
      height: 34px;
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink);
      border-radius: 6px;
      font: inherit;
      font-size: 13px;
    }}
    input {{
      width: min(34vw, 360px);
      padding: 0 10px;
    }}
    select {{ padding: 0 8px; }}
    textarea {{
      width: 100%;
      min-height: 74px;
      height: auto;
      padding: 9px 10px;
      resize: vertical;
      line-height: 1.35;
    }}
    button {{
      padding: 0 10px;
      cursor: pointer;
    }}
    main {{
      height: calc(100vh - 58px);
      display: grid;
      grid-template-columns: minmax(0, 1fr) 340px;
    }}
    #graph {{
      width: 100%;
      height: 100%;
      display: block;
      background:
        linear-gradient(rgba(40, 52, 55, 0.045) 1px, transparent 1px),
        linear-gradient(90deg, rgba(40, 52, 55, 0.045) 1px, transparent 1px);
      background-size: 28px 28px;
    }}
    aside {{
      border-left: 1px solid var(--line);
      background: var(--panel);
      padding: 16px;
      overflow: auto;
    }}
    .detail-title {{
      font-size: 15px;
      font-weight: 700;
      overflow-wrap: anywhere;
    }}
    .detail-type {{
      margin-top: 6px;
      color: var(--muted);
      font-size: 13px;
    }}
    .detail-id {{
      margin-top: 12px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fafafa;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      overflow-wrap: anywhere;
    }}
    .graph-chat {{
      display: grid;
      gap: 10px;
      padding-bottom: 14px;
      margin-bottom: 14px;
      border-bottom: 1px solid var(--line);
    }}
    .graph-chat label {{
      font-size: 13px;
      font-weight: 700;
    }}
    .facet-row {{
      display: grid;
      grid-template-columns: 112px minmax(0, 1fr);
      gap: 8px;
    }}
    .facet-row input {{
      width: 100%;
    }}
    .graph-chat-actions {{
      display: flex;
      gap: 8px;
      align-items: center;
    }}
    .graph-chat-actions button:first-child {{
      background: var(--page);
      border-color: var(--page);
      color: #fff;
      font-weight: 700;
    }}
    .graph-answer {{
      display: none;
      max-height: 220px;
      overflow: auto;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fafafa;
      font-size: 12px;
      line-height: 1.45;
      white-space: pre-wrap;
    }}
    .graph-answer.active {{ display: block; }}
    .facet-status {{
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }}
    .graph-matches {{
      display: grid;
      gap: 6px;
    }}
    .graph-match {{
      height: auto;
      min-height: 30px;
      padding: 7px 8px;
      text-align: left;
      overflow-wrap: anywhere;
      color: var(--page);
      border-color: rgba(42, 127, 143, 0.42);
      background: #fff;
    }}
    .legend {{
      margin-top: 18px;
      display: grid;
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
    }}
    .legend span {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }}
    .detail-actions {{
      margin-top: 12px;
      display: flex;
      gap: 8px;
    }}
    .detail-actions button {{
      height: 32px;
      border-color: var(--page);
      color: var(--page);
      font-weight: 700;
    }}
    .swatch {{
      width: 11px;
      height: 11px;
      border-radius: 50%;
      display: inline-block;
    }}
    .empty {{
      color: var(--muted);
      line-height: 1.45;
      font-size: 13px;
    }}
    .edge {{ stroke: rgba(70, 82, 84, 0.28); stroke-width: 1.2; }}
    .node {{ cursor: grab; stroke: #fff; stroke-width: 1.6; }}
    .node.search-hit {{
      stroke: #e1b92f;
      stroke-width: 3.2;
      filter: drop-shadow(0 0 5px rgba(225, 185, 47, 0.65));
    }}
    .node:active {{ cursor: grabbing; }}
    .node-label {{
      font-size: 11px;
      fill: #273033;
      paint-order: stroke;
      stroke: rgba(255,255,255,0.9);
      stroke-width: 3px;
      stroke-linejoin: round;
      pointer-events: none;
    }}
    .dim {{ opacity: 0.12; }}
    .hidden {{ display: none; }}
    @media (max-width: 820px) {{
      header {{ height: auto; min-height: 58px; flex-wrap: wrap; padding: 10px 12px; }}
      .toolbar {{ width: 100%; margin-left: 0; }}
      input {{ width: 100%; flex: 1; }}
      main {{ height: calc(100vh - 104px); grid-template-columns: 1fr; }}
      aside {{ display: none; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Astro Wiki Graph</h1>
    <div class="stats">{node_count} nodes · {edge_count} edges</div>
    <div class="toolbar">
      <input id="search" type="search" placeholder="Search pages or paper IDs" aria-label="Search graph">
      <select id="typeFilter" aria-label="Filter by node type">
        <option value="all">All nodes</option>
        <option value="page">Papers</option>
        <option value="arxiv">Paper IDs</option>
        <option value="topic">Topics</option>
        <option value="method">Methods</option>
        <option value="observation">Observations</option>
      </select>
      <select id="viewMode" aria-label="Graph view mode">
        <option value="all">All relations</option>
        <option value="topic">Topic view</option>
        <option value="method">Method view</option>
        <option value="observation">Observation view</option>
      </select>
      <button id="layoutToggle" type="button" title="Run graph layout briefly">Run Layout</button>
      <button id="reset" type="button">Reset</button>
    </div>
  </header>
  <main>
    <svg id="graph" role="img" aria-label="Astro wiki knowledge graph"></svg>
    <aside>
      <form id="graphChat" class="graph-chat">
        <label for="graphQuestion">Semantic Search</label>
        <textarea id="graphQuestion" placeholder="Search by meaning; matching wiki papers will be highlighted." aria-label="Semantic graph search"></textarea>
        <div class="graph-chat-actions">
          <button id="graphAsk" type="submit">Search</button>
          <button id="clearGraphAsk" type="button">Clear</button>
        </div>
        <div id="graphAnswer" class="graph-answer" aria-live="polite"></div>
        <div id="graphMatches" class="graph-matches"></div>
      </form>
      <form id="customFacet" class="graph-chat">
        <label for="facetLabel">Custom Facet</label>
        <div class="facet-row">
          <select id="facetType" aria-label="Custom facet type">
            <option value="observation">Observation / data</option>
            <option value="method">Method</option>
            <option value="topic">Topic</option>
          </select>
          <input id="facetLabel" type="text" placeholder="GMT" aria-label="Custom facet label">
        </div>
        <textarea id="facetKeywords" placeholder="Optional synonyms, comma separated. Example: GMT, Giant Magellan Telescope" aria-label="Custom facet keywords"></textarea>
        <div class="graph-chat-actions">
          <button type="submit">Add Facet</button>
        </div>
        <div id="facetStatus" class="facet-status" aria-live="polite"></div>
      </form>
      <div id="detail" class="empty">Select a node to inspect its path, type, and links.</div>
      <div class="legend">
        <span><i class="swatch" style="background: var(--page)"></i> Wiki page</span>
        <span><i class="swatch" style="background: var(--arxiv)"></i> arXiv source</span>
        <span><i class="swatch" style="background: var(--topic)"></i> Topic</span>
        <span><i class="swatch" style="background: var(--method)"></i> Method</span>
        <span><i class="swatch" style="background: var(--observation)"></i> Observation / data</span>
      </div>
    </aside>
  </main>
  <script id="graph-data" type="application/json">{graph_json}</script>
  <script>
    const graph = JSON.parse(document.getElementById("graph-data").textContent);
    const svg = document.getElementById("graph");
    const detail = document.getElementById("detail");
    const search = document.getElementById("search");
    const typeFilter = document.getElementById("typeFilter");
    const viewMode = document.getElementById("viewMode");
    const layoutToggle = document.getElementById("layoutToggle");
    const resetButton = document.getElementById("reset");
    const graphChat = document.getElementById("graphChat");
    const graphQuestion = document.getElementById("graphQuestion");
    const graphAnswer = document.getElementById("graphAnswer");
    const graphMatches = document.getElementById("graphMatches");
    const clearGraphAsk = document.getElementById("clearGraphAsk");
    const customFacet = document.getElementById("customFacet");
    const facetType = document.getElementById("facetType");
    const facetLabel = document.getElementById("facetLabel");
    const facetKeywords = document.getElementById("facetKeywords");
    const facetStatus = document.getElementById("facetStatus");
    const colors = {{
      page: "#2a7f8f",
      arxiv: "#b65f34",
      topic: "#6f63b6",
      method: "#2d7c52",
      observation: "#9a6a18"
    }};
    const nodes = graph.nodes.map((node, index) => ({{
      ...node,
      index,
      x: 120 + (index % 12) * 62,
      y: 100 + Math.floor(index / 12) * 58,
      vx: 0,
      vy: 0
    }}));
    const byId = new Map(nodes.map(node => [node.id, node]));
    const links = graph.edges
      .map(edge => ({{ ...edge, source: byId.get(edge.source), target: byId.get(edge.target) }}))
      .filter(edge => edge.source && edge.target);
    const ns = "http://www.w3.org/2000/svg";
    const viewport = document.createElementNS(ns, "g");
    const edgeLayer = document.createElementNS(ns, "g");
    const nodeLayer = document.createElementNS(ns, "g");
    const labelLayer = document.createElementNS(ns, "g");
    svg.appendChild(viewport);
    viewport.append(edgeLayer, nodeLayer, labelLayer);

    function make(tag, attrs) {{
      const element = document.createElementNS(ns, tag);
      for (const [key, value] of Object.entries(attrs || {{}})) element.setAttribute(key, value);
      return element;
    }}

    const edgeEls = links.map(link => {{
      const element = make("line", {{ class: "edge" }});
      edgeLayer.appendChild(element);
      return element;
    }});
    const nodeEls = nodes.map(node => {{
      const degree = links.filter(link => link.source === node || link.target === node).length;
      node.degree = degree;
      const radius = Math.max(5, Math.min(15, 5 + Math.sqrt(degree) * 2.2));
      const element = make("circle", {{
        class: "node",
        r: radius,
        fill: colors[node.type] || "#687176",
        tabindex: 0
      }});
      element.addEventListener("click", () => selectNode(node));
      element.addEventListener("keydown", event => {{
        if (event.key === "Enter" || event.key === " ") selectNode(node);
      }});
      attachDrag(element, node);
      nodeLayer.appendChild(element);
      return element;
    }});
    const labelEls = nodes.map(node => {{
      const text = make("text", {{ class: "node-label", "text-anchor": "middle" }});
      text.textContent = node.label.length > 26 ? node.label.slice(0, 24) + "…" : node.label;
      labelLayer.appendChild(text);
      return text;
    }});

    let width = 0, height = 0;
    let zoom = 1, panX = 0, panY = 0;
    let selected = null;
    let paused = true;
    let frameId = 0;
    let layoutStopAt = 0;
    let highlightedNodeIds = new Set();
    const initialFocusNodeId = new URLSearchParams(window.location.search).get("focus") || "";
    const LAYOUT_WARMUP_MS = 3000;
    const MAX_REPULSION_CHECKS_PER_FRAME = 45000;

    function resize() {{
      const rect = svg.getBoundingClientRect();
      width = rect.width;
      height = rect.height;
      if (!panX && !panY) {{
        panX = width * 0.08;
        panY = height * 0.08;
      }}
      svg.setAttribute("viewBox", `0 0 ${{width}} ${{height}}`);
    }}
    window.addEventListener("resize", resize);
    resize();

    function startLayout(durationMs = LAYOUT_WARMUP_MS) {{
      paused = false;
      layoutStopAt = durationMs ? performance.now() + durationMs : 0;
      updateLayoutToggle();
      requestTick();
    }}

    function pauseLayout() {{
      paused = true;
      layoutStopAt = 0;
      for (const node of nodes) {{
        node.vx = 0;
        node.vy = 0;
      }}
      if (frameId) {{
        cancelAnimationFrame(frameId);
        frameId = 0;
      }}
      updateLayoutToggle();
      render();
    }}

    function requestTick() {{
      if (!frameId) frameId = requestAnimationFrame(tick);
    }}

    function updateLayoutToggle() {{
      layoutToggle.textContent = paused ? "Run Layout" : "Pause Layout";
      layoutToggle.title = paused ? "Run graph layout for 3 seconds" : "Pause graph layout";
      layoutToggle.setAttribute("aria-pressed", String(!paused));
    }}

    function tick(now) {{
      frameId = 0;
      if (!paused) {{
        const centerX = width / 2;
        const centerY = height / 2;
        for (const node of nodes) {{
          node.vx += (centerX - node.x) * 0.0009;
          node.vy += (centerY - node.y) * 0.0009;
        }}
        for (const link of links) {{
          const dx = link.target.x - link.source.x;
          const dy = link.target.y - link.source.y;
          const distance = Math.max(1, Math.hypot(dx, dy));
          const desired = link.relation === "cites" ? 94 : 118;
          const force = (distance - desired) * 0.0025;
          const fx = dx / distance * force;
          const fy = dy / distance * force;
          link.source.vx += fx;
          link.source.vy += fy;
          link.target.vx -= fx;
          link.target.vy -= fy;
        }}
        const totalPairs = Math.max(1, nodes.length * (nodes.length - 1) / 2);
        const repulsionStride = Math.max(1, Math.ceil(totalPairs / MAX_REPULSION_CHECKS_PER_FRAME));
        let pairIndex = 0;
        for (let i = 0; i < nodes.length; i++) {{
          for (let j = i + 1; j < nodes.length; j++, pairIndex++) {{
            if (pairIndex % repulsionStride !== 0) continue;
            const a = nodes[i], b = nodes[j];
            const dx = b.x - a.x;
            const dy = b.y - a.y;
            const distanceSq = Math.max(36, dx * dx + dy * dy);
            const force = 95 / distanceSq;
            a.vx -= dx * force;
            a.vy -= dy * force;
            b.vx += dx * force;
            b.vy += dy * force;
          }}
        }}
        for (const node of nodes) {{
          if (node.fixed) continue;
          node.vx *= 0.86;
          node.vy *= 0.86;
          node.x += node.vx;
          node.y += node.vy;
        }}
      }}
      renderPositions();
      if (!paused && layoutStopAt && now >= layoutStopAt) {{
        pauseLayout();
        return;
      }}
      if (!paused) requestTick();
    }}

    function renderPositions() {{
      viewport.setAttribute("transform", `translate(${{panX}} ${{panY}}) scale(${{zoom}})`);
      edgeEls.forEach((element, index) => {{
        const link = links[index];
        element.setAttribute("x1", link.source.x);
        element.setAttribute("y1", link.source.y);
        element.setAttribute("x2", link.target.x);
        element.setAttribute("y2", link.target.y);
      }});
      nodeEls.forEach((element, index) => {{
        const node = nodes[index];
        element.setAttribute("cx", node.x);
        element.setAttribute("cy", node.y);
      }});
      labelEls.forEach((element, index) => {{
        const node = nodes[index];
        element.setAttribute("x", node.x);
        element.setAttribute("y", node.y + 24);
      }});
    }}

    function render() {{
      renderPositions();
      applyFilters();
    }}

    function selectNode(node) {{
      selected = node;
      if (node) highlightedNodeIds.add(node.id);
      const neighbors = links
        .filter(link => link.source === node || link.target === node)
        .map(link => link.source === node ? link.target : link.source);
      const paperId = paperIdForNode(node);
      const actions = paperId
        ? `<div class="detail-actions"><button id="openChat" type="button">Open in Chat</button></div>`
        : "";
      detail.className = "";
      detail.innerHTML = `
        <div class="detail-title">${{escapeHtml(node.label)}}</div>
        <div class="detail-type">${{escapeHtml(node.type)}} · ${{node.degree}} links</div>
        <div class="detail-id">${{escapeHtml(node.id)}}</div>
        ${{actions}}
        <h2 style="font-size:13px;margin:18px 0 8px">Connected Nodes</h2>
        <div class="empty">${{neighbors.slice(0, 18).map(n => escapeHtml(n.label)).join("<br>") || "No connected nodes."}}</div>
      `;
      const openChat = document.getElementById("openChat");
      if (openChat && paperId) openChat.addEventListener("click", () => openPaperChat(paperId));
      applyFilters();
    }}

    function focusNode(node) {{
      selected = node;
      node.fixed = true;
      node.x = Math.max(80, width / 2);
      node.y = Math.max(80, height / 2);
      node.vx = 0;
      node.vy = 0;
      node.fixed = false;
      panX = width * 0.08;
      panY = height * 0.08;
      zoom = 1;
      selectNode(node);
      renderPositions();
    }}

    function paperIdForNode(node) {{
      if (node.type !== "page" || !node.id.startsWith("wiki/papers/") || !node.id.endsWith(".md")) return "";
      return node.id.slice("wiki/papers/".length, -".md".length).replace("_", "/");
    }}

    function openPaperChat(arxivId) {{
      if (window.parent && window.parent !== window) {{
        window.parent.postMessage({{ type: "open-paper-chat", arxiv_id: arxivId }}, window.location.origin);
      }} else {{
        window.location.href = `/?chatPaper=${{encodeURIComponent(arxivId)}}`;
      }}
    }}

    function graphNodeIdForSource(source) {{
      if (!source) return "";
      if (source.startsWith("wiki/papers/") && source.endsWith(".md")) return source;
      const textMatch = source.match(/^data\\/text\\/(.+)\\.txt$/);
      if (textMatch) return `wiki/papers/${{textMatch[1]}}.md`;
      const summaryMatch = source.match(/^data\\/summaries\\/ko\\/(.+)\\.md$/);
      if (summaryMatch) return `wiki/papers/${{summaryMatch[1]}}.md`;
      return "";
    }}

    function sourceNodeIds(sources) {{
      const ids = [];
      for (const source of sources || []) {{
        const nodeId = graphNodeIdForSource(source);
        if (nodeId && byId.has(nodeId) && !ids.includes(nodeId)) ids.push(nodeId);
      }}
      return ids;
    }}

    function renderGraphMatches(nodeIds) {{
      graphMatches.innerHTML = "";
      for (const nodeId of nodeIds.slice(0, 12)) {{
        const node = byId.get(nodeId);
        if (!node) continue;
        const button = document.createElement("button");
        button.type = "button";
        button.className = "graph-match";
        button.textContent = `${{node.label}} · ${{node.id}}`;
        button.addEventListener("click", () => focusNode(node));
        graphMatches.appendChild(button);
      }}
      if (!graphMatches.children.length) {{
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "No graph node matched the retrieved sources.";
        graphMatches.appendChild(empty);
      }}
    }}

    function resetVisibleGraph(options = {{}}) {{
      search.value = "";
      typeFilter.value = "all";
      viewMode.value = "all";
      selected = null;
      highlightedNodeIds = new Set();
      zoom = 1;
      panX = width * 0.08;
      panY = height * 0.08;
      detail.className = "empty";
      detail.textContent = "Select a node to inspect its path, type, and links.";
      if (options.clearMatches !== false) graphMatches.innerHTML = "";
      render();
    }}

    async function askGraph(question) {{
      graphAnswer.classList.add("active");
      graphAnswer.textContent = "Searching graph context...";
      resetVisibleGraph();
      let highlighted = false;
      try {{
        const searchResponse = await fetch("/api/graph-search", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ question }}),
        }});
        const searchData = await searchResponse.json();
        if (!searchResponse.ok || searchData.error) throw new Error(searchData.error || `HTTP ${{searchResponse.status}}`);
        const initialNodeIds = searchData.node_ids?.length ? searchData.node_ids : sourceNodeIds(searchData.sources || []);
        resetVisibleGraph({{ clearMatches: false }});
        highlightedNodeIds = new Set(initialNodeIds);
        renderGraphMatches(initialNodeIds);
        if (initialNodeIds.length) focusNode(byId.get(initialNodeIds[0]));
        else render();
        highlighted = true;
        graphAnswer.textContent = initialNodeIds.length
          ? `Found ${{initialNodeIds.length}} matching graph nodes. Generating answer...`
          : "No matching graph node found yet. Generating answer from retrieved wiki context...";

        const response = await fetch("/api/chat", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ question }}),
        }});
        const data = await response.json();
        if (!response.ok || data.error) throw new Error(data.error || `HTTP ${{response.status}}`);
        graphAnswer.textContent = data.model ? `[${{data.model}}]\\n\\n${{data.answer}}` : data.answer;
        const answerNodeIds = sourceNodeIds(data.sources || []);
        const mergedNodeIds = Array.from(new Set([...initialNodeIds, ...answerNodeIds]));
        resetVisibleGraph({{ clearMatches: false }});
        highlightedNodeIds = new Set(mergedNodeIds);
        renderGraphMatches(mergedNodeIds);
        if (mergedNodeIds.length) focusNode(byId.get(mergedNodeIds[0]));
        else render();
      }} catch (error) {{
        graphAnswer.textContent = error.message || String(error);
        if (!highlighted) highlightedNodeIds = new Set();
        render();
      }}
    }}

    async function addCustomFacet() {{
      const label = facetLabel.value.trim();
      if (!label) {{
        facetStatus.textContent = "Enter a facet label.";
        return;
      }}
      facetStatus.textContent = "Adding facet and rebuilding graph...";
      try {{
        const response = await fetch("/api/graph-facet", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{
            facet_type: facetType.value,
            label,
            keywords: facetKeywords.value
          }}),
        }});
        const data = await response.json();
        if (!response.ok || data.error) throw new Error(data.error || `HTTP ${{response.status}}`);
        facetStatus.textContent = `Added ${{data.label}} with ${{data.edge_count}} graph links. Reloading...`;
        window.location.href = `/graph.html?ts=${{Date.now()}}&focus=${{encodeURIComponent(data.node_id)}}`;
      }} catch (error) {{
        facetStatus.textContent = error.message || String(error);
      }}
    }}

    function applyFilters() {{
      const query = search.value.trim().toLowerCase();
      const type = typeFilter.value;
      const mode = viewMode.value;
      const modeRelations = {{
        all: null,
        topic: "has_topic",
        method: "has_method",
        observation: "has_observation"
      }};
      const relationFilter = modeRelations[mode];
      const relationVisible = link => !relationFilter || link.relation === relationFilter;
      const modeNodeIds = new Set();
      if (relationFilter) {{
        for (const link of links) {{
          if (relationVisible(link)) {{
            modeNodeIds.add(link.source.id);
            modeNodeIds.add(link.target.id);
          }}
        }}
      }}
      const matches = new Set(nodes.filter(node => {{
        const typeMatch = type === "all" || node.type === type;
        const queryMatch = !query || node.id.toLowerCase().includes(query) || node.label.toLowerCase().includes(query);
        const modeMatch = !relationFilter || modeNodeIds.has(node.id);
        return typeMatch && queryMatch && modeMatch;
      }}));
      const related = new Set();
      if (selected) {{
        related.add(selected);
        for (const link of links) {{
          if (link.source === selected) related.add(link.target);
          if (link.target === selected) related.add(link.source);
        }}
      }}
      for (const nodeId of highlightedNodeIds) {{
        const node = byId.get(nodeId);
        if (node) related.add(node);
      }}
      nodeEls.forEach((element, index) => {{
        const node = nodes[index];
        const visible = matches.has(node);
        element.classList.toggle("hidden", !visible);
        element.classList.toggle("search-hit", highlightedNodeIds.has(node.id));
        element.classList.toggle("dim", Boolean(selected || highlightedNodeIds.size) && !related.has(node));
      }});
      labelEls.forEach((element, index) => {{
        const node = nodes[index];
        const visible = matches.has(node) && (node.degree > 1 || node === selected || highlightedNodeIds.has(node.id) || search.value);
        element.classList.toggle("hidden", !visible);
        element.classList.toggle("dim", Boolean(selected || highlightedNodeIds.size) && !related.has(node));
      }});
      edgeEls.forEach((element, index) => {{
        const link = links[index];
        const visible = matches.has(link.source) && matches.has(link.target) && relationVisible(link);
        const selectedEdge = selected && (link.source === selected || link.target === selected);
        const highlightedEdge = highlightedNodeIds.has(link.source.id) || highlightedNodeIds.has(link.target.id);
        element.classList.toggle("hidden", !visible);
        element.classList.toggle("dim", Boolean(selected || highlightedNodeIds.size) && !selectedEdge && !highlightedEdge);
        element.style.strokeWidth = selectedEdge || highlightedEdge ? "2.4" : "1.2";
      }});
    }}

    function attachDrag(element, node) {{
      element.addEventListener("pointerdown", event => {{
        event.preventDefault();
        pauseLayout();
        node.fixed = true;
        element.setPointerCapture(event.pointerId);
        const move = moveEvent => {{
          const point = svgPoint(moveEvent);
          node.x = (point.x - panX) / zoom;
          node.y = (point.y - panY) / zoom;
          renderPositions();
        }};
        const up = () => {{
          node.fixed = false;
          renderPositions();
          element.removeEventListener("pointermove", move);
          element.removeEventListener("pointerup", up);
          element.removeEventListener("pointercancel", up);
        }};
        element.addEventListener("pointermove", move);
        element.addEventListener("pointerup", up);
        element.addEventListener("pointercancel", up);
      }});
    }}

    function svgPoint(event) {{
      const rect = svg.getBoundingClientRect();
      return {{ x: event.clientX - rect.left, y: event.clientY - rect.top }};
    }}

    svg.addEventListener("wheel", event => {{
      event.preventDefault();
      const factor = event.deltaY < 0 ? 1.08 : 0.92;
      zoom = Math.max(0.25, Math.min(3, zoom * factor));
      renderPositions();
    }}, {{ passive: false }});

    let panning = null;
    svg.addEventListener("pointerdown", event => {{
      if (event.target !== svg) return;
      panning = {{ x: event.clientX, y: event.clientY, panX, panY }};
      svg.setPointerCapture(event.pointerId);
    }});
    svg.addEventListener("pointermove", event => {{
      if (!panning) return;
      panX = panning.panX + event.clientX - panning.x;
      panY = panning.panY + event.clientY - panning.y;
      renderPositions();
    }});
    svg.addEventListener("pointerup", () => panning = null);
    svg.addEventListener("pointercancel", () => panning = null);

    search.addEventListener("input", applyFilters);
    typeFilter.addEventListener("change", applyFilters);
    viewMode.addEventListener("change", applyFilters);
    layoutToggle.addEventListener("click", () => {{
      if (paused) startLayout();
      else pauseLayout();
    }});
    graphChat.addEventListener("submit", event => {{
      event.preventDefault();
      const question = graphQuestion.value.trim();
      if (question) askGraph(question);
    }});
    customFacet.addEventListener("submit", event => {{
      event.preventDefault();
      addCustomFacet();
    }});
    clearGraphAsk.addEventListener("click", () => {{
      graphQuestion.value = "";
      graphAnswer.textContent = "";
      graphAnswer.classList.remove("active");
      resetVisibleGraph();
    }});
    resetButton.addEventListener("click", async () => {{
      resetVisibleGraph();
      facetStatus.textContent = "Resetting custom facets...";
      try {{
        const response = await fetch("/api/graph-facets/reset", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{}})
        }});
        const data = await response.json();
        if (!response.ok || data.error) throw new Error(data.error || `HTTP ${{response.status}}`);
        if (data.removed_count > 0) {{
          facetStatus.textContent = `Removed ${{data.removed_count}} custom facets. Reloading...`;
          window.location.href = `/graph.html?ts=${{Date.now()}}`;
        }} else {{
          facetStatus.textContent = "";
        }}
      }} catch (error) {{
        facetStatus.textContent = error.message || String(error);
      }}
    }});

    function escapeHtml(value) {{
      return String(value).replace(/[&<>"']/g, char => ({{
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        "\\"": "&quot;",
        "'": "&#039;"
      }}[char]));
    }}

    updateLayoutToggle();
    render();
    startLayout();
    if (initialFocusNodeId && byId.has(initialFocusNodeId)) {{
      highlightedNodeIds = new Set([initialFocusNodeId]);
      setTimeout(() => focusNode(byId.get(initialFocusNodeId)), 120);
    }}
  </script>
</body>
</html>
"""
