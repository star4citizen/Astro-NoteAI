#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import date, datetime

import _bootstrap  # noqa: F401

from astro_wiki.config import load_yaml, project_path
from astro_wiki.db import connect, init_db, rows_for_status, update_paper_status
from astro_wiki.logging import get_agent_logger
from astro_wiki.pdf_text import extract_pdf_text
from astro_wiki.wiki_io import safe_arxiv_filename


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    if value == "today":
        return date.today().isoformat()
    return datetime.fromisoformat(value).date().isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract text from downloaded PDFs.")
    parser.add_argument("--date", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--arxiv-id", default=None)
    args = parser.parse_args()

    target_date = parse_date(args.date)
    logger = get_agent_logger("extract_text", target_date or date.today().isoformat())
    cfg = load_yaml("config/agents.yml").get("agents", {}).get("text_extraction", {})
    min_chars = int(cfg.get("min_text_chars", 5000))
    extracted = failed = 0
    with connect() as conn:
        init_db(conn)
        rows = rows_for_status(conn, ["downloaded"], target_date=target_date, limit=args.limit, arxiv_id=args.arxiv_id)
        for row in rows:
            if not row["pdf_path"]:
                update_paper_status(conn, row["id"], "failed_text_extraction")
                failed += 1
                continue
            pdf_path = project_path(row["pdf_path"])
            text_path = project_path("data", "text", f"{safe_arxiv_filename(row['arxiv_id'])}.txt")
            try:
                text = extract_pdf_text(pdf_path)
                if len(text) < min_chars:
                    raise RuntimeError(f"Extracted text too short: {len(text)} chars")
                text_path.parent.mkdir(parents=True, exist_ok=True)
                text_path.write_text(text, encoding="utf-8")
                update_paper_status(conn, row["id"], "text_extracted", text_path=str(text_path.relative_to(project_path())))
                extracted += 1
            except Exception as exc:
                logger.exception("paper_id=%s action=extract_text status=failed message=%s", row["id"], exc)
                update_paper_status(conn, row["id"], "failed_text_extraction")
                failed += 1
    logger.info("action=extract_text status=ok extracted=%s failed=%s", extracted, failed)
    print(f"Text extraction: extracted={extracted}, failed={failed}")


if __name__ == "__main__":
    main()
