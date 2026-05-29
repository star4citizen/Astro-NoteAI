#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import date, datetime

import _bootstrap  # noqa: F401

from astro_wiki.config import load_yaml, project_path
from astro_wiki.db import connect, init_db
from astro_wiki.logging import append_wiki_log, get_agent_logger
from astro_wiki.monthly_questions import (
    affected_paper_paths,
    build_question_pages,
    changed_paper_paths,
    existing_question_pages,
    kst_today,
    load_default_graph,
    month_window,
    render_index,
    write_if_changed,
)
from astro_wiki.wiki_io import now_iso, wiki_rel


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    if value == "today":
        return kst_today()
    return datetime.fromisoformat(value).date()


def main() -> None:
    parser = argparse.ArgumentParser(description="Incrementally maintain cross-paper research questions.")
    parser.add_argument("--month", default="previous", help="'previous' or YYYY-MM. Ignored when --since/--until are set.")
    parser.add_argument("--since", default=None, help="Inclusive YYYY-MM-DD changed-paper window start.")
    parser.add_argument("--until", default=None, help="Inclusive YYYY-MM-DD changed-paper window end.")
    parser.add_argument("--all", action="store_true", help="Consider all graph paper pages, still capped by --max-paper-pages.")
    parser.add_argument("--dry-run", action="store_true", help="Report planned changes without writing wiki/questions.")
    parser.add_argument("--max-paper-pages", type=int, default=None)
    parser.add_argument("--max-related-papers", type=int, default=None)
    parser.add_argument("--max-questions-per-paper", type=int, default=None)
    parser.add_argument("--min-shared-facets", type=int, default=None)
    args = parser.parse_args()

    agents = load_yaml("config/agents.yml").get("agents", {})
    cfg = agents.get("monthly_question_curator", {})
    max_paper_pages = args.max_paper_pages or int(cfg.get("max_paper_pages_per_run", 100))
    max_related = args.max_related_papers or int(cfg.get("max_related_papers", 5))
    max_questions = args.max_questions_per_paper or int(cfg.get("max_questions_per_paper", 5))
    min_shared = args.min_shared_facets or int(cfg.get("min_shared_facets", 2))
    excluded_topic_slugs = set(cfg.get("excluded_topic_slugs", []) or [])

    since = parse_date(args.since)
    until = parse_date(args.until)
    if since or until:
        if not since or not until:
            raise SystemExit("--since and --until must be provided together.")
        window_label = f"{since.isoformat()}..{until.isoformat()}"
        start_date, end_date = since, until
    else:
        window = month_window(args.month, today=kst_today())
        window_label = window.label
        start_date, end_date = window.start, window.end

    logger = get_agent_logger("monthly_question_curator", kst_today().isoformat())
    graph = load_default_graph()
    graph_papers = sorted(
        [
            node["id"]
            for node in graph.get("nodes", [])
            if isinstance(node.get("id"), str)
            and node["id"].startswith("wiki/papers/")
            and node["id"].endswith(".md")
            and not node["id"].endswith("-deep-summary.md")
        ],
        reverse=True,
    )

    with connect() as conn:
        init_db(conn)
        changed = graph_papers if args.all else changed_paper_paths(conn, start_date, end_date)

    affected = affected_paper_paths(
        changed,
        graph,
        max_pages=max_paper_pages,
        max_related_per_changed=max_related,
        min_shared_facets=min_shared,
        excluded_topic_slugs=excluded_topic_slugs,
    )
    pages = build_question_pages(
        graph,
        affected,
        month_label=window_label,
        max_related=max_related,
        max_questions=max_questions,
        min_shared_facets=min_shared,
        excluded_topic_slugs=excluded_topic_slugs,
    )

    if args.dry_run:
        print(
            "Monthly question curator dry-run: "
            f"window={window_label}, changed={len(changed)}, affected={len(affected)}, pages={len(pages)}"
        )
        for page in pages[:30]:
            print(f"- {page.arxiv_id}: {page.question_count} questions, {page.related_count} related papers")
        if len(pages) > 30:
            print(f"- ... {len(pages) - 30} additional pages omitted")
        return

    changed_pages = 0
    for page in pages:
        if write_if_changed(page.path, page.content):
            changed_pages += 1
            append_wiki_log(
                f"{now_iso()} monthly_question_curator update `{wiki_rel(page.path)}` {page.arxiv_id} "
                f"-- {page.question_count} cross-paper questions"
            )

    existing_pages = {page.arxiv_id: page for page in existing_question_pages()}
    existing_pages.update({page.arxiv_id: page for page in pages})
    index_path = project_path("wiki", "questions", "index.md")
    if write_if_changed(index_path, render_index(list(existing_pages.values()))):
        append_wiki_log(f"{now_iso()} monthly_question_curator update `{wiki_rel(index_path)}` -- question index")

    logger.info(
        "action=monthly_question_curator status=ok window=%s changed=%s affected=%s pages=%s updated=%s",
        window_label,
        len(changed),
        len(affected),
        len(pages),
        changed_pages,
    )
    print(
        "Monthly question curator: "
        f"window={window_label}, changed={len(changed)}, affected={len(affected)}, "
        f"pages={len(pages)}, updated={changed_pages}"
    )


if __name__ == "__main__":
    main()
