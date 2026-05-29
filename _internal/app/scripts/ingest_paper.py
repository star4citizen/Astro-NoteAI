#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime

import _bootstrap  # noqa: F401

from astro_wiki.config import chat_model, llm_provider, load_yaml, project_path
from astro_wiki.db import connect, init_db, register_wiki_page, rows_for_status, update_paper_status
from astro_wiki.logging import append_wiki_log, get_agent_logger
from astro_wiki.ollama_client import chat
from astro_wiki.wiki_ingest import (
    improve_paper_body,
    link_source_citations,
    normalize_chunk_references,
    structural_validation_errors,
)
from astro_wiki.wiki_io import append_unique_line, now_iso, paper_page_path, wiki_rel


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    if value == "today":
        return date.today().isoformat()
    return datetime.fromisoformat(value).date().isoformat()


def fallback_summary(row, text: str) -> str:
    abstract = row["abstract"].strip()
    first = " ".join(text[:2200].split())
    return (
        "## Scientific Question\n\n"
        "To be reviewed from the source text.\n\n"
        "## Data\n\n"
        "Not extracted by the fallback summarizer.\n\n"
        "## Method\n\n"
        "Not extracted by the fallback summarizer.\n\n"
        "## Main Results\n\n"
        f"{abstract}\n\n"
        "## Limitations\n\n"
        "Requires manual or LLM review.\n\n"
        "## Follow-up Questions\n\n"
        "- Which figures and tables contain the main quantitative evidence?\n\n"
        "## Source Text Excerpt\n\n"
        f"{first}\n"
    )


def summarize(row, text: str, model: str, *, allow_fallback: bool = True, max_chars: int = 24000) -> str:
    prompt = project_path("config", "prompts", "summarize_paper.md").read_text(encoding="utf-8")
    content = (
        f"{prompt}\n\n"
        f"Paper ID: {row['arxiv_id']}\n"
        f"Title: {row['title']}\n"
        f"Abstract: {row['abstract']}\n\n"
        "Formatting requirements:\n"
        "- Do not add a second top-level title.\n"
        "- Use plain Markdown headings only; do not use emoji.\n"
        "- Include these sections exactly: ## Scientific Question, ## Data, ## Method, ## Main Results, ## Limitations, ## Follow-up Questions.\n"
        "- Keep the summary concise and source-grounded.\n\n"
        f"Paper text excerpt:\n{text[:max_chars]}"
    )
    try:
        return normalize_summary_markdown(
            chat([{"role": "user", "content": content}], model=model, options={"num_predict": 900})
        )
    except Exception:
        if not allow_fallback:
            raise
        return fallback_summary(row, text)


def normalize_summary_markdown(markdown: str) -> str:
    replacements = {
        "## 🔬 Scientific Question": "## Scientific Question",
        "## 📊 Data": "## Data",
        "## ⚙️ Method": "## Method",
        "## 🎯 Main Results": "## Main Results",
        "## 🚧 Limitations": "## Limitations",
        "## ❓ Follow-up Questions": "## Follow-up Questions",
    }
    for old, new in replacements.items():
        markdown = markdown.replace(old, new)
    markdown = re.sub(r"^# .+\n+", "", markdown, count=1, flags=re.MULTILINE)
    return markdown.strip()


FALLBACK_MARKERS = (
    "To be reviewed from the source text.",
    "Not extracted by the fallback summarizer.",
    "Requires manual or LLM review.",
    "fallback summarizer",
)


def compact_error(value: str, *, limit: int = 1200) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(r"([?&]key=)[^&\s]+", r"\1<redacted>", text, flags=re.IGNORECASE)
    text = re.sub(r"(api[_-]?key['\"]?\s*[:=]\s*['\"]?)[^'\"\s,]+", r"\1<redacted>", text, flags=re.IGNORECASE)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def is_limit_error(value: str) -> bool:
    lowered = str(value or "").lower()
    return any(
        marker in lowered
        for marker in (
            "429",
            "quota",
            "rate limit",
            "resource_exhausted",
            "usage limit",
            "token limit",
            "tokens",
            "토큰",
            "할당량",
        )
    )


