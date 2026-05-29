#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import _bootstrap  # noqa: F401

from astro_wiki.config import chat_model, load_yaml, project_path
from astro_wiki.db import connect, init_db, register_wiki_page
from astro_wiki.graphify import topic_canonical_key
from astro_wiki.logging import append_wiki_log, get_agent_logger
from astro_wiki.ollama_client import chat
from astro_wiki.semantic_wiki import load_graph, semantic_index_content, semantic_topic_pages
from astro_wiki.wiki_io import append_unique_line, now_iso, wiki_rel


def parse_date(value: str | None) -> str:
    if not value or value == "today":
        return datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    return datetime.fromisoformat(value).date().isoformat()


def agent_config() -> dict:
    return load_yaml("config/agents.yml").get("agents", {}).get("semantic_wiki", {})


def write_if_changed(path: Path, content: str) -> bool:
    normalized = content.rstrip() + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == normalized:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(normalized, encoding="utf-8")
    return True


def cache_paths(topic_slug: str) -> tuple[Path, Path]:
    base = project_path("data", "cache", "semantic_wiki")
    return base / f"{topic_slug}.article.md", base / f"{topic_slug}.evidence.sha256"


def evidence_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def reusable_article_cache_paths(page, article_path: Path) -> list[Path]:
    candidates = [article_path] if article_path.exists() else []
    target_key = topic_canonical_key(page.label)
    for candidate in sorted(article_path.parent.glob("*.article.md")):
        if candidate == article_path:
            continue
        candidate_label = candidate.name.removesuffix(".article.md").replace("-", " ")
        if topic_canonical_key(candidate_label) == target_key:
            candidates.append(candidate)
    return candidates


def reusable_cached_article(page, article_path: Path) -> str | None:
    for candidate in reusable_article_cache_paths(page, article_path):
        article = normalize_internal_paper_links(candidate.read_text(encoding="utf-8"))
        article = ensure_paragraph_citations(article, page.content)
        article_path.parent.mkdir(parents=True, exist_ok=True)
        article_path.write_text(article.rstrip() + "\n", encoding="utf-8")
        return article
    return None


def fallback_article(page) -> str:
    return (
        "### Overview\n\n"
        f"`{page.label}` is represented in this wiki by {page.paper_count} graph-connected papers. "
        "The generated article synthesis is unavailable in this run, so the connected-paper evidence below is the authoritative source trail.\n\n"
        "### Research Background From This Wiki\n\n"
        "Use the connected papers below as the current background set for this topic.\n\n"
        "### Current Findings In The Collected Papers\n\n"
        "The paper-level evidence excerpts below summarize the source-grounded findings currently available in the local wiki.\n\n"
        "### Methods, Surveys, And Data\n\n"
        "See the graph-derived method and observation counts below.\n\n"
        "### Open Questions\n\n"
        "Open questions should be added after reviewing the connected paper pages.\n\n"
        "### Source Trail\n\n"
        "The source trail is the connected-paper list below.\n"
    )


def normalize_internal_paper_links(markdown: str) -> str:
    markdown = re.sub(
        r"\[(?P<arxiv_id>\d{4}\.\d{4,5})\.md\](?!\()",
        lambda match: f"[{match.group('arxiv_id')}](../../papers/{match.group('arxiv_id')}.md)",
        markdown,
    )
    markdown = markdown.replace("[[", "[")
    markdown = re.sub(r"(\.\./\.\./papers/\d{4}\.\d{4,5}\.md\))\]", r"\1", markdown)
    return markdown


def first_internal_paper_link(evidence_content: str) -> str | None:
    match = re.search(r"\((?P<link>\.\./\.\./papers/\d{4}\.\d{4,5}\.md)\)", evidence_content)
    if not match:
        return None
    arxiv_id = Path(match.group("link")).stem
    return f"[{arxiv_id}]({match.group('link')})"


def ensure_paragraph_citations(markdown: str, evidence_content: str) -> str:
    fallback_link = first_internal_paper_link(evidence_content)
    if not fallback_link:
        return markdown
    blocks = markdown.split("\n\n")
    fixed = []
    for block in blocks:
        stripped = block.strip()
        if (
            not stripped
            or stripped.startswith("#")
            or stripped.startswith(("*", "-"))
            or "](../../papers/" in stripped
            or len(stripped) < 80
        ):
            fixed.append(block)
            continue
        fixed.append(block.rstrip() + f" {fallback_link}.")
    return "\n\n".join(fixed)


def insert_living_article(content: str, article: str) -> str:
    article = article.strip()
    if not article:
        return content
    marker = "\n## Connected Papers\n\n"
    section = f"\n## Living Article\n\n{article}\n"
    if marker in content:
        return content.replace(marker, f"{section}{marker}", 1)
    return content.rstrip() + section


def article_from_cache_or_llm(page, *, model: str, timeout: float, max_tokens: int, no_llm: bool, logger) -> str:
    article_path, hash_path = cache_paths(Path(page.path).stem)
    current_hash = evidence_hash(page.content)
    if article_path.exists() and hash_path.exists() and hash_path.read_text(encoding="utf-8").strip() == current_hash:
        article = normalize_internal_paper_links(article_path.read_text(encoding="utf-8"))
        article = ensure_paragraph_citations(article, page.content)
        article_path.write_text(article.rstrip() + "\n", encoding="utf-8")
        return article
    if no_llm:
        if article := reusable_cached_article(page, article_path):
            return article
        return fallback_article(page)
    else:
        prompt = project_path("config", "prompts", "semantic_topic_article.md").read_text(encoding="utf-8")
        user_content = (
            f"Topic label: {page.label}\n"
            f"Topic source: {page.source}\n"
            f"Connected paper count: {page.paper_count}\n\n"
            "Evidence packet with internal links:\n"
            f"{page.content[:45000]}"
        )
        try:
            article = chat(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_content},
                ],
                model=model,
                timeout=timeout,
                options={"num_predict": max_tokens, "temperature": 0.2},
            ).strip()
        except Exception as exc:
            logger.exception("topic_id=%s action=synthesize status=failed message=%s", page.topic_id, exc)
            if article := reusable_cached_article(page, article_path):
                return article
            return fallback_article(page)
    article = normalize_internal_paper_links(article)
    article = ensure_paragraph_citations(article, page.content)
    if not article.strip():
        return fallback_article(page)
    article_path.parent.mkdir(parents=True, exist_ok=True)
    article_path.write_text(article.rstrip() + "\n", encoding="utf-8")
    hash_path.write_text(current_hash + "\n", encoding="utf-8")
    return article


