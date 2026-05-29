from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import db_path, ensure_parent


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or db_path()
    ensure_parent(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS papers (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          arxiv_id TEXT NOT NULL,
          version INTEGER NOT NULL DEFAULT 1,
          title TEXT NOT NULL,
          authors_json TEXT NOT NULL DEFAULT '[]',
          abstract TEXT NOT NULL DEFAULT '',
          categories TEXT NOT NULL DEFAULT '',
          primary_category TEXT NOT NULL DEFAULT '',
          published TEXT,
          updated TEXT,
          announced_date TEXT,
          abs_url TEXT,
          pdf_url TEXT,
          status TEXT NOT NULL DEFAULT 'discovered',
          pdf_path TEXT,
          text_path TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(arxiv_id, version)
        );

        CREATE TABLE IF NOT EXISTS classifications (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
          created_at TEXT NOT NULL,
          topic TEXT NOT NULL,
          relevance_score INTEGER NOT NULL,
          rationale TEXT NOT NULL,
          keywords_json TEXT NOT NULL DEFAULT '[]',
          model TEXT NOT NULL DEFAULT 'heuristic',
          raw_response TEXT,
          UNIQUE(paper_id)
        );

        CREATE TABLE IF NOT EXISTS wiki_pages (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          path TEXT NOT NULL UNIQUE,
          page_type TEXT NOT NULL,
          arxiv_id TEXT,
          title TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS links (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source_path TEXT NOT NULL,
          target_path TEXT NOT NULL,
          relation TEXT NOT NULL DEFAULT 'links_to',
          arxiv_id TEXT,
          created_at TEXT NOT NULL,
          UNIQUE(source_path, target_path, relation)
        );

        CREATE TABLE IF NOT EXISTS conversation_sessions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          started_at TEXT NOT NULL,
          ended_at TEXT,
          title TEXT,
          mode TEXT NOT NULL DEFAULT 'chat',
          summary TEXT,
          saved_to_markdown_path TEXT
        );

        CREATE TABLE IF NOT EXISTS conversation_messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id INTEGER NOT NULL REFERENCES conversation_sessions(id) ON DELETE CASCADE,
          timestamp TEXT NOT NULL,
          role TEXT NOT NULL,
          content TEXT NOT NULL,
          cited_sources_json TEXT NOT NULL DEFAULT '[]',
          save_policy TEXT NOT NULL DEFAULT 'transient'
        );

        CREATE TABLE IF NOT EXISTS conversation_notes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id INTEGER REFERENCES conversation_sessions(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          note_type TEXT NOT NULL,
          content TEXT NOT NULL,
          source_message_ids_json TEXT NOT NULL DEFAULT '[]',
          related_arxiv_ids_json TEXT NOT NULL DEFAULT '[]',
          related_wiki_pages_json TEXT NOT NULL DEFAULT '[]',
          approved INTEGER NOT NULL DEFAULT 0,
          applied_to_wiki INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS interest_signals (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id INTEGER REFERENCES conversation_sessions(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          signal_type TEXT NOT NULL,
          topic TEXT NOT NULL,
          weight_delta REAL NOT NULL DEFAULT 0,
          evidence TEXT NOT NULL DEFAULT '',
          explicit INTEGER NOT NULL DEFAULT 0,
          approved INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS agent_state (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )
    conn.commit()


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM agent_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO agent_state(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, utc_now()),
    )
    conn.commit()


def upsert_paper(conn: sqlite3.Connection, paper: dict[str, Any]) -> int:
    now = utc_now()
    authors_json = json.dumps(paper.get("authors", []), ensure_ascii=True)
    version = int(paper.get("version") or 1)
    fields = {
        "arxiv_id": paper["arxiv_id"],
        "version": version,
        "title": paper.get("title", "").strip(),
        "authors_json": authors_json,
        "abstract": paper.get("abstract", "").strip(),
        "categories": paper.get("categories", "").strip(),
        "primary_category": paper.get("primary_category", "").strip(),
        "published": paper.get("published"),
        "updated": paper.get("updated"),
        "announced_date": paper.get("announced_date"),
        "abs_url": paper.get("abs_url"),
        "pdf_url": paper.get("pdf_url"),
        "created_at": now,
        "updated_at": now,
    }
    conn.execute(
        """
        INSERT INTO papers (
          arxiv_id, version, title, authors_json, abstract, categories, primary_category,
          published, updated, announced_date, abs_url, pdf_url, created_at, updated_at
        )
        VALUES (
          :arxiv_id, :version, :title, :authors_json, :abstract, :categories, :primary_category,
          :published, :updated, :announced_date, :abs_url, :pdf_url, :created_at, :updated_at
        )
        ON CONFLICT(arxiv_id, version) DO UPDATE SET
          title = excluded.title,
          authors_json = excluded.authors_json,
          abstract = excluded.abstract,
          categories = excluded.categories,
          primary_category = excluded.primary_category,
          published = excluded.published,
          updated = excluded.updated,
          announced_date = excluded.announced_date,
          abs_url = excluded.abs_url,
          pdf_url = excluded.pdf_url,
          updated_at = excluded.updated_at
        """,
        fields,
    )
    row = conn.execute(
        "SELECT id FROM papers WHERE arxiv_id = ? AND version = ?",
        (paper["arxiv_id"], version),
    ).fetchone()
    conn.commit()
    return int(row["id"])


def rows_for_status(
    conn: sqlite3.Connection,
    statuses: Iterable[str],
    target_date: str | None = None,
    limit: int | None = None,
    arxiv_id: str | None = None,
) -> list[sqlite3.Row]:
    status_list = list(statuses)
    params: list[Any] = status_list[:]
    where = [f"status IN ({','.join('?' for _ in status_list)})"]
    if target_date:
        where.append(
            "("
            "announced_date = ? OR date(published) = ? OR date(updated) = ? "
            "OR date(created_at) = ? OR date(updated_at) = ? "
            "OR date(created_at, '+9 hours') = ? OR date(updated_at, '+9 hours') = ?"
            ")"
        )
        params.extend([target_date] * 7)
    if arxiv_id:
        where.append("arxiv_id = ?")
        params.append(arxiv_id)
    sql = f"SELECT * FROM papers WHERE {' AND '.join(where)} ORDER BY updated DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


def update_paper_status(conn: sqlite3.Connection, paper_id: int, status: str, **fields: Any) -> None:
    assignments = ["status = ?", "updated_at = ?"]
    params: list[Any] = [status, utc_now()]
    for key, value in fields.items():
        assignments.append(f"{key} = ?")
        params.append(value)
    params.append(paper_id)
    conn.execute(f"UPDATE papers SET {', '.join(assignments)} WHERE id = ?", params)
    conn.commit()


def save_classification(
    conn: sqlite3.Connection,
    paper_id: int,
    topic: str,
    relevance_score: int,
    rationale: str,
    keywords: list[str],
    model: str,
    raw_response: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO classifications (
          paper_id, created_at, topic, relevance_score, rationale, keywords_json, model, raw_response
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(paper_id) DO UPDATE SET
          created_at = excluded.created_at,
          topic = excluded.topic,
          relevance_score = excluded.relevance_score,
          rationale = excluded.rationale,
          keywords_json = excluded.keywords_json,
          model = excluded.model,
          raw_response = excluded.raw_response
        """,
        (
            paper_id,
            utc_now(),
            topic,
            int(relevance_score),
            rationale,
            json.dumps(keywords, ensure_ascii=True),
            model,
            raw_response,
        ),
    )
    conn.commit()


def register_wiki_page(conn: sqlite3.Connection, path: str, page_type: str, title: str, arxiv_id: str | None = None) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO wiki_pages(path, page_type, arxiv_id, title, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
          page_type = excluded.page_type,
          arxiv_id = excluded.arxiv_id,
          title = excluded.title,
          updated_at = excluded.updated_at
        """,
        (path, page_type, arxiv_id, title, now, now),
    )
    conn.commit()
