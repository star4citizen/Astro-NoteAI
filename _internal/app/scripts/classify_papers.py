#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import date, datetime

import _bootstrap  # noqa: F401

from astro_wiki.config import chat_model, load_yaml
from astro_wiki.db import connect, init_db, rows_for_status, save_classification, update_paper_status
from astro_wiki.logging import get_agent_logger
from astro_wiki.ollama_client import OllamaError, chat
from astro_wiki.schemas import ClassificationResult


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    if value == "today":
        return date.today().isoformat()
    return datetime.fromisoformat(value).date().isoformat()


def heuristic_classify(row, topics_cfg: dict) -> ClassificationResult:
    text = f"{row['title']} {row['abstract']} {row['categories']}".lower()
    topics = topics_cfg.get("topics", {})
    scores: dict[str, int] = {}
    matched: dict[str, list[str]] = {}
    for key, cfg in topics.items():
        words = [word.lower() for word in cfg.get("high_value_keywords", [])]
        anchors = [word.lower() for word in cfg.get("anchor_keywords", [])]
        required_science = [word.lower() for word in cfg.get("required_science_keywords", [])]
        supporting = [word.lower() for word in cfg.get("supporting_keywords", [])]
        negative = [word.lower() for word in cfg.get("negative_keywords", [])]
        hits = [word for word in words if word in text]
        anchor_hits = [word for word in anchors if word in text]
        science_hits = [word for word in required_science if word in text]
        supporting_hits = [word for word in supporting if word in text]
        negative_hits = [word for word in negative if word in text]
        category_hits = set(row["categories"].split()) & set(cfg.get("categories", []))
        if key == "external_galaxy_evolution":
            if negative_hits or not anchor_hits or not science_hits:
                score = 0
            else:
                score = len(set(science_hits)) * 2 + len(set(anchor_hits)) + len(set(supporting_hits))
                score += 2 if category_hits else 0
                score = max(0, min(5, score))
        else:
            score = min(5, len(hits) + (2 if category_hits else 0))
        scores[key] = score
        matched[key] = sorted(set(anchor_hits + science_hits + hits + supporting_hits))[:10]
    external = scores.get("external_galaxy_evolution", 0)
    ml = scores.get("ml_in_astronomy", 0)
    if external >= 3 and ml >= 3:
        topic = "both"
        score = max(external, ml)
        keywords = sorted(set(matched.get("external_galaxy_evolution", []) + matched.get("ml_in_astronomy", [])))
    elif external >= ml and external > 0:
        topic = "external_galaxy_evolution"
        score = external
        keywords = matched.get("external_galaxy_evolution", [])
    elif ml > 0:
        topic = "ml_in_astronomy"
        score = ml
        keywords = matched.get("ml_in_astronomy", [])
    else:
        topic = "neither"
        score = 0
        keywords = []
    return ClassificationResult(
        topic=topic,
        relevance_score=score,
        rationale=f"Heuristic match from categories/keywords: {', '.join(keywords) if keywords else 'none'}",
        keywords=keywords,
    )


def ollama_classify(row, prompt: str, model: str) -> tuple[ClassificationResult, str]:
    content = (
        f"{prompt}\n\n"
        f"Title: {row['title']}\n"
        f"Categories: {row['categories']}\n"
        f"Abstract: {row['abstract']}\n"
    )
    response = chat(
        [{"role": "user", "content": content}],
        model=model,
        format_json=True,
    )
    data = json.loads(response)
    return ClassificationResult.model_validate(data), response


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify discovered papers for project relevance.")
    parser.add_argument("--date", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-llm", action="store_true")
    args = parser.parse_args()

    target_date = parse_date(args.date)
    logger = get_agent_logger("classify_papers", target_date or date.today().isoformat())
    topics_cfg = load_yaml("config/topics.yml")
    agents = load_yaml("config/agents.yml").get("agents", {})
    cfg = agents.get("classifier", {})
    min_score = int(cfg.get("min_relevance_score", topics_cfg.get("selection", {}).get("min_relevance_score", 3)))
    model = cfg.get("model", chat_model())
    prompt = load_yaml("config/prompts/classify_paper.md") if False else ""
    prompt_path = "config/prompts/classify_paper.md"
    with open(prompt_path, "r", encoding="utf-8") as handle:
        prompt = handle.read()

    with connect() as conn:
        init_db(conn)
        rows = rows_for_status(conn, ["discovered", "classified"], target_date=target_date, limit=args.limit)
        selected = rejected = 0
        accept_topics = set(topics_cfg.get("selection", {}).get("accept_topics", []))
        for row in rows:
            raw_response = None
            source_model = "heuristic"
            try:
                if args.no_llm:
                    raise OllamaError("LLM disabled")
                result, raw_response = ollama_classify(row, prompt, model)
                source_model = model
            except Exception as exc:
                logger.info("paper_id=%s action=classify_llm status=fallback message=%s", row["id"], exc)
                result = heuristic_classify(row, topics_cfg)
            save_classification(
                conn,
                row["id"],
                result.topic,
                result.relevance_score,
                result.rationale,
                result.keywords,
                source_model,
                raw_response,
            )
            is_selected = (result.relevance_score >= min_score or result.topic == "both") and (
                not accept_topics or result.topic in accept_topics
            )
            update_paper_status(conn, row["id"], "selected" if is_selected else "rejected")
            selected += int(is_selected)
            rejected += int(not is_selected)
    logger.info("action=classify_papers status=ok selected=%s rejected=%s", selected, rejected)
    print(f"Classified {selected + rejected} papers: selected={selected}, rejected={rejected}")


if __name__ == "__main__":
    main()
