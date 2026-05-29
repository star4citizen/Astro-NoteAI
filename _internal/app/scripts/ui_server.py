#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import html
import json
import mimetypes
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from datetime import date, datetime
from difflib import SequenceMatcher
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import yaml

import _bootstrap  # noqa: F401

from astro_wiki import arxiv_client
from astro_wiki.config import (
    LOCAL_SETTINGS_ENV_KEYS,
    api_provider_default_base_url,
    api_provider_default_chat_model,
    api_provider_default_retrieval_model,
    api_provider_label,
    api_provider_model_catalog,
    api_provider_names,
    apply_local_settings_to_env,
    chat_model,
    context_window,
    db_path,
    is_api_provider,
    load_local_settings,
    local_settings_path,
    llm_provider,
    load_yaml,
    nasa_ads_api_key,
    ollama_base_url,
    openai_api_key,
    openai_base_url,
    project_path,
    retrieval_model,
    save_local_settings,
)
from astro_wiki.codex_client import CodexError, chat as codex_chat, codex_status
from astro_wiki.db import connect, get_state, init_db, register_wiki_page, set_state, upsert_paper, update_paper_status, utc_now
from astro_wiki.logging import append_wiki_log
from astro_wiki.pdf_text import extract_pdf_text
from astro_wiki.qmd_client import collections_for
from astro_wiki.retrieval import (
    RetrievedPage,
    build_context,
    contains_korean,
    excerpt_for,
    expand_query_terms,
    qmd_search_pages,
    score_text,
    search_wiki,
    tokenize,
)
from astro_wiki.ollama_client import chat
from astro_wiki.wiki_io import paper_filename, safe_arxiv_filename

ROOT = project_path()
apply_local_settings_to_env()
UI_DIR = ROOT / "ui"
LANDING_PAGE = ROOT / "index.html" if (ROOT / "index.html").exists() else ROOT.parent / "index.html"
ALLOWED_READ_ROOTS = [ROOT / "wiki", ROOT / "data" / "text", ROOT / "data" / "markdown", ROOT / "graphify-out"]
ALLOWED_WIKI_UPDATE_DIRS = {
    "topics": "topic",
    "methods": "method",
    "surveys": "survey",
    "simulations": "simulation",
    "concepts": "concept",
    "entities": "entity",
}
PDF_ROOT = ROOT / "data" / "raw"
KOREAN_SUMMARY_DIR = ROOT / "data" / "summaries" / "ko"
UPLOAD_PROGRESS: dict[str, dict] = {}
UPLOAD_PROGRESS_LOCK = threading.Lock()
UPLOAD_PROGRESS_TTL_SECONDS = 6 * 60 * 60
DEEP_SUMMARY_DIR = ROOT / "data" / "summaries" / "deep" / "ko"
DEEP_SUMMARY_WIKI_EXPORT_DIR = ROOT / "data" / "summaries" / "deep" / "wiki"
CODEX_MODEL_PREFIX = "codex:"
GRAPH_FACETS_PATH = ROOT / "config" / "graph_facets.yml"
DEFAULT_PAPERFORGE_ROOT = Path("/root/PaperForge")
if os.name == "nt":
    DEFAULT_PAPERFORGE_ROOT = Path.home() / "PaperForge"
UPLOAD_WORK_PROMPT_FILE = "ingest_reduce_paper.md"
UPLOAD_WORK_PROMPT_SETTING = "upload_work_prompt"
BATCH_UPLOAD_MANIFEST_PATH = ROOT / "data" / "config" / "batch_upload_manifest.json"


def script_command(script_path: str, *args: str) -> list[str]:
    runner = os.getenv("ASTRO_WIKI_SCRIPT_RUNNER")
    if runner:
        return [runner, "--run-script", script_path, *args]
    return [sys.executable, script_path, *args]


def row_to_dict(row) -> dict:
    return dict(row) if row is not None else {}


def json_response(handler: BaseHTTPRequestHandler, payload: dict, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler: BaseHTTPRequestHandler, text: str, content_type: str = "text/plain; charset=utf-8", status: int = 200) -> None:
    body = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def safe_path(relative_path: str) -> Path:
    candidate = (ROOT / relative_path).resolve()
    for allowed_root in ALLOWED_READ_ROOTS:
        try:
            candidate.relative_to(allowed_root.resolve())
            return candidate
        except ValueError:
            continue
    raise ValueError("Path is outside allowed roots")


def safe_pdf_path(relative_path: str) -> Path:
    candidate = (ROOT / relative_path).resolve()
    candidate.relative_to(PDF_ROOT.resolve())
    if candidate.suffix.lower() != ".pdf":
        raise ValueError("Only PDF files can be served here")
    return candidate


def markdown_heading_anchor(text: str) -> str:
    plain = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", str(text or ""))
    plain = re.sub(r"[*_`]+", "", plain)
    plain = re.sub(r"\s+", " ", plain).strip(" .:-")
    lowered = plain.lower()
    if lowered == "abstract":
        return "abstract"
    section = re.match(r"(?P<number>\d{1,2}(?:\.\d+)*)(?:\.|\s+)", plain)
    if section and not re.fullmatch(r"(?:19|20)\d{2}", section.group("number")):
        return "section-" + section.group("number").replace(".", "-")
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return slug[:96].strip("-")


def html_id_attr(anchor: str) -> str:
    if not anchor:
        return ""
    safe = re.sub(r"[^A-Za-z0-9_.:-]+", "-", anchor).strip("-")
    return f' id="{html.escape(safe, quote=True)}"' if safe else ""


def markdown_to_html(markdown: str) -> str:
    lines = markdown.splitlines()
    out: list[str] = []
    in_list = False
    in_code = False
    code_lines: list[str] = []

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                out.append(f"<pre>{html.escape(chr(10).join(code_lines))}</pre>")
                code_lines = []
                in_code = False
            else:
                close_list()
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not stripped:
            close_list()
            continue
        if stripped.startswith("---"):
            continue
        source_anchor = re.fullmatch(r"<!--\s*astro-note-anchor:\s*([A-Za-z0-9_.:-]+)\s*-->", stripped)
        if source_anchor:
            close_list()
            out.append(f'<span{html_id_attr(source_anchor.group(1))} class="source-anchor"></span>')
            continue
        heading = re.match(r"^(#{1,4})\s+(.+?)\s*$", stripped)
        if heading:
            close_list()
            level = len(heading.group(1))
            title = heading.group(2)
            out.append(f"<h{level}{html_id_attr(markdown_heading_anchor(title))}>{inline_md(title)}</h{level}>")
        elif stripped.startswith(("- ", "* ")):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline_md(stripped[2:])}</li>")
        else:
            close_list()
            out.append(f"<p>{inline_md(stripped)}</p>")
    close_list()
    if in_code:
        out.append(f"<pre>{html.escape(chr(10).join(code_lines))}</pre>")
    return "\n".join(out)