def require_llm_failure_message(result) -> str:
    provider = llm_provider().lower()
    reason = compact_error(getattr(result, "generation_error", "") or "unknown LLM failure")
    if provider == "gemini" and is_limit_error(reason):
        return (
            "Gemini API 토큰 제한으로 wiki 생성이 불가능합니다. "
            "더 낮은 모델로 시도해 보세요. "
            "LLM Settings에서 Gemini 모델을 gemini-2.5-flash-lite, "
            "gemini-2.0-flash-lite 또는 gemini-1.5-flash로 낮춘 뒤 다시 업로드하세요. "
            f"generation_mode={result.generation_mode}; reason={reason}"
        )
    return (
        "LLM wiki generation did not complete; "
        f"generation_mode={result.generation_mode}; reason={reason}"
    )


def row_value(row, key: str, default: str = ""):
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        value = getattr(row, key, default)
    return default if value is None else value


def paper_page_path_from_row(row):
    return paper_page_path(
        str(row_value(row, "arxiv_id")),
        title=row_value(row, "title"),
        authors=row_value(row, "authors_json"),
        year=row_value(row, "published") or row_value(row, "announced_date") or row_value(row, "updated"),
    )


def paper_page_candidates(arxiv_id: str, row=None, conn=None) -> list:
    candidates = []
    if conn is not None:
        for page in conn.execute(
            """
            SELECT path
            FROM wiki_pages
            WHERE arxiv_id = ? AND page_type = 'paper'
            ORDER BY updated_at DESC
            """,
            (arxiv_id,),
        ):
            if page["path"]:
                candidates.append(project_path(page["path"]))
    if row is not None:
        candidates.append(paper_page_path_from_row(row))
    candidates.append(paper_page_path(arxiv_id))

    deduped = []
    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            deduped.append(candidate)
            seen.add(key)
    return deduped


def existing_paper_page(arxiv_id: str, row=None, conn=None):
    candidates = paper_page_candidates(arxiv_id, row=row, conn=conn)
    for page in candidates:
        if page.exists():
            return page
    return candidates[0]


def is_fallback_page(row_or_arxiv_id, conn=None) -> bool:
    row = None if isinstance(row_or_arxiv_id, str) else row_or_arxiv_id
    arxiv_id = row_or_arxiv_id if row is None else str(row_value(row, "arxiv_id"))
    page = existing_paper_page(arxiv_id, row=row, conn=conn)
    if not page.exists():
        return False
    text = page.read_text(encoding="utf-8", errors="ignore")
    return any(marker in text for marker in FALLBACK_MARKERS)


def is_stale_page(row_or_arxiv_id, conn=None) -> bool:
    row = None if isinstance(row_or_arxiv_id, str) else row_or_arxiv_id
    arxiv_id = row_or_arxiv_id if row is None else str(row_value(row, "arxiv_id"))
    page = existing_paper_page(arxiv_id, row=row, conn=conn)
    if not page.exists():
        return True
    text = page.read_text(encoding="utf-8", errors="ignore")
    if 'ingest_method: "llm_map_reduce"' not in text:
        return True
    if "Generation Error" in text or "map_llm_reduce_fallback" in text:
        return True
    if re.search(r"\bchunk\s+\d+\b", text, flags=re.IGNORECASE):
        return True
    if "existing wiki excerpt" in text:
        return True
    if f"arXiv:{arxiv_id}" in text or f"(arXiv:{arxiv_id})" in text:
        return True
    if structural_validation_errors(text):
        return True
    return False


