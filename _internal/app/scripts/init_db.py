#!/usr/bin/env python3
from __future__ import annotations

import _bootstrap  # noqa: F401

from astro_wiki.db import connect, init_db
from astro_wiki.config import project_path


def main() -> None:
    for path in [
        "data/metadata",
        "data/raw/arxiv",
        "data/text",
        "data/cache/arxiv",
        "wiki/papers",
        "wiki/daily",
        "wiki/interests",
        "wiki/topics",
        "conversations",
        "graphify-out",
        "logs",
        "reports",
    ]:
        project_path(path).mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        init_db(conn)
    print("Initialized data/metadata/papers.sqlite")


if __name__ == "__main__":
    main()
