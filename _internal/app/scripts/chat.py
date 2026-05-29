#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import date

import _bootstrap  # noqa: F401

from astro_wiki.config import chat_model, project_path
from astro_wiki.db import connect, init_db, utc_now
from astro_wiki.ollama_client import chat
from astro_wiki.retrieval import build_context, search_wiki


def save_session(question: str, answer: str, sources: list[str], save_policy: str) -> None:
    conversation_path = project_path("conversations", f"{date.today().isoformat()}.md")
    conversation_path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        init_db(conn)
        cur = conn.execute(
            "INSERT INTO conversation_sessions(started_at, ended_at, title, mode, saved_to_markdown_path) VALUES (?, ?, ?, ?, ?)",
            (utc_now(), utc_now(), question[:80], "chat", str(conversation_path.relative_to(project_path()))),
        )
        session_id = cur.lastrowid
        conn.execute(
            "INSERT INTO conversation_messages(session_id, timestamp, role, content, cited_sources_json, save_policy) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, utc_now(), "user", question, "[]", save_policy),
        )
        conn.execute(
            "INSERT INTO conversation_messages(session_id, timestamp, role, content, cited_sources_json, save_policy) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, utc_now(), "assistant", answer, json.dumps(sources, ensure_ascii=True), save_policy),
        )
        conn.commit()
    with conversation_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n## {utc_now()}\n\n**User:** {question}\n\n**Assistant:**\n\n{answer}\n\nSources: {', '.join(sources)}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask the local wiki using retrieved context.")
    parser.add_argument("question", nargs="*", help="Question to ask.")
    parser.add_argument("--max-pages", type=int, default=8)
    parser.add_argument("--save", action="store_true", help="Save this chat turn to conversations and SQLite.")
    parser.add_argument("--no-llm", action="store_true")
    args = parser.parse_args()

    question = " ".join(args.question).strip()
    if not question:
        raise SystemExit("Provide a question.")
    pages = search_wiki(question, max_pages=args.max_pages)
    context = build_context(pages)
    sources = [page.path for page in pages]
    if args.no_llm:
        answer = (
            "Retrieved context only; LLM disabled.\n\n"
            + (context if context else "No relevant wiki pages found.")
        )
    else:
        prompt = project_path("config", "prompts", "answer_question.md").read_text(encoding="utf-8")
        if not context:
            answer = "No relevant wiki pages were found. Run the ingest pipeline or broaden the question."
        else:
            try:
                answer = chat(
                    [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": f"Question: {question}\n\nContext:\n{context}"},
                    ],
                    model=chat_model(),
                )
            except Exception as exc:
                answer = f"Ollama call failed: {exc}\n\nRetrieved context:\n\n{context}"
    print(answer)
    if args.save:
        save_session(question, answer, sources, "saved_by_user")


if __name__ == "__main__":
    main()
