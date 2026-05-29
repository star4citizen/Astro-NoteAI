from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from .config import project_path


def get_agent_logger(agent_name: str, target_date: str | None = None) -> logging.Logger:
    target_date = target_date or date.today().isoformat()
    log_dir = project_path("logs", target_date)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(agent_name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s\t%(name)s\t%(levelname)s\t%(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    file_handler = logging.FileHandler(log_dir / f"{agent_name}.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    return logger


def append_wiki_log(message: str, path: Path | None = None) -> None:
    path = path or project_path("wiki", "log.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n- {message}\n")
