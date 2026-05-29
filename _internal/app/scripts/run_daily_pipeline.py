#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import _bootstrap  # noqa: F401

from astro_wiki.config import project_path
from astro_wiki.logging import get_agent_logger


STAGES = [
    ("init_db", ["scripts/init_db.py"]),
    ("fetch_arxiv", ["scripts/fetch_arxiv.py"]),
    ("classify_papers", ["scripts/classify_papers.py"]),
    ("download_papers", ["scripts/download_papers.py"]),
    ("extract_text", ["scripts/extract_text.py"]),
    ("ingest_paper", ["scripts/ingest_paper.py"]),
    ("daily_digest", ["scripts/daily_digest.py"]),
    ("daily_newsletter", ["scripts/daily_newsletter.py"]),
    ("curate_wiki", ["scripts/curate_wiki.py"]),
    ("build_graph", ["scripts/build_graph.py"]),
    ("semantic_wiki_agent", ["scripts/semantic_wiki_agent.py", "--prune-stale"]),
    ("precompute_korean_summaries", ["scripts/precompute_korean_summaries.py"]),
    ("update_qmd_index", ["scripts/update_qmd_index.py"]),
]


ACTIVE_PROCESS_MARKERS = tuple(command[0] for _, command in STAGES) + ("scripts/run_daily_pipeline.py",)


def parse_date(value: str) -> str:
    if value == "today":
        return datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    return datetime.fromisoformat(value).date().isoformat()


def process_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore")


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def is_pipeline_process(pid: int) -> bool:
    if pid == os.getpid():
        return False
    if not Path("/proc").exists():
        return process_exists(pid)
    cmdline = process_cmdline(pid)
    return any(marker in cmdline for marker in ACTIVE_PROCESS_MARKERS)


def any_pipeline_process_running() -> bool:
    proc = Path("/proc")
    if not proc.exists():
        return False
    for child in proc.iterdir():
        if child.name.isdigit() and is_pipeline_process(int(child.name)):
            return True
    return False


def lock_pid(lock_path: Path) -> int | None:
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pid = data.get("pid")
    return int(pid) if isinstance(pid, int) else None


def acquire_lock(lock_path: Path, logger) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        pid = lock_pid(lock_path)
        if pid is not None and is_pipeline_process(pid):
            raise SystemExit(f"Pipeline lock exists: {lock_path}")
        if any_pipeline_process_running():
            raise SystemExit(f"Pipeline lock exists: {lock_path}")
        logger.warning("action=remove_stale_lock path=%s", lock_path)
        lock_path.unlink()
    payload = {
        "pid": os.getpid(),
        "cwd": str(Path.cwd()),
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    lock_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the daily astro-ph wiki pipeline.")
    parser.add_argument("--date", default="today")
    parser.add_argument("--limit", type=int, default=None, help="Optional per-stage limit for development.")
    parser.add_argument("--no-llm", action="store_true", help="Use deterministic fallbacks in LLM-backed stages.")
    parser.add_argument("--search-query", default="", help="Optional arXiv search query for the fetch stage. Empty uses default configured topics.")
    args = parser.parse_args()

    target_date = parse_date(args.date)
    logger = get_agent_logger("pipeline", target_date)
    lock_path = project_path("logs", target_date, "pipeline.lock")
    acquire_lock(lock_path, logger)
    try:
        for stage_name, command in STAGES:
            full_command = [sys.executable, *command]
            if stage_name not in {"init_db", "build_graph"}:
                full_command.extend(["--date", target_date])
            if args.limit and stage_name == "fetch_arxiv":
                full_command.extend(["--max-results", str(args.limit)])
            if args.search_query and stage_name == "fetch_arxiv":
                full_command.extend(["--search-query", args.search_query])
            elif args.limit and stage_name in {
                "classify_papers",
                "download_papers",
                "extract_text",
                "ingest_paper",
                "daily_digest",
                "daily_newsletter",
                "curate_wiki",
                "semantic_wiki_agent",
                "precompute_korean_summaries",
            }:
                full_command.extend(["--limit", str(args.limit)])
            if args.no_llm and stage_name in {
                "classify_papers",
                "ingest_paper",
                "daily_newsletter",
                "semantic_wiki_agent",
                "precompute_korean_summaries",
            }:
                full_command.append("--no-llm")
            logger.info("stage=%s action=start command=%s", stage_name, " ".join(full_command))
            result = subprocess.run(full_command, cwd=project_path(), text=True, capture_output=True)
            log_text = result.stdout + result.stderr
            project_path("logs", target_date, f"{stage_name}.pipeline.out").write_text(log_text, encoding="utf-8")
            if result.returncode != 0:
                logger.error("stage=%s status=failed returncode=%s", stage_name, result.returncode)
                raise SystemExit(result.returncode)
            logger.info("stage=%s status=ok", stage_name)
    finally:
        lock_path.unlink(missing_ok=True)
    print(f"Daily pipeline completed for {target_date}")


if __name__ == "__main__":
    main()