def rel_path(path) -> str:
    try:
        return str(path.relative_to(project_path())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def ingest_details_markdown(result) -> str:
    validation = result.validation
    validation_lines = []
    if validation:
        validation_lines.extend(
            [
                f"- Numeric tokens checked: {validation.checked_count}",
                f"- Numeric validation warnings: {validation.warning_count}",
            ]
        )
        if result.validation_markdown_path:
            validation_lines.append(f"- Validation report: `{rel_path(result.validation_markdown_path)}`")
    else:
        validation_lines.append("- Numeric validation: disabled")
    return (
        "## Ingest Details\n\n"
        f"- Generation mode: `{result.generation_mode}`\n"
        f"- Source type: `{result.source_type}`\n"
        f"- Source material: `{rel_path(result.source_material_path)}`\n"
        f"- Source chunks: {result.chunk_count}\n"
        f"- Map model: `{result.map_model}`\n"
        f"- Reduce model: `{result.reduce_model}`\n"
        f"- Chunk evidence cache: `{rel_path(result.chunk_cache_dir)}`\n"
        + "\n".join(validation_lines)
        + "\n\n"
    )


def rows_for_refresh(
    conn,
    target_date: str | None,
    until_date: str | None,
    limit: int | None,
    arxiv_id: str | None,
    fallback_only: bool,
    stale_only: bool,
):
    where = ["text_path IS NOT NULL", "status IN ('ingested', 'digested', 'curated', 'graphed', 'deep_summarized')"]
    params: list[str] = []
    if target_date:
        where.append(
            "("
            "announced_date = ? OR date(published) = ? OR date(updated) = ? "
            "OR date(created_at) = ? OR date(updated_at) = ? "
            "OR date(created_at, '+9 hours') = ? OR date(updated_at, '+9 hours') = ?"
            ")"
        )
        params.extend([target_date] * 7)
    if until_date:
        where.append(
            "COALESCE(announced_date, date(published), date(updated), date(created_at), "
            "date(updated_at), date(created_at, '+9 hours'), date(updated_at, '+9 hours')) <= ?"
        )
        params.append(until_date)
    if arxiv_id:
        where.append("arxiv_id = ?")
        params.append(arxiv_id)
    sql = f"SELECT * FROM papers WHERE {' AND '.join(where)} ORDER BY updated DESC"
    rows = list(conn.execute(sql, params))
    if fallback_only:
        rows = [row for row in rows if is_fallback_page(row, conn)]
    if stale_only:
        rows = [row for row in rows if is_stale_page(row, conn)]
    if limit:
        rows = rows[:limit]
    return rows


def load_cached_extracts_from_page(markdown: str) -> list[dict]:
    match = re.search(r"- Chunk evidence cache: `([^`]+)`", markdown)
    if not match:
        return []
    cache_dir = project_path(match.group(1))
    if not cache_dir.exists():
        return []
    extracts = []
    for path in sorted(cache_dir.glob("*.json")):
        try:
            extracts.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return extracts


def source_material_path_from_page(markdown: str):
    match = re.search(r"^- Source material:\s*`([^`]+)`", markdown, flags=re.MULTILINE)
    if not match:
        return None
    return project_path(match.group(1))


def normalize_existing_page_citations(page) -> bool:
    markdown = page.read_text(encoding="utf-8", errors="ignore")
    extracts = load_cached_extracts_from_page(markdown)
    if not extracts:
        return False
    normalized = normalize_chunk_references(markdown, extracts)
    normalized = link_source_citations(normalized, extracts, source_material_path_from_page(markdown))
    if normalized == markdown:
        return False
    page.write_text(normalized, encoding="utf-8")
    return True


def normalize_existing_citations(arxiv_id: str | None, limit: int | None, logger) -> None:
    if arxiv_id:
        with connect() as conn:
            init_db(conn)
            row = conn.execute(
                """
                SELECT *
                FROM papers
                WHERE arxiv_id = ?
                ORDER BY version DESC
                LIMIT 1
                """,
                (arxiv_id,),
            ).fetchone()
            pages = [page for page in paper_page_candidates(arxiv_id, row=row, conn=conn) if page.exists()]
            if not pages:
                pages = [existing_paper_page(arxiv_id, row=row, conn=conn)]
    else:
        pages = sorted(project_path("wiki", "papers").glob("*.md"))
    if limit:
        pages = pages[:limit]
    changed = skipped = 0
    for page in pages:
        if not page.exists():
            skipped += 1
            continue
        if normalize_existing_page_citations(page):
            rel = wiki_rel(page)
            append_wiki_log(f"{now_iso()} Paper citation normalize: `{rel}`")
            changed += 1
            logger.info("action=normalize_citations status=updated path=%s", rel)
        else:
            skipped += 1
    logger.info("action=normalize_citations status=ok changed=%s skipped=%s", changed, skipped)
    print(f"Paper citation normalization: changed={changed}, skipped={skipped}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create source-grounded paper wiki pages.")
    parser.add_argument("--date", default=None)
    parser.add_argument("--until-date", default=None, help="With --refresh-existing, rewrite papers on or before this date.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--require-llm", action="store_true", help="Fail instead of writing fallback/non-LLM wiki output.")
    parser.add_argument("--arxiv-id", default=None)
    parser.add_argument("--refresh-existing", action="store_true", help="Rewrite existing paper pages without changing processed statuses.")
    parser.add_argument("--fallback-only", action="store_true", help="With --refresh-existing, only rewrite pages that contain fallback markers.")
    parser.add_argument("--stale-only", action="store_true", help="With --refresh-existing, only rewrite pages missing the upgraded ingest output.")
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-chars", type=int, default=24000, help="Maximum extracted-text characters to send to the LLM.")
    parser.add_argument("--legacy-single-call", action="store_true", help="Use the old single-call excerpt summarizer.")
    parser.add_argument("--source", choices=["auto", "markdown", "text"], default=None, help="Source used by the map-reduce ingester.")
    parser.add_argument("--map-model", default=None, help="Model used for per-chunk evidence extraction.")
    parser.add_argument("--reduce-model", default=None, help="Model used for final wiki composition.")
    parser.add_argument("--max-chunk-chars", type=int, default=None)
    parser.add_argument("--max-chunks", type=int, default=None)
    parser.add_argument("--parallel-map", type=int, default=None)
    parser.add_argument("--reduce-input-max-chars", type=int, default=None)
    parser.add_argument("--reduce-num-predict", type=int, default=None)
    parser.add_argument("--reduce-timeout", type=float, default=None)
    parser.add_argument("--reduce-retries", type=int, default=None)
    parser.add_argument("--no-numeric-validation", action="store_true")
    parser.add_argument(
        "--normalize-citations-only",
        action="store_true",
        help="Rewrite existing paper pages to replace internal chunk references with section/page labels.",
    )
    args = parser.parse_args()
    if args.require_llm and args.no_llm:
        raise RuntimeError("--require-llm cannot be used with --no-llm")

    target_date = parse_date(args.date)
    until_date = parse_date(args.until_date)
    logger = get_agent_logger("ingest_paper", target_date or date.today().isoformat())
    if args.normalize_citations_only:
        normalize_existing_citations(args.arxiv_id, args.limit, logger)
        return

    agents = load_yaml("config/agents.yml").get("agents", {})
    cfg = agents.get("paper_ingest", {})
    base_model = args.model or cfg.get("model") or chat_model()
    map_model = args.map_model or cfg.get("map_model") or base_model
    reduce_model = args.reduce_model or cfg.get("reduce_model") or base_model
    source = args.source or cfg.get("source") or "auto"
    max_chunk_chars = args.max_chunk_chars or int(cfg.get("max_chunk_chars", 9000))
    max_chunks = args.max_chunks if args.max_chunks is not None else cfg.get("max_chunks")
    parallel_map = args.parallel_map or int(cfg.get("parallel_map", 4))
    reduce_input_max_chars = args.reduce_input_max_chars or int(cfg.get("reduce_input_max_chars", 120000))
    reduce_num_predict = args.reduce_num_predict or int(cfg.get("reduce_num_predict", 4096))
    reduce_timeout = args.reduce_timeout or float(cfg.get("reduce_timeout_seconds", 600))
    reduce_retries = args.reduce_retries if args.reduce_retries is not None else int(cfg.get("reduce_retries", 1))
    validate_numeric = (not args.no_numeric_validation) and bool(cfg.get("numeric_validation", True))
    ingested = failed = 0
    with connect() as conn:
        init_db(conn)
        if args.refresh_existing:
            rows = rows_for_refresh(
                conn,
                target_date,
                until_date,
                args.limit,
                args.arxiv_id,
                args.fallback_only,
                args.stale_only,
            )
        else:
            rows = rows_for_status(conn, ["text_extracted"], target_date=target_date, limit=args.limit, arxiv_id=args.arxiv_id)
        for row in rows:
            try:
                text = project_path(row["text_path"]).read_text(encoding="utf-8", errors="ignore")
                ingest_result = None
                if args.legacy_single_call:
                    if args.require_llm:
                        raise RuntimeError("--require-llm requires the map-reduce ingester")
                    body = (
                        fallback_summary(row, text)
                        if args.no_llm
                        else summarize(row, text, base_model, allow_fallback=not args.refresh_existing, max_chars=args.max_chars)
                    )
                    ingest_method = "legacy_single_call"
                    ingest_details = ""
                    validation_warnings = ""
                else:
                    ingest_result = improve_paper_body(
                        row,
                        source=source,
                        map_model=map_model,
                        reduce_model=reduce_model,
                        use_llm=not args.no_llm,
                        max_chunk_chars=max_chunk_chars,
                        max_chunks=max_chunks,
                        parallel_map=parallel_map,
                        validate_numeric=validate_numeric,
                        reduce_input_max_chars=reduce_input_max_chars,
                        reduce_num_predict=reduce_num_predict,
                        reduce_timeout=reduce_timeout,
                        reduce_retries=reduce_retries,
                    )
                    if args.require_llm and ingest_result.generation_mode != "llm_map_reduce":
                        raise RuntimeError(require_llm_failure_message(ingest_result))
                    body = ingest_result.body
                    ingest_method = ingest_result.generation_mode
                    ingest_details = ingest_details_markdown(ingest_result)
                    validation_warnings = (
                        str(ingest_result.validation.warning_count)
                        if ingest_result.validation
                        else ""
                    )
                authors = json.loads(row["authors_json"])
                page = paper_page_path(
                    row["arxiv_id"],
                    title=row["title"],
                    authors=authors,
                    year=row["published"] or row["announced_date"] or row["updated"],
                )
                legacy_page = paper_page_path(row["arxiv_id"])
                if legacy_page != page and legacy_page.exists():
                    legacy_page.unlink()
                source_line = (
                    f"- Source URL: [{row['abs_url']}]({row['abs_url']})\n"
                    if str(row["abs_url"] or "").startswith("http")
                    else ""
                )
                pdf_path = str(row["pdf_path"] or "").strip()
                pdf_line = f"- PDF local path: `{pdf_path}`\n" if pdf_path else ""
                frontmatter = (
                    "---\n"
                    f"title: \"{row['title'].replace(chr(34), chr(39))}\"\n"
                    "page_type: paper\n"
                    f"arxiv_id: \"{row['arxiv_id']}\"\n"
                    f"version: {row['version']}\n"
                    f"created_at: \"{now_iso()}\"\n"
                    "llm_generated: true\n"
                    f"ingest_method: \"{ingest_method}\"\n"
                )
                if validation_warnings != "":
                    frontmatter += f"numeric_validation_warnings: {validation_warnings}\n"
                frontmatter += "---\n\n"
                content = frontmatter + (
                    f"# {row['title']}\n\n"
                    f"- Paper ID: {row['arxiv_id']}\n"
                    f"{source_line}"
                    f"- Version: v{row['version']}\n"
                    f"- Categories: {row['categories']}\n"
                    f"- Authors: {', '.join(authors[:12])}{' et al.' if len(authors) > 12 else ''}\n"
                    f"{pdf_line}"
                    f"- Text local path: `{row['text_path']}`\n\n"
                    "## UI Links\n\n"
                    f"- [Open this paper in the UI](/?paper={row['arxiv_id']})\n"
                    f"- [Chat about this paper](/?chatPaper={row['arxiv_id']})\n\n"
                    "## Source Abstract\n\n"
                    f"{row['abstract']}\n\n"
                    f"{ingest_details}"
                    f"{body}\n"
                )
                page.parent.mkdir(parents=True, exist_ok=True)
                page.write_text(content.rstrip() + "\n", encoding="utf-8")
                rel = wiki_rel(page)
                conn.execute(
                    "DELETE FROM wiki_pages WHERE arxiv_id = ? AND path != ? AND page_type = 'paper'",
                    (row["arxiv_id"], rel),
                )
                register_wiki_page(conn, rel, "paper", row["title"], row["arxiv_id"])
                if not args.refresh_existing:
                    update_paper_status(conn, row["id"], "ingested")
                append_unique_line(
                    project_path("wiki", "index.md"),
                    "## Recent Papers",
                    f"- [{row['title']}](papers/{page.name}) ({row['arxiv_id']})",
                    sort="arxiv_desc",
                )
                validation_note = ""
                if ingest_result and ingest_result.validation:
                    validation_note = f", numeric warnings={ingest_result.validation.warning_count}"
                append_wiki_log(f"{now_iso()} Paper ingest: `{row['arxiv_id']}` -> `{rel}` ({ingest_method}{validation_note})")
                ingested += 1
            except Exception as exc:
                logger.exception("paper_id=%s action=ingest status=failed message=%s", row["id"], exc)
                if not args.refresh_existing:
                    update_paper_status(conn, row["id"], "failed_ingest")
                failed += 1
    logger.info("action=ingest_paper status=ok ingested=%s failed=%s", ingested, failed)
    print(f"Paper ingest: ingested={ingested}, failed={failed}")
    if failed and (args.require_llm or args.arxiv_id):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
