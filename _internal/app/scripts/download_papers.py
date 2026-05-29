#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import date, datetime

import httpx

import _bootstrap  # noqa: F401

from astro_wiki.arxiv_client import polite_headers
from astro_wiki.config import load_yaml, project_path
from astro_wiki.db import connect, init_db, rows_for_status, update_paper_status
from astro_wiki.logging import get_agent_logger
from astro_wiki.wiki_io import safe_arxiv_filename


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    if value == "today":
        return date.today().isoformat()
    return datetime.fromisoformat(value).date().isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(description="Download PDFs for selected papers.")
    parser.add_argument("--date", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--arxiv-id", default=None)
    args = parser.parse_args()

    target_date = parse_date(args.date)
    logger = get_agent_logger("download_papers", target_date or date.today().isoformat())
    cfg = load_yaml("config/agents.yml").get("agents", {}).get("acquisition", {})
    overwrite = bool(cfg.get("overwrite_existing", False))
    request_timeout = float(cfg.get("request_timeout_seconds", 45))
    if not bool(cfg.get("download_pdfs", True)):
        print("PDF download disabled in config/agents.yml")
        return

    with connect() as conn:
        init_db(conn)
        rows = rows_for_status(conn, ["selected", "failed_download"], target_date=target_date, limit=args.limit, arxiv_id=args.arxiv_id)
        downloaded = skipped = failed = 0
        timeout = httpx.Timeout(request_timeout, connect=15.0)
        with httpx.Client(headers=polite_headers(), follow_redirects=True, timeout=timeout) as client:
            for row in rows:
                announced = row["announced_date"] or target_date or date.today().isoformat()
                out_path = project_path("data", "raw", "arxiv", announced, f"{safe_arxiv_filename(row['arxiv_id'])}.pdf")
                if out_path.exists() and not overwrite:
                    update_paper_status(conn, row["id"], "downloaded", pdf_path=str(out_path.relative_to(project_path())))
                    skipped += 1
                    continue
                out_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    logger.info("paper_id=%s action=download status=start arxiv_id=%s", row["id"], row["arxiv_id"])
                    response = client.get(row["pdf_url"])
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "")
                    if "pdf" not in content_type.lower() and not response.content.startswith(b"%PDF"):
                        raise RuntimeError(f"Unexpected content type: {content_type}")
                    out_path.write_bytes(response.content)
                    update_paper_status(conn, row["id"], "downloaded", pdf_path=str(out_path.relative_to(project_path())))
                    downloaded += 1
                except Exception as exc:
                    logger.exception("paper_id=%s action=download status=failed message=%s", row["id"], exc)
                    update_paper_status(conn, row["id"], "failed_download")
                    failed += 1
    logger.info("action=download_papers status=ok downloaded=%s skipped=%s failed=%s", downloaded, skipped, failed)
    print(f"PDFs: downloaded={downloaded}, skipped={skipped}, failed={failed}")


if __name__ == "__main__":
    main()
