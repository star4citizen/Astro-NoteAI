#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import _bootstrap  # noqa: F401

from astro_wiki.config import chat_model, load_yaml, project_path
from astro_wiki.db import connect, init_db
from astro_wiki.logging import append_wiki_log, get_agent_logger
from astro_wiki.wiki_io import now_iso
from ui_server import get_korean_paper_summary, get_paper, korean_summary_fallback, summary_cache_path


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    if value == "today":
        return datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    return datetime.fromisoformat(value).date().isoformat()


def write_fallback_summary(arxiv_id: str) -> None:
    paper_payload = get_paper(arxiv_id)
    paper = paper_payload["paper"]
    wiki_path = project_path("wiki", "papers", f"{arxiv_id.replace('/', '_')}.md")
    wiki_markdown = wiki_path.read_text(encoding="utf-8") if wiki_path.exists() else ""
    cache_path = summary_cache_path(arxiv_id)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(korean_summary_fallback(paper, wiki_markdown).rstrip() + "\n", encoding="utf-8")


def agent_config() -> dict:
    return load_yaml("config/agents.yml").get("agents", {}).get("korean_summary_precompute", {})


def graphed_rows(target_date: str | None, limit: int | None) -> list:
    where = ["status = ?"]
    params: list[str | int] = ["graphed"]
    if target_date:
        where.append("(announced_date = ? OR date(published) = ? OR date(updated) = ? OR date(created_at) = ?)")
        params.extend([target_date, target_date, target_date, target_date])
    sql = f"""
        SELECT arxiv_id, title, announced_date, published, updated_at
        FROM papers
        WHERE {' AND '.join(where)}
        ORDER BY updated_at DESC
    """
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with connect() as conn:
        init_db(conn)
        return list(conn.execute(sql, params))


def main() -> None:
    config = agent_config()
    parser = argparse.ArgumentParser(description="Precompute Korean summaries for graphed papers.")
    parser.add_argument("--date", default=None, help="Optional paper date filter, e.g. 2026-04-17.")
    parser.add_argument("--limit", type=int, default=config.get("max_papers_per_run"))
    parser.add_argument("--refresh", action="store_true", help="Regenerate summaries even when cache files exist.")
    parser.add_argument("--sleep-seconds", type=float, default=float(config.get("sleep_seconds", 2)))
    parser.add_argument("--model", default=config.get("model") or chat_model())
    parser.add_argument("--no-llm", action="store_true", help="Write deterministic fallback summaries instead of calling the LLM.")
    args = parser.parse_args()

    target_date = parse_date(args.date)
    logger = get_agent_logger("korean_summary_precompute", target_date or date.today().isoformat())
    rows = graphed_rows(target_date, args.limit)
    created = skipped = failed = 0
    for row in rows:
        arxiv_id = row["arxiv_id"]
        cache_path = summary_cache_path(arxiv_id)
        if cache_path.exists() and not args.refresh:
            skipped += 1
            logger.info("paper_id=%s action=skip status=cached path=%s", arxiv_id, cache_path)
            continue
        try:
            logger.info("paper_id=%s action=precompute_summary status=start model=%s", arxiv_id, args.model)
            if args.no_llm:
                write_fallback_summary(arxiv_id)
            else:
                get_korean_paper_summary(arxiv_id, refresh=args.refresh, model=args.model)
            created += 1
            append_wiki_log(f"{now_iso()} Korean summary precomputed: `{arxiv_id}` -> `{cache_path.relative_to(project_path())}`")
            logger.info("paper_id=%s action=precompute_summary status=ok path=%s", arxiv_id, cache_path)
        except Exception as exc:
            failed += 1
            logger.exception("paper_id=%s action=precompute_summary status=failed message=%s", arxiv_id, exc)
        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)
    print(f"Korean summaries: created={created}, skipped={skipped}, failed={failed}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
