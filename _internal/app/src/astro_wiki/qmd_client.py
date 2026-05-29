from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from .config import load_yaml, project_path


@dataclass(frozen=True)
class QmdHit:
    path: str
    score: float
    snippet: str
    title: str = ""
    raw_file: str = ""


def qmd_config() -> dict:
    agents = load_yaml("config/agents.yml").get("agents", {})
    research_chat = agents.get("research_chat", {})
    qmd = research_chat.get("qmd", {})
    return qmd if isinstance(qmd, dict) else {}


def qmd_enabled() -> bool:
    return bool(qmd_config().get("enabled", False))


def qmd_command() -> list[str]:
    configured = os.getenv("ASTRO_WIKI_QMD_COMMAND") or str(qmd_config().get("command", "qmd"))
    return shlex.split(configured)


def qmd_available() -> bool:
    command = qmd_command()
    return bool(command) and shutil.which(command[0]) is not None


def qmd_timeout(default: float = 8.0) -> float:
    try:
        return float(qmd_config().get("timeout_seconds", default))
    except (TypeError, ValueError):
        return default


def qmd_index() -> str:
    return str(qmd_config().get("index", "astro-ph-llm-wiki")).strip()


def qmd_mode() -> str:
    mode = str(qmd_config().get("mode", "search")).strip().lower()
    return mode if mode in {"search", "query"} else "search"


def configured_collections() -> list[dict[str, str]]:
    raw = qmd_config().get("collections", [])
    if not isinstance(raw, list):
        return []
    collections: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        path = str(item.get("path", "")).strip()
        if not name or not path:
            continue
        collections.append(
            {
                "name": name,
                "path": path,
                "mask": str(item.get("mask", "**/*.md")).strip() or "**/*.md",
            }
        )
    return collections


def collection_names() -> list[str]:
    return [collection["name"] for collection in configured_collections()]


def collections_for(key: str, default: list[str] | None = None) -> list[str]:
    configured = qmd_config().get(key)
    if isinstance(configured, list):
        names = [str(item).strip() for item in configured if str(item).strip()]
        if names:
            return names
    return default or collection_names()


def qmd_base_command() -> list[str]:
    command = qmd_command()
    index = qmd_index()
    if index:
        return [*command, "--index", index]
    return command


def parse_json_array(text: str) -> list:
    stripped = text.strip()
    if not stripped:
        return []
    decoder = json.JSONDecoder()
    for start, char in enumerate(stripped):
        if char != "[":
            continue
        try:
            parsed, _ = decoder.raw_decode(stripped[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            return parsed
    return []


def denormalize_qmd_relpath(relpath: str) -> str:
    parts = relpath.split("/")
    if not parts:
        return relpath
    filename = parts[-1]
    match = re.match(r"^(\d{4})-(\d{5})(\.(?:md|txt))$", filename)
    if match:
        parts[-1] = f"{match.group(1)}.{match.group(2)}{match.group(3)}"
    return "/".join(parts)


def collection_path_map() -> dict[str, str]:
    return {collection["name"]: collection["path"] for collection in configured_collections()}


def qmd_uri_to_project_path(uri: str) -> str:
    if not uri.startswith("qmd://"):
        return uri
    match = re.match(r"^qmd://([^/]+)/(.+)$", uri)
    if not match:
        return uri
    collection, relpath = match.groups()
    base = collection_path_map().get(collection)
    if not base:
        return uri
    relpath = unquote(relpath)
    candidates = [denormalize_qmd_relpath(relpath), relpath]
    for candidate in candidates:
        full_path = project_path(base, candidate)
        if full_path.exists():
            return str(full_path.relative_to(project_path())).replace("\\", "/")
    return str(Path(base, candidates[0])).replace("\\", "/")


def clean_snippet(snippet: str) -> str:
    lines = snippet.splitlines()
    if lines and lines[0].startswith("@@"):
        lines = lines[1:]
    return "\n".join(lines).strip()


def qmd_search(
    query: str,
    *,
    max_results: int = 8,
    collections: list[str] | None = None,
    mode: str | None = None,
    timeout_seconds: float | None = None,
) -> list[QmdHit]:
    if not query.strip() or not qmd_enabled() or not qmd_available():
        return []
    selected_mode = mode or qmd_mode()
    command = [
        *qmd_base_command(),
        selected_mode,
        query,
        "--json",
        "-n",
        str(max_results),
    ]
    for collection in collections or collection_names():
        command.extend(["-c", collection])
    try:
        result = subprocess.run(
            command,
            cwd=project_path(),
            text=True,
            capture_output=True,
            timeout=timeout_seconds if timeout_seconds is not None else qmd_timeout(),
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    payload = parse_json_array(result.stdout)
    hits: list[QmdHit] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        raw_file = str(item.get("file", "")).strip()
        if not raw_file:
            continue
        try:
            score = float(item.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        hits.append(
            QmdHit(
                path=qmd_uri_to_project_path(raw_file),
                score=score,
                snippet=clean_snippet(str(item.get("snippet", "")).strip()),
                title=str(item.get("title", "")).strip(),
                raw_file=raw_file,
            )
        )
    return hits
