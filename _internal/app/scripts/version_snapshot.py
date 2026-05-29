#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
from datetime import date

import _bootstrap  # noqa: F401

from astro_wiki.config import project_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a local git snapshot if this directory is a git repository.")
    parser.add_argument("--message", default=f"Daily astro wiki snapshot {date.today().isoformat()}")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = project_path()
    if not (root / ".git").exists():
        print("Not a git repository; snapshot skipped.")
        return
    commands = [
        ["git", "add", "config", "scripts", "src", "wiki", "conversations", "reports", "graphify-out/graph.json", "graphify-out/GRAPH_REPORT.md"],
        ["git", "commit", "-m", args.message],
    ]
    for command in commands:
        print(" ".join(command))
        if not args.dry_run:
            result = subprocess.run(command, cwd=root, text=True)
            if result.returncode != 0:
                raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