def configured_exclusions(cfg: dict) -> set[str]:
    values = cfg.get("exclude_topic_slugs", [])
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return set()
    return {str(value).strip() for value in values if str(value).strip()}


def main() -> None:
    cfg = agent_config()
    parser = argparse.ArgumentParser(description="Materialize graph topic nodes into source-grounded wiki topic pages.")
    parser.add_argument("--date", default="today", help="Log date only; semantic topic pages are built from the current graph.")
    parser.add_argument("--graph", default=str(project_path("graphify-out", "graph.json")))
    parser.add_argument("--min-papers", type=int, default=int(cfg.get("min_papers", 2)))
    parser.add_argument("--max-topics", type=int, default=cfg.get("max_topics", 80))
    parser.add_argument("--max-papers-per-topic", type=int, default=int(cfg.get("max_papers_per_topic", 25)))
    parser.add_argument("--limit", type=int, default=None, help="Development alias for --max-topics.")
    parser.add_argument("--prune-stale", action="store_true", help="Delete generated semantic topic pages that are no longer selected.")
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM synthesis and write deterministic fallback article text.")
    parser.add_argument("--no-synthesis", action="store_true", help="Write evidence-list topic pages without the living article section.")
    parser.add_argument("--synthesis-limit", type=int, default=cfg.get("synthesis_max_topics_per_run"))
    parser.add_argument("--model", default=cfg.get("model") or chat_model())
    args = parser.parse_args()

    target_date = parse_date(args.date)
    logger = get_agent_logger("semantic_wiki_agent", target_date)
    max_topics = args.limit if args.limit is not None else args.max_topics
    graph = load_graph(Path(args.graph))
    pages = semantic_topic_pages(
        graph,
        min_papers=args.min_papers,
        max_topics=max_topics,
        max_papers_per_topic=args.max_papers_per_topic,
        excluded_topic_slugs=configured_exclusions(cfg),
    )
    synthesize = bool(cfg.get("synthesize_articles", True)) and not args.no_synthesis
    timeout = float(cfg.get("synthesis_timeout_seconds", 300))
    max_tokens = int(cfg.get("synthesis_max_tokens", 1800))
    synthesis_budget = args.synthesis_limit if args.synthesis_limit is not None else len(pages)

    changed = 0
    synthesized = 0
    with connect() as conn:
        init_db(conn)
        for index, page in enumerate(pages):
            content = page.content
            if synthesize and index < synthesis_budget:
                article = article_from_cache_or_llm(
                    page,
                    model=args.model,
                    timeout=timeout,
                    max_tokens=max_tokens,
                    no_llm=args.no_llm,
                    logger=logger,
                )
                content = insert_living_article(page.content, article)
                synthesized += 1
            action = "update" if page.path.exists() else "create"
            if not write_if_changed(page.path, content):
                logger.info("topic_id=%s action=skip status=unchanged path=%s", page.topic_id, page.path)
                continue
            rel = wiki_rel(page.path)
            register_wiki_page(conn, rel, "semantic_topic", f"Topic: {page.label}")
            append_wiki_log(
                f"{now_iso()} semantic_wiki_agent {action} {rel} -- "
                f"{page.paper_count} connected papers for topic `{page.label}`"
            )
            changed += 1
            logger.info(
                "topic_id=%s action=%s status=ok path=%s papers=%s",
                page.topic_id,
                action,
                rel,
                page.paper_count,
            )

        index_path = project_path("wiki", "topics", "semantic-index.md")
        index_content = semantic_index_content(pages)
        index_action = "update" if index_path.exists() else "create"
        if write_if_changed(index_path, index_content):
            register_wiki_page(conn, wiki_rel(index_path), "semantic_topic_index", "Semantic Topic Index")
            append_wiki_log(f"{now_iso()} semantic_wiki_agent {index_action} {wiki_rel(index_path)} -- {len(pages)} semantic topics")
            changed += 1
        append_unique_line(project_path("wiki", "index.md"), "## Topics", "- [Semantic Topic Index](topics/semantic-index.md)")

        if args.prune_stale:
            selected = {page.path for page in pages}
            for stale_path in sorted(project_path("wiki", "topics", "semantic").glob("*.md")):
                if stale_path in selected:
                    continue
                rel = wiki_rel(stale_path)
                stale_path.unlink()
                conn.execute("DELETE FROM wiki_pages WHERE path = ?", (rel,))
                append_wiki_log(f"{now_iso()} semantic_wiki_agent delete {rel} -- stale generated semantic topic")
                changed += 1
            conn.commit()

    logger.info("action=semantic_wiki_agent status=ok topics=%s synthesized=%s changed=%s", len(pages), synthesized, changed)
    print(f"Semantic wiki topics: topics={len(pages)}, synthesized={synthesized}, changed={changed}")


if __name__ == "__main__":
    main()
