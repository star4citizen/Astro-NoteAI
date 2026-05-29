#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import date, datetime

import _bootstrap  # noqa: F401

from astro_wiki.config import project_path
from astro_wiki.db import connect, init_db, register_wiki_page, rows_for_status, update_paper_status
from astro_wiki.logging import append_wiki_log, get_agent_logger
from astro_wiki.wiki_io import append_unique_line, now_iso, wiki_rel


def profile_terms() -> set[str]:
    path = project_path("wiki", "interests", "profile.md")
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8", errors="ignore").lower()
    terms = set()
    for term in [
        "environment",
        "cluster",
        "group",
        "quenching",
        "star formation",
        "jwst",
        "nirspec",
        "nircam",
        "machine learning",
        "simulation",
        "photometric redshift",
        "metallicity",
        "morphology",
    ]:
        if term in text:
            terms.add(term)
    return terms


def parse_date(value: str | None) -> str:
    if not value or value == "today":
        return date.today().isoformat()
    return datetime.fromisoformat(value).date().isoformat()


def arxiv_sort_key(row) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in row["arxiv_id"].split("."))
    except (KeyError, ValueError):
        return (0,)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a daily digest for ingested papers.")
    parser.add_argument("--date", default="today")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--arxiv-id", default=None)
    args = parser.parse_args()

    target_date = None if args.arxiv_id else parse_date(args.date)
    logger = get_agent_logger("daily_digest", target_date or date.today().isoformat())
    with connect() as conn:
        init_db(conn)
        rows = rows_for_status(
            conn,
            ["ingested", "digested", "curated", "graphed", "deep_summarized"],
            target_date=target_date,
            limit=args.limit,
            arxiv_id=args.arxiv_id,
        )
        rows = sorted(rows, key=arxiv_sort_key, reverse=True)
        high_interest = []
        high_interest_seen = set()
        brief_notes = []
        method_terms = []
        interest_terms = profile_terms()
        interest_hits = []
        for row in rows:
            class_row = conn.execute(
                "SELECT topic, relevance_score, rationale FROM classifications WHERE paper_id = ?",
                (row["id"],),
            ).fetchone()
            topic = class_row["topic"] if class_row else "unknown"
            score = class_row["relevance_score"] if class_row else 0
            rationale = class_row["rationale"] if class_row else "No classification row."
            paper_path = f"../papers/{row['arxiv_id'].replace('/', '_')}.md"
            wiki_link = f"[wiki]({paper_path})"
            paper_link = f"[paper]({paper_path})"
            line = f"- **{row['title']}** ({wiki_link}; {paper_link}; {row['arxiv_id']}; {topic}). {rationale}"
            brief_notes.append(line)
            if score >= 4 or topic == "both":
                if row["arxiv_id"] not in high_interest_seen:
                    high_interest.append(line)
                    high_interest_seen.add(row["arxiv_id"])
            lower = f"{row['title']} {row['abstract']}".lower()
            matched_interest = sorted(term for term in interest_terms if term in lower)
            if matched_interest:
                if row["arxiv_id"] not in high_interest_seen:
                    high_interest.append(line)
                    high_interest_seen.add(row["arxiv_id"])
                interest_hits.append(f"- {row['arxiv_id']}: {', '.join(matched_interest)}")
            for term in ["jwst", "alma", "euclid", "desi", "photometric redshift", "diffusion", "transformer", "simulation"]:
                if term in lower and term not in method_terms:
                    method_terms.append(term)

        digest_date = target_date
        if not digest_date and rows:
            first = rows[0]
            digest_date = first["announced_date"] or str(first["published"] or "")[:10] or date.today().isoformat()
        digest_date = digest_date or date.today().isoformat()
        content = (
            "---\n"
            f"date: {digest_date}\n"
            f"paper_count: {len(rows)}\n"
            f"high_interest_count: {len(high_interest)}\n"
            f"created_at: {now_iso()}\n"
            "---\n\n"
            f"# Daily Astro-ph Digest: {digest_date}\n\n"
            "## Executive Summary\n\n"
            f"{len(rows)} selected papers were ingested for this date.\n\n"
            "## High-interest Papers\n\n"
            + ("\n".join(high_interest) if high_interest else "No high-interest papers crossed the current threshold.")
            + "\n\n## Brief Notes on All Selected Papers\n\n"
            + ("\n".join(brief_notes) if brief_notes else "No ingested papers for this date.")
            + "\n\n## Method / Data Highlights\n\n"
            + ("\n".join(f"- {term}" for term in method_terms) if method_terms else "No recurring method/data terms detected.")
            + "\n\n## Possible Follow-up Reading\n\n"
            "Review high-interest paper pages and source PDFs before adding synthesis claims.\n\n"
            "## Interest Signals Used\n\n"
            + (
                "\n".join(interest_hits)
                if interest_hits
                else "Configured topics and existing interest profile; no direct profile term hits detected."
            )
            + "\n"
        )
        digest_path = project_path("wiki", "daily", f"{digest_date}.md")
        digest_path.parent.mkdir(parents=True, exist_ok=True)
        digest_path.write_text(content, encoding="utf-8")
        register_wiki_page(conn, wiki_rel(digest_path), "daily_digest", f"Daily Astro-ph Digest: {digest_date}")
        for row in rows:
            update_paper_status(conn, row["id"], "digested")
        append_unique_line(
            project_path("wiki", "index.md"),
            "## Daily Digests",
            f"- [{digest_date}](daily/{digest_date}.md)",
            sort="date_desc",
        )
        append_wiki_log(f"{now_iso()} Daily digest created: `{wiki_rel(digest_path)}`")
    logger.info("action=daily_digest status=ok paper_count=%s high_interest_count=%s", len(rows), len(high_interest))
    print(f"Daily digest created for {digest_date}: {len(rows)} papers")


if __name__ == "__main__":
    main()
