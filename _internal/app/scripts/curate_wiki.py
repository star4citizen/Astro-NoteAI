#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import date, datetime

import _bootstrap  # noqa: F401

from astro_wiki.config import project_path
from astro_wiki.db import connect, init_db, rows_for_status, update_paper_status
from astro_wiki.logging import append_wiki_log, get_agent_logger
from astro_wiki.wiki_io import append_unique_line, now_iso


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    if value == "today":
        return date.today().isoformat()
    return datetime.fromisoformat(value).date().isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(description="Conservatively update topic pages from digested papers.")
    parser.add_argument("--date", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--arxiv-id", default=None)
    args = parser.parse_args()

    target_date = parse_date(args.date)
    logger = get_agent_logger("curate_wiki", target_date or date.today().isoformat())
    updated = 0
    with connect() as conn:
        init_db(conn)
        rows = rows_for_status(conn, ["digested"], target_date=target_date, limit=args.limit, arxiv_id=args.arxiv_id)
        for row in rows:
            class_row = conn.execute("SELECT topic, relevance_score FROM classifications WHERE paper_id = ?", (row["id"],)).fetchone()
            topic = class_row["topic"] if class_row else "unknown"
            page_name = None
            if topic in {"external_galaxy_evolution", "both"}:
                page_name = "external-galaxy-evolution.md"
                added = append_unique_line(
                    project_path("wiki", "topics", page_name),
                    "## Recent Papers",
                    f"- [{row['title']}](../papers/{row['arxiv_id'].replace('/', '_')}.md) ({row['arxiv_id']})",
                    sort="arxiv_desc",
                )
                updated += int(added)
            if topic in {"ml_in_astronomy", "both"}:
                page_name = "ml-in-astronomy.md"
                added = append_unique_line(
                    project_path("wiki", "topics", page_name),
                    "## Recent Papers",
                    f"- [{row['title']}](../papers/{row['arxiv_id'].replace('/', '_')}.md) ({row['arxiv_id']})",
                    sort="arxiv_desc",
                )
                updated += int(added)
            update_paper_status(conn, row["id"], "curated")
        append_wiki_log(f"{now_iso()} Wiki curator conservative update: {len(rows)} papers processed.")
    logger.info("action=curate_wiki status=ok processed=%s updated_lines=%s", len(rows), updated)
    print(f"Wiki curation processed={len(rows)}, updated_lines={updated}")


if __name__ == "__main__":
    main()
