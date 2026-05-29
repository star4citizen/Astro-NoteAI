#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if ROOT.parent.name == "_internal":
    DEFAULT_OUTPUT = ROOT.parent.parent.parent
else:
    DEFAULT_OUTPUT = ROOT.parent / "packaged"
EXAMPLE_PAPER_COUNT = 0


def title_from_markdown(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="ignore")
    return title_from_markdown_text(text, path.stem)


def title_from_markdown_text(text: str, fallback: str) -> str:
    match = re.search(r'^title:\s*["\']?(.*?)["\']?\s*$', text, flags=re.MULTILINE)
    if match:
        return match.group(1).strip()
    match = re.search(r"^#\s+(.+)$", text, flags=re.MULTILINE)
    return match.group(1).strip() if match else fallback


def section_from_markdown(markdown: str, heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}\s*$\n(?P<body>.*?)(?=^## |\Z)", markdown, flags=re.MULTILINE | re.DOTALL)
    return " ".join(match.group("body").split()) if match else ""


def line_value(markdown: str, label: str) -> str:
    match = re.search(rf"^- {re.escape(label)}:\s*(.+)$", markdown, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def safe_paper_id(arxiv_id: str) -> str:
    return arxiv_id.replace("/", "_")


def source_paper_artifacts() -> dict[str, dict[str, str]]:
    db_path = ROOT / "data" / "metadata" / "papers.sqlite"
    if not db_path.exists():
        return {}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT arxiv_id, pdf_path, text_path FROM papers").fetchall()
    return {
        row["arxiv_id"]: {"pdf_path": row["pdf_path"] or "", "text_path": row["text_path"] or ""}
        for row in rows
    }


def copy_packaged_artifact(app_root: Path, source_rel: str, dest_rel: str) -> str | None:
    if not source_rel:
        return None
    source = ROOT / source_rel
    if not source.exists() or not source.is_file():
        return None
    dest = app_root / dest_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return dest_rel


def reset_packaged_app_data(app_root: Path, keep_count: int = EXAMPLE_PAPER_COUNT) -> None:
    wiki_root = app_root / "wiki"
    paper_root = wiki_root / "papers"
    keep_pages: list[tuple[str, str]] = []
    if keep_count > 0:
        keep_pages = [
            (path.name, path.read_text(encoding="utf-8", errors="ignore"))
            for path in sorted(paper_root.glob("*.md"), reverse=True)
            if not path.name.endswith("-deep-summary.md")
        ][:keep_count]

    for rel in ["papers", "daily", "newsletters", "questions", "topics", "interests", "document"]:
        shutil.rmtree(wiki_root / rel, ignore_errors=True)
        (wiki_root / rel).mkdir(parents=True, exist_ok=True)
    paper_root.mkdir(parents=True, exist_ok=True)
    if keep_pages:
        for name, text in keep_pages:
            (paper_root / name).write_text(text, encoding="utf-8")
        (wiki_root / "topics" / "semantic").mkdir(parents=True, exist_ok=True)

    for rel in [
        "data/raw/arxiv",
        "data/raw/uploads",
        "data/raw/papers",
        "data/text",
        "data/markdown",
        "data/cache",
        "data/summaries",
        "data/paperforge",
    ]:
        shutil.rmtree(app_root / rel, ignore_errors=True)
    for rel in ["data/raw/papers", "data/raw/uploads", "data/text", "data/markdown", "data/metadata", "data/cache", "data/summaries"]:
        (app_root / rel).mkdir(parents=True, exist_ok=True)

    (wiki_root / "log.md").write_text("# Wiki Log\n\nDistribution data reset.\n", encoding="utf-8")

    env = {**dict(), **__import__("os").environ, "ASTRO_WIKI_PROJECT_ROOT": str(app_root)}
    subprocess.run([sys.executable, str(ROOT / "scripts" / "init_db.py")], cwd=ROOT, env=env, check=True)

    db_path = app_root / "data" / "metadata" / "papers.sqlite"
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    source_artifacts = source_paper_artifacts()
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM classifications")
        conn.execute("DELETE FROM links")
        conn.execute("DELETE FROM wiki_pages")
        conn.execute("DELETE FROM papers")
        for idx, (name, markdown) in enumerate(keep_pages):
            arxiv_id = Path(name).stem
            safe_id = safe_paper_id(arxiv_id)
            title = title_from_markdown_text(markdown, Path(name).stem)
            authors = line_value(markdown, "Authors")
            categories = line_value(markdown, "Categories") or "example"
            abstract = section_from_markdown(markdown, "Source Abstract")
            announced = f"2026-05-{max(1, 20 - idx):02d}"
            artifacts = source_artifacts.get(arxiv_id, {})
            pdf_rel = copy_packaged_artifact(app_root, artifacts.get("pdf_path", ""), f"data/raw/papers/{safe_id}.pdf")
            text_rel = copy_packaged_artifact(app_root, artifacts.get("text_path", ""), f"data/text/{safe_id}.txt")
            conn.execute(
                """
                INSERT INTO papers (
                  arxiv_id, version, title, authors_json, abstract, categories, primary_category,
                  published, updated, announced_date, abs_url, pdf_url, status, pdf_path, text_path,
                  created_at, updated_at
                )
                VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'graphed', ?, ?, ?, ?)
                """,
                (
                    arxiv_id,
                    title,
                    json.dumps([item.strip() for item in authors.split(",") if item.strip()], ensure_ascii=True),
                    abstract,
                    categories,
                    categories.split()[0] if categories else "example",
                    announced,
                    announced,
                    announced,
                    f"https://arxiv.org/abs/{arxiv_id}" if not arxiv_id.startswith("local-") else f"local-upload:{arxiv_id}",
                    f"https://arxiv.org/pdf/{arxiv_id}.pdf" if not arxiv_id.startswith("local-") else "",
                    pdf_rel,
                    text_rel,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO wiki_pages(path, page_type, arxiv_id, title, created_at, updated_at)
                VALUES (?, 'paper', ?, ?, ?, ?)
                """,
                (f"wiki/papers/{name}", arxiv_id, title, now, now),
            )
        conn.commit()

    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_graph.py")], cwd=ROOT, env=env, check=True)
    if keep_pages:
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "semantic_wiki_agent.py"),
                "--max-topics",
                str(keep_count),
                "--no-llm",
                "--prune-stale",
            ],
            cwd=ROOT,
            env=env,
            check=True,
        )

    semantic_pages = sorted((wiki_root / "topics" / "semantic").glob("*.md"))[:keep_count] if keep_pages else []
    index_lines = ["# Astro-Note AI", "", "No papers or wiki documents are bundled with this distribution.", ""]
    if keep_pages:
        index_lines.extend(["## Example Papers", ""])
        for name, text in keep_pages:
            title = title_from_markdown_text(text, Path(name).stem)
            index_lines.append(f"- [{title}](papers/{name}) ({Path(name).stem})")
        index_lines.extend(["", "## Semantic Topics", ""])
        if semantic_pages:
            for path in semantic_pages:
                index_lines.append(f"- [{title_from_markdown(path)}](topics/semantic/{path.name})")
        else:
            index_lines.append("- No semantic topics available for the packaged examples.")
    (wiki_root / "index.md").write_text("\n".join(index_lines).rstrip() + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Astro-Note AI desktop app with PyInstaller.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()

    dist_dir = args.output_dir.resolve()
    build_dir = ROOT / "build" / "pyinstaller"
    dist_dir.mkdir(parents=True, exist_ok=True)
    if args.clean:
        shutil.rmtree(build_dir, ignore_errors=True)
        shutil.rmtree(dist_dir / "Astro-Note-AI", ignore_errors=True)

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(build_dir),
        str(ROOT / "astro_wiki_desktop.spec"),
    ]
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit(result.returncode)
    app_root = dist_dir / "Astro-Note-AI" / "_internal" / "app"
    reset_packaged_app_data(app_root)
    icon_path = ROOT / "Astro-Note-AI.ico"
    if icon_path.exists():
        shutil.copy2(icon_path, dist_dir / "Astro-Note-AI" / "Astro-Note-AI.ico")
    system = platform.system().lower()
    print(f"Packaged {system} build at: {dist_dir / 'Astro-Note-AI'}")


if __name__ == "__main__":
    main()