def markdown_section(markdown: str, heading: str) -> str:
    pattern = re.compile(
        rf"^## {re.escape(heading)}\s*$\n(?P<body>.*?)(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(markdown)
    return match.group("body").strip() if match else ""


def compact_markdown_for_summary(markdown: str) -> str:
    sections = []
    for heading in ["Scientific Question", "Data", "Method", "Main Results", "Limitations", "Follow-up Questions"]:
        body = markdown_section(markdown, heading)
        if body:
            sections.append(f"## {heading}\n\n{body}")
    if sections:
        return "\n\n".join(sections)
    return markdown[:8000]


def summary_cache_path(arxiv_id: str) -> Path:
    safe_id = arxiv_id.replace("/", "_")
    return KOREAN_SUMMARY_DIR / f"{safe_id}.md"


def deep_summary_cache_path(arxiv_id: str) -> Path:
    return DEEP_SUMMARY_DIR / f"{paper_safe_id(arxiv_id)}.md"


def deep_summary_wiki_export_path(arxiv_id: str) -> Path:
    return DEEP_SUMMARY_WIKI_EXPORT_DIR / f"{paper_safe_id(arxiv_id)}.wiki"


def paper_deep_summary_wiki_rel(arxiv_id: str) -> str:
    return f"wiki/papers/{paper_safe_id(arxiv_id)}-deep-summary.md"


def paper_wiki_rel(arxiv_id: str) -> str:
    fallback = f"wiki/papers/{arxiv_id.replace('/', '_')}.md"
    if not arxiv_id:
        return fallback
    try:
        with connect() as conn:
            init_db(conn)
            page = conn.execute(
                """
                SELECT path
                FROM wiki_pages
                WHERE arxiv_id = ? AND page_type = 'paper'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (arxiv_id,),
            ).fetchone()
            if page and page["path"]:
                return str(page["path"])
            paper = conn.execute(
                """
                SELECT title, authors_json, published, announced_date, updated
                FROM papers
                WHERE arxiv_id = ?
                ORDER BY version DESC
                LIMIT 1
                """,
                (arxiv_id,),
            ).fetchone()
            if paper:
                return "wiki/papers/" + paper_filename(
                    arxiv_id,
                    title=paper["title"],
                    authors=paper["authors_json"],
                    year=paper["published"] or paper["announced_date"] or paper["updated"],
                )
    except Exception:
        pass
    return fallback


def attach_paper_wiki_state(paper: dict) -> dict:
    arxiv_id = str(paper.get("arxiv_id") or "").strip()
    if not arxiv_id:
        paper["wiki_path"] = ""
        paper["wiki_exists"] = False
        return paper
    wiki_rel = paper_wiki_rel(arxiv_id)
    paper["wiki_path"] = wiki_rel
    paper["wiki_exists"] = (ROOT / wiki_rel).exists()
    return paper


def paper_safe_id(arxiv_id: str) -> str:
    return arxiv_id.replace("/", "_")


def paper_markdown_rel(arxiv_id: str) -> str:
    return f"data/markdown/{paper_safe_id(arxiv_id)}.md"


def read_wiki_excerpt(relative_path: str, max_chars: int = 3600) -> str:
    try:
        full_path = safe_path(relative_path)
    except ValueError:
        return ""
    if not full_path.exists() or full_path.suffix != ".md":
        return ""
    text = full_path.read_text(encoding="utf-8", errors="ignore")
    if relative_path.startswith("wiki/papers/"):
        return compact_markdown_for_summary(text)[:max_chars]
    return text[:max_chars]


def safe_project_file(relative_path: str | None, allowed_roots: list[Path], suffix: str | None = None) -> Path | None:
    if not relative_path:
        return None
    candidate = (ROOT / relative_path).resolve()
    if suffix and candidate.suffix.lower() != suffix:
        return None
    for allowed_root in allowed_roots:
        try:
            candidate.relative_to(allowed_root.resolve())
            return candidate
        except ValueError:
            continue
    return None


def unlink_if_exists(path: Path | None) -> bool:
    if path is None or not path.exists():
        return False
    if path.is_file():
        path.unlink()
        return True
    return False


def remove_tree_if_exists(path: Path | None) -> bool:
    if path is None or not path.exists():
        return False
    if path.is_dir():
        shutil.rmtree(path)
        return True
    if path.is_file():
        path.unlink()
        return True
    return False


def prune_paper_references(arxiv_id: str) -> list[str]:
    safe_id = paper_safe_id(arxiv_id)
    paper_rel = paper_wiki_rel(arxiv_id)
    paper_name = Path(paper_rel).name
    needles = {
        arxiv_id,
        f"papers/{safe_id}.md",
        f"wiki/papers/{safe_id}.md",
        f"../papers/{safe_id}.md",
        f"papers/{paper_name}",
        paper_rel,
        f"../papers/{paper_name}",
    }
    changed: list[str] = []
    candidate_roots = [
        ROOT / "wiki" / "index.md",
        ROOT / "wiki" / "daily",
        ROOT / "wiki" / "topics",
        ROOT / "wiki" / "methods",
        ROOT / "wiki" / "surveys",
        ROOT / "wiki" / "simulations",
        ROOT / "wiki" / "concepts",
        ROOT / "wiki" / "entities",
    ]
    paths: list[Path] = []
    for root in candidate_roots:
        if root.is_file():
            paths.append(root)
        elif root.is_dir():
            paths.extend(sorted(root.rglob("*.md")))
    for path in paths:
        if path.name == "log.md" or path == ROOT / paper_wiki_rel(arxiv_id):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        kept = [line for line in lines if not any(needle in line for needle in needles)]
        if kept != lines:
            path.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
            if path.match("*/wiki/daily/*.md"):
                normalize_daily_digest_counts(path)
            changed.append(str(path.relative_to(ROOT)).replace("\\", "/"))
    return changed


def markdown_section_text(markdown: str, heading: str) -> str:
    pattern = re.compile(rf"^## {re.escape(heading)}\s*$\n(?P<body>.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
    match = pattern.search(markdown)
    return match.group("body") if match else ""


def normalize_daily_digest_counts(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="ignore")
    brief_count = len(re.findall(r"^- \*\*", markdown_section_text(text, "Brief Notes on All Selected Papers"), re.MULTILINE))
    high_count = len(re.findall(r"^- \*\*", markdown_section_text(text, "High-interest Papers"), re.MULTILINE))
    text = re.sub(r"^paper_count:\s*\d+\s*$", f"paper_count: {brief_count}", text, flags=re.MULTILINE)
    text = re.sub(r"^high_interest_count:\s*\d+\s*$", f"high_interest_count: {high_count}", text, flags=re.MULTILINE)
    text = re.sub(
        r"^\d+ selected papers were ingested for this date\.$",
        f"{brief_count} selected {'paper was' if brief_count == 1 else 'papers were'} ingested for this date.",
        text,
        flags=re.MULTILINE,
    )
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def extracted_text_context(paper: dict, question: str = "", max_chars: int = 14000) -> str:
    text_path = paper.get("text_path")
    if not text_path:
        return ""
    try:
        full_path = safe_path(text_path)
    except ValueError:
        return ""
    if not full_path.exists():
        return ""
    text = full_path.read_text(encoding="utf-8", errors="ignore")
    compact = " ".join(text.split())
    chunks: list[str] = []

    def add_chunk(label: str, start: int, length: int = 3200) -> None:
        if start < 0:
            return
        chunk = compact[start : start + length].strip()
        if chunk and chunk not in chunks:
            chunks.append(f"[{label}]\n{chunk}")

    lowered = compact.lower()
    add_chunk("paragraph 1", 0, 2600)
    hint_patterns = question_context_patterns(question)
    for label, patterns in hint_patterns:
        positions = [lowered.find(pattern) for pattern in patterns if lowered.find(pattern) >= 0]
        if positions:
            add_chunk(label, max(0, min(positions) - 700), 5200)
    for label, patterns in [
        ("methods/data neighborhood", ["methods", "methodology", "observations", "data", "sample"]),
        ("results neighborhood", ["results", "main results"]),
        ("discussion neighborhood", ["discussion", "limitations"]),
        ("conclusion neighborhood", ["conclusions", "conclusion", "summary"]),
    ]:
        positions = [lowered.find(pattern) for pattern in patterns if lowered.find(pattern) >= 0]
        if positions:
            add_chunk(label, max(0, min(positions) - 500), 3600)

    terms = tokenize(question + " " + question_context_terms(question))
    if terms:
        query_excerpt = excerpt_for(compact, terms, max_chars=3600)
        if query_excerpt:
            chunks.append(f"[question-relevant neighborhood]\n{query_excerpt}")

    output = "\n\n".join(chunks)
    return output[:max_chars]


def markdown_source_context(arxiv_id: str, question: str = "", max_chars: int = 16000) -> str:
    markdown_rel = paper_markdown_rel(arxiv_id)
    try:
        full_path = safe_path(markdown_rel)
    except ValueError:
        return ""
    if not full_path.exists():
        return ""
    markdown = full_path.read_text(encoding="utf-8", errors="ignore")
    chunks: list[str] = []

    def add_chunk(label: str, body: str, limit: int) -> None:
        compact = re.sub(r"\n{3,}", "\n\n", body).strip()
        if not compact:
            return
        if len(compact) > limit:
            compact = compact[:limit].rsplit("\n", 1)[0].rstrip() or compact[:limit].rstrip()
        item = f"[{label}]\n{compact}"
        if item not in chunks:
            chunks.append(item)

    add_chunk("markdown opening", markdown[:3200], 3200)
    headings = list(re.finditer(r"^(#{1,6})\s+(.+?)\s*$", markdown, flags=re.MULTILINE))
    terms = expand_query_terms(question + " " + question_context_terms(question))
    wanted = [
        ("abstract", ["abstract"]),
        ("methods/data", ["method", "methods", "methodology", "observation", "observations", "data", "sample"]),
        ("results", ["result", "results"]),
        ("discussion/limitations", ["discussion", "limitation", "limitations", "caveat", "caveats"]),
        ("conclusion/summary", ["conclusion", "conclusions", "summary"]),
    ]
    for index, match in enumerate(headings):
        title = match.group(2).strip()
        lowered_title = title.lower()
        start = match.start()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(markdown)
        section = markdown[start:end].strip()
        for label, needles in wanted:
            if any(needle in lowered_title for needle in needles):
                add_chunk(f"markdown {label}: {title}", section, 3800)
                break
        if terms and any(term in lowered_title or term in section.lower() for term in terms):
            add_chunk(f"markdown question-relevant section: {title}", section, 4200)
        if len("\n\n".join(chunks)) >= max_chars:
            break
    output = "\n\n".join(chunks)
    return output[:max_chars]


def question_context_terms(question: str) -> str:
    lowered = question.lower()
    terms: list[str] = []
    if any(token in lowered for token in ["초기", "initial", "ic", "조건", "시뮬레이션"]):
        terms.extend(
            [
                "initial conditions",
                "ICs",
                "Zel'dovich",
                "Zel’dovich",
                "z = 127",
                "GADGET-4",
                "Lbox",
                "850",
                "cosmological parameters",
                "cosmic string loops",
            ]
        )
    if any(token in lowered for token in ["해상도", "resolution", "softening", "질량"]):
        terms.extend(["mass resolution", "softening length", "dark matter", "gas particles"])
    return " ".join(terms)


def question_context_patterns(question: str) -> list[tuple[str, list[str]]]:
    lowered = question.lower()
    patterns: list[tuple[str, list[str]]] = []
    if any(token in lowered for token in ["초기", "initial", "ic", "조건", "시뮬레이션"]):
        patterns.append(
            (
                "initial-conditions neighborhood",
                [
                    "initial conditions",
                    "λcdm ics",
                    "the λcdm ics were generated",
                    "z = 127",
                    "zel’dovich approximation",
                    "zel'dovich approximation",
                    "implementation of cosmic string loops in the initial",
                ],
            )
        )
    return patterns


def wants_broader_paper_context(question: str) -> bool:
    lowered = question.lower()
    broader_terms = [
        "비교",
        "관련",
        "연결",
        "graph",
        "다른 논문",
        "compare",
        "related",
        "similar",
        "contrast",
        "context",
    ]
    return any(term in lowered for term in broader_terms)


def graph_neighbors_for_paper(arxiv_id: str, max_related_papers: int = 6) -> dict:
    graph_path = ROOT / "graphify-out" / "graph.json"
    start = paper_wiki_rel(arxiv_id)
    if not graph_path.exists():
        return {"connectors": [], "related_papers": []}
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    node_ids = {node.get("id") for node in graph.get("nodes", [])}
    connectors: list[str] = []
    for edge in graph.get("edges", []):
        source = edge.get("source")
        target = edge.get("target")
        if source == start and isinstance(target, str) and target in node_ids:
            connectors.append(target)
        elif target == start and isinstance(source, str) and source in node_ids:
            connectors.append(source)
    connectors = [
        path
        for path in connectors
        if path != "wiki/index.md" and not path.startswith("wiki/daily/")
    ]

    def connector_rank(path: str) -> tuple[int, str]:
        if path.startswith("wiki/topics/"):
            return (0, path)
        if path.startswith("wiki/methods/") or path.startswith("wiki/entities/"):
            return (1, path)
        return (3, path)

    connectors = sorted(dict.fromkeys(connectors), key=connector_rank)
    related: list[str] = []
    for connector in connectors:
        for edge in graph.get("edges", []):
            source = edge.get("source")
            target = edge.get("target")
            if source == connector and isinstance(target, str) and target.startswith("wiki/papers/") and target != start:
                related.append(target)
            elif target == connector and isinstance(source, str) and source.startswith("wiki/papers/") and source != start:
                related.append(source)
    related = list(dict.fromkeys(related))[:max_related_papers]
    return {"connectors": connectors[:4], "related_papers": related}


def build_paper_chat_context(question: str, arxiv_id: str) -> tuple[str, list[str]]:
    selected = paper_wiki_rel(arxiv_id)
    paper_payload = get_paper(arxiv_id)
    paper = paper_payload["paper"]
    sources: list[str] = []
    chunks: list[str] = []

    selected_excerpt = read_wiki_excerpt(selected, max_chars=7000)
    if selected_excerpt:
        sources.append(selected)
        chunks.append(f"Selected paper source: {selected}\n{selected_excerpt}")

    markdown_context = markdown_source_context(arxiv_id, question, max_chars=16000)
    markdown_rel = paper_markdown_rel(arxiv_id)
    if markdown_context:
        sources.append(markdown_rel)
        chunks.append(f"Selected paper markdown source: {markdown_rel}\n{markdown_context}")

    selected_qmd_paths = {
        selected,
        markdown_rel,
        str(summary_cache_path(arxiv_id).relative_to(ROOT)).replace("\\", "/"),
        str(deep_summary_cache_path(arxiv_id).relative_to(ROOT)).replace("\\", "/"),
        paper_deep_summary_wiki_rel(arxiv_id),
    }
    if paper.get("text_path"):
        selected_qmd_paths.add(str(paper["text_path"]))
    qmd_queries = [question]
    hint_terms = question_context_terms(question)
    if hint_terms:
        qmd_queries.append(hint_terms)
    qmd_seen: set[str] = set()
    for qmd_query in qmd_queries:
        for page in qmd_search_pages(
            qmd_query,
            max_pages=8,
            collections=collections_for("selected_paper_collections", ["astro-papers", "astro-ko-summaries", "astro-text"]),
        ):
            if page.path not in selected_qmd_paths or page.path in sources or page.path in qmd_seen:
                continue
            qmd_seen.add(page.path)
            sources.append(page.path)
            chunks.append(f"QMD-selected snippet for this paper: {page.path}\n{page.excerpt}")

    text_context = extracted_text_context(paper, question, max_chars=14000)
    if text_context:
        text_source = paper.get("text_path") or "extracted text"
        sources.append(text_source)
        chunks.append(f"Selected paper extracted text source: {text_source}\n{text_context}")

    cached_summary = summary_cache_path(arxiv_id)
    if cached_summary.exists():
        sources.append(str(cached_summary.relative_to(ROOT)).replace("\\", "/"))
        chunks.append(f"Cached Korean summary for selected paper:\n{cached_summary.read_text(encoding='utf-8')[:3600]}")

    cached_deep_summary = deep_summary_cache_path(arxiv_id)
    if cached_deep_summary.exists():
        sources.append(str(cached_deep_summary.relative_to(ROOT)).replace("\\", "/"))
        chunks.append(
            "Cached PaperForge deep summary for selected paper:\n"
            f"{cached_deep_summary.read_text(encoding='utf-8')[:5200]}"
        )

    if wants_broader_paper_context(question):
        neighbors = graph_neighbors_for_paper(arxiv_id)
        for path in neighbors["connectors"]:
            excerpt = read_wiki_excerpt(path, max_chars=1800)
            if excerpt:
                sources.append(path)
                chunks.append(f"Graph connector source: {path}\n{excerpt}")
        for path in neighbors["related_papers"]:
            excerpt = read_wiki_excerpt(path, max_chars=2200)
            if excerpt:
                sources.append(path)
                chunks.append(f"Graph-connected paper evidence source: {path}\n{excerpt}")

        for page in search_wiki(f"{arxiv_id} {question}", max_pages=5):
            if page.path in {"wiki/index.md", "wiki/log.md"} or page.path.startswith("wiki/daily/"):
                continue
            if page.path not in sources:
                sources.append(page.path)
                chunks.append(f"Question search source: {page.path}\n{page.excerpt}")
            if len([source for source in sources if source.startswith("wiki/")]) >= 7:
                break

    return "\n\n---\n\n".join(chunks), list(dict.fromkeys(sources))


def paper_search_with_terms(query: str, terms: set[str], max_pages: int = 8) -> list[RetrievedPage]:
    pages: list[RetrievedPage] = []
    paper_root = ROOT / "wiki" / "papers"
    if not paper_root.exists():
        return []
    for path in paper_root.glob("*.md"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        title_match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else path.stem
        score = score_text(query, text, title)
        if score == 0:
            continue
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        excerpt = compact_markdown_for_summary(text)
        if len(excerpt) > 2400:
            excerpt = excerpt_for(excerpt, terms, max_chars=2400)
        pages.append(RetrievedPage(path=rel, score=score, excerpt=excerpt))
    pages.sort(key=lambda item: item.score, reverse=True)
    return pages[:max_pages]


def translated_retrieval_query(question: str) -> str:
    prompt = (ROOT / "config" / "prompts" / "retrieval_query_translate.md").read_text(encoding="utf-8")
    try:
        query = chat(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": question},
            ],
            model=retrieval_model(),
            timeout=45.0,
        ).strip()
    except Exception:
        return ""
    query = re.sub(r"[\r\n]+", " ", query).strip().strip('"')
    return query[:500]


def merge_pages(primary: list[RetrievedPage], secondary: list[RetrievedPage], max_pages: int) -> list[RetrievedPage]:
    merged: dict[str, RetrievedPage] = {page.path: page for page in primary}
    for page in secondary:
        if page.path in merged:
            current = merged[page.path]
            merged[page.path] = RetrievedPage(
                path=page.path,
                score=current.score + page.score,
                excerpt=current.excerpt,
            )
        else:
            merged[page.path] = page
    return sorted(merged.values(), key=lambda item: item.score, reverse=True)[:max_pages]


def search_paper_wiki(question: str, max_pages: int = 8) -> list[RetrievedPage]:
    terms = expand_query_terms(question)
    qmd_pages = qmd_search_pages(
        question,
        max_pages=max_pages,
        collections=collections_for("paper_collections", ["astro-papers", "astro-ko-summaries", "astro-text"]),
    )
    pages = paper_search_with_terms(question, terms, max_pages=max_pages)
    pages = merge_pages(qmd_pages, pages, max_pages)
    if contains_korean(question) and len(pages) < 3:
        english_query = translated_retrieval_query(question)
        if english_query:
            translated_terms = expand_query_terms(f"{question} {english_query}")
            translated_qmd_pages = qmd_search_pages(
                english_query,
                max_pages=max_pages,
                collections=collections_for("paper_collections", ["astro-papers", "astro-ko-summaries", "astro-text"]),
            )
            translated_pages = paper_search_with_terms(english_query, translated_terms, max_pages=max_pages)
            translated_pages = merge_pages(translated_qmd_pages, translated_pages, max_pages)
            pages = merge_pages(pages, translated_pages, max_pages)
    return pages


def inline_md(text: str) -> str:
    import re

    escaped = html.escape(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: f'<a href="#" data-link="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>',
        escaped,
    )
    return escaped


def get_summary() -> dict:
    with connect() as conn:
        init_db(conn)
        counts = {
            row["status"]: row["count"]
            for row in conn.execute("SELECT status, count(*) AS count FROM papers GROUP BY status")
        }
        total = conn.execute("SELECT count(*) AS count FROM papers").fetchone()["count"]
        recent_papers = [
            row_to_dict(row)
            for row in conn.execute(
                """
                SELECT arxiv_id, title, status, announced_date, published, updated_at
                FROM papers
                WHERE status = 'graphed'
                ORDER BY COALESCE(announced_date, date(published), date(updated), date(created_at)) DESC,
                         arxiv_id DESC,
                         updated_at DESC
                LIMIT 10
                """
            )
        ]
        current_search_query = get_state(conn, "current_search_query") or ""
    topic_map = paper_topics_from_graph()
    for paper in recent_papers:
        topics = topic_map.get(safe_arxiv_filename(paper["arxiv_id"]), [])
        paper["topics"] = topics
        paper["topic"] = topics[0] if topics else ""
    semantic_pages = []
    for path in sorted((ROOT / "wiki" / "topics" / "semantic").glob("*.md")):
        title = title_for_wiki_page(path, path.stem.replace("-", " ").title())
        semantic_pages.append({"path": str(path.relative_to(ROOT)), "title": title})
    graph = {"nodes": 0, "edges": 0}
    graph_path = ROOT / "graphify-out" / "graph.json"
    if graph_path.exists():
        data = json.loads(graph_path.read_text(encoding="utf-8"))
        graph = {"nodes": len(data.get("nodes", [])), "edges": len(data.get("edges", []))}
    topics_cfg = load_yaml("config/topics.yml")
    default_topics = [
        cfg.get("label", key.replace("_", " "))
        for key, cfg in topics_cfg.get("topics", {}).items()
    ]
    default_label = " or ".join(default_topics) or "configured default topics"
    return {
        "db_path": str(db_path().relative_to(ROOT)),
        "model": chat_model(),
        "current_search_query": current_search_query,
        "current_search_label": current_search_query or f"Default topics: {default_label}",
        "total_papers": total,
        "status_counts": counts,
        "recent_papers": recent_papers,
        "digests": [],
        "newsletters": [],
        "semantic_pages": semantic_pages[:20],
        "graph": graph,
    }


def paper_topics_from_graph(limit: int = 20) -> dict[str, list[str]]:
    graph_path = ROOT / "graphify-out" / "graph.json"
    if not graph_path.exists():
        return {}
    try:
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    nodes = {node.get("id"): node for node in graph.get("nodes", []) if isinstance(node, dict) and node.get("id")}
    topic_counts: dict[str, int] = {}
    paper_edges: dict[str, set[str]] = {}
    for edge in graph.get("edges", []):
        if not isinstance(edge, dict) or edge.get("relation") != "has_topic":
            continue
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if not source.startswith("wiki/papers/") or not source.endswith(".md") or nodes.get(target, {}).get("type") != "topic":
            continue
        paper_key = Path(source).stem
        paper_edges.setdefault(paper_key, set()).add(target)
        topic_counts[target] = topic_counts.get(target, 0) + 1

    selected_topic_ids = {
        str(nodes[topic_id].get("id") or topic_id)
        for path in (ROOT / "wiki" / "topics" / "semantic").glob("*.md")
        for topic_id in [f"topic:{path.stem}"]
        if topic_id in nodes
    }
    if not selected_topic_ids:
        selected_topic_ids = {
            topic_id
            for topic_id, _count in sorted(
                topic_counts.items(),
                key=lambda item: (-item[1], str(nodes.get(item[0], {}).get("label") or item[0]).lower()),
            )[:limit]
        }

    def topic_label(topic_id: str) -> str:
        node = nodes.get(topic_id, {})
        return str(node.get("label") or topic_id.split(":", 1)[-1]).strip()

    result: dict[str, list[str]] = {}
    for paper_key, topic_ids in paper_edges.items():
        labels = [
            topic_label(topic_id)
            for topic_id in sorted(
                topic_ids & selected_topic_ids,
                key=lambda item: (topic_counts.get(item, 0), topic_label(item).lower()),
            )
        ]
        if labels:
            result[paper_key] = labels
    return result


def group_papers_by_topic(papers: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for paper in papers:
        topic = paper.get("topic") or "Unclassified"
        groups.setdefault(topic, []).append(paper)
    return [
        {"topic": topic, "count": len(items), "papers": items}
        for topic, items in sorted(groups.items(), key=lambda item: (item[0] == "Unclassified", item[0].lower()))
    ]


def clamp_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def get_papers(params: dict[str, list[str]]) -> dict:
    q = params.get("q", [""])[0].strip().lower()
    status = params.get("status", [""])[0].strip()
    where = []
    values: list[str] = []
    if status:
        where.append("p.status = ?")
        values.append(status)
    if q:
        where.append("(lower(p.title) LIKE ? OR lower(p.abstract) LIKE ? OR lower(p.arxiv_id) LIKE ?)")
        needle = f"%{q}%"
        values.extend([needle, needle, needle])
    sql = """
        SELECT p.arxiv_id, p.title,
               p.status,
               p.announced_date, p.published, p.updated,
               c.topic, c.relevance_score, c.rationale
        FROM papers p
        LEFT JOIN classifications c ON c.paper_id = p.id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += """
        ORDER BY p.title COLLATE NOCASE ASC,
                 p.arxiv_id ASC,
                 p.updated_at DESC
        LIMIT 300
    """
    with connect() as conn:
        init_db(conn)
        papers = [row_to_dict(row) for row in conn.execute(sql, values)]
        statuses = [
            row["status"]
            for row in conn.execute(
                "SELECT DISTINCT status FROM papers WHERE COALESCE(status, '') != '' ORDER BY status"
            )
        ]
    topic_map = paper_topics_from_graph()
    for paper in papers:
        graph_topics = topic_map.get(safe_arxiv_filename(paper["arxiv_id"]), [])
        db_topic = paper.get("topic") or ""
        topics = graph_topics or ([db_topic] if db_topic else [])
        paper["topics"] = topics
        paper["topic"] = topics[0] if topics else ""
        attach_paper_wiki_state(paper)
    papers.sort(key=lambda paper: ((paper.get("topic") or "Unclassified").lower(), paper["title"].lower()))
    return {"papers": papers, "paper_groups": group_papers_by_topic(papers), "statuses": statuses}


def paper_search_score(query: str, paper: dict) -> float:
    terms = [term.casefold() for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9.+_-]{2,}", query)]
    if not terms:
        return 0.0
    title = str(paper.get("title") or "").casefold()
    abstract = str(paper.get("abstract") or "").casefold()
    categories = str(paper.get("categories") or "").casefold()
    score = 0.0
    for term in terms:
        if term in title:
            score += 3.0
        if term in abstract:
            score += 1.0
        if term in categories:
            score += 0.5
    return score


def paper_search_snippet(query: str, abstract: str, max_chars: int = 380) -> str:
    text = " ".join(str(abstract or "").split())
    if len(text) <= max_chars:
        return text
    terms = [term.casefold() for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9.+_-]{2,}", query)]
    lowered = text.casefold()
    hit_positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    start = max(0, min(hit_positions) - 90) if hit_positions else 0
    end = min(len(text), start + max_chars)
    start = max(0, end - max_chars)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet += "..."
    return snippet


_KOREAN_SEARCH_EXPANSIONS = {
    "환경": ["environment", "environmental"],
    "환경적": ["environment", "environmental"],
    "영향": ["effects", "influence", "interactions"],
    "진화": ["evolution", "evolutionary"],
    "진화하는": ["evolution", "evolutionary"],
    "왜소": ["dwarf"],
    "은하": ["galaxy", "galaxies"],
    "왜소은하": ["dwarf", "galaxy", "galaxies"],
    "위성": ["satellite"],
    "조석": ["tidal"],
    "램압": ["ram", "pressure", "stripping"],
    "별형성": ["star", "formation"],
    "성단": ["cluster"],
    "군집": ["cluster", "environment"],
}

_INTERACTIVE_SEARCH_STOPWORDS = {
    "the",
    "and",
    "are",
    "is",
    "was",
    "were",
    "be",
    "been",
    "being",
    "for",
    "with",
    "from",
    "into",
    "by",
    "of",
    "on",
    "that",
    "this",
    "about",
    "various",
    "paper",
    "papers",
    "study",
    "studies",
    "research",
    "formation",  # kept for title scoring, but too broad for arXiv AND queries
    "galaxies",  # handled together with dwarf/galaxy below
}


_INTERACTIVE_SEARCH_TERM_CORRECTIONS = {
    "affacted": "affected",
    "affectd": "affected",
    "enviromental": "environmental",
    "enviornmental": "environmental",
    "galaxie": "galaxy",
    "galxies": "galaxies",
}


def correct_interactive_search_query(query: str) -> str:
    def replace_term(match: re.Match) -> str:
        raw = match.group(0)
        corrected = _INTERACTIVE_SEARCH_TERM_CORRECTIONS.get(raw.casefold())
        if not corrected:
            return raw
        return corrected.capitalize() if raw[:1].isupper() else corrected

    corrected = re.sub(r"\b[A-Za-z][A-Za-z'-]*\b", replace_term, str(query or ""))
    corrected = re.sub(
        r"\bvarious\s+environmental\s+effect\b",
        "various environmental effects",
        corrected,
        flags=re.IGNORECASE,
    )
    return " ".join(corrected.split())


def looks_like_natural_language_goal(query: str) -> bool:
    tokens = [term.casefold() for term in re.findall(r"[A-Za-z][A-Za-z'-]*", query)]
    if len(tokens) < 5:
        return False
    markers = {
        "is",
        "are",
        "was",
        "were",
        "be",
        "being",
        "been",
        "by",
        "on",
        "of",
        "how",
        "why",
        "what",
        "whether",
        "affected",
        "affect",
        "affects",
        "effect",
        "effects",
        "impact",
        "impacts",
        "influence",
        "influences",
        "various",
    }
    return bool(markers & set(tokens))


def expanded_interactive_search_terms(query: str) -> list[str]:
    terms = []
    for raw_term in re.findall(r"[A-Za-z0-9][A-Za-z0-9.+_-]{2,}", query):
        term = raw_term.casefold()
        term = _INTERACTIVE_SEARCH_TERM_CORRECTIONS.get(term, term)
        if term not in _INTERACTIVE_SEARCH_STOPWORDS:
            terms.append(term)
    lowered = query.casefold()
    for korean, expansions in _KOREAN_SEARCH_EXPANSIONS.items():
        if korean in query:
            terms.extend(expansions)
    if "dwarf" in lowered and ("galaxy" in lowered or "galaxies" in lowered):
        terms.extend(["dwarf", "galaxy", "galaxies"])
    return list(dict.fromkeys(term for term in terms if term))


def title_phrase_query(cleaned: str) -> str:
    title_terms = [
        term.casefold()
        for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9.+_-]{2,}", cleaned)
        if term.casefold() not in {"the", "and", "for", "with", "from", "into", "that", "this", "about"}
    ]
    distinctive = [
        term
        for term in title_terms
        if term
        not in {
            "galaxy",
            "galaxies",
            "dwarf",
            "dwarfs",
            "formation",
            "evolution",
            "study",
            "studies",
        }
    ]
    selected = distinctive[:4] or title_terms[:5]
    return " AND ".join(f"ti:{term}" for term in selected)


def interactive_atom_search_query(category_query: str, query: str) -> str:
    cleaned = " ".join((query or "").split())
    if not cleaned:
        return category_query
    has_arxiv_field = bool(
        re.search(r"\b(?:all|ti|abs|au|cat|id|jr|co|rn):", cleaned, flags=re.IGNORECASE)
    )
    if has_arxiv_field or " AND " in cleaned.upper() or " OR " in cleaned.upper():
        return arxiv_client.atom_search_query(category_query, cleaned)

    maybe_title = (
        len(cleaned.split()) >= 5
        and not contains_korean(cleaned)
        and not looks_like_natural_language_goal(cleaned)
    )
    if maybe_title:
        title_query = title_phrase_query(cleaned)
        if title_query:
            return f"({category_query}) AND ({title_query})"

    terms = expanded_interactive_search_terms(cleaned)
    if not terms:
        return category_query

    groups: list[str] = []
    term_set = set(terms)
    if "dwarf" in term_set and ({"galaxy", "galaxies"} & term_set):
        groups.append("(all:dwarf AND (all:galaxy OR all:galaxies))")
        term_set -= {"dwarf", "galaxy", "galaxies"}
    if {
        "environment",
        "environmental",
        "effect",
        "effects",
        "affect",
        "affected",
        "influence",
        "interactions",
        "evolution",
        "evolutionary",
    } & term_set:
        env_terms = [
            term
            for term in [
                "environment",
                "environmental",
                "effect",
                "effects",
                "affect",
                "affected",
                "influence",
                "interactions",
                "evolution",
                "evolutionary",
            ]
            if term in term_set
        ]
        groups.append("(" + " OR ".join(f"all:{term}" for term in env_terms) + ")")
        term_set -= set(env_terms)
    groups.extend(f"all:{term}" for term in list(term_set)[:5])
    return f"({category_query}) AND ({' AND '.join(groups)})"


def query_atom_for_interactive_search(
    *,
    categories: list[str],
    query: str,
    max_results: int,
    request_delay_seconds: float,
) -> list[dict]:
    category_query = " OR ".join(f"cat:{category}" for category in categories)
    search_query = interactive_atom_search_query(category_query, query)
    params = {
        "search_query": search_query,
        "start": "0",
        "max_results": str(max_results),
        "sortBy": "lastUpdatedDate",
        "sortOrder": "descending",
    }
    try:
        with httpx.Client(headers=arxiv_client.polite_headers(), timeout=60.0) as client:
            response = client.get(arxiv_client.QUERY_URL, params=params)
            response.raise_for_status()
    except httpx.TransportError as exc:
        if "CERTIFICATE_VERIFY_FAILED" not in str(exc):
            raise
        with httpx.Client(headers=arxiv_client.polite_headers(), timeout=60.0, verify=False) as client:
            response = client.get(arxiv_client.QUERY_URL, params=params)
            response.raise_for_status()
    time.sleep(request_delay_seconds)
    return arxiv_client._parse_atom_xml(response.text)


def search_local_paper_candidates(query: str, max_results: int) -> list[dict]:
    with connect() as conn:
        init_db(conn)
        rows = conn.execute(
            """
            SELECT arxiv_id, version, title, authors_json, abstract, categories,
                   primary_category, published, updated, announced_date, abs_url,
                   pdf_url, status
            FROM papers
            ORDER BY COALESCE(updated, published, announced_date, updated_at) DESC
            LIMIT 800
            """
        ).fetchall()
    candidates: list[dict] = []
    for row in rows:
        paper = row_to_dict(row)
        try:
            authors = json.loads(paper.get("authors_json") or "[]")
        except json.JSONDecodeError:
            authors = []
        paper["authors"] = authors if isinstance(authors, list) else []
        score = paper_search_score(query, paper)
        if score <= 0:
            continue
        paper["abstract_snippet"] = paper_search_snippet(query, paper.get("abstract", ""))
        paper["search_score"] = score
        paper["already_added"] = True
        paper["local_status"] = paper.get("status") or ""
        candidates.append(paper)
    candidates.sort(key=lambda item: (item["search_score"], item.get("updated") or ""), reverse=True)
    return candidates[:max_results]


def clean_arxiv_search_html(value: str) -> str:
    text = re.sub(r"<script\b.*?</script>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(html.unescape(text).split())


def arxiv_web_search_candidates(query: str, max_results: int) -> list[dict]:
    cleaned = " ".join(query.split())
    if not cleaned:
        return []
    title_terms = [
        term
        for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9.+_-]{2,}", cleaned)
        if term.casefold() not in {"the", "and", "for", "with", "from", "into", "that", "this", "about"}
    ]
    if not title_terms:
        return []
    term = " ".join(title_terms[:8])
    params = {
        "advanced": "",
        "terms-0-operator": "AND",
        "terms-0-term": term,
        "terms-0-field": "title",
        "classification-physics_archives": "astro-ph",
        "classification-include_cross_list": "include",
        "date-filter_by": "all_dates",
        "abstracts": "show",
        "size": str(min(max_results, 50)),
        "order": "-announced_date_first",
    }
    try:
        with httpx.Client(headers=arxiv_client.polite_headers(), timeout=30.0) as client:
            response = client.get("https://arxiv.org/search/advanced", params=params)
            response.raise_for_status()
    except httpx.TransportError as exc:
        if "CERTIFICATE_VERIFY_FAILED" not in str(exc):
            raise
        with httpx.Client(headers=arxiv_client.polite_headers(), timeout=30.0, verify=False) as client:
            response = client.get("https://arxiv.org/search/advanced", params=params)
            response.raise_for_status()

    papers: list[dict] = []
    blocks = re.findall(
        r'<li class="arxiv-result">(.*?)</li>\s*(?=<li class="arxiv-result">|</ol>|<nav)',
        response.text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for block in blocks:
        id_match = re.search(r"https://arxiv\.org/abs/([^\"<]+)", block)
        if not id_match:
            continue
        arxiv_id, version = arxiv_client.normalize_arxiv_id(id_match.group(1))
        title_match = re.search(
            r'<p class="title[^"]*">\s*(.*?)\s*</p>',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        authors_match = re.search(
            r'<p class="authors">\s*(.*?)\s*</p>',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        abstract_match = re.search(
            r'<span class="abstract-full[^"]*"[^>]*>\s*(.*?)\s*<a\b',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        ) or re.search(
            r'<span class="abstract-short[^"]*"[^>]*>\s*(.*?)\s*<a\b',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        title = clean_arxiv_search_html(title_match.group(1)) if title_match else arxiv_id
        authors_text = clean_arxiv_search_html(authors_match.group(1)) if authors_match else ""
        authors_text = re.sub(r"^Authors?:\s*", "", authors_text, flags=re.IGNORECASE)
        authors = [item.strip() for item in authors_text.split(",") if item.strip()]
        abstract = clean_arxiv_search_html(abstract_match.group(1)) if abstract_match else ""
        papers.append(
            {
                "arxiv_id": arxiv_id,
                "version": version,
                "title": title,
                "authors": authors[:8],
                "abstract": abstract,
                "categories": "astro-ph",
                "primary_category": "astro-ph",
                "published": None,
                "updated": None,
                "announced_date": None,
                "abs_url": f"https://arxiv.org/abs/{arxiv_id}",
                "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
            }
        )
    return papers[:max_results]


def paper_search_bool_param(params: dict[str, list[str]], name: str, default: bool = False) -> bool:
    value = str(params.get(name, ["1" if default else "0"])[0] or "").strip().casefold()
    return value in {"1", "true", "yes", "on"}


def paper_search_goal_plan(query: str, use_llm: bool) -> dict:
    cleaned = " ".join(str(query or "").split())
    if not use_llm or not cleaned:
        corrected = correct_interactive_search_query(cleaned)
        return {
            "corrected_query": corrected,
            "normalized_goal": corrected,
            "include_terms": expanded_interactive_search_terms(corrected),
            "ads_queries": [],
            "arxiv_queries": [],
        }
    prompt = (
        "You are preparing a literature-search plan for astronomy and astrophysics papers. "
        "First correct spelling and grammar in the user's natural-language query without changing its scientific intent. "
        "Then convert it into concise English search intent. "
        "Return only JSON with these keys: corrected_query, normalized_goal, include_terms, exclude_terms, ads_queries, arxiv_queries. "
        "corrected_query must be one corrected English sentence or phrase. "
        "include_terms and exclude_terms must be arrays of short English terms or phrases. "
        "ads_queries and arxiv_queries must be arrays of at most 3 query strings. "
        "Prefer astronomy terms, target objects, physical processes, methods, and survey/instrument names. "
        "Do not invent a specific paper title unless the user provided one.\n\n"
        f"User goal: {cleaned}"
    )
    try:
        response = chat(
            [{"role": "user", "content": prompt}],
            model=retrieval_model() or chat_model(),
            format_json=True,
            timeout=45,
            options={"temperature": 0, "num_predict": 900},
        )
        data = parse_llm_json(response)
    except Exception:
        data = {}
    corrected = correct_interactive_search_query(" ".join(str(data.get("corrected_query") or cleaned).split()))
    normalized = correct_interactive_search_query(" ".join(str(data.get("normalized_goal") or corrected or cleaned).split()))
    include_terms = data.get("include_terms") if isinstance(data.get("include_terms"), list) else []
    include_terms = [" ".join(str(term).split()) for term in include_terms if str(term).strip()]
    if not include_terms:
        include_terms = expanded_interactive_search_terms(normalized or corrected or cleaned)
    ads_queries = data.get("ads_queries") if isinstance(data.get("ads_queries"), list) else []
    arxiv_queries = data.get("arxiv_queries") if isinstance(data.get("arxiv_queries"), list) else []
    return {
        "corrected_query": corrected,
        "normalized_goal": normalized,
        "include_terms": include_terms[:12],
        "exclude_terms": [
            " ".join(str(term).split())
            for term in (data.get("exclude_terms") if isinstance(data.get("exclude_terms"), list) else [])
            if str(term).strip()
        ][:8],
        "ads_queries": [correct_interactive_search_query(item) for item in ads_queries if str(item).strip()][:3],
        "arxiv_queries": [correct_interactive_search_query(item) for item in arxiv_queries if str(item).strip()][:3],
    }


def paper_search_ranking_query(query: str, plan: dict) -> str:
    parts = [
        query,
        str(plan.get("corrected_query") or ""),
        str(plan.get("normalized_goal") or ""),
        " ".join(str(term) for term in plan.get("include_terms", []) if str(term).strip()),
        " ".join(str(item) for item in plan.get("ads_queries", []) if str(item).strip()),
        " ".join(str(item) for item in plan.get("arxiv_queries", []) if str(item).strip()),
    ]
    return " ".join(" ".join(part.split()) for part in parts if str(part).strip())


def ads_escape(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def ads_query_from_text(query: str, plan: dict | None = None) -> str:
    cleaned = " ".join(str(query or "").split())
    plan = plan or {}
    supplied_queries = [str(item).strip() for item in plan.get("ads_queries", []) if str(item).strip()]
    if supplied_queries:
        return "(" + ") OR (".join(supplied_queries[:3]) + ")"

    normalized = " ".join(str(plan.get("normalized_goal") or cleaned).split())
    terms = [str(term).strip() for term in plan.get("include_terms", []) if str(term).strip()]
    if not terms:
        terms = expanded_interactive_search_terms(normalized or cleaned)

    maybe_title = (
        len(cleaned.split()) >= 5
        and not contains_korean(cleaned)
        and not looks_like_natural_language_goal(cleaned)
    )
    if maybe_title:
        phrase = ads_escape(cleaned)
        return f'title:"{phrase}" OR abs:"{phrase}"'

    phrase_terms = [term for term in terms if " " in term][:6]
    single_terms = [
        term
        for term in terms
        if " " not in term and term.casefold() not in _INTERACTIVE_SEARCH_STOPWORDS
    ][:8]
    clauses: list[str] = []
    clauses.extend(f'title:"{ads_escape(term)}" OR abs:"{ads_escape(term)}"' for term in phrase_terms)
    if single_terms:
        required = " AND ".join(f'("{ads_escape(term)}")' for term in single_terms[:5])
        clauses.append(f"title:({required}) OR abs:({required})")
    if normalized and not clauses:
        phrase = ads_escape(normalized)
        clauses.append(f'title:"{phrase}" OR abs:"{phrase}"')
    return "(" + ") OR (".join(clauses[:6]) + ")" if clauses else "database:astronomy"


def ads_arxiv_id_from_identifiers(identifiers: object) -> tuple[str, int]:
    items = identifiers if isinstance(identifiers, list) else []
    for item in items:
        match = re.search(r"(?:arXiv:)?(\d{4}\.\d{4,5})(?:v(\d+))?", str(item), flags=re.IGNORECASE)
        if match:
            return match.group(1), int(match.group(2) or 1)
    return "", 1


def ads_paper_id(record: dict) -> str:
    arxiv_id, _ = ads_arxiv_id_from_identifiers(record.get("identifier"))
    if arxiv_id:
        return arxiv_id
    doi = record.get("doi")
    if isinstance(doi, list) and doi:
        return normalize_uploaded_paper_id(f"doi-{doi[0]}")
    return normalize_uploaded_paper_id(f"ads-{record.get('bibcode') or 'paper'}")


def search_ads_papers(query: str, max_results: int, plan: dict) -> list[dict]:
    api_key = nasa_ads_api_key()
    if not api_key:
        raise ValueError("NASA ADS API key is not configured")
    ads_query = ads_query_from_text(query, plan)
    params = {
        "q": f"database:astronomy AND ({ads_query})",
        "fl": "bibcode,title,abstract,author,year,bibstem,doi,identifier,citation_count,reference_count,property,arxiv_class",
        "rows": str(max_results),
        "sort": "score desc,citation_count desc,date desc",
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "Astro-Note-AI/1.0 paper discovery",
    }
    with httpx.Client(headers=headers, timeout=45.0) as client:
        response = client.get("https://api.adsabs.harvard.edu/v1/search/query", params=params)
        response.raise_for_status()
    docs = response.json().get("response", {}).get("docs", [])
    papers: list[dict] = []
    for rank, record in enumerate(docs):
        if not isinstance(record, dict):
            continue
        arxiv_id, version = ads_arxiv_id_from_identifiers(record.get("identifier"))
        paper_id = arxiv_id or ads_paper_id(record)
        title = record.get("title")
        if isinstance(title, list):
            title = title[0] if title else ""
        authors = record.get("author") if isinstance(record.get("author"), list) else []
        abstract = str(record.get("abstract") or "")
        bibcode = str(record.get("bibcode") or "")
        can_build = bool(arxiv_id)
        citation_count = int(record.get("citation_count") or 0)
        papers.append(
            {
                "arxiv_id": paper_id,
                "version": version,
                "title": str(title or paper_id).strip(),
                "authors": [str(author) for author in authors[:8]],
                "abstract": abstract,
                "categories": " ".join(str(item) for item in (record.get("arxiv_class") or []) if str(item).strip())
                if isinstance(record.get("arxiv_class"), list)
                else str(record.get("arxiv_class") or "ADS"),
                "primary_category": "ADS",
                "published": str(record.get("year") or "") or None,
                "updated": str(record.get("year") or "") or None,
                "announced_date": str(record.get("year") or "") or None,
                "abs_url": f"https://ui.adsabs.harvard.edu/abs/{bibcode}/abstract" if bibcode else "",
                "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}.pdf" if arxiv_id else "",
                "source": "ads",
                "source_id": bibcode,
                "can_build": can_build,
                "search_score": max(0.1, 30.0 - rank * 0.2 + min(citation_count, 200) / 80.0),
            }
        )
    return papers[:max_results]


def search_arxiv_papers(params: dict[str, list[str]]) -> dict:
    query = " ".join(params.get("q", [""])[0].split())
    if not query:
        raise ValueError("Search query is required")
    provider = str(params.get("provider", ["auto"])[0] or "auto").strip().casefold()
    if provider not in {"auto", "ads", "arxiv", "local"}:
        raise ValueError("Search provider must be auto, ads, arxiv, or local")
    use_llm = paper_search_bool_param(params, "use_llm", default=True)
    max_results = clamp_int(
        params.get("limit", ["50"])[0],
        default=50,
        minimum=1,
        maximum=200,
    )
    plan = paper_search_goal_plan(query, use_llm)
    ranking_query = paper_search_ranking_query(query, plan)
    agents = load_yaml("config/agents.yml").get("agents", {})
    scout_cfg = agents.get("arxiv_scout", {})
    categories = list(
        scout_cfg.get(
            "categories",
            ["astro-ph.GA", "astro-ph.CO", "astro-ph.IM", "astro-ph.HE", "astro-ph.SR", "astro-ph.EP"],
        )
    )
    request_delay = min(1.0, float(scout_cfg.get("request_delay_seconds", 0.2) or 0.2))
    warning = ""
    source = provider
    papers: list[dict] = []

    if provider in {"auto", "ads"} and nasa_ads_api_key():
        try:
            papers = search_ads_papers(query, max_results, plan)
            source = "ads"
        except httpx.HTTPStatusError as exc:
            if provider == "ads":
                raise
            warning = f"NASA ADS search failed with HTTP {exc.response.status_code}; falling back to arXiv. "
        except Exception as exc:
            if provider == "ads":
                raise
            warning = f"NASA ADS search failed ({exc}); falling back to arXiv. "
    elif provider == "ads":
        warning = "NASA ADS API key is not configured; falling back to arXiv. "

    if not papers and provider == "local":
        papers = search_local_paper_candidates(ranking_query, max_results)
        source = "local"

    if not papers and provider in {"auto", "ads", "arxiv"}:
        arxiv_query = query
        arxiv_queries = plan.get("arxiv_queries") if isinstance(plan.get("arxiv_queries"), list) else []
        if use_llm and arxiv_queries:
            arxiv_query = str(arxiv_queries[0])
        elif use_llm and plan.get("normalized_goal"):
            arxiv_query = str(plan.get("normalized_goal"))
        try:
            papers = query_atom_for_interactive_search(
                categories=categories,
                query=arxiv_query,
                max_results=max_results,
                request_delay_seconds=request_delay,
            )
            source = "arxiv"
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 429:
                raise
            papers = arxiv_web_search_candidates(arxiv_query, max_results)
            if papers:
                source = "arxiv-web"
                warning += (
                    "arXiv API temporarily rate-limited this search (HTTP 429), "
                    "so results were recovered from arXiv web search."
                )
            else:
                papers = search_local_paper_candidates(ranking_query, max_results)
                source = "local"
                warning += (
                    "arXiv temporarily rate-limited this search (HTTP 429). "
                    "Showing matching local-library candidates; wait a few minutes and search again for live arXiv results."
                )
        except httpx.TimeoutException:
            papers = search_local_paper_candidates(ranking_query, max_results)
            source = "local"
            warning += (
                "arXiv search timed out. Showing matching local-library candidates; try again for live arXiv results."
            )

    existing: dict[str, str] = {}
    with connect() as conn:
        init_db(conn)
        rows = conn.execute(
            "SELECT arxiv_id, status FROM papers WHERE arxiv_id IN (%s)"
            % ",".join("?" for _ in papers),
            [paper["arxiv_id"] for paper in papers],
        ).fetchall() if papers else []
        existing = {row["arxiv_id"]: row["status"] for row in rows}

    results = []
    for paper in papers:
        score = max(float(paper.get("search_score") or 0), paper_search_score(ranking_query, paper))
        paper_id = str(paper.get("arxiv_id") or "").strip()
        results.append(
            {
                "arxiv_id": paper_id,
                "version": paper.get("version"),
                "title": paper.get("title"),
                "authors": paper.get("authors", [])[:8],
                "abstract": paper.get("abstract", ""),
                "abstract_snippet": paper_search_snippet(ranking_query, paper.get("abstract", "")),
                "categories": paper.get("categories", ""),
                "primary_category": paper.get("primary_category", ""),
                "published": paper.get("published"),
                "updated": paper.get("updated"),
                "announced_date": paper.get("announced_date"),
                "abs_url": paper.get("abs_url"),
                "pdf_url": paper.get("pdf_url"),
                "source": paper.get("source") or source,
                "source_id": paper.get("source_id") or "",
                "can_build": paper.get("can_build", bool(paper.get("pdf_url"))),
                "search_score": score,
                "already_added": paper_id in existing,
                "local_status": existing.get(paper_id, ""),
            }
        )
    results.sort(key=lambda item: (item["search_score"], item.get("updated") or ""), reverse=True)
    return {
        "query": query,
        "categories": categories,
        "count": len(results),
        "papers": results,
        "source": source,
        "warning": warning,
        "llm_plan": plan if use_llm else {},
        "ranking_note": (
            "LLM goal terms were used for query expansion and ranking. "
            if use_llm else ""
        ) + "Scores are search/ranking aids, not calibrated relevance confidence.",
    }


def import_arxiv_search_paper(payload: dict) -> dict:
    paper = payload.get("paper")
    if not isinstance(paper, dict):
        raise ValueError("Paper payload is required")
    arxiv_id = str(paper.get("arxiv_id") or "").strip()
    if not arxiv_id:
        raise ValueError("arXiv ID is required")
    pdf_url = str(paper.get("pdf_url") or "").strip()
    if paper.get("can_build") is False or not pdf_url:
        raise ValueError("This search result does not expose an arXiv PDF, so the wiki builder cannot download it automatically.")
    progress_job_id = str(payload.get("progress_job_id") or "").strip()
    update_upload_progress(
        progress_job_id,
        status="running",
        stage="Registering paper",
        message=f"Registering {arxiv_id} from search results.",
        percent=4,
        file_index=1,
        total_files=1,
        filename=arxiv_id,
    )
    normalized = {
        "arxiv_id": arxiv_id,
        "version": int(paper.get("version") or 1),
        "title": str(paper.get("title") or arxiv_id).strip(),
        "authors": paper.get("authors") if isinstance(paper.get("authors"), list) else [],
        "abstract": str(paper.get("abstract") or "").strip(),
        "categories": str(paper.get("categories") or "").strip(),
        "primary_category": str(paper.get("primary_category") or "").strip(),
        "published": paper.get("published"),
        "updated": paper.get("updated"),
        "announced_date": paper.get("announced_date"),
        "abs_url": paper.get("abs_url") or f"https://arxiv.org/abs/{arxiv_id}",
        "pdf_url": pdf_url,
    }
    with connect() as conn:
        init_db(conn)
        paper_id = upsert_paper(conn, normalized)
    append_wiki_log(f"{utc_now()} ui_server import paper search result {arxiv_id}")
    update_upload_progress(
        progress_job_id,
        status="running",
        stage="Paper registered",
        message=f"{arxiv_id} is ready for PDF download and wiki generation.",
        percent=8,
        file_index=1,
        total_files=1,
        filename=arxiv_id,
    )
    return {"ok": True, "paper_id": paper_id, "arxiv_id": arxiv_id, "paper": normalized}


def get_paper(arxiv_id: str) -> dict:
    with connect() as conn:
        init_db(conn)
        row = conn.execute(
            """
            SELECT p.*, c.topic, c.relevance_score, c.rationale, c.keywords_json, c.model
            FROM papers p
            LEFT JOIN classifications c ON c.paper_id = p.id
            WHERE p.arxiv_id = ?
            ORDER BY p.version DESC
            LIMIT 1
            """,
            (arxiv_id,),
        ).fetchone()
    if row is None:
        raise KeyError("Paper not found")
    paper = attach_paper_wiki_state(row_to_dict(row))
    graph_topics = paper_topics_from_graph().get(safe_arxiv_filename(arxiv_id), [])
    db_topic = paper.get("topic") or ""
    paper["topics"] = graph_topics or ([db_topic] if db_topic else [])
    paper["topic"] = paper["topics"][0] if paper["topics"] else ""
    wiki_path = ROOT / "wiki" / "papers" / f"{arxiv_id.replace('/', '_')}.md"
    wiki_html = markdown_to_html(wiki_path.read_text(encoding="utf-8")) if wiki_path.exists() else ""
    return {"paper": paper, "wiki_html": wiki_html, "wiki_exists": paper["wiki_exists"], "wiki_path": paper["wiki_path"]}


def hangul_count(text: str) -> int:
    return len(re.findall(r"[\uac00-\ud7a3]", text or ""))


def looks_like_korean_summary(markdown: str) -> bool:
    if hangul_count(markdown) < 80:
        return False
    mojibake_markers = ("�", "?쒓", "?붿", "?섏", "?댁", "?덈", "?먮", "?곌")
    if any(marker in markdown for marker in mojibake_markers):
        return False
    if markdown.count("?") > max(10, len(markdown) // 80):
        return False
    return True


def korean_summary_retry_instruction() -> str:
    return (
        "\n\n출력 언어 규칙:\n"
        "- 반드시 한국어 문장으로 작성하세요.\n"
        "- 영어 문장으로 요약하지 마세요.\n"
        "- 논문 제목, 고유명사, 수식, 단위, survey 이름만 필요한 경우 영어 표기를 유지하세요.\n"
        "- 첫 줄은 반드시 '## 한글 요약'이어야 합니다.\n"
    )


def korean_summary_fallback(paper: dict, wiki_markdown: str = "") -> str:
    main_results = markdown_section(wiki_markdown, "Main Results") if wiki_markdown else ""
    source = main_results or paper.get("abstract") or "요약할 원문 정보가 충분하지 않습니다."
    source = " ".join(source.split())[:1800]
    return (
        "## 한글 요약\n\n"
        "### 한 줄 요약\n\n"
        f"`{paper['arxiv_id']}` 논문에 대한 자동 한글 요약 생성에 실패했습니다. 아래에는 원문 기반 핵심 발췌를 남깁니다.\n\n"
        "### 연구 질문\n\n"
        "원문 wiki 페이지 또는 abstract를 확인해야 합니다.\n\n"
        "### 자료와 방법\n\n"
        "자동 추출된 한국어 설명이 아직 없습니다.\n\n"
        "### 주요 결과\n\n"
        f"{source}\n\n"
        "### 주의할 점\n\n"
        "이 항목은 Ollama 요약 실패 시 생성된 대체 표시입니다.\n\n"
        "### 이 논문을 읽어야 하는 이유\n\n"
        "PDF와 wiki 페이지를 함께 확인해 연구 관련성을 판단해야 합니다.\n"
    )


def get_korean_paper_summary(arxiv_id: str, refresh: bool = False, model: str | None = None) -> dict:
    paper_payload = get_paper(arxiv_id)
    paper = paper_payload["paper"]
    cache_path = summary_cache_path(arxiv_id)
    if cache_path.exists() and not refresh:
        markdown = cache_path.read_text(encoding="utf-8")
        if looks_like_korean_summary(markdown):
            return {"markdown": markdown, "html": markdown_to_html(markdown), "cached": True}

    wiki_path = ROOT / "wiki" / "papers" / f"{arxiv_id.replace('/', '_')}.md"
    wiki_markdown = wiki_path.read_text(encoding="utf-8") if wiki_path.exists() else ""
    prompt = (ROOT / "config" / "prompts" / "korean_paper_summary.md").read_text(encoding="utf-8")
    context = compact_markdown_for_summary(wiki_markdown)
    text_context = extracted_text_context(paper, "summary results methods limitations", max_chars=18000)
    user_content = (
        f"Paper ID: {paper['arxiv_id']}\n"
        f"Title: {paper['title']}\n"
        f"Categories: {paper.get('categories') or ''}\n"
        f"Classification topic: {paper.get('topic') or 'unclassified'}\n"
        f"Classification rationale: {paper.get('rationale') or ''}\n\n"
        f"Abstract:\n{paper.get('abstract') or ''}\n\n"
        f"Wiki excerpt:\n{context[:12000]}\n\n"
        f"Extracted paper text excerpt:\n{text_context}"
    )
    try:
        markdown = chat(
            [
                {"role": "system", "content": prompt + korean_summary_retry_instruction()},
                {"role": "user", "content": user_content + korean_summary_retry_instruction()},
            ],
            model=model or chat_model(),
            timeout=180.0,
        ).strip()
        if not looks_like_korean_summary(markdown):
            markdown = chat(
                [
                    {
                        "role": "system",
                        "content": (
                            prompt
                            + korean_summary_retry_instruction()
                            + "\n이전 응답은 한국어 요약 기준을 충족하지 못했습니다. 이번 응답은 반드시 한글 중심의 한국어 Markdown이어야 합니다."
                        ),
                    },
                    {"role": "user", "content": user_content + korean_summary_retry_instruction()},
                ],
                model=model or chat_model(),
                timeout=240.0,
            ).strip()
    except Exception:
        markdown = korean_summary_fallback(paper, wiki_markdown)
    if not looks_like_korean_summary(markdown):
        markdown = korean_summary_fallback(paper, wiki_markdown)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(markdown.rstrip() + "\n", encoding="utf-8")
    return {"markdown": markdown, "html": markdown_to_html(markdown), "cached": False}


def paperforge_config() -> dict:
    agents = load_yaml("config/agents.yml").get("agents", {})
    cfg = agents.get("paperforge_deep_summary", {})
    return cfg if isinstance(cfg, dict) else {}


def paperforge_root() -> Path:
    cfg = paperforge_config()
    return Path(os.getenv("PAPERFORGE_ROOT") or cfg.get("root") or DEFAULT_PAPERFORGE_ROOT).expanduser().resolve()


def paperforge_python(root: Path) -> Path:
    configured = os.getenv("PAPERFORGE_PYTHON") or paperforge_config().get("python")
    if configured:
        return Path(configured).expanduser()
    venv_python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return venv_python if venv_python.exists() else Path(sys.executable)


def paperforge_output_dir() -> Path:
    cfg = paperforge_config()
    configured = os.getenv("PAPERFORGE_OUTPUT_DIR") or cfg.get("output_dir") or "data/paperforge"
    path = Path(str(configured)).expanduser()
    return path if path.is_absolute() else (ROOT / path).resolve()


def paperforge_timeout_seconds() -> float:
    cfg = paperforge_config()
    try:
        return float(os.getenv("PAPERFORGE_TIMEOUT_SECONDS") or cfg.get("timeout_seconds") or 3600)
    except (TypeError, ValueError):
        return 3600.0


def configure_paperforge_env(env: dict[str, str]) -> dict[str, str]:
    cfg = paperforge_config()
    protected_keys = set(env)
    env = dict(env)
    env.setdefault("PAPER_DEBATE_OUTPUT_DIR", str(paperforge_output_dir()))
    env.setdefault("PAPER_DEBATE_UNLOAD_MODELS_AFTER_RUN", "false")

    if is_api_provider():
        model = chat_model()
        api_key = openai_api_key()
        env.setdefault("PAPER_DEBATE_TEXT_BACKEND", "remote_gemma4")
        env.setdefault("PAPER_DEBATE_REMOTE_GEMMA4_URL", openai_base_url())
        if api_key:
            env.setdefault("PAPER_DEBATE_REMOTE_GEMMA4_API_KEY", api_key)
            env.setdefault("OPENAI_API_KEY", api_key)
        env.setdefault("PAPER_DEBATE_REMOTE_GEMMA4_MODEL", model)
        env.setdefault("PAPER_DEBATE_MODEL", model)
        env.setdefault("PAPER_DEBATE_SUMMARIZER_MODEL", model)
        env.setdefault("PAPER_DEBATE_VISION_MODEL", model)
        env.setdefault("PAPER_DEBATE_CRITIC_MODEL", model)
        env.setdefault("PAPER_DEBATE_DEFENDER_MODEL", model)
        env.setdefault("PAPER_DEBATE_RESEARCHER_MODEL", model)
        env.setdefault("PAPER_DEBATE_MODERATOR_MODEL", model)
        env.setdefault("PAPER_DEBATE_NUM_CTX", str(context_window(128000)))

    for cfg_key, env_key in [
        ("text_backend", "PAPER_DEBATE_TEXT_BACKEND"),
        ("model", "PAPER_DEBATE_MODEL"),
        ("summarizer_model", "PAPER_DEBATE_SUMMARIZER_MODEL"),
        ("vision_model", "PAPER_DEBATE_VISION_MODEL"),
        ("critic_model", "PAPER_DEBATE_CRITIC_MODEL"),
        ("defender_model", "PAPER_DEBATE_DEFENDER_MODEL"),
        ("researcher_model", "PAPER_DEBATE_RESEARCHER_MODEL"),
        ("moderator_model", "PAPER_DEBATE_MODERATOR_MODEL"),
        ("remote_gemma4_url", "PAPER_DEBATE_REMOTE_GEMMA4_URL"),
        ("remote_gemma4_api_key", "PAPER_DEBATE_REMOTE_GEMMA4_API_KEY"),
        ("remote_gemma4_model", "PAPER_DEBATE_REMOTE_GEMMA4_MODEL"),
        ("remote_gemma4_timeout", "PAPER_DEBATE_REMOTE_GEMMA4_TIMEOUT"),
        ("remote_gemma4_max_tokens", "PAPER_DEBATE_REMOTE_GEMMA4_MAX_TOKENS"),
        ("feedback_context_chars", "PAPER_DEBATE_FEEDBACK_CONTEXT_CHARS"),
        ("num_ctx", "PAPER_DEBATE_NUM_CTX"),
        ("num_predict", "PAPER_DEBATE_NUM_PREDICT"),
        ("temperature", "PAPER_DEBATE_TEMPERATURE"),
        ("top_p", "PAPER_DEBATE_TOP_P"),
        ("chunk_chars", "PAPER_DEBATE_CHUNK_CHARS"),
        ("reduce_chars", "PAPER_DEBATE_REDUCE_CHARS"),
        ("rounds", "PAPER_DEBATE_ROUNDS"),
        ("keep_alive", "PAPER_DEBATE_KEEP_ALIVE"),
        ("download_references", "PAPER_DEBATE_DOWNLOAD_REFERENCES"),
        ("use_reference_rag", "PAPER_DEBATE_USE_REFERENCE_RAG"),
        ("download_reference_limit", "PAPER_DEBATE_DOWNLOAD_REFERENCE_LIMIT"),
        ("reference_rag_top_k", "PAPER_DEBATE_REFERENCE_RAG_TOP_K"),
        ("reference_chunk_chars", "PAPER_DEBATE_REFERENCE_CHUNK_CHARS"),
        ("prefer_docling", "PAPER_DEBATE_PREFER_DOCLING"),
        ("max_figure_pages", "PAPER_DEBATE_MAX_FIGURE_PAGES"),
        ("max_figure_crops_per_page", "PAPER_DEBATE_MAX_FIGURE_CROPS_PER_PAGE"),
        ("unload_models_after_run", "PAPER_DEBATE_UNLOAD_MODELS_AFTER_RUN"),
    ]:
        if cfg_key not in cfg or env_key in protected_keys:
            continue
        value = cfg[cfg_key]
        if isinstance(value, bool):
            env[env_key] = "true" if value else "false"
        else:
            env[env_key] = str(value)
    return env


def run_paperforge_deep_summary(pdf_path: Path) -> dict:
    root = paperforge_root()
    if not root.exists():
        raise FileNotFoundError(f"PaperForge root not found: {root}")
    python_path = paperforge_python(root)
    if not python_path.exists():
        raise FileNotFoundError(f"PaperForge Python not found: {python_path}")

    output_dir = paperforge_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    script = r"""
import json
import sys
from pathlib import Path

from paper_debate_core import load_config, run_note_pipeline

pdf_path = Path(sys.argv[1])
output_dir = Path(sys.argv[2])
config = load_config()
config.output_dir = output_dir

markdown_note = ""
wiki_note = ""
for event in run_note_pipeline(pdf_path, config):
    if event.type == "error":
        print(json.dumps({"error": event.content}, ensure_ascii=False))
        raise SystemExit(2)
    if event.type == "note_complete":
        markdown_note = event.content
        wiki_note = (event.metadata or {}).get("wiki", "")

print(json.dumps({"markdown": markdown_note, "wiki": wiki_note}, ensure_ascii=False))
"""
    result = subprocess.run(
        [str(python_path), "-c", script, str(pdf_path), str(output_dir)],
        cwd=root,
        env=configure_paperforge_env(os.environ),
        text=True,
        capture_output=True,
        timeout=paperforge_timeout_seconds(),
    )
    stdout_lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    payload: dict | None = None
    for line in reversed(stdout_lines):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            payload = parsed
            break
    if result.returncode != 0:
        message = payload.get("error") if payload else None
        details = message or (result.stderr.strip() or result.stdout.strip() or "PaperForge failed")
        raise RuntimeError(details[:4000])
    if not payload or not str(payload.get("markdown") or "").strip():
        details = result.stderr.strip() or result.stdout.strip() or "PaperForge returned an empty deep summary"
        raise RuntimeError(details[:4000])
    return payload


def deep_summary_wiki_markdown(paper: dict, markdown_note: str, wiki_export_rel: str) -> str:
    title = paper.get("title") or paper.get("arxiv_id") or "Paper"
    arxiv_id = paper["arxiv_id"]
    source_rel = paper_wiki_rel(arxiv_id)
    return (
        "---\n"
        "page_type: paper_deep_summary\n"
        f"arxiv_id: \"{arxiv_id}\"\n"
        "generator: PaperForge\n"
        f"source_paper: \"{source_rel}\"\n"
        f"wiki_export: \"{wiki_export_rel}\"\n"
        f"updated_at: \"{utc_now()}\"\n"
        "---\n\n"
        f"# Deep Summary: {title}\n\n"
        f"- Source paper: [{arxiv_id}](./{Path(paper_wiki_rel(arxiv_id)).name})\n"
        "- Generator: PaperForge\n"
        f"- MediaWiki export: `{wiki_export_rel}`\n\n"
        f"{markdown_note.strip()}\n"
    )


def ensure_paper_deep_summary_link(arxiv_id: str, title: str) -> bool:
    paper_path = ROOT / paper_wiki_rel(arxiv_id)
    if not paper_path.exists():
        return False
    rel_name = f"{paper_safe_id(arxiv_id)}-deep-summary.md"
    line = f"- [PaperForge deep summary](./{rel_name})"
    text = paper_path.read_text(encoding="utf-8")
    if line in text:
        return False
    heading = "## Deep Summary Wiki"
    if heading in text:
        text = text.replace(f"{heading}\n", f"{heading}\n\n{line}\n", 1)
    else:
        text = text.rstrip() + f"\n\n{heading}\n\n{line}\n"
    paper_path.write_text(text.rstrip() + "\n", encoding="utf-8")
    return True


def get_paper_deep_summary(arxiv_id: str, refresh: bool = False) -> dict:
    paper_payload = get_paper(arxiv_id)
    paper = paper_payload["paper"]
    cache_path = deep_summary_cache_path(arxiv_id)
    wiki_path = ROOT / paper_deep_summary_wiki_rel(arxiv_id)
    wiki_export_path = deep_summary_wiki_export_path(arxiv_id)
    if cache_path.exists() and wiki_path.exists() and not refresh:
        markdown = cache_path.read_text(encoding="utf-8")
        return {
            "markdown": markdown,
            "html": markdown_to_html(markdown),
            "cached": True,
            "wiki_path": str(wiki_path.relative_to(ROOT)).replace("\\", "/"),
        }

    pdf_path = safe_project_file(paper.get("pdf_path"), [ROOT / "data" / "raw"], ".pdf")
    if pdf_path is None or not pdf_path.exists():
        raise FileNotFoundError("로컬 PDF가 없어 PaperForge deep summary를 생성할 수 없습니다.")

    result = run_paperforge_deep_summary(pdf_path)
    markdown = str(result.get("markdown") or "").strip()
    wiki_export = str(result.get("wiki") or "").strip()
    if not markdown:
        raise RuntimeError("PaperForge deep summary result is empty")

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(markdown.rstrip() + "\n", encoding="utf-8")
    wiki_export_path.parent.mkdir(parents=True, exist_ok=True)
    if wiki_export:
        wiki_export_path.write_text(wiki_export.rstrip() + "\n", encoding="utf-8")

    wiki_rel = paper_deep_summary_wiki_rel(arxiv_id)
    wiki_export_rel = str(wiki_export_path.relative_to(ROOT)).replace("\\", "/")
    action = "update" if wiki_path.exists() else "create"
    wiki_path.parent.mkdir(parents=True, exist_ok=True)
    wiki_path.write_text(deep_summary_wiki_markdown(paper, markdown, wiki_export_rel), encoding="utf-8")
    linked = ensure_paper_deep_summary_link(arxiv_id, paper.get("title") or arxiv_id)

    with connect() as conn:
        init_db(conn)
        register_wiki_page(conn, wiki_rel, "paper_deep_summary", f"Deep Summary: {paper.get('title') or arxiv_id}", arxiv_id)
        conn.execute(
            """
            INSERT INTO links(source_path, target_path, relation, arxiv_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_path, target_path, relation) DO NOTHING
            """,
            (paper_wiki_rel(arxiv_id), wiki_rel, "deep_summary", arxiv_id, utc_now()),
        )
        conn.commit()

    append_wiki_log(f"{utc_now()} ui_server {action} {wiki_rel} {arxiv_id} -- PaperForge deep summary")
    if linked:
        append_wiki_log(f"{utc_now()} ui_server update {paper_wiki_rel(arxiv_id)} {arxiv_id} -- link PaperForge deep summary")

    return {
        "markdown": markdown,
        "html": markdown_to_html(markdown),
        "cached": False,
        "wiki_path": wiki_rel,
        "wiki_export_path": wiki_export_rel,
    }


def wiki_list() -> dict:
    topic_map = paper_topics_from_graph()
    pages = []
    for path in sorted((ROOT / "wiki").rglob("*.md")):
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        if rel.startswith("wiki/papers/") and rel.endswith("-deep-summary.md"):
            continue
        page = {"path": rel, "title": path.stem}
        if rel.startswith("wiki/papers/") and "/" not in rel.removeprefix("wiki/papers/"):
            topics = topic_map.get(path.stem, [])
            page["topics"] = topics
            page["topic"] = topics[0] if topics else "Unclassified"
        pages.append(page)
    return {"pages": pages}


def read_wiki(path: str) -> dict:
    full_path = safe_path(path)
    if not full_path.exists() or full_path.suffix != ".md":
        raise FileNotFoundError("Wiki page not found")
    text = full_path.read_text(encoding="utf-8")
    return {"path": path, "markdown": text, "html": markdown_to_html(text)}


def obsidian_export_path(output_dir: str | None = None) -> Path:
    if output_dir and str(output_dir).strip():
        directory = Path(str(output_dir)).expanduser()
        if not directory.is_absolute():
            directory = Path.home() / directory
        return directory.resolve() / "Astro-Note-AI-Obsidian.zip"
    return ROOT / "exports" / "obsidian" / "Astro-Note-AI-Obsidian.zip"


def obsidian_vault_member(rel: str) -> str:
    return f"Astro-Note-AI-Obsidian/{rel.removeprefix('wiki/')}"


def export_obsidian_vault(output_dir: str | None = None) -> dict:
    wiki_root = ROOT / "wiki"
    if not wiki_root.exists():
        raise FileNotFoundError("Wiki directory not found")
    pages = []
    for path in sorted(wiki_root.rglob("*.md")):
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        if rel.startswith("wiki/papers/") and rel.endswith("-deep-summary.md"):
            continue
        pages.append((path, rel))

    out_path = obsidian_export_path(output_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        readme = (
            "# Astro-Note AI Obsidian Vault\n\n"
            "This vault was exported from Astro-Note AI.\n\n"
            "- Open the extracted `Astro-Note-AI-Obsidian` folder with Obsidian.\n"
            "- Markdown folder structure is preserved from the app wiki.\n"
        )
        archive.writestr("Astro-Note-AI-Obsidian/README.md", readme)
        archive.writestr("Astro-Note-AI-Obsidian/.obsidian/app.json", "{\n  \"legacyEditor\": false\n}\n")
        for path, rel in pages:
            archive.write(path, obsidian_vault_member(rel))

    try:
        display_path = str(out_path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        display_path = str(out_path)
    return {
        "ok": True,
        "count": len(pages),
        "path": display_path,
        "absolute_path": str(out_path),
        "download_url": "/download/obsidian-vault",
    }


def normalize_wiki_link_target(base_path: str, link: str) -> str:
    raw_target = str(link or "").strip().replace("\\", "/")
    if not raw_target:
        raise ValueError("Citation target is empty")
    embedded_paper = re.search(r"(?:^|/|\.{2}/)papers/([^/?#]+\.md)", raw_target)
    if embedded_paper and not embedded_paper.group(1).endswith("-deep-summary.md"):
        return f"wiki/papers/{embedded_paper.group(1)}"
    example_match = re.search(r"https?://example\.com/papers/([^/?#]+\.md)", raw_target)
    if example_match:
        return f"wiki/papers/{example_match.group(1)}"
    arxiv_match = re.search(r"https?://arxiv\.org/abs/([^/?#\s]+)", raw_target)
    if arxiv_match:
        return paper_wiki_rel(arxiv_match.group(1).removesuffix(".pdf"))
    target = raw_target.split("#", 1)[0]
    if target.startswith("wiki/"):
        return posixpath.normpath(target)
    base = str(base_path or "wiki/index.md").strip().replace("\\", "/")
    if not base.startswith("wiki/"):
        base = "wiki/index.md"
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(base), target))
    return resolved


def arxiv_id_from_paper_wiki_path(path: str) -> str | None:
    match = re.fullmatch(r"wiki/papers/([^/]+)\.md", path)
    if not match:
        return None
    stem = match.group(1)
    if stem.endswith("-deep-summary"):
        return None
    try:
        with connect() as conn:
            init_db(conn)
            row = conn.execute("SELECT arxiv_id FROM wiki_pages WHERE path = ? LIMIT 1", (path,)).fetchone()
            if row and row["arxiv_id"]:
                return str(row["arxiv_id"])
    except Exception:
        pass
    candidate = ROOT / path
    if candidate.exists():
        text = candidate.read_text(encoding="utf-8", errors="ignore")
        frontmatter_match = re.search(r'^arxiv_id:\s*["\']?([^"\'\n]+)', text, flags=re.MULTILINE)
        if frontmatter_match:
            return frontmatter_match.group(1).strip()
    return stem.replace("_", "/")


def markdown_title(markdown: str, fallback: str) -> str:
    frontmatter = proposal_frontmatter(markdown)
    if frontmatter.get("title"):
        return frontmatter["title"]
    match = re.search(r"^#\s+(.+)$", markdown, re.MULTILINE)
    return match.group(1).strip() if match else fallback


def compact_text(text: str, max_chars: int = 1200) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    return compact[:max_chars].rstrip()


def paper_metadata(arxiv_id: str) -> dict:
    try:
        with connect() as conn:
            init_db(conn)
            row = conn.execute(
                """
                SELECT p.*, c.topic, c.relevance_score
                FROM papers p
                LEFT JOIN classifications c ON c.paper_id = p.id
                WHERE p.arxiv_id = ?
                ORDER BY p.version DESC
                LIMIT 1
                """,
                (arxiv_id,),
            ).fetchone()
    except Exception:
        row = None
    paper = row_to_dict(row)
    authors = []
    try:
        authors = json.loads(paper.get("authors_json") or "[]")
    except Exception:
        authors = []
    if isinstance(authors, list) and authors:
        paper["authors_display"] = ", ".join(str(author) for author in authors[:6])
        if len(authors) > 6:
            paper["authors_display"] += " et al."
    else:
        paper["authors_display"] = ""
    return paper


def ingest_cache_dir(markdown: str) -> Path | None:
    match = re.search(r"^- Chunk evidence cache:\s*`([^`]+)`", markdown, re.MULTILINE)
    if not match:
        return None
    candidate = (ROOT / match.group(1)).resolve()
    try:
        candidate.relative_to((ROOT / "data" / "cache" / "wiki_ingest").resolve())
    except ValueError:
        return None
    return candidate if candidate.exists() and candidate.is_dir() else None


def load_ingest_cache(markdown: str) -> list[dict]:
    cache_dir = ingest_cache_dir(markdown)
    if cache_dir is None:
        return []
    extracts: list[dict] = []
    for path in sorted(cache_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            payload["_cache_file"] = str(path.relative_to(ROOT)).replace("\\", "/")
            extracts.append(payload)
    return extracts


def flatten_ingest_extract(extract: dict) -> str:
    parts = [str(extract.get("source") or ""), str(extract.get("chunk_label") or ""), str(extract.get("evidence_excerpt") or "")]
    for key in [
        "scientific_question",
        "data",
        "method",
        "main_results",
        "limitations",
        "figures_tables",
        "follow_up_questions",
    ]:
        value = extract.get(key)
        if isinstance(value, list):
            parts.extend(str(item) for item in value)
        elif value:
            parts.append(str(value))
    return "\n".join(part for part in parts if part)


def citation_source_labels(context: str) -> set[str]:
    lowered = str(context or "").lower()
    labels: set[str] = set()
    for label in ["abstract", "paper opening", "paper title"]:
        if label in lowered:
            labels.add(label)
    for match in re.finditer(r"\bparagraph\s+\d+\b", lowered):
        labels.add(re.sub(r"\s+", " ", match.group(0)).strip())
    for match in re.finditer(r"\b(?:section|appendix)\s+[a-z]?(?:\d+(?:\.\d+)*|[ivxlcdm]+)", lowered):
        labels.add(re.sub(r"\s+", " ", match.group(0)).strip())
    return labels


def evidence_items_from_extract(extract: dict, limit: int = 8) -> list[dict]:
    labels = {
        "scientific_question": "Scientific Question",
        "data": "Data",
        "method": "Method",
        "main_results": "Main Results",
        "limitations": "Limitations",
        "figures_tables": "Figures / Tables",
        "follow_up_questions": "Follow-up Questions",
    }
    items: list[dict] = []
    for key, label in labels.items():
        value = extract.get(key)
        values = value if isinstance(value, list) else [value] if value else []
        for item in values:
            text = compact_text(str(item), 420)
            if text:
                items.append({"label": label, "text": text})
            if len(items) >= limit:
                return items
    return items


def citation_display_label(source: str) -> str:
    label = compact_text(source, 180)
    if not label:
        return ""
    lowered = label.lower()
    if lowered in {"markdown preamble", "paper opening", "paper title"}:
        return "paragraph 1"
    passage = re.fullmatch(r"source passages?\s+(\d+)", lowered)
    if passage:
        return f"paragraph {passage.group(1)}"
    label = re.sub(r"^markdown h[1-6]:\s*", "", label, flags=re.IGNORECASE).strip()
    label = re.sub(r"\s+part\s+\d+$", "", label, flags=re.IGNORECASE).strip()
    match = re.match(r"(?P<section>\d+(?:\.\d+)*)(?:\.)?\s*(?P<title>.*)$", label)
    if match:
        title = re.sub(r"[_*`]+", "", match.group("title")).strip(" .:-")
        if title.isupper():
            title = title.title()
        return f"section {match.group('section')}" + (f": {title}" if title else "")
    return label


def best_cached_trace(markdown: str, context: str) -> dict | None:
    extracts = load_ingest_cache(markdown)
    if not extracts:
        return None
    labels = citation_source_labels(context)
    best: tuple[int, dict] | None = None
    for extract in extracts:
        source = compact_text(extract.get("source") or extract.get("chunk_label") or "", 160)
        flat = flatten_ingest_extract(extract)
        score = score_text(context or source, flat, source)
        source_lower = source.lower()
        for label in labels:
            if label in source_lower:
                score += 30
            elif label in flat.lower():
                score += 10
        if best is None or score > best[0]:
            best = (score, extract)
    if best is None:
        return None
    score, extract = best
    if score <= 0 and citation_source_labels(context):
        return None
    label = citation_display_label(extract.get("source") or extract.get("chunk_label") or f"chunk {extract.get('chunk_index')}")
    excerpt = compact_text(extract.get("evidence_excerpt") or "", 1200)
    evidence_items = evidence_items_from_extract(extract)
    if not excerpt and evidence_items:
        excerpt = evidence_items[0]["text"]
    return {
        "section_label": label or "cached extract",
        "section_source": "ingest_cache",
        "excerpt": excerpt,
        "evidence_items": evidence_items,
        "cache_file": extract.get("_cache_file", ""),
    }


def best_wiki_section_trace(markdown: str, context: str) -> dict:
    section_names = [
        "Source Abstract",
        "Scientific Question",
        "Data",
        "Method",
        "Main Results",
        "Limitations",
        "Follow-up Questions",
    ]
    candidates: list[tuple[int, str, str]] = []
    for name in section_names:
        body = markdown_section(markdown, name)
        if body:
            candidates.append((score_text(context or name, body, name), name, body))
    if not candidates:
        return {
            "section_label": "paper wiki",
            "section_source": "paper_wiki",
            "excerpt": compact_text(markdown, 1200),
            "evidence_items": [],
        }
    candidates.sort(key=lambda item: item[0], reverse=True)
    _, name, body = candidates[0]
    terms = expand_query_terms(context)
    excerpt = excerpt_for(body, terms, max_chars=1200) if terms else body[:1200]
    return {
        "section_label": name,
        "section_source": "paper_wiki",
        "excerpt": compact_text(excerpt, 1200),
        "evidence_items": [{"label": name, "text": compact_text(excerpt, 520)}],
    }


def citation_trace_payload(payload: dict) -> dict:
    base_path = str(payload.get("base_path") or "wiki/index.md")
    target_path = normalize_wiki_link_target(base_path, str(payload.get("target") or ""))
    arxiv_id = arxiv_id_from_paper_wiki_path(target_path)
    if not arxiv_id:
        raise ValueError("Citation target is not a paper wiki page")
    full_path = safe_path(target_path)
    if not full_path.exists() or full_path.suffix != ".md":
        raise FileNotFoundError("Paper wiki page not found")
    markdown = full_path.read_text(encoding="utf-8")
    paper = paper_metadata(arxiv_id)
    context = compact_text(payload.get("context") or "", 1600)
    trace = best_cached_trace(markdown, context) or best_wiki_section_trace(markdown, context)
    title = paper.get("title") or markdown_title(markdown, arxiv_id)
    return {
        "ok": True,
        "source_path": base_path,
        "target_path": target_path,
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": paper.get("authors_display", ""),
        "published": paper.get("published") or "",
        "announced_date": paper.get("announced_date") or "",
        "topic": paper.get("topic") or "",
        "abs_url": paper.get("abs_url") or f"https://arxiv.org/abs/{arxiv_id}",
        "pdf_path": paper.get("pdf_path") or "",
        "source_context": context,
        **trace,
    }


def latest_lint_report() -> dict:
    reports = sorted((ROOT / "reports").glob("wiki-lint-*.md"), reverse=True)
    if not reports:
        return {"path": "", "html": "<p>No lint report has been generated.</p>", "markdown": ""}
    report = reports[0]
    text = report.read_text(encoding="utf-8")
    return {"path": str(report.relative_to(ROOT)).replace("\\", "/"), "markdown": text, "html": markdown_to_html(text)}


def proposal_frontmatter(markdown: str) -> dict[str, str]:
    if not markdown.startswith("---\n"):
        return {}
    try:
        frontmatter = markdown.split("---\n", 2)[1]
    except IndexError:
        return {}
    data: dict[str, str] = {}
    for line in frontmatter.splitlines():
        key, _, value = line.partition(":")
        if key.strip():
            data[key.strip()] = value.strip().strip("'\"")
    return data


def review_queue() -> dict:
    proposals = []
    for path in sorted((ROOT / "wiki" / "proposals").glob("*.md"), reverse=True):
        text = path.read_text(encoding="utf-8", errors="ignore")
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        meta = proposal_frontmatter(text)
        try:
            target = extract_proposal_target(text)
        except Exception:
            target = ""
        title_match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        proposals.append(
            {
                "path": rel,
                "title": title_match.group(1).strip() if title_match else path.stem,
                "status": meta.get("status", "unknown"),
                "created_at": meta.get("created_at", ""),
                "applied_at": meta.get("applied_at", ""),
                "target_path": meta.get("applied_to", target),
            }
        )
    return {"proposals": proposals, "lint": latest_lint_report()}


def get_app_settings() -> dict:
    settings = load_local_settings()
    provider = llm_provider()
    return {
        "provider": provider,
        "provider_label": api_provider_label(provider) if is_api_provider(provider) else "Ollama",
        "providers": llm_provider_options(),
        "openai_base_url": openai_base_url(),
        "openai_api_key": openai_api_key(),
        "nasa_ads_api_key": nasa_ads_api_key(),
        "ollama_base_url": ollama_base_url(),
        "chat_model": chat_model(),
        "retrieval_model": retrieval_model(),
        "context_window": context_window(),
        "settings_path": str(local_settings_path()),
        "saved": bool(settings),
    }


def save_app_settings_payload(payload: dict) -> dict:
    provider = str(payload.get("provider") or llm_provider()).strip()
    valid_providers = {"ollama", *api_provider_names()}
    if provider not in valid_providers:
        raise ValueError(f"Provider must be one of: {', '.join(sorted(valid_providers))}")
    requested_chat_model = str(payload.get("chat_model") or "").strip()
    requested_retrieval_model = str(payload.get("retrieval_model") or "").strip()
    if provider == "ollama":
        requested_chat_model = requested_chat_model or chat_model()
        requested_retrieval_model = requested_retrieval_model or requested_chat_model or retrieval_model()
        base_url = str(payload.get("openai_base_url") or openai_base_url()).strip()
    else:
        requested_chat_model = requested_chat_model or api_provider_default_chat_model(provider)
        requested_retrieval_model = (
            requested_retrieval_model
            or requested_chat_model
            or api_provider_default_retrieval_model(provider)
        )
        base_url = str(payload.get("openai_base_url") or api_provider_default_base_url(provider)).strip()
    settings = load_local_settings()
    settings.update({
        "provider": provider,
        "openai_base_url": base_url,
        "openai_api_key": str(payload.get("openai_api_key") or ""),
        "nasa_ads_api_key": str(payload.get("nasa_ads_api_key") or ""),
        "ollama_base_url": str(payload.get("ollama_base_url") or ollama_base_url()).strip(),
        "chat_model": requested_chat_model,
        "retrieval_model": requested_retrieval_model,
        "context_window": int(payload.get("context_window") or context_window()),
    })
    test_result = validate_llm_settings(settings)
    save_local_settings(settings)
    apply_local_settings_to_env(settings)
    append_wiki_log(
        f"{utc_now()} UI settings updated: provider=`{provider}`, model=`{settings['chat_model']}`, "
        f"connection_status={test_result.get('status', 'ok')}, "
        f"connection_test_ms={test_result.get('latency_ms', '')}"
    )
    return {"ok": True, "settings": get_app_settings(), "connection": test_result}


def default_upload_work_prompt() -> str:
    return (ROOT / "config" / "prompts" / UPLOAD_WORK_PROMPT_FILE).read_text(encoding="utf-8")


def upload_work_prompt_payload() -> dict:
    default_prompt = default_upload_work_prompt()
    settings = load_local_settings()
    custom_prompt = str(settings.get(UPLOAD_WORK_PROMPT_SETTING) or "").strip()
    return {
        "ok": True,
        "prompt_name": UPLOAD_WORK_PROMPT_FILE,
        "default_prompt": default_prompt,
        "current_prompt": custom_prompt or default_prompt,
        "saved": bool(custom_prompt),
        "settings_path": str(local_settings_path()),
    }


def save_upload_work_prompt_payload(payload: dict) -> dict:
    settings = load_local_settings()
    if truthy(payload.get("reset")):
        settings.pop(UPLOAD_WORK_PROMPT_SETTING, None)
        save_local_settings(settings)
        append_wiki_log(f"{utc_now()} Upload work prompt reset to default")
        return upload_work_prompt_payload()
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("Prompt cannot be empty")
    settings[UPLOAD_WORK_PROMPT_SETTING] = prompt
    save_local_settings(settings)
    append_wiki_log(f"{utc_now()} Upload work prompt saved: {len(prompt)} chars")
    return upload_work_prompt_payload()


def redact_llm_error(value: object, *, limit: int = 900) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(r"([?&]key=)[^&\s]+", r"\1<redacted>", text, flags=re.IGNORECASE)
    text = re.sub(r"(api[_-]?key['\"]?\s*[:=]\s*['\"]?)[^'\"\s,]+", r"\1<redacted>", text, flags=re.IGNORECASE)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def is_llm_quota_or_rate_limit(value: object) -> bool:
    lowered = str(value or "").lower()
    return any(
        marker in lowered
        for marker in (
            "429",
            "quota",
            "rate limit",
            "resource_exhausted",
            "usage limit",
            "too many requests",
        )
    )


def validate_llm_settings(settings: dict) -> dict:
    model = str(settings.get("chat_model") or "").strip()
    if settings.get("provider") == "ollama" and not model:
        raise ValueError("Chat model is required")
    if settings.get("provider") != "ollama" and settings.get("provider") != "openai_compatible" and not model:
        raise ValueError("Chat model is required for this API provider")

    previous_env = {env_key: os.environ.get(env_key) for env_key in LOCAL_SETTINGS_ENV_KEYS.values()}
    apply_local_settings_to_env(settings)
    started = time.perf_counter()
    try:
        response = chat(
            [{"role": "user", "content": "Reply with exactly this word: OK"}],
            model=model or None,
            timeout=25,
            options={"temperature": 0, "num_predict": 8},
            think=False,
        )
    except Exception as exc:
        for env_key, value in previous_env.items():
            if value is None:
                os.environ.pop(env_key, None)
            else:
                os.environ[env_key] = value
        provider = settings.get("provider") or "LLM"
        if is_llm_quota_or_rate_limit(exc):
            return {
                "ok": False,
                "status": "limited",
                "provider": provider,
                "model": model or "server default",
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "response_excerpt": "",
                "message": (
                    "Settings were saved, but the connection test hit an API quota or rate limit. "
                    "Wiki generation will work after quota recovers or after switching to a model/API key with available quota."
                ),
                "error_excerpt": redact_llm_error(exc),
            }
        raise RuntimeError(f"LLM connection test failed for {provider} / {model}: {exc}") from exc

    latency_ms = int((time.perf_counter() - started) * 1000)
    response_text = " ".join(str(response or "").split())
    if not response_text:
        for env_key, value in previous_env.items():
            if value is None:
                os.environ.pop(env_key, None)
            else:
                os.environ[env_key] = value
        raise RuntimeError(f"LLM connection test returned an empty response for {settings.get('provider')} / {model}")
    return {
        "ok": True,
        "provider": settings.get("provider"),
        "model": model or "server default",
        "status": "ok",
        "latency_ms": latency_ms,
        "response_excerpt": response_text[:120],
        "message": "LLM connection test passed",
    }


MultipartField = dict[str, bytes | str | None]
MARKDOWN_UPLOAD_SUFFIXES = {".md", ".markdown"}


def parse_multipart_form(handler: BaseHTTPRequestHandler) -> dict[str, list[MultipartField]]:
    content_type = handler.headers.get("Content-Type", "")
    if not content_type.startswith("multipart/form-data"):
        raise ValueError("Expected multipart/form-data")
    length = int(handler.headers.get("Content-Length", "0"))
    body = handler.rfile.read(length)
    message = BytesParser(policy=email_policy).parsebytes(
        b"Content-Type: " + content_type.encode("utf-8") + b"\r\n"
        b"MIME-Version: 1.0\r\n\r\n"
        + body
    )
    fields: dict[str, list[MultipartField]] = {}
    for part in message.iter_parts():
        disposition = part.get("Content-Disposition", "")
        if not disposition:
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        fields.setdefault(str(name), []).append({
            "filename": part.get_filename(),
            "content_type": part.get_content_type(),
            "data": part.get_payload(decode=True) or b"",
        })
    return fields


def field_value(form: dict[str, list[MultipartField]], name: str, default: str = "") -> str:
    fields = form.get(name)
    if not fields:
        return default
    field = fields[0]
    data = field.get("data")
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="ignore").strip()
    return str(data or default).strip()


def cleanup_upload_progress() -> None:
    cutoff = time.time() - UPLOAD_PROGRESS_TTL_SECONDS
    with UPLOAD_PROGRESS_LOCK:
        stale_ids = [
            job_id
            for job_id, progress in UPLOAD_PROGRESS.items()
            if float(progress.get("updated_at") or 0) < cutoff
        ]
        for job_id in stale_ids:
            UPLOAD_PROGRESS.pop(job_id, None)


def update_upload_progress(
    job_id: str,
    *,
    status: str = "running",
    stage: str = "",
    message: str = "",
    percent: float | int | None = None,
    file_index: int | None = None,
    total_files: int | None = None,
    filename: str = "",
) -> dict:
    if not job_id:
        return {}
    now = time.time()
    percent_value = 0 if percent is None else max(0, min(100, int(round(float(percent)))))
    event = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "stage": stage,
        "message": message,
        "percent": percent_value,
        "filename": filename,
    }
    with UPLOAD_PROGRESS_LOCK:
        current = UPLOAD_PROGRESS.get(job_id, {})
        log = list(current.get("log") or [])
        if message and (not log or log[-1].get("message") != message or log[-1].get("stage") != stage):
            log.append(event)
        UPLOAD_PROGRESS[job_id] = {
            "ok": True,
            "job_id": job_id,
            "status": status,
            "stage": stage or current.get("stage", ""),
            "message": message or current.get("message", ""),
            "percent": percent_value,
            "file_index": file_index if file_index is not None else current.get("file_index"),
            "total_files": total_files if total_files is not None else current.get("total_files"),
            "filename": filename or current.get("filename", ""),
            "updated_at": now,
            "log": log[-80:],
        }
        return dict(UPLOAD_PROGRESS[job_id])


def upload_progress_for(job_id: str) -> dict:
    cleanup_upload_progress()
    if not job_id:
        return {"ok": False, "error": "Missing upload job id"}
    with UPLOAD_PROGRESS_LOCK:
        progress = UPLOAD_PROGRESS.get(job_id)
        if not progress:
            return {"ok": False, "job_id": job_id, "status": "unknown", "message": "No progress is available for this upload."}
        return dict(progress)


def truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def upload_file_kind(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        if not data.startswith(b"%PDF"):
            raise ValueError("Uploaded file does not look like a PDF")
        return "pdf"
    if suffix in MARKDOWN_UPLOAD_SUFFIXES:
        return "markdown"
    raise ValueError("Only PDF and Markdown uploads are supported")


def decode_uploaded_markdown(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp949", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("utf-8", errors="ignore")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) < 20:
        raise RuntimeError(f"Markdown source too short: {len(text)} chars")
    return text


def categories_for_uploaded_file(form: dict[str, list[MultipartField]], file_kind: str) -> str:
    raw = field_value(form, "categories", "")
    if file_kind == "markdown" and raw.lower() in {"", "paper"}:
        return "document"
    return raw or "paper"


def safe_document_stem(filename: str) -> str:
    stem = Path(filename).stem.strip() or "document"
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", stem).strip("-_.") or "document"


def unique_wiki_document_path(filename: str) -> Path:
    directory = ROOT / "wiki" / "document"
    base = safe_document_stem(filename)
    candidate = directory / f"{base}.md"
    index = 2
    while candidate.exists():
        candidate = directory / f"{base}-{index}.md"
        index += 1
    return candidate


def parse_llm_json(text: str) -> dict:
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("LLM metadata response was not an object")
    return data


def normalize_uploaded_paper_id(value: object) -> str:
    paper_id = " ".join(str(value or "").split()).strip()
    paper_id = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", paper_id, flags=re.IGNORECASE)
    paper_id = re.sub(r"^(?:dx\.)?doi\.org/", "", paper_id, flags=re.IGNORECASE)
    paper_id = re.sub(r"^doi:\s*", "", paper_id, flags=re.IGNORECASE)
    paper_id = re.sub(r"[^A-Za-z0-9._/-]+", "-", paper_id).strip("-/")
    paper_id = re.sub(r"^https?-//(?:dx\.)?doi\.org/", "", paper_id, flags=re.IGNORECASE)
    paper_id = re.sub(r"^(?:dx\.)?doi\.org/", "", paper_id, flags=re.IGNORECASE)
    paper_id = re.sub(r"^doi[-:/]+", "", paper_id, flags=re.IGNORECASE)
    return paper_id.strip("-/")


def clean_extracted_line(line: str) -> str:
    return " ".join(line.replace("\uf0a0", " ").split())


def likely_author_line(line: str) -> bool:
    if not line or line.lower() in {"abstract", "introduction"}:
        return False
    if re.match(r"^\d+\s", line) or "@" in line:
        return False
    if line.lower().startswith(("received ", "revised ", "accepted ", "published ")):
        return False
    return bool(re.search(r"\b[A-Z][A-Za-z.-]+(?:\s+[A-Z][A-Za-z.-]+)+\d", line))


def split_author_names(author_block: str) -> list[str]:
    author_block = re.sub(r"\d+(?:,\d+)*", "", author_block)
    author_block = re.sub(r"\s+and\s+", ", ", author_block)
    author_block = re.sub(r"\s+", " ", author_block)
    authors = []
    for part in author_block.split(","):
        name = re.sub(r"[^A-Za-z .'-]+", " ", part).strip(" .,-")
        name = " ".join(name.split())
        if len(name.split()) >= 2 and name.lower() not in {"and"}:
            authors.append(name)
    return list(dict.fromkeys(authors))


def fallback_uploaded_paper_metadata(text: str, filename: str) -> dict:
    raw_lines = [clean_extracted_line(line) for line in text.splitlines()]
    lines = [line for line in raw_lines if line and not line.startswith("--- Page ")]
    if not lines:
        return {"paper_id": "", "title": Path(filename).stem, "authors": [], "abstract": ""}

    title_lines: list[str] = []
    author_start = 0
    for idx, line in enumerate(lines[:40]):
        if title_lines and likely_author_line(line):
            author_start = idx
            break
        if line.lower() == "abstract":
            author_start = idx
            break
        title_lines.append(line)
        author_start = idx + 1
        if len(title_lines) >= 4:
            break
    title = " ".join(title_lines).strip() or Path(filename).stem

    abstract_index = next((idx for idx, line in enumerate(lines) if line.lower() == "abstract"), -1)
    author_lines: list[str] = []
    if author_start and abstract_index > author_start:
        for line in lines[author_start:abstract_index]:
            if re.match(r"^\d+\s", line) or line.lower().startswith(("received ", "revised ", "accepted ", "published ")):
                break
            author_lines.append(line)
    authors = split_author_names(" ".join(author_lines))

    abstract = ""
    if abstract_index >= 0:
        body_lines: list[str] = []
        for line in lines[abstract_index + 1 :]:
            lowered = line.lower()
            if "astronomy thesaurus" in lowered or lowered.startswith(("keywords:", "1. introduction", "1 introduction")):
                break
            body_lines.append(line)
        abstract = " ".join(body_lines).strip()

    paper_id_match = re.search(r"\b(?:arXiv:)?(\d{4}\.\d{4,5}(?:v\d+)?)\b", text, flags=re.IGNORECASE)
    doi_match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b", text)
    paper_id = paper_id_match.group(1) if paper_id_match else (doi_match.group(0) if doi_match else "")
    return {"paper_id": normalize_uploaded_paper_id(paper_id), "title": title, "authors": authors, "abstract": abstract}


def infer_uploaded_paper_metadata(text: str, filename: str) -> dict:
    prompt = (
        "Extract bibliographic metadata from the uploaded research paper text. "
        "Return only a JSON object with these keys: paper_id, title, authors, abstract. "
        "paper_id should be a stable identifier when visible, preferring arXiv ID, DOI, or another explicit manuscript/report identifier. "
        "If no stable identifier is visible, use an empty string. "
        "authors must be an array of author names. "
        "Use the paper's abstract when present. If the document has no explicit abstract, write a concise source-grounded summary of the introduction/objective instead. "
        "If a value is not present, use an empty string or an empty array.\n\n"
        f"Filename: {filename}\n\n"
        f"Paper text excerpt:\n{text[:18000]}"
    )
    data: dict = {}
    try:
        response = chat(
            [{"role": "user", "content": prompt}],
            model=chat_model(),
            format_json=True,
            timeout=90,
            options={"temperature": 0, "num_predict": 1600},
        )
        data = parse_llm_json(response)
    except Exception:
        data = {}
    fallback = fallback_uploaded_paper_metadata(text, filename)
    title = " ".join(str(data.get("title") or fallback.get("title") or "").split())
    abstract = " ".join(str(data.get("abstract") or fallback.get("abstract") or "").split())
    authors_raw = data.get("authors") or []
    if isinstance(authors_raw, str):
        authors = [part.strip() for part in re.split(r";|,", authors_raw) if part.strip()]
    elif isinstance(authors_raw, list):
        authors = [" ".join(str(part).split()) for part in authors_raw if str(part).strip()]
    else:
        authors = []
    if not authors:
        authors = list(fallback.get("authors") or [])
    paper_id = normalize_uploaded_paper_id(data.get("paper_id") or fallback.get("paper_id"))
    if not title or not authors or not abstract:
        raise RuntimeError("LLM metadata extraction did not return title, authors, and abstract or summary. Upload was not saved.")
    return {"paper_id": paper_id, "title": title, "authors": authors, "abstract": abstract}


def upload_record_date() -> str:
    return date.today().isoformat()


WIKI_FALLBACK_MARKERS = (
    "Generation Error",
    "Heuristic fallback extract",
    "Requires LLM reduce or human review.",
    "Not extracted by the fallback summarizer.",
    "To be reviewed from the source text.",
    "fallback summarizer",
    'ingest_method: "map_llm_reduce_fallback"',
    'ingest_method: "deterministic_no_llm"',
    'ingest_method: "legacy_single_call"',
)


def comparable_wiki_text(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]+", " ", value.lower()).strip()


def compact_upload_output(value: str, *, limit: int = 2400) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[-limit:].lstrip()


def is_gemini_limit_output(value: str) -> bool:
    lowered = str(value or "").lower()
    return "gemini" in lowered and any(
        marker in lowered
        for marker in (
            "429",
            "quota",
            "rate limit",
            "resource_exhausted",
            "usage limit",
            "token",
            "tokens",
            "토큰",
            "할당량",
        )
    )


def upload_llm_failure_message(ingest_output: str) -> str:
    output = compact_upload_output(ingest_output)
    if is_gemini_limit_output(output):
        return (
            "Gemini API 토큰 제한으로 wiki 생성이 불가능합니다.\n\n"
            "더 낮은 모델로 시도해 보세요. LLM Settings에서 Gemini 모델을 "
            "gemini-2.5-flash-lite, gemini-2.0-flash-lite 또는 gemini-1.5-flash로 낮춘 뒤 다시 업로드하세요.\n\n"
            "품질이 낮은 fallback wiki는 생성하지 않았습니다.\n\n"
            f"Ingest output:\n{output or '(empty)'}"
        )
    return (
        "Upload failed: LLM wiki generation did not complete. "
        "Check the LLM setting and retry the upload.\n\n"
        f"Ingest output:\n{output or '(empty)'}"
    )


def validate_uploaded_paper_wiki_content(markdown: str, abstract: str) -> list[str]:
    warnings: list[str] = []
    if 'ingest_method: "llm_map_reduce"' not in markdown:
        raise RuntimeError(
            "Upload failed: LLM wiki generation did not complete. "
            "품질이 낮은 fallback wiki는 생성하지 않았습니다. "
            "LLM Settings에서 더 낮은 모델로 변경한 뒤 다시 업로드하세요."
        )
    for marker in WIKI_FALLBACK_MARKERS:
        if marker in markdown:
            raise RuntimeError(
                "Upload failed: LLM wiki generation fell back to low-quality output. "
                "품질이 낮은 fallback wiki는 생성하지 않았습니다. "
                "LLM Settings에서 더 낮은 모델로 변경한 뒤 다시 업로드하세요."
            )
    if "- Source type: `markdown`" not in markdown:
        raise RuntimeError(
            "Upload failed: wiki page was not generated from the full PDF Markdown source. "
            "Check PDF-to-Markdown conversion and retry."
        )
    main_results = markdown_section(markdown, "Main Results")
    if not main_results.strip():
        raise RuntimeError("Upload failed: generated wiki is missing the Main Results section.")
    main_norm = comparable_wiki_text(main_results)
    abstract_norm = comparable_wiki_text(abstract or markdown_section(markdown, "Source Abstract"))
    if len(main_norm) >= 80 and len(abstract_norm) >= 80:
        similarity = SequenceMatcher(None, main_norm, abstract_norm).ratio()
        if similarity >= 0.88 or main_norm in abstract_norm or abstract_norm in main_norm:
            raise RuntimeError(
                "Upload failed: generated wiki Main Results is too similar to the abstract. "
                "This indicates that the LLM wiki-writing step did not complete."
            )
    return warnings


def verify_uploaded_paper_wiki(arxiv_id: str, wiki_path: Path, ingest_output: str, abstract: str) -> list[str]:
    failed_match = re.search(r"\bfailed=(\d+)\b", ingest_output)
    ingested_match = re.search(r"\bingested=(\d+)\b", ingest_output)
    if failed_match and int(failed_match.group(1)) > 0:
        raise RuntimeError(upload_llm_failure_message(ingest_output))
    if ingested_match and int(ingested_match.group(1)) < 1:
        raise RuntimeError(
            "Upload failed: wiki ingest did not create a paper wiki page. "
            "Check the LLM setting and retry the upload.\n\n"
            f"Ingest output:\n{ingest_output.strip() or '(empty)'}"
        )
    if not wiki_path.exists() or wiki_path.stat().st_size == 0:
        raise RuntimeError(
            "Upload failed: wiki page was not generated. "
            "Check the LLM setting and retry the upload.\n\n"
            f"Expected wiki: {wiki_path.relative_to(ROOT)}\n\n"
            f"Ingest output:\n{ingest_output.strip() or '(empty)'}"
        )
    markdown = wiki_path.read_text(encoding="utf-8", errors="ignore")
    warnings = validate_uploaded_paper_wiki_content(markdown, abstract)
    markdown_source_path = ROOT / "data" / "markdown" / f"{safe_arxiv_filename(arxiv_id)}.md"
    if not markdown_source_path.exists() or markdown_source_path.stat().st_size == 0:
        raise RuntimeError(
            "Upload failed: full-paper Markdown source was not saved. "
            "Check the uploaded Markdown or PDF-to-Markdown conversion and retry."
        )
    rel = str(wiki_path.relative_to(ROOT)).replace("\\", "/")
    with connect() as conn:
        init_db(conn)
        row = conn.execute(
            "SELECT 1 FROM wiki_pages WHERE path = ? AND page_type = 'paper' LIMIT 1",
            (rel,),
        ).fetchone()
    if row is None:
        raise RuntimeError(
            "Upload failed: wiki page file exists but was not registered in the local wiki database. "
            "Retry the upload or check the wiki ingest log.\n\n"
            f"Wiki path: {rel}\n\n"
            f"Ingest output:\n{ingest_output.strip() or '(empty)'}"
        )
    return warnings


def cleanup_failed_upload(arxiv_id: str, reason: str) -> None:
    try:
        delete_paper(arxiv_id)
    except Exception as cleanup_exc:
        append_wiki_log(
            f"{utc_now()} Upload cleanup failed for `{arxiv_id}` after {reason}: `{cleanup_exc}`"
        )


def process_uploaded_paper_file(
    form: dict[str, list[MultipartField]],
    file_item: MultipartField,
    *,
    sequence: int,
    total: int,
    progress=None,
    cleanup_existing: bool = False,
) -> dict:
    def tick(percent: int, stage: str, message: str) -> None:
        if progress:
            progress(percent, stage, message, filename)

    if file_item is None or not file_item.get("filename"):
        raise ValueError("PDF or Markdown file is required")
    filename = Path(str(file_item.get("filename"))).name
    raw_data = file_item.get("data")
    file_bytes = raw_data if isinstance(raw_data, bytes) else b""
    tick(2, "Reading upload", f"Reading uploaded file: {filename}")
    file_kind = upload_file_kind(filename, file_bytes)
    if file_kind == "markdown":
        return process_uploaded_markdown_wiki_file(form, filename, file_bytes, progress=progress)
    pdf_bytes = b""
    markdown_text = ""
    pdf_bytes = file_bytes
    temp_pdf_path: Path | None = None
    try:
        tick(8, "Extracting text", "Extracting text from the uploaded PDF.")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp_pdf:
            temp_pdf.write(pdf_bytes)
            temp_pdf_path = Path(temp_pdf.name)
        text = extract_pdf_text(temp_pdf_path)
    finally:
        if temp_pdf_path is not None:
            temp_pdf_path.unlink(missing_ok=True)
    if len(text.strip()) < 200:
        raise RuntimeError(f"Extracted text too short: {len(text.strip())} chars")

    tick(20, "Reading metadata", "Asking the LLM to identify title, authors, abstract, and paper id.")
    metadata = infer_uploaded_paper_metadata(text, filename)
    now = datetime.now().replace(microsecond=0)
    title = metadata["title"]
    authors = metadata["authors"]
    abstract = metadata["abstract"]
    arxiv_id = metadata["paper_id"]
    if not arxiv_id:
        title_slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")[:48] or Path(filename).stem[:48]
        suffix = f"-{sequence}" if total > 1 else ""
        arxiv_id = f"local-{now.strftime('%Y%m%d-%H%M%S')}{suffix}-{title_slug}"
    categories = categories_for_uploaded_file(form, file_kind)
    announced_date = field_value(form, "announced_date", upload_record_date())
    if cleanup_existing:
        tick(26, "Clearing existing wiki", f"Removing existing wiki artifacts for {arxiv_id}.")
        try:
            delete_paper(arxiv_id)
        except KeyError:
            pass
    safe_id = safe_arxiv_filename(arxiv_id)
    pdf_path = ROOT / "data" / "raw" / "papers" / f"{safe_id}.pdf" if file_kind == "pdf" else None
    text_path = ROOT / "data" / "text" / f"{safe_id}.txt"
    markdown_path = ROOT / "data" / "markdown" / f"{safe_id}.md"
    if pdf_path is not None:
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    if pdf_path is not None:
        tick(32, "Saving source files", "Saving PDF, extracted text, and Markdown source files.")
        pdf_path.write_bytes(pdf_bytes)
    else:
        markdown_path.write_text(markdown_text.rstrip() + "\n", encoding="utf-8")

    tick(38, "Updating database", "Registering the uploaded paper in the local database.")
    with connect() as conn:
        init_db(conn)
        paper_id = upsert_paper(
            conn,
            {
                "arxiv_id": arxiv_id,
                "version": 1,
                "title": title,
                "authors": authors,
                "abstract": abstract,
                "categories": categories,
                "primary_category": categories.split()[0] if categories else "paper",
                "published": announced_date,
                "updated": announced_date,
                "announced_date": announced_date,
                "abs_url": f"local-upload:{arxiv_id}",
                "pdf_url": "",
            },
        )
        if pdf_path is not None:
            update_paper_status(conn, paper_id, "downloaded", pdf_path=str(pdf_path.relative_to(ROOT)))
        else:
            update_paper_status(conn, paper_id, "downloaded", pdf_path=None)

    text_path.write_text(text, encoding="utf-8")
    with connect() as conn:
        init_db(conn)
        row = conn.execute("SELECT id FROM papers WHERE arxiv_id = ? ORDER BY version DESC LIMIT 1", (arxiv_id,)).fetchone()
        update_paper_status(conn, row["id"], "text_extracted", text_path=str(text_path.relative_to(ROOT)))

    try:
        tick(48, "Generating wiki", "Generating the wiki page with the LLM. This is usually the slowest step.")
        args = script_command(
            "scripts/ingest_paper.py",
            "--arxiv-id",
            arxiv_id,
            "--model",
            chat_model(),
            "--source",
            "markdown",
            "--require-llm",
        )
        result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, timeout=3600, env=os.environ.copy())
        ingest_output = result.stdout + result.stderr
        if result.returncode != 0:
            raise RuntimeError(ingest_output or "Paper ingest failed")

        tick(72, "Verifying wiki", "Verifying that the generated wiki page was written correctly.")
        wiki_path = ROOT / paper_wiki_rel(arxiv_id)
        upload_warnings = verify_uploaded_paper_wiki(arxiv_id, wiki_path, ingest_output, abstract)
        tick(80, "Generating Korean summary", "Generating or refreshing the Korean summary.")
        summary = get_korean_paper_summary(arxiv_id, refresh=True)
        korean_summary_path = summary_cache_path(arxiv_id)
        tick(90, "Refreshing graph", "Refreshing wiki graph data.")
        graph_output = rebuild_graph_output()
        with connect() as conn:
            init_db(conn)
            row = conn.execute("SELECT id FROM papers WHERE arxiv_id = ? ORDER BY version DESC LIMIT 1", (arxiv_id,)).fetchone()
            if row is not None:
                update_paper_status(conn, row["id"], "graphed")
    except Exception:
        cleanup_failed_upload(arxiv_id, "wiki generation failure")
        raise

    source_path = pdf_path or markdown_path
    append_wiki_log(f"{utc_now()} Uploaded paper: `{arxiv_id}` -> `{source_path.relative_to(ROOT)}`")
    tick(98, "Finishing", "Upload processing is complete.")
    return {
        "ok": True,
        "filename": filename,
        "source_type": file_kind,
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "categories": categories,
        "pdf_path": str(pdf_path.relative_to(ROOT)) if pdf_path is not None else "",
        "markdown_path": str(markdown_path.relative_to(ROOT)),
        "text_path": str(text_path.relative_to(ROOT)),
        "wiki_path": str(wiki_path.relative_to(ROOT)),
        "korean_summary_path": str(korean_summary_path.relative_to(ROOT)),
        "korean_summary_cached": summary.get("cached", False),
        "status": "graphed",
        "graph_output": graph_output,
        "ingest_output": ingest_output,
        "warnings": upload_warnings,
    }


def process_uploaded_markdown_wiki_file(
    form: dict[str, list[MultipartField]],
    filename: str,
    file_bytes: bytes,
    progress=None,
) -> dict:
    def tick(percent: int, stage: str, message: str) -> None:
        if progress:
            progress(percent, stage, message, filename)

    tick(12, "Reading Markdown", f"Reading Markdown document: {filename}")
    markdown_text = decode_uploaded_markdown(file_bytes)
    categories = categories_for_uploaded_file(form, "markdown")
    wiki_path = unique_wiki_document_path(filename)
    tick(45, "Saving wiki document", "Saving uploaded Markdown as a wiki document.")
    wiki_path.parent.mkdir(parents=True, exist_ok=True)
    wiki_path.write_text(markdown_text.rstrip() + "\n", encoding="utf-8")
    rel = str(wiki_path.relative_to(ROOT)).replace("\\", "/")
    title = markdown_title(markdown_text, Path(filename).stem or wiki_path.stem)
    with connect() as conn:
        init_db(conn)
        register_wiki_page(conn, rel, "document", title)
    tick(75, "Refreshing graph", "Refreshing wiki graph data.")
    graph_output = rebuild_graph_output()
    append_wiki_log(f"{utc_now()} Uploaded document wiki: `{rel}`")
    tick(96, "Finishing", "Markdown document upload is complete.")
    return {
        "ok": True,
        "filename": filename,
        "source_type": "document",
        "title": title,
        "categories": categories,
        "wiki_path": rel,
        "status": "uploaded",
        "graph_output": graph_output,
    }


def upload_paper_from_form(handler: BaseHTTPRequestHandler) -> dict:
    cleanup_upload_progress()
    form = parse_multipart_form(handler)
    job_id = field_value(form, "upload_job_id", "")
    files = [field for field in form.get("paper", []) if field.get("filename")]
    if not files:
        raise ValueError("PDF or Markdown file is required")
    total_files = len(files)
    if job_id:
        update_upload_progress(
            job_id,
            stage="Starting upload",
            message=f"Starting upload for {total_files} file{'s' if total_files != 1 else ''}.",
            percent=1,
            file_index=0,
            total_files=total_files,
        )

    def file_progress(index: int, total: int):
        def update(within_percent: int, stage: str, message: str, filename: str = "") -> None:
            span = 98 / max(1, total)
            overall = 1 + (index - 1) * span + span * (max(0, min(100, within_percent)) / 100)
            update_upload_progress(
                job_id,
                stage=stage,
                message=message,
                percent=min(99, overall),
                file_index=index,
                total_files=total,
                filename=filename,
            )

        return update

    results = []
    errors = []
    for index, file_item in enumerate(files, start=1):
        try:
            update_upload_progress(
                job_id,
                stage="Processing file",
                message=f"Processing file {index} of {total_files}: {file_item.get('filename') or f'file-{index}'}",
                percent=1 + (index - 1) * (98 / max(1, total_files)),
                file_index=index,
                total_files=total_files,
                filename=str(file_item.get("filename") or f"file-{index}"),
            )
            results.append(process_uploaded_paper_file(form, file_item, sequence=index, total=total_files, progress=file_progress(index, total_files)))
        except Exception as exc:
            filename = str(file_item.get("filename") or f"file-{index}")
            errors.append({"filename": filename, "error": str(exc)})
            update_upload_progress(
                job_id,
                status="failed" if not results else "running",
                stage="Upload failed",
                message=f"{filename} failed: {exc}",
                percent=1 + index * (98 / max(1, total_files)),
                file_index=index,
                total_files=total_files,
                filename=filename,
            )
    if not results:
        message = errors[0]["error"] if errors else "No papers were uploaded"
        update_upload_progress(
            job_id,
            status="failed",
            stage="Upload failed",
            message=message,
            percent=100,
            total_files=total_files,
        )
        raise RuntimeError(message)
    paper_results = [item for item in results if item.get("source_type") != "document"]
    document_results = [item for item in results if item.get("source_type") == "document"]
    final_status = "completed_with_errors" if errors else "completed"
    final_message = (
        f"Uploaded {len(results)} file{'s' if len(results) != 1 else ''}; {len(errors)} failed."
        if errors
        else f"Uploaded {len(results)} file{'s' if len(results) != 1 else ''} successfully."
    )
    final_progress = update_upload_progress(
        job_id,
        status=final_status,
        stage="Complete",
        message=final_message,
        percent=100,
        file_index=total_files,
        total_files=total_files,
    )
    return {
        "ok": True,
        "count": len(results),
        "failed_count": len(errors),
        "items": results,
        "documents": document_results,
        "papers": paper_results,
        "errors": errors,
        "upload_progress": final_progress,
        **results[0],
    }


def load_batch_upload_manifest() -> dict:
    if not BATCH_UPLOAD_MANIFEST_PATH.exists():
        return {"version": 1, "files": {}}
    try:
        data = json.loads(BATCH_UPLOAD_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "files": {}}
    if not isinstance(data, dict):
        return {"version": 1, "files": {}}
    files = data.get("files")
    if not isinstance(files, dict):
        data["files"] = {}
    data.setdefault("version", 1)
    return data


def save_batch_upload_manifest(manifest: dict) -> None:
    BATCH_UPLOAD_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    BATCH_UPLOAD_MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def batch_file_signature(path: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = path.stat()
    return {
        "sha256": digest.hexdigest(),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def batch_manifest_entry_is_current(entry: dict | None, signature: dict) -> bool:
    if not isinstance(entry, dict) or entry.get("signature") != signature:
        return False
    wiki_path = str(entry.get("wiki_path") or "").strip()
    if wiki_path and not (ROOT / wiki_path).exists():
        return False
    return True


def batch_pdf_files(folder: Path) -> list[Path]:
    return sorted(path for path in folder.iterdir() if path.is_file() and path.suffix.lower() == ".pdf")


def process_batch_upload(payload: dict) -> dict:
    cleanup_upload_progress()
    job_id = str(payload.get("upload_job_id") or "").strip()
    folder_text = str(payload.get("folder_path") or "").strip()
    mode = str(payload.get("mode") or "new").strip()
    categories = str(payload.get("categories") or "paper").strip() or "paper"
    if mode not in {"new", "reprocess"}:
        raise ValueError("Batch mode must be 'new' or 'reprocess'")
    if not folder_text:
        raise ValueError("Batch folder is required")
    folder = Path(folder_text).expanduser().resolve()
    if not folder.exists() or not folder.is_dir():
        update_upload_progress(job_id, status="failed", stage="Batch failed", message="Batch folder was not found.", percent=100)
        raise ValueError("Batch folder was not found")
    files = batch_pdf_files(folder)
    if not files:
        update_upload_progress(job_id, status="failed", stage="Batch failed", message="No PDF files found in the selected folder.", percent=100)
        raise ValueError("No PDF files found in the selected folder")

    total_files = len(files)
    update_upload_progress(
        job_id,
        stage="Starting batch",
        message=f"Starting batch for {total_files} PDF file{'s' if total_files != 1 else ''}.",
        percent=1,
        file_index=0,
        total_files=total_files,
    )

    form: dict[str, list[MultipartField]] = {
        "categories": [{"filename": None, "content_type": "text/plain", "data": categories.encode("utf-8")}],
    }
    manifest = load_batch_upload_manifest()
    manifest_files = manifest.setdefault("files", {})
    results: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []

    def file_progress(index: int, total: int):
        def update(within_percent: int, stage: str, message: str, filename: str = "") -> None:
            span = 98 / max(1, total)
            overall = 1 + (index - 1) * span + span * (max(0, min(100, within_percent)) / 100)
            update_upload_progress(
                job_id,
                stage=stage,
                message=message,
                percent=min(99, overall),
                file_index=index,
                total_files=total,
                filename=filename,
            )

        return update

    for index, path in enumerate(files, start=1):
        key = str(path)
        filename = path.name
        start_percent = 1 + (index - 1) * (98 / max(1, total_files))
        try:
            update_upload_progress(
                job_id,
                stage="Checking PDF",
                message=f"Checking {filename} ({index}/{total_files}).",
                percent=start_percent,
                file_index=index,
                total_files=total_files,
                filename=filename,
            )
            signature = batch_file_signature(path)
            entry = manifest_files.get(key)
            if mode == "new" and batch_manifest_entry_is_current(entry, signature):
                skipped_item = {
                    "filename": filename,
                    "path": key,
                    "reason": "unchanged",
                    "arxiv_id": entry.get("arxiv_id", "") if isinstance(entry, dict) else "",
                    "wiki_path": entry.get("wiki_path", "") if isinstance(entry, dict) else "",
                }
                skipped.append(skipped_item)
                update_upload_progress(
                    job_id,
                    stage="Skipped",
                    message=f"Skipped unchanged file: {filename}",
                    percent=1 + index * (98 / max(1, total_files)),
                    file_index=index,
                    total_files=total_files,
                    filename=filename,
                )
                continue

            file_item: MultipartField = {
                "filename": filename,
                "content_type": "application/pdf",
                "data": path.read_bytes(),
            }
            result = process_uploaded_paper_file(
                form,
                file_item,
                sequence=index,
                total=total_files,
                progress=file_progress(index, total_files),
                cleanup_existing=mode == "reprocess",
            )
            results.append(result)
            manifest_files[key] = {
                "signature": signature,
                "folder": str(folder),
                "filename": filename,
                "processed_at": utc_now(),
                "arxiv_id": result.get("arxiv_id", ""),
                "title": result.get("title", ""),
                "wiki_path": result.get("wiki_path", ""),
                "status": result.get("status", ""),
            }
        except Exception as exc:
            errors.append({"filename": filename, "path": key, "error": str(exc)})
            update_upload_progress(
                job_id,
                status="running",
                stage="Batch file failed",
                message=f"{filename} failed: {exc}",
                percent=1 + index * (98 / max(1, total_files)),
                file_index=index,
                total_files=total_files,
                filename=filename,
            )

    save_batch_upload_manifest(manifest)
    final_status = "completed_with_errors" if errors else "completed"
    final_message = f"Processed {len(results)}, skipped {len(skipped)}, failed {len(errors)}."
    final_progress = update_upload_progress(
        job_id,
        status=final_status,
        stage="Batch complete",
        message=final_message,
        percent=100,
        file_index=total_files,
        total_files=total_files,
    )
    append_wiki_log(
        f"{utc_now()} Batch upload folder `{folder}` mode={mode} processed={len(results)} skipped={len(skipped)} failed={len(errors)}"
    )
    return {
        "ok": not errors,
        "folder_path": str(folder),
        "batch_mode": mode,
        "count": len(results),
        "skipped_count": len(skipped),
        "failed_count": len(errors),
        "items": results,
        "papers": [item for item in results if item.get("source_type") != "document"],
        "skipped": skipped,
        "errors": errors,
        "upload_progress": final_progress,
    }


def list_ollama_models() -> dict:
    fallback = chat_model()
    detected = detect_ollama_models(ollama_base_url(), fallback=fallback)
    if detected["models"]:
        return {"models": detected["models"], "default": detected["default"], "base_url": detected["base_url"], "message": detected["message"]}
    try:
        result = subprocess.run(
            ["ollama", "list"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
    except Exception:
        return {"models": [fallback], "default": fallback}
    models: list[str] = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if parts:
            models.append(parts[0])
    if fallback not in models:
        models.insert(0, fallback)
    return {"models": models, "default": fallback}


def ollama_models_from_base_url(base_url: str, timeout: float = 3.0) -> list[str]:
    if not base_url:
        return []
    with httpx.Client(timeout=timeout) as client:
        response = client.get(f"{base_url.rstrip('/')}/api/tags")
        response.raise_for_status()
    data = response.json()
    models = [
        str(item.get("name") or item.get("model") or "").strip()
        for item in data.get("models", [])
        if item.get("name") or item.get("model")
    ]
    return list(dict.fromkeys(model for model in models if model))


def detect_ollama_models(base_url: str | None = None, fallback: str | None = None) -> dict:
    fallback = fallback or chat_model()
    candidates = [
        str(base_url or "").strip(),
        str(ollama_base_url() or "").strip(),
        "http://localhost:11434",
        "http://127.0.0.1:11434",
    ]
    seen: set[str] = set()
    errors: list[str] = []
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            models = ollama_models_from_base_url(candidate)
        except Exception as exc:
            errors.append(f"{candidate}: {exc}")
            continue
        return {
            "provider": "ollama",
            "base_url": candidate,
            "models": models,
            "default": fallback if fallback in models else (models[0] if models else fallback),
            "available": True,
            "message": f"Ollama found at {candidate}",
        }
    return {
        "provider": "ollama",
        "base_url": base_url or ollama_base_url(),
        "models": [fallback] if fallback else [],
        "default": fallback,
        "available": False,
        "message": "Ollama was not found. " + ("; ".join(errors[:2]) if errors else "Check that Ollama is running."),
    }


def llm_provider_options() -> list[dict[str, str]]:
    options = [{"value": "ollama", "label": "Ollama"}]
    preferred_order = ["openai", "gemini", "anthropic", "openrouter", "groq", "deepseek", "xai", "mistral", "openai_compatible"]
    names = api_provider_names()
    for provider in [*preferred_order, *sorted(names - set(preferred_order))]:
        if provider not in names:
            continue
        options.append({"value": provider, "label": api_provider_label(provider)})
    return options


def api_models_from_base_url(base_url: str, api_key: str, timeout: float = 10.0) -> list[str]:
    if not base_url:
        return []
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    with httpx.Client(timeout=timeout) as client:
        response = client.get(f"{base_url.rstrip('/')}/models", headers=headers)
        response.raise_for_status()
    data = response.json()
    return [str(item.get("id")).strip() for item in data.get("data", []) if str(item.get("id") or "").strip()]


def normalise_api_model_id(provider: str, model: str) -> str:
    if provider == "gemini" and model.startswith("models/"):
        return model.split("/", 1)[1]
    return model


def is_gemini_chat_model(model: str) -> bool:
    name = model.removeprefix("models/").lower()
    if not name.startswith("gemini-"):
        return False
    excluded = (
        "embedding",
        "image",
        "audio",
        "tts",
        "veo",
        "imagen",
        "lyria",
        "robotics",
        "computer-use",
        "deep-research",
        "antigravity",
    )
    return not any(part in name for part in excluded)


def gemini_models_from_base_url(base_url: str, api_key: str, timeout: float = 10.0) -> list[str]:
    native_base_url = base_url.rstrip("/")
    if native_base_url.endswith("/openai"):
        native_base_url = native_base_url[: -len("/openai")]
    headers = {"x-goog-api-key": api_key} if api_key else {}
    with httpx.Client(timeout=timeout) as client:
        response = client.get(f"{native_base_url}/models", headers=headers)
        response.raise_for_status()
    data = response.json()
    models: list[str] = []
    for item in data.get("models", []):
        name = str(item.get("name") or "").strip()
        methods = item.get("supportedGenerationMethods") or []
        if "generateContent" in methods and is_gemini_chat_model(name):
            models.append(normalise_api_model_id("gemini", name))
    return models


def list_api_provider_models(
    provider: str | None = None,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    fallback: str | None = None,
) -> dict:
    provider = str(provider or llm_provider()).strip()
    fallback = fallback if fallback is not None else chat_model()
    base_url = str(base_url or openai_base_url()).strip()
    api_key = openai_api_key() if api_key is None else api_key
    catalog = api_provider_model_catalog(provider)
    remote_models: list[str] = []
    message = "Using built-in model catalog."
    available = True
    if api_key and base_url:
        try:
            remote_models = gemini_models_from_base_url(base_url, api_key) if provider == "gemini" else api_models_from_base_url(base_url, api_key)
            remote_models = [normalise_api_model_id(provider, model) for model in remote_models]
            message = f"Loaded built-in catalog and {len(remote_models)} live model(s) from {api_provider_label(provider)}."
        except Exception as exc:
            available = False
            message = f"Live model listing failed: {exc}. Using built-in catalog."
    elif provider != "openai_compatible":
        message = "Using built-in model catalog. Add an API key to merge the provider live /models list."
    models = list(dict.fromkeys(item for item in [fallback, *catalog, *remote_models] if item))
    return {
        "provider": provider,
        "provider_label": api_provider_label(provider),
        "base_url": base_url,
        "models": models,
        "default": fallback if fallback in models else (models[0] if models else fallback),
        "available": available,
        "message": message,
    }


def settings_model_options(params: dict[str, list[str]]) -> dict:
    provider = params.get("provider", [llm_provider()])[0]
    requested_chat = params.get("chat_model", [""])[0]
    requested_retrieval = params.get("retrieval_model", [""])[0]
    if provider == "ollama":
        requested_chat = requested_chat or chat_model()
        requested_retrieval = requested_retrieval or retrieval_model()
        detected = detect_ollama_models(params.get("ollama_base_url", [ollama_base_url()])[0], fallback=requested_chat)
        models = detected["models"]
        detected["models"] = models
        detected["chat_default"] = requested_chat if requested_chat in models else detected["default"]
        detected["retrieval_default"] = requested_retrieval if requested_retrieval in models else detected["chat_default"]
        if detected["available"] and (requested_chat and requested_chat not in models):
            detected["message"] += f"; configured chat model `{requested_chat}` is not installed"
        if detected["available"] and (requested_retrieval and requested_retrieval not in models):
            detected["message"] += f"; configured retrieval model `{requested_retrieval}` is not installed"
        return detected
    requested_chat = requested_chat or api_provider_default_chat_model(provider)
    requested_retrieval = requested_retrieval or requested_chat or api_provider_default_retrieval_model(provider)
    base_url = params.get("openai_base_url", [api_provider_default_base_url(provider)])[0] or api_provider_default_base_url(provider)
    api_key = params.get("openai_api_key", [""])[0] or openai_api_key()
    detected = list_api_provider_models(provider, base_url=base_url, api_key=api_key, fallback=requested_chat)
    models = detected["models"]
    detected["chat_default"] = requested_chat if requested_chat in models else detected["default"]
    detected["retrieval_default"] = requested_retrieval if requested_retrieval in models else detected["chat_default"]
    if provider == "openai_compatible" and not models:
        detected["message"] = "Custom API-compatible mode: enter a model name, or leave blank only if the server has a default model."
    return detected


def codex_chat_model() -> str:
    return load_yaml("config/models.yml").get("codex", {}).get("chat_model", "gpt-5.5")


def list_chat_models() -> dict:
    provider = llm_provider()
    if is_api_provider(provider):
        llm = list_api_provider_models(provider)
        options = [
            {
                "value": model,
                "label": f"{llm.get('provider_label', api_provider_label(provider))} - {model}",
                "provider": provider,
                "available": True,
                "message": llm.get("message", ""),
            }
            for model in llm["models"]
        ]
        default = llm["default"]
    else:
        ollama = list_ollama_models()
        options = [
            {
                "value": model,
                "label": f"Ollama - {model}",
                "provider": "ollama",
                "available": True,
                "message": "",
            }
            for model in ollama["models"]
        ]
        default = ollama["default"]
    return {
        "models": [option["value"] for option in options if option["available"]],
        "default": default,
        "options": options,
    }


def selected_chat_backend(payload: dict) -> dict:
    requested = str(payload.get("model") or "").strip()
    if requested.startswith(CODEX_MODEL_PREFIX):
        status = codex_status()
        if not status["available"]:
            raise ValueError(f"Codex CLI unavailable: {status['message']}")
        return {"provider": "codex", "model": requested.removeprefix(CODEX_MODEL_PREFIX)}
    provider = llm_provider()
    if is_api_provider(provider):
        return {"provider": provider, "model": requested or chat_model()}
    available = list_ollama_models()["models"]
    if requested and requested in available:
        return {"provider": "ollama", "model": requested}
    if requested:
        raise ValueError(f"Model is not available in ollama list: {requested}")
    return {"provider": "ollama", "model": chat_model()}


def run_agent(payload: dict) -> dict:
    command = payload.get("command", "")
    date = payload.get("date") or ""
    limit = str(payload.get("limit") or "").strip()
    search_query = " ".join(str(payload.get("search_query") or "").split())
    no_llm = bool(payload.get("no_llm", False))
    commands: dict[str, list[str]] = {
        "daily": ["scripts/run_daily_pipeline.py"],
        "fetch": ["scripts/fetch_arxiv.py"],
        "classify": ["scripts/classify_papers.py"],
        "download": ["scripts/download_papers.py"],
        "extract": ["scripts/extract_text.py"],
        "ingest": ["scripts/ingest_paper.py"],
        "digest": ["scripts/daily_digest.py"],
        "newsletter": ["scripts/daily_newsletter.py"],
        "curate": ["scripts/curate_wiki.py"],
        "graph": ["scripts/build_graph.py"],
        "semantic": ["scripts/semantic_wiki_agent.py"],
        "qmd": ["scripts/update_qmd_index.py"],
        "summaries": ["scripts/precompute_korean_summaries.py"],
        "lint": ["scripts/lint_wiki.py"],
    }
    if command not in commands:
        raise ValueError("Unsupported command")
    args = script_command(commands[command][0])
    if date and command not in {"graph", "lint"}:
        args.extend(["--date", date])
        if command == "fetch":
            args.extend(["--from-date", date, "--until-date", date])
    if limit:
        args.extend(["--max-results" if command == "fetch" else "--limit", limit])
    if search_query and command in {"daily", "fetch"}:
        args.extend(["--search-query", search_query])
    if no_llm and command in {"daily", "classify", "ingest", "newsletter"}:
        args.append("--no-llm")
    if command in {"daily", "fetch"}:
        with connect() as conn:
            init_db(conn)
            set_state(conn, "current_search_query", search_query)
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, timeout=1800)
    output = result.stdout + result.stderr
    if result.returncode != 0:
        return {"ok": False, "output": output, "returncode": result.returncode}
    return {"ok": True, "output": output, "returncode": result.returncode}


def set_paper_selected(arxiv_id: str) -> dict:
    if not arxiv_id:
        raise ValueError("Paper ID is required")
    with connect() as conn:
        init_db(conn)
        row = conn.execute(
            "SELECT id, status FROM papers WHERE arxiv_id = ? ORDER BY version DESC LIMIT 1",
            (arxiv_id,),
        ).fetchone()
        if row is None:
            raise KeyError("Paper not found")
        conn.execute("UPDATE papers SET status = ?, updated_at = ? WHERE id = ?", ("selected", utc_now(), row["id"]))
        conn.commit()
    return {"ok": True, "arxiv_id": arxiv_id, "status": "selected"}


def rebuild_graph_output() -> str:
    result = subprocess.run(
        script_command("scripts/build_graph.py"),
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=1800,
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(output or "Graph rebuild failed")
    return output


def split_keywords(value: object) -> list[str]:
    if isinstance(value, list):
        raw_items = [str(item) for item in value]
    else:
        raw_items = re.split(r"[\n,]+", str(value or ""))
    return [item.strip() for item in raw_items if item.strip()]


def read_graph_facets_config() -> dict:
    if not GRAPH_FACETS_PATH.exists():
        return {"facets": {"topic": {}, "method": {}, "observation": {}}}
    data = yaml.safe_load(GRAPH_FACETS_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        data = {}
    facets = data.setdefault("facets", {})
    if not isinstance(facets, dict):
        data["facets"] = {}
        facets = data["facets"]
    for facet_type in ["topic", "method", "observation"]:
        if not isinstance(facets.get(facet_type), dict):
            facets[facet_type] = {}
    return data


def count_graph_node_edges(node_id: str) -> int:
    graph_path = ROOT / "graphify-out" / "graph.json"
    if not graph_path.exists():
        return 0
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    return sum(1 for edge in graph.get("edges", []) if edge.get("source") == node_id or edge.get("target") == node_id)


def add_graph_facet(payload: dict) -> dict:
    facet_type = str(payload.get("facet_type") or "").strip().lower()
    if facet_type == "data":
        facet_type = "observation"
    if facet_type not in {"topic", "method", "observation"}:
        raise ValueError("Facet type must be topic, method, or observation")
    label = " ".join(str(payload.get("label") or "").split())
    if not label:
        raise ValueError("Facet label is required")
    if len(label) > 80:
        raise ValueError("Facet label is too long")
    keywords = split_keywords(payload.get("keywords"))
    if label not in keywords:
        keywords.insert(0, label)
    keywords = list(dict.fromkeys(keywords))[:12]

    data = read_graph_facets_config()
    facets = data["facets"]
    existing = facets[facet_type].get(label, {})
    existing_keywords: list[str] = []
    if isinstance(existing, dict):
        existing_keywords = split_keywords(existing.get("keywords", []))
    else:
        existing_keywords = split_keywords(existing)
    merged_keywords = list(dict.fromkeys([*existing_keywords, *keywords]))
    facets[facet_type][label] = {"keywords": merged_keywords, "source": "custom"}
    GRAPH_FACETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    GRAPH_FACETS_PATH.write_text(yaml.safe_dump(data, sort_keys=True, allow_unicode=True), encoding="utf-8")

    output = rebuild_graph_output()
    node_id = f"{facet_type}:{re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-')}"
    edge_count = count_graph_node_edges(node_id)
    append_wiki_log(
        f"{utc_now()} ui_server update config/graph_facets.yml -- add custom {facet_type} facet {label}"
    )
    return {
        "ok": True,
        "facet_type": facet_type,
        "label": label,
        "keywords": merged_keywords,
        "node_id": node_id,
        "edge_count": edge_count,
        "output": output,
    }


def reset_graph_facets(payload: dict | None = None) -> dict:
    data = read_graph_facets_config()
    facets = data.get("facets", {})
    removed_count = 0
    if isinstance(facets, dict):
        for labels in facets.values():
            if isinstance(labels, dict):
                removed_count += len(labels)

    data["facets"] = {"topic": {}, "method": {}, "observation": {}}
    GRAPH_FACETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    GRAPH_FACETS_PATH.write_text(yaml.safe_dump(data, sort_keys=True, allow_unicode=True), encoding="utf-8")

    output = rebuild_graph_output()
    append_wiki_log(
        f"{utc_now()} ui_server update config/graph_facets.yml -- reset custom graph facets"
    )
    return {
        "ok": True,
        "removed_count": removed_count,
        "output": output,
    }


def reject_paper(arxiv_id: str) -> dict:
    if not arxiv_id:
        raise ValueError("Paper ID is required")
    removed: list[str] = []
    pruned: list[str] = []
    with connect() as conn:
        init_db(conn)
        row = conn.execute(
            "SELECT id, status, pdf_path, text_path FROM papers WHERE arxiv_id = ? ORDER BY version DESC LIMIT 1",
            (arxiv_id,),
        ).fetchone()
        if row is None:
            raise KeyError("Paper not found")

        pdf_path = safe_project_file(row["pdf_path"], [ROOT / "data" / "raw"], ".pdf")
        text_path = safe_project_file(row["text_path"], [ROOT / "data" / "text"], ".txt")
        wiki_path = ROOT / paper_wiki_rel(arxiv_id)
        legacy_wiki_path = ROOT / f"wiki/papers/{paper_safe_id(arxiv_id)}.md"
        summary_path = summary_cache_path(arxiv_id)
        deep_summary_path = deep_summary_cache_path(arxiv_id)
        deep_summary_wiki_path = ROOT / paper_deep_summary_wiki_rel(arxiv_id)
        deep_summary_wiki_export = deep_summary_wiki_export_path(arxiv_id)
        cache_path = ROOT / "data" / "cache" / "ollama" / paper_safe_id(arxiv_id)

        for path in [
            pdf_path,
            text_path,
            wiki_path,
            legacy_wiki_path if legacy_wiki_path != wiki_path else None,
            summary_path,
            deep_summary_path,
            deep_summary_wiki_path,
            deep_summary_wiki_export,
        ]:
            if unlink_if_exists(path):
                removed.append(str(path.relative_to(ROOT)).replace("\\", "/"))
        if remove_tree_if_exists(cache_path):
            removed.append(str(cache_path.relative_to(ROOT)).replace("\\", "/"))

        pruned = prune_paper_references(arxiv_id)
        paper_rel = paper_wiki_rel(arxiv_id)
        conn.execute(
            "UPDATE papers SET status = ?, pdf_path = NULL, text_path = NULL, updated_at = ? WHERE id = ?",
            ("rejected", utc_now(), row["id"]),
        )
        conn.execute("DELETE FROM wiki_pages WHERE arxiv_id = ? OR path = ?", (arxiv_id, paper_rel))
        conn.execute(
            "DELETE FROM links WHERE arxiv_id = ? OR source_path = ? OR target_path = ?",
            (arxiv_id, paper_rel, paper_rel),
        )
        conn.commit()

    graph_output = rebuild_graph_output()
    append_wiki_log(
        f"{utc_now()} ui_server mark rejected {paper_wiki_rel(arxiv_id)} {arxiv_id} -- "
        f"removed {len(removed)} files, pruned {len(pruned)} pages"
    )
    return {
        "ok": True,
        "arxiv_id": arxiv_id,
        "status": "rejected",
        "removed": removed,
        "pruned": pruned,
        "output": graph_output,
    }


def delete_paper(arxiv_id: str) -> dict:
    if not arxiv_id:
        raise ValueError("Paper ID is required")
    removed: list[str] = []
    pruned: list[str] = []
    with connect() as conn:
        init_db(conn)
        row = conn.execute(
            "SELECT id, pdf_path, text_path FROM papers WHERE arxiv_id = ? ORDER BY version DESC LIMIT 1",
            (arxiv_id,),
        ).fetchone()
        if row is None:
            raise KeyError("Paper not found")

        safe_id = paper_safe_id(arxiv_id)
        paper_rel = paper_wiki_rel(arxiv_id)
        legacy_paper_rel = f"wiki/papers/{safe_id}.md"
        paths = [
            safe_project_file(row["pdf_path"], [ROOT / "data" / "raw"], ".pdf"),
            safe_project_file(row["text_path"], [ROOT / "data" / "text"], ".txt"),
            ROOT / paper_rel,
            ROOT / legacy_paper_rel if legacy_paper_rel != paper_rel else None,
            ROOT / "data" / "markdown" / f"{safe_id}.md",
            summary_cache_path(arxiv_id),
            deep_summary_cache_path(arxiv_id),
            ROOT / paper_deep_summary_wiki_rel(arxiv_id),
            deep_summary_wiki_export_path(arxiv_id),
        ]
        for path in paths:
            if unlink_if_exists(path):
                removed.append(str(path.relative_to(ROOT)).replace("\\", "/"))
        cache_path = ROOT / "data" / "cache" / "ollama" / safe_id
        if remove_tree_if_exists(cache_path):
            removed.append(str(cache_path.relative_to(ROOT)).replace("\\", "/"))

        pruned = prune_paper_references(arxiv_id)
        conn.execute("DELETE FROM wiki_pages WHERE arxiv_id = ? OR path = ?", (arxiv_id, paper_rel))
        conn.execute(
            "DELETE FROM links WHERE arxiv_id = ? OR source_path = ? OR target_path = ?",
            (arxiv_id, paper_rel, paper_rel),
        )
        conn.execute("DELETE FROM papers WHERE arxiv_id = ?", (arxiv_id,))
        conn.commit()

    graph_output = rebuild_graph_output()
    append_wiki_log(
        f"{utc_now()} ui_server delete paper {paper_wiki_rel(arxiv_id)} {arxiv_id} -- "
        f"removed {len(removed)} files, pruned {len(pruned)} pages"
    )
    return {
        "ok": True,
        "arxiv_id": arxiv_id,
        "status": "deleted",
        "removed": removed,
        "pruned": pruned,
        "output": graph_output,
    }


def paper_action_stage_label(stage: list[str]) -> str:
    script = Path(stage[0]).name
    labels = {
        "download_papers.py": "Downloading PDF",
        "extract_text.py": "Extracting text",
        "ingest_paper.py": "Building wiki",
        "daily_digest.py": "Writing digest",
        "curate_wiki.py": "Curating wiki",
        "build_graph.py": "Building graph",
    }
    return labels.get(script, script)


def process_paper_to_graph(arxiv_id: str, progress_job_id: str = "") -> dict:
    paper = get_paper(arxiv_id)["paper"]
    is_local_upload = str(paper.get("abs_url") or "").startswith("local-upload:") or not paper.get("pdf_url")
    if is_local_upload:
        stages = []
        if paper.get("status") == "downloaded":
            stages.append(["scripts/extract_text.py", "--arxiv-id", arxiv_id])
        if paper.get("status") in {"downloaded", "text_extracted", "failed_ingest"}:
            stages.append(["scripts/ingest_paper.py", "--arxiv-id", arxiv_id, "--require-llm"])
        stages.extend(
            [
                ["scripts/daily_digest.py", "--arxiv-id", arxiv_id],
                ["scripts/curate_wiki.py", "--arxiv-id", arxiv_id],
                ["scripts/build_graph.py"],
            ]
        )
    else:
        set_paper_selected(arxiv_id)
        stages = [
            ["scripts/download_papers.py", "--arxiv-id", arxiv_id],
            ["scripts/extract_text.py", "--arxiv-id", arxiv_id],
            ["scripts/ingest_paper.py", "--arxiv-id", arxiv_id, "--require-llm"],
            ["scripts/daily_digest.py", "--arxiv-id", arxiv_id],
            ["scripts/curate_wiki.py", "--arxiv-id", arxiv_id],
            ["scripts/build_graph.py"],
        ]
    output_parts: list[str] = []
    total = max(1, len(stages))
    update_upload_progress(
        progress_job_id,
        status="running",
        stage="Starting Build Wiki",
        message=f"Starting wiki build for {arxiv_id}.",
        percent=10,
        file_index=1,
        total_files=1,
        filename=arxiv_id,
    )
    for index, stage in enumerate(stages, start=1):
        label = paper_action_stage_label(stage)
        start_percent = 10 + int(((index - 1) / total) * 82)
        end_percent = 10 + int((index / total) * 82)
        update_upload_progress(
            progress_job_id,
            status="running",
            stage=label,
            message=f"{label} ({index}/{total}) for {arxiv_id}.",
            percent=start_percent,
            file_index=1,
            total_files=1,
            filename=arxiv_id,
        )
        args = script_command(stage[0], *stage[1:])
        result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, timeout=1800)
        stage_output = result.stdout + result.stderr
        output_parts.append(f"$ {' '.join(stage)}\n{stage_output}".rstrip())
        if result.returncode != 0:
            update_upload_progress(
                progress_job_id,
                status="failed",
                stage=f"{label} failed",
                message=f"{label} failed for {arxiv_id}.",
                percent=100,
                file_index=1,
                total_files=1,
                filename=arxiv_id,
            )
            return {"ok": False, "arxiv_id": arxiv_id, "returncode": result.returncode, "output": "\n\n".join(output_parts)}
        update_upload_progress(
            progress_job_id,
            status="running",
            stage=f"{label} complete",
            message=f"{label} complete ({index}/{total}) for {arxiv_id}.",
            percent=end_percent,
            file_index=1,
            total_files=1,
            filename=arxiv_id,
        )
    paper = get_paper(arxiv_id)["paper"]
    update_upload_progress(
        progress_job_id,
        status="completed",
        stage="Build Wiki complete",
        message=f"Built wiki for {arxiv_id}.",
        percent=100,
        file_index=1,
        total_files=1,
        filename=arxiv_id,
    )
    return {"ok": True, "arxiv_id": arxiv_id, "status": paper.get("status"), "output": "\n\n".join(output_parts)}


def handle_paper_action(payload: dict) -> dict:
    arxiv_id = str(payload.get("arxiv_id") or "").strip()
    if not arxiv_id and payload.get("wiki_path"):
        arxiv_id = arxiv_id_from_paper_wiki_path(str(payload.get("wiki_path") or "")) or ""
    action = str(payload.get("action") or "").strip()
    if action == "select":
        return set_paper_selected(arxiv_id)
    if action == "reject":
        return reject_paper(arxiv_id)
    if action == "delete":
        return delete_paper(arxiv_id)
    if action == "process_to_graph":
        return process_paper_to_graph(arxiv_id, str(payload.get("progress_job_id") or ""))
    raise ValueError("Unsupported paper action")


def ask_wiki(payload: dict) -> dict:
    question = str(payload.get("question", "")).strip()
    if not question:
        raise ValueError("Question is empty")
    paper_id = str(payload.get("paper_id") or "").strip()
    if paper_id:
        context, sources = build_paper_chat_context(question, paper_id)
    else:
        pages = search_paper_wiki(question, max_pages=15)
        context = build_context(pages)
        sources = [page.path for page in pages]
    if not context:
        return {"answer": "No relevant wiki pages were found.", "sources": []}
    backend = selected_chat_backend(payload)
    model = backend["model"]
    prompt = (ROOT / "config" / "prompts" / "answer_question.md").read_text(encoding="utf-8")
    if paper_id and wants_broader_paper_context(question):
        scope = (
            f"Selected paper: {paper_id}\n"
            "Use the selected paper and graph-connected papers together as the cited evidence set. "
            "Keep the selected paper at the center of the answer. "
            "When graph-connected papers strengthen, qualify, or contrast the selected paper, use them as supporting evidence. "
            "Clearly separate selected-paper evidence from broader comparison.\n\n"
        )
    elif paper_id:
        scope = (
            f"Selected paper: {paper_id}\n"
            "Answer directly from the selected paper's wiki page and extracted text. "
            "Do not discuss graph-connected papers unless the user explicitly asks for comparison or related work. "
            "Do not create a graph-connected evidence section for this question. "
            "Cite the selected paper page or extracted text for concrete values. "
            "Keep the answer focused on the user's question.\n\n"
        )
    else:
        scope = (
            "No selected paper is loaded. Use `wiki/papers` pages as retrieved evidence, but also provide standard astrophysics background when it helps answer the question. "
            "If several papers are relevant, compare them briefly and cite each paper page or paper ID. "
            "Clearly separate retrieved evidence from uncited general background.\n\n"
        )
    try:
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"{scope}Question: {question}\n\nContext:\n{context}"},
        ]
        if backend["provider"] == "codex":
            answer = codex_chat(messages, model=model, timeout=600.0)
        else:
            answer = chat(
                messages,
                model=model,
                timeout=600.0,
                options={"num_ctx": 16384, "num_predict": 1800, "temperature": 0.2},
                think=False,
            )
    except CodexError as exc:
        answer = f"Codex CLI unavailable: {exc}\n\nRetrieved context:\n\n{context}"
    except Exception as exc:
        provider_names = {
            "codex": "Codex CLI",
            "openai_compatible": "OpenAI-compatible LLM",
            "ollama": "Ollama",
        }
        provider_name = provider_names.get(backend["provider"]) or api_provider_label(backend["provider"])
        answer = f"{provider_name} call failed: {exc}\n\nRetrieved context:\n\n{context}"
    label = f"Codex CLI · {model}" if backend["provider"] == "codex" else model
    return {"answer": answer, "answer_html": markdown_to_html(answer), "sources": sources, "model": label}


def graph_node_for_source(source: str) -> str:
    if source.startswith("wiki/papers/") and source.endswith(".md"):
        return source
    text_match = re.match(r"^data/text/(?P<arxiv_id>.+)\.txt$", source)
    if text_match:
        return paper_wiki_rel(text_match.group("arxiv_id"))
    summary_match = re.match(r"^data/summaries/ko/(?P<arxiv_id>.+)\.md$", source)
    if summary_match:
        return paper_wiki_rel(summary_match.group("arxiv_id"))
    deep_summary_match = re.match(r"^data/summaries/deep/ko/(?P<arxiv_id>.+)\.md$", source)
    if deep_summary_match:
        return paper_wiki_rel(deep_summary_match.group("arxiv_id"))
    deep_wiki_match = re.match(r"^wiki/papers/(?P<arxiv_id>.+)-deep-summary\.md$", source)
    if deep_wiki_match:
        return paper_wiki_rel(deep_wiki_match.group("arxiv_id"))
    return ""


def graph_search(payload: dict) -> dict:
    question = str(payload.get("question", "")).strip()
    if not question:
        raise ValueError("Question is empty")
    pages = search_paper_wiki(question, max_pages=12)
    matches: list[dict] = []
    node_ids: list[str] = []
    for page in pages:
        node_id = graph_node_for_source(page.path)
        if node_id and node_id not in node_ids:
            node_ids.append(node_id)
        matches.append(
            {
                "path": page.path,
                "node_id": node_id,
                "score": page.score,
                "excerpt": page.excerpt[:1200],
            }
        )
    return {"matches": matches, "sources": [page.path for page in pages], "node_ids": node_ids}


def interest_topics_from_text(text: str) -> list[str]:
    lowered = text.lower()
    topic_map = [
        ("environment effects", ["환경", "environment", "cluster", "group", "density", "filament", "void"]),
        ("quenching", ["퀜칭", "소광", "수동화", "quench", "quenched", "passive", "quiescent"]),
        ("star formation", ["별형성", "별 형성", "star formation", "sfr", "star-forming"]),
        ("external galaxy evolution", ["외부은하", "은하 진화", "galaxy evolution", "galaxies"]),
        ("jwst observations", ["jwst", "nircam", "nirspec", "제임스웹"]),
        ("machine learning astronomy", ["기계학습", "머신러닝", "machine learning", "deep learning", "neural", "transformer"]),
        ("simulations", ["시뮬레이션", "simulation", "tng", "eagle", "illustris"]),
        ("photometric redshift", ["photo-z", "photometric redshift", "측광적색편이"]),
    ]
    topics = []
    for topic, needles in topic_map:
        if any(needle in lowered for needle in needles):
            topics.append(topic)
    return topics


def ensure_interest_profile() -> Path:
    path = ROOT / "wiki" / "interests" / "profile.md"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Research Interest Profile\n\n"
            "## Current High-weight Interests\n\n"
            "## Science Questions\n\n"
            "## Preferred Methods\n\n"
            "## Surveys / Instruments / Simulations\n\n"
            "## Redshift / Mass / Environment Focus\n\n"
            "## Negative Preferences\n\n"
            "## Evidence From Conversations\n\n"
            "## Update Log\n",
            encoding="utf-8",
        )
    return path


def append_interest_profile(question: str, answer: str, sources: list[str], topics: list[str]) -> None:
    path = ensure_interest_profile()
    text = path.read_text(encoding="utf-8")
    stamp = utc_now()
    if topics:
        lines = [f"- `{topic}` (+0.5 from saved chat, {stamp})" for topic in topics]
        marker = "## Current High-weight Interests\n"
        if marker in text:
            text = text.replace(marker, marker + "\n" + "\n".join(lines) + "\n", 1)
    evidence = (
        f"- {stamp}: saved Q&A about \"{question[:120]}\""
        + (f" Sources: {', '.join(sources[:5])}" if sources else "")
    )
    marker = "## Evidence From Conversations\n"
    if marker in text:
        text = text.replace(marker, marker + "\n" + evidence + "\n", 1)
    else:
        text = text.rstrip() + "\n\n## Evidence From Conversations\n\n" + evidence + "\n"
    update = f"- {stamp}: Conversation Memory Agent updated profile from saved chat."
    marker = "## Update Log\n"
    if marker in text:
        text = text.replace(marker, marker + "\n" + update + "\n", 1)
    else:
        text = text.rstrip() + "\n\n## Update Log\n\n" + update + "\n"
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def create_conversation_note(session_id: int, question: str, answer: str, sources: list[str]) -> None:
    note = (
        f"Question: {question}\n\n"
        f"Answer summary:\n{answer[:2000]}\n\n"
        f"Sources: {', '.join(sources)}"
    )
    topics = interest_topics_from_text(f"{question}\n{answer}")
    with connect() as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO conversation_notes(
              session_id, created_at, note_type, content, related_wiki_pages_json,
              approved, applied_to_wiki
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, utc_now(), "saved_chat_summary", note, json.dumps(sources, ensure_ascii=True), 1, 0),
        )
        for topic in topics:
            conn.execute(
                """
                INSERT INTO interest_signals(
                  session_id, created_at, signal_type, topic, weight_delta, evidence, explicit, approved
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, utc_now(), "saved_chat", topic, 0.5, question[:240], 1, 1),
            )
        conn.commit()
    append_interest_profile(question, answer, sources, topics)
    append_wiki_log(f"{utc_now()} Conversation memory saved: session `{session_id}`, topics: {', '.join(topics) or 'none'}")


def save_chat_payload(payload: dict) -> dict:
    question = str(payload.get("question", "")).strip()
    answer = str(payload.get("answer", "")).strip()
    sources = payload.get("sources") or []
    if not question:
        raise ValueError("Question is empty")
    if not answer:
        raise ValueError("Answer is empty")
    if not isinstance(sources, list):
        sources = []
    session_id = save_chat_turn(question, answer, [str(source) for source in sources])
    create_conversation_note(session_id, question, answer, [str(source) for source in sources])
    return {"ok": True, "session_id": session_id}


def save_chat_turn(question: str, answer: str, sources: list[str]) -> int:
    conversation_path = ROOT / "conversations" / f"{date.today().isoformat()}.md"
    conversation_path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        init_db(conn)
        cur = conn.execute(
            "INSERT INTO conversation_sessions(started_at, ended_at, title, mode, saved_to_markdown_path) VALUES (?, ?, ?, ?, ?)",
            (utc_now(), utc_now(), question[:80], "chat", str(conversation_path.relative_to(ROOT))),
        )
        session_id = cur.lastrowid
        conn.execute(
            "INSERT INTO conversation_messages(session_id, timestamp, role, content, cited_sources_json, save_policy) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, utc_now(), "user", question, "[]", "saved_by_user"),
        )
        conn.execute(
            "INSERT INTO conversation_messages(session_id, timestamp, role, content, cited_sources_json, save_policy) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, utc_now(), "assistant", answer, json.dumps(sources, ensure_ascii=True), "saved_by_user"),
        )
        conn.commit()
    with conversation_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n## {utc_now()}\n\n**User:** {question}\n\n**Assistant:**\n\n{answer}\n\nSources: {', '.join(sources)}\n")
    return int(session_id)


def proposal_slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9가-힣]+", "-", text).strip("-").lower()
    return slug[:60] or "proposal"


def safe_wiki_update_path(relative_path: str) -> tuple[str, Path, str]:
    cleaned = relative_path.strip().strip("`'\"")
    cleaned = cleaned.replace("\\", "/")
    if not cleaned.startswith("wiki/") or not cleaned.endswith(".md"):
        raise ValueError("Target page must be a wiki Markdown path")
    parts = cleaned.split("/")
    if len(parts) < 3:
        raise ValueError("Target page must be inside a supported wiki directory")
    section = parts[1]
    page_type = ALLOWED_WIKI_UPDATE_DIRS.get(section)
    if page_type is None:
        allowed = ", ".join(f"wiki/{name}/" for name in sorted(ALLOWED_WIKI_UPDATE_DIRS))
        raise ValueError(f"Proposal can only update these wiki areas: {allowed}")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Target page path contains an unsafe path segment")
    candidate = (ROOT / cleaned).resolve()
    allowed_root = (ROOT / "wiki" / section).resolve()
    candidate.relative_to(allowed_root)
    return cleaned, candidate, page_type


def proposal_path_from_payload(payload: dict) -> tuple[str, Path]:
    relative_path = str(payload.get("path") or "").strip().replace("\\", "/")
    if not relative_path.startswith("wiki/proposals/") or not relative_path.endswith(".md"):
        raise ValueError("Only wiki proposal files can be applied")
    path = safe_path(relative_path)
    path.relative_to((ROOT / "wiki" / "proposals").resolve())
    if not path.exists():
        raise FileNotFoundError("Proposal file not found")
    return relative_path, path


def extract_proposal_target(markdown: str) -> str:
    section = markdown_section(markdown, "Target Page Recommendation")
    match = re.search(
        r"wiki/(topics|methods|surveys|simulations|concepts|entities)/[A-Za-z0-9가-힣._/-]+\.md",
        section,
    )
    if not match:
        raise ValueError("Proposal does not contain an allowed target wiki path")
    return match.group(0)


def extract_proposal_status(markdown: str) -> str:
    if not markdown.startswith("---\n"):
        return ""
    try:
        frontmatter = markdown.split("---\n", 2)[1]
    except IndexError:
        return ""
    for line in frontmatter.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "status":
            return value.strip().strip("'\"")
    return ""


def replace_proposal_frontmatter(markdown: str, status: str, applied_to: str) -> str:
    lines = [
        "page_type: wiki_update_proposal",
        f"updated_at: \"{utc_now()}\"",
        f"status: {status}",
    ]
    if status == "applied":
        lines.extend([f"applied_at: \"{utc_now()}\"", f"applied_to: \"{applied_to}\""])
    if markdown.startswith("---\n"):
        try:
            frontmatter, body = markdown.split("---\n", 2)[1:]
            kept = []
            for line in frontmatter.splitlines():
                key = line.partition(":")[0].strip()
                if key not in {"page_type", "updated_at", "status", "applied_at", "applied_to"}:
                    kept.append(line)
            lines = kept + lines
            return "---\n" + "\n".join(lines).rstrip() + "\n---\n" + body
        except ValueError:
            pass
    return "---\n" + "\n".join(lines).rstrip() + "\n---\n\n" + markdown.lstrip()


def title_for_wiki_page(path: Path, fallback: str) -> str:
    if path.exists():
        match = re.search(r"^#\s+(.+)$", path.read_text(encoding="utf-8", errors="ignore"), re.MULTILINE)
        if match:
            return match.group(1).strip()
    return fallback.replace("-", " ").replace("_", " ").title()


def apply_wiki_proposal_payload(payload: dict) -> dict:
    proposal_rel, proposal_path = proposal_path_from_payload(payload)
    proposal = proposal_path.read_text(encoding="utf-8")
    target_rel, target_path, page_type = safe_wiki_update_path(extract_proposal_target(proposal))
    proposed_update = markdown_section(proposal, "Proposed Update").strip()
    if not proposed_update:
        raise ValueError("Proposal does not contain a Proposed Update section")

    status = extract_proposal_status(proposal)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        target_text = target_path.read_text(encoding="utf-8")
    else:
        target_title = title_for_wiki_page(target_path, target_path.stem)
        target_text = f"# {target_title}\n\n"

    already_applied = proposal_rel in target_text or status == "applied"
    if not already_applied:
        heading = "## Approved Chat Updates"
        entry = (
            f"\n\n### {utc_now()} from `{proposal_rel}`\n\n"
            f"{proposed_update.rstrip()}\n"
        )
        if f"{heading}\n" in target_text:
            target_text = target_text.rstrip() + entry
        else:
            target_text = target_text.rstrip() + f"\n\n{heading}" + entry
        target_path.write_text(target_text.rstrip() + "\n", encoding="utf-8")

    proposal_path.write_text(replace_proposal_frontmatter(proposal, "applied", target_rel), encoding="utf-8")
    with connect() as conn:
        init_db(conn)
        register_wiki_page(conn, target_rel, page_type, title_for_wiki_page(target_path, target_path.stem))
    append_wiki_log(f"{utc_now()} Wiki proposal applied: `{proposal_rel}` -> `{target_rel}`")
    graph_refreshed = False
    graph_output = ""
    if not already_applied:
        result = subprocess.run(
            script_command("scripts/build_graph.py"),
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=600,
        )
        graph_output = (result.stdout + result.stderr).strip()
        graph_refreshed = result.returncode == 0
    return {
        "ok": True,
        "path": proposal_rel,
        "target_path": target_rel,
        "already_applied": already_applied,
        "graph_refreshed": graph_refreshed,
        "graph_output": graph_output,
    }


def propose_wiki_update_payload(payload: dict) -> dict:
    question = str(payload.get("question", "")).strip()
    answer = str(payload.get("answer", "")).strip()
    sources = payload.get("sources") or []
    if not question:
        raise ValueError("Question is empty")
    if not answer:
        raise ValueError("Answer is empty")
    if not isinstance(sources, list):
        sources = []
    source_lines = "\n".join(f"- {source}" for source in sources)
    prompt = (ROOT / "config" / "prompts" / "propose_wiki_update.md").read_text(encoding="utf-8")
    content = (
        f"User question:\n{question}\n\n"
        f"Assistant answer:\n{answer}\n\n"
        f"Cited sources:\n{source_lines or '- none'}"
    )
    try:
        proposal_body = chat(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": content},
            ],
            model=chat_model(),
            timeout=180.0,
        ).strip()
    except Exception as exc:
        proposal_body = (
            "# Wiki Update Proposal\n\n"
            "## Target Page Recommendation\n\n"
            "Manual review required.\n\n"
            "## Proposed Update\n\n"
            f"{answer}\n\n"
            "## Source Evidence\n\n"
            f"{source_lines or '- none'}\n\n"
            "## Review Notes\n\n"
            f"LLM proposal generation failed: {exc}\n"
        )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = ROOT / "wiki" / "proposals" / f"{stamp}-{proposal_slug(question)}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = (
        "---\n"
        "page_type: wiki_update_proposal\n"
        f"created_at: \"{utc_now()}\"\n"
        "status: draft\n"
        "---\n\n"
    )
    audit = (
        "\n\n---\n\n"
        "## Original Chat Turn\n\n"
        f"**Question:** {question}\n\n"
        f"**Answer:**\n\n{answer}\n\n"
        f"**Sources:** {', '.join(str(source) for source in sources)}\n"
    )
    path.write_text(frontmatter + proposal_body.rstrip() + audit, encoding="utf-8")
    rel = str(path.relative_to(ROOT)).replace("\\", "/")
    return {"ok": True, "path": rel}


class UiHandler(BaseHTTPRequestHandler):
    server_version = "AstroWikiUI/0.1"

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            params = parse_qs(parsed.query)
            if path in {"/", "/index.html"}:
                self.serve_file(LANDING_PAGE)
            elif path in {"/astro-ph-llm-wiki", "/astro-ph-llm-wiki/", "/app", "/app/"}:
                self.serve_file(UI_DIR / "index.html")
            elif path == "/static/app.css":
                self.serve_file(UI_DIR / "app.css")
            elif path == "/static/app.js":
                self.serve_file(UI_DIR / "app.js")
            elif path == "/static/vendor/mathjax/tex-chtml-full.js":
                self.serve_file(UI_DIR / "vendor" / "mathjax" / "tex-chtml-full.js")
            elif path == "/graph.html":
                self.serve_file(ROOT / "graphify-out" / "graph.html")
            elif path == "/api/summary":
                json_response(self, get_summary())
            elif path == "/api/models":
                json_response(self, list_chat_models())
            elif path == "/api/settings-models":
                json_response(self, settings_model_options(params))
            elif path == "/api/settings":
                json_response(self, get_app_settings())
            elif path == "/api/papers":
                json_response(self, get_papers(params))
            elif path == "/api/paper-search":
                json_response(self, search_arxiv_papers(params))
            elif path == "/api/paper":
                json_response(self, get_paper(params.get("id", [""])[0]))
            elif path == "/api/paper-summary":
                json_response(
                    self,
                    get_korean_paper_summary(
                        params.get("id", [""])[0],
                        refresh=params.get("refresh", ["0"])[0] in {"1", "true", "yes"},
                    ),
                )
            elif path == "/api/paper-deep-summary":
                json_response(
                    self,
                    get_paper_deep_summary(
                        params.get("id", params.get("arxiv_id", [""]))[0],
                        refresh=params.get("refresh", ["0"])[0] in {"1", "true", "yes"},
                    ),
                )
            elif path == "/api/wiki-list":
                json_response(self, wiki_list())
            elif path == "/api/wiki":
                json_response(self, read_wiki(params.get("path", [""])[0]))
            elif path == "/api/upload-progress":
                json_response(self, upload_progress_for(params.get("id", [""])[0]))
            elif path == "/api/upload-prompt":
                json_response(self, upload_work_prompt_payload())
            elif path == "/api/obsidian-export":
                json_response(self, export_obsidian_vault())
            elif path == "/download/obsidian-vault":
                self.serve_obsidian_vault()
            elif path == "/api/pdf-info":
                pdf_path = safe_pdf_path(params.get("path", [""])[0])
                import fitz

                with fitz.open(pdf_path) as document:
                    page = document.load_page(0) if document.page_count else None
                    rect = page.rect if page is not None else None
                    json_response(
                        self,
                        {
                            "pages": document.page_count,
                            "page_width": float(rect.width) if rect is not None else 0,
                            "page_height": float(rect.height) if rect is not None else 0,
                        },
                    )
            elif path == "/pdf-page":
                self.serve_pdf_page(params)
            elif path == "/api/review-queue":
                json_response(self, review_queue())
            elif path == "/api/lint-report":
                json_response(self, latest_lint_report())
            elif path == "/pdf":
                self.serve_pdf(params.get("path", [""])[0])
            else:
                json_response(self, {"error": "Not found"}, 404)
        except Exception as exc:
            json_response(self, {"error": str(exc)}, 500)

    def do_HEAD(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/pdf":
                path = safe_pdf_path(parse_qs(parsed.query).get("path", [""])[0])
                if not path.exists():
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(path.stat().st_size))
                self.send_header("Content-Disposition", f'inline; filename="{path.name}"')
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
        except Exception:
            self.send_response(500)
            self.end_headers()

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/upload-paper":
                json_response(self, upload_paper_from_form(self))
                return
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if parsed.path == "/api/run":
                json_response(self, run_agent(payload))
            elif parsed.path == "/api/settings":
                json_response(self, save_app_settings_payload(payload))
            elif parsed.path == "/api/upload-batch":
                json_response(self, process_batch_upload(payload))
            elif parsed.path == "/api/upload-prompt":
                json_response(self, save_upload_work_prompt_payload(payload))
            elif parsed.path == "/api/graph-search":
                json_response(self, graph_search(payload))
            elif parsed.path == "/api/graph-facet":
                json_response(self, add_graph_facet(payload))
            elif parsed.path == "/api/graph-facets/reset":
                json_response(self, reset_graph_facets(payload))
            elif parsed.path == "/api/chat":
                json_response(self, ask_wiki(payload))
            elif parsed.path == "/api/citation-trace":
                json_response(self, citation_trace_payload(payload))
            elif parsed.path == "/api/save-chat":
                json_response(self, save_chat_payload(payload))
            elif parsed.path == "/api/propose-wiki-update":
                json_response(self, propose_wiki_update_payload(payload))
            elif parsed.path == "/api/apply-wiki-proposal":
                json_response(self, apply_wiki_proposal_payload(payload))
            elif parsed.path == "/api/obsidian-export":
                json_response(self, export_obsidian_vault(payload.get("output_dir")))
            elif parsed.path == "/api/run-lint":
                result = run_agent({"command": "lint"})
                result["lint"] = latest_lint_report()
                json_response(self, result)
            elif parsed.path == "/api/paper-action":
                json_response(self, handle_paper_action(payload))
            elif parsed.path == "/api/paper-search-import":
                json_response(self, import_arxiv_search_paper(payload))
            else:
                json_response(self, {"error": "Not found"}, 404)
        except Exception as exc:
            json_response(self, {"error": str(exc)}, 500)

    def serve_file(self, path: Path) -> None:
        if not path.exists():
            json_response(self, {"error": "Not found"}, 404)
            return
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def serve_obsidian_vault(self) -> None:
        path = obsidian_export_path()
        if not path.exists():
            export_obsidian_vault()
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", 'attachment; filename="Astro-Note-AI-Obsidian.zip"')
        self.end_headers()
        self.wfile.write(data)

    def serve_pdf(self, relative_path: str) -> None:
        try:
            path = safe_pdf_path(relative_path)
        except Exception as exc:
            json_response(self, {"error": str(exc)}, 403)
            return
        if not path.exists():
            json_response(self, {"error": "PDF not found"}, 404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'inline; filename="{path.name}"')
        self.end_headers()
        self.wfile.write(data)

    def serve_pdf_page(self, params: dict[str, list[str]]) -> None:
        try:
            path = safe_pdf_path(params.get("path", [""])[0])
            page_number = max(1, int(params.get("page", ["1"])[0]))
            zoom = min(3.0, max(0.45, float(params.get("zoom", ["1.35"])[0])))
            import fitz

            with fitz.open(path) as document:
                if page_number > document.page_count:
                    raise ValueError("PDF page is out of range")
                page = document.load_page(page_number - 1)
                pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                data = pixmap.tobytes("png")
        except Exception as exc:
            json_response(self, {"error": str(exc)}, 400)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))


def find_port(start: int) -> int:
    import socket

    port = start
    while port < start + 50:
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                port += 1
    raise RuntimeError("No free port found")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Astro Wiki local UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    port = find_port(args.port)
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((args.host, port), UiHandler)
    print(f"Astro-Note AI UI: http://{args.host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
