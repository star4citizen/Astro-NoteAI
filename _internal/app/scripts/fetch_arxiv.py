#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import _bootstrap  # noqa: F401

from astro_wiki import arxiv_client
from astro_wiki.config import load_yaml
from astro_wiki.db import connect, get_state, init_db, set_state, upsert_paper
from astro_wiki.logging import get_agent_logger


def parse_date(value: str) -> date:
    if value == "today":
        return datetime.now(ZoneInfo("Asia/Seoul")).date()
    return date.fromisoformat(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch new or updated astro-ph metadata from arXiv.")
    parser.add_argument("--date", default="today", help="Target Asia/Seoul calendar date, or 'today'.")
    parser.add_argument("--from-date", default=None, help="Explicit OAI-PMH window start date, YYYY-MM-DD.")
    parser.add_argument("--until-date", default=None, help="Explicit OAI-PMH window end date, YYYY-MM-DD.")
    parser.add_argument("--max-results", type=int, default=None)
    parser.add_argument("--search-query", default="", help="Optional arXiv Atom search query. Empty uses configured default topic categories.")
    parser.add_argument("--fallback-atom", action="store_true", help="Use the Atom query API instead of OAI-PMH.")
    args = parser.parse_args()

    target_date = parse_date(args.date)
    logger = get_agent_logger("fetch_arxiv", target_date.isoformat())
    agents = load_yaml("config/agents.yml").get("agents", {})
    scout_cfg = agents.get("arxiv_scout", {})
    delay = float(scout_cfg.get("request_delay_seconds", 3))
    categories = list(scout_cfg.get("categories", ["astro-ph.GA", "astro-ph.CO", "astro-ph.IM"]))
    configured_max_results = scout_cfg.get("max_results", 200)
    atom_max_results = args.max_results or int(configured_max_results or 200)

    with connect() as conn:
        init_db(conn)
        started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        search_query = " ".join(args.search_query.split())
        if args.fallback_atom or search_query:
            logger.info("action=query_atom status=start search_query=%s", search_query or "<default categories>")
            papers = arxiv_client.query_atom(
                categories=categories,
                query=search_query or None,
                max_results=atom_max_results,
                request_delay_seconds=delay,
            )
            should_advance_oai_state = False
        else:
            if args.from_date or args.until_date:
                from_date = parse_date(args.from_date or args.date)
                until_date = parse_date(args.until_date or args.from_date or args.date)
            else:
                last_success = get_state(conn, "last_successful_oai_run_utc")
                from_date, until_date = arxiv_client.default_harvest_window(last_success, target_date)
            logger.info("action=harvest_oai status=start window=%s..%s", from_date, until_date)
            should_advance_oai_state = args.max_results is None
            try:
                papers = arxiv_client.harvest_oai(
                    from_date=from_date,
                    until_date=until_date,
                    request_delay_seconds=delay,
                    max_records=args.max_results,
                )
            except Exception as exc:
                logger.exception("action=harvest_oai status=failed message=%s", exc)
                logger.info("action=fallback_atom status=start")
                papers = arxiv_client.query_atom(categories=categories, max_results=atom_max_results, request_delay_seconds=delay)
                should_advance_oai_state = False
        count = 0
        for paper in papers:
            if not set(paper.get("categories", "").split()) & set(categories):
                continue
            upsert_paper(conn, paper)
            count += 1
        if should_advance_oai_state:
            set_state(conn, "last_successful_oai_run_utc", started_at)
    logger.info("action=fetch_arxiv status=ok count=%s", count)
    print(f"Fetched or updated {count} papers")


if __name__ == "__main__":
    main()
