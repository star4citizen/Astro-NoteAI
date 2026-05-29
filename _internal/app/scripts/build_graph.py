#!/usr/bin/env python3
from __future__ import annotations

import _bootstrap  # noqa: F401

from astro_wiki.db import connect, init_db, rows_for_status, update_paper_status
from astro_wiki.graphify import build_simple_graph
from astro_wiki.logging import append_wiki_log, get_agent_logger
from astro_wiki.wiki_io import now_iso


def main() -> None:
    logger = get_agent_logger("build_graph")
    graph = build_simple_graph()
    with connect() as conn:
        init_db(conn)
        for row in rows_for_status(conn, ["curated"], limit=None):
            update_paper_status(conn, row["id"], "graphed")
    append_wiki_log(f"{now_iso()} Graph build: {len(graph['nodes'])} nodes, {len(graph['edges'])} edges.")
    logger.info("action=build_graph status=ok nodes=%s edges=%s", len(graph["nodes"]), len(graph["edges"]))
    print(f"Graph built: nodes={len(graph['nodes'])}, edges={len(graph['edges'])}")


if __name__ == "__main__":
    main()
