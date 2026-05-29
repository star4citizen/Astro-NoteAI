#!/usr/bin/env python3
from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from astro_wiki.config import chat_model, load_yaml, project_path
from astro_wiki.db import connect, init_db, register_wiki_page
from astro_wiki.logging import append_wiki_log, get_agent_logger
from astro_wiki.newsletter import (
    build_llm_context,
    load_paper_briefs,
    newsletter_path,
    paper_ids_from_db,
    paper_ids_from_digest,
    parse_target_date,
    render_fallback_newsletter,
    source_digest_path,
    wrap_newsletter_frontmatter,
)
from astro_wiki.ollama_client import chat
from astro_wiki.wiki_io import append_unique_line, now_iso, wiki_rel


DEFAULT_PROMPT = """Write a Korean astro-ph daily newsletter from the provided local wiki context.

Requirements:
- Title must be '# Astro-ph Research Brief - {date}'.
- Keep the style readable for a research group newsletter: concise, current, and editorial.
- Start from the scientific flow of the day, not a plain paper list.
- Every concrete scientific claim must include an inline paper ID, for example '(2605.15145)' or a local paper ID.
- Use Markdown links for paper pages, for example '[2605.xxxxx](../papers/2605.xxxxx.md)'.
- Do not invent details that are not supported by the provided excerpts.
- Include exactly these sections:
  1. ## 오늘의 주요 뉴스
  2. ## Editor's Picks
  3. ## 한눈에 보는 오늘의 논문 지도
  4. ## Methods / Data Watch
  5. ## 고민해 볼 만한 질문들
"""


def prompt_template() -> str:
    path = project_path("config", "prompts", "daily_newsletter.md")
    if path.exists():
        return path.read_text(encoding="utf-8")
    return DEFAULT_PROMPT


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a Korean newsletter-style daily astro-ph brief.")
    parser.add_argument("--date", default="today")
    parser.add_argument("--limit", type=int, default=None, help="Maximum papers to include in the newsletter context.")
    parser.add_argument("--no-llm", action="store_true", help="Use a deterministic fallback newsletter.")
    args = parser.parse_args()

    newsletter_date = parse_target_date(args.date)
    logger = get_agent_logger("daily_newsletter", newsletter_date)
    agents = load_yaml("config/agents.yml").get("agents", {})
    cfg = agents.get("daily_newsletter", {})
    max_papers = args.limit or int(cfg.get("max_papers", 10))
    model = str(cfg.get("model") or chat_model())
    timeout = int(cfg.get("timeout_seconds", 600))
    max_tokens = int(cfg.get("max_tokens", 3200))
    temperature = float(cfg.get("temperature", 0.25))

    digest_path = source_digest_path(newsletter_date)
    digest_markdown = digest_path.read_text(encoding="utf-8", errors="ignore") if digest_path.exists() else ""
    arxiv_ids = paper_ids_from_digest(digest_markdown, limit=max_papers)
    with connect() as conn:
        init_db(conn)
        if not arxiv_ids:
            arxiv_ids = paper_ids_from_db(conn, newsletter_date, limit=max_papers)
        briefs = load_paper_briefs(arxiv_ids)
        if args.no_llm:
            content = render_fallback_newsletter(newsletter_date, briefs)
            llm_generated = False
        else:
            context = build_llm_context(digest_markdown, briefs)
            template = prompt_template().format(date=newsletter_date)
            content = chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are an expert astrophysics newsletter editor. "
                            "Write in Korean and use only the provided local wiki context."
                        ),
                    },
                    {"role": "user", "content": f"{template}\n\nLocal wiki context:\n{context}"},
                ],
                model=model,
                timeout=timeout,
                options={"temperature": temperature, "num_predict": max_tokens},
            )
            llm_generated = True

        output = wrap_newsletter_frontmatter(
            content,
            newsletter_date=newsletter_date,
            paper_count=len(briefs),
            llm_generated=llm_generated,
            model=model,
            created_at=now_iso(),
        )
        out_path = newsletter_path(newsletter_date)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output, encoding="utf-8")
        register_wiki_page(conn, wiki_rel(out_path), "newsletter", f"Astro-ph Research Brief: {newsletter_date}")
        append_unique_line(
            project_path("wiki", "index.md"),
            "## Newsletters",
            f"- [{newsletter_date}](newsletters/{newsletter_date}.md)",
            sort="date_desc",
        )
        append_wiki_log(
            f"{now_iso()} daily_newsletter create `{wiki_rel(out_path)}` -- {len(briefs)} papers, llm={llm_generated}"
        )

    logger.info(
        "action=daily_newsletter status=ok date=%s paper_count=%s llm=%s",
        newsletter_date,
        len(briefs),
        llm_generated,
    )
    print(f"Daily newsletter created for {newsletter_date}: {len(briefs)} papers -> {wiki_rel(out_path)}")


if __name__ == "__main__":
    main()
