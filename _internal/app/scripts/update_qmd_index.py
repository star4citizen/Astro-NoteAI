#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import _bootstrap  # noqa: F401

from astro_wiki.logging import get_agent_logger
from astro_wiki.qmd_client import configured_collections, qmd_available, qmd_base_command, qmd_config, qmd_enabled


def parse_date(value: str) -> str:
    if value == "today":
        return datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    return datetime.fromisoformat(value).date().isoformat()


def run_qmd(args: list[str], timeout: float = 300.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*qmd_base_command(), *args],
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def existing_collection_names() -> set[str]:
    result = run_qmd(["collection", "list"], timeout=30.0)
    if result.returncode != 0:
        return set()
    names: set[str] = set()
    for line in result.stdout.splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)\s+\(qmd://", line.strip())
        if match:
            names.add(match.group(1))
    return names


def main() -> None:
    parser = argparse.ArgumentParser(description="Create/update the optional QMD/K-QMD retrieval index.")
    parser.add_argument("--date", default="today")
    args = parser.parse_args()

    target_date = parse_date(args.date)
    logger = get_agent_logger("update_qmd_index", target_date)
    cfg = qmd_config()
    if not qmd_enabled():
        logger.info("status=skipped reason=qmd_disabled")
        print("QMD retrieval is disabled.")
        return
    if not qmd_available():
        logger.warning("status=skipped reason=qmd_command_unavailable")
        print("QMD command is unavailable; install K-QMD with `npm install -g kqmd`.")
        return

    required = bool(cfg.get("required", False))
    try:
        existing = existing_collection_names()
        for collection in configured_collections():
            if collection["name"] in existing:
                continue
            command = [
                "collection",
                "add",
                collection["path"],
                "--name",
                collection["name"],
                "--mask",
                collection["mask"],
            ]
            result = run_qmd(command, timeout=120.0)
            if result.returncode != 0:
                message = (result.stdout + result.stderr).strip()
                logger.warning("collection=%s status=failed message=%s", collection["name"], message)
                if required:
                    raise SystemExit(result.returncode)
            else:
                logger.info("collection=%s status=created", collection["name"])
        result = run_qmd(["update"], timeout=float(cfg.get("update_timeout_seconds", 600)))
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            logger.warning("status=failed_update returncode=%s output=%s", result.returncode, output)
            if required:
                raise SystemExit(result.returncode)
            print(output or "QMD update failed, but retrieval will fall back to built-in search.")
            return
        logger.info("status=ok output=%s", output)
        print(output or "QMD index updated.")
    except subprocess.TimeoutExpired as exc:
        logger.warning("status=timeout command=%s", " ".join(exc.cmd) if exc.cmd else "qmd")
        if required:
            raise SystemExit(124)
        print("QMD update timed out, but retrieval will fall back to built-in search.")


if __name__ == "__main__":
    main()
