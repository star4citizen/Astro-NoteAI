#!/usr/bin/env python3
from __future__ import annotations

import re
from datetime import date

import _bootstrap  # noqa: F401

from astro_wiki.config import project_path
from astro_wiki.logging import append_wiki_log
from astro_wiki.wiki_io import now_iso


def main() -> None:
    wiki_root = project_path("wiki")
    pages = list(wiki_root.rglob("*.md"))
    indexed_text = project_path("wiki", "index.md").read_text(encoding="utf-8") if project_path("wiki", "index.md").exists() else ""
    missing_sources = []
    paper_not_indexed = []
    orphan_pages = []
    broken_links = []
    stale_topic_pages = []
    repeated_concepts = {}
    linked_targets = set()
    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    concept_terms = [
        "environment",
        "cluster",
        "quenching",
        "star formation",
        "metallicity",
        "morphology",
        "photometric redshift",
        "jwst",
        "simulation",
        "machine learning",
    ]
    for page in pages:
        text = page.read_text(encoding="utf-8", errors="ignore")
        rel = str(page.relative_to(project_path())).replace("\\", "/")
        is_maintenance_page = rel in {"wiki/index.md", "wiki/log.md"} or rel.startswith(("wiki/interests/", "wiki/proposals/", "wiki/daily/"))
        if page.match("*/papers/*.md") and rel not in indexed_text and page.name not in indexed_text:
            paper_not_indexed.append(rel)
        if page.match("*/papers/*.md") and not re.search(r"Paper ID:|arXiv ID:|arxiv_id:|ArXiv:", text, re.IGNORECASE):
            missing_sources.append(rel)
        if not is_maintenance_page and not page.match("*/papers/*.md") and not re.search(r"wiki/papers/|\[[^\]]+\]\([^)]*papers/|arXiv", text):
            missing_sources.append(rel)
        for link in link_pattern.findall(text):
            if not link.startswith("http"):
                target = (page.parent / link).resolve()
                linked_targets.add(str(target))
                if not target.exists():
                    broken_links.append(f"{rel} -> {link}")
        lower = text.lower()
        for term in concept_terms:
            if term in lower:
                repeated_concepts.setdefault(term, []).append(rel)
    for page in pages:
        rel = str(page.relative_to(project_path())).replace("\\", "/")
        is_maintenance_page = rel in {"wiki/index.md", "wiki/log.md"} or rel.startswith(("wiki/interests/", "wiki/proposals/", "wiki/daily/"))
        if page.name == "index.md" or is_maintenance_page:
            continue
        if str(page.resolve()) not in linked_targets and page.match("*/papers/*.md") is False:
            orphan_pages.append(rel)
        if page.match("*/topics/*.md"):
            text = page.read_text(encoding="utf-8", errors="ignore")
            if "## Approved Chat Updates" not in text and "Recent Papers" not in text and "arXiv" not in text:
                stale_topic_pages.append(str(page.relative_to(project_path())).replace("\\", "/"))
    repeated_concept_lines = [
        f"- {term}: {len(paths)} pages mention this term; consider a dedicated concept/method page."
        for term, paths in sorted(repeated_concepts.items())
        if len(set(paths)) >= 5
    ]
    report_path = project_path("reports", f"wiki-lint-{date.today().isoformat()}.md")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = (
        f"# Wiki Lint Report: {date.today().isoformat()}\n\n"
        f"## Missing Source Citations\n\n{chr(10).join(f'- {p}' for p in missing_sources) or 'None'}\n\n"
        f"## Paper Pages Not Listed In Index\n\n{chr(10).join(f'- {p}' for p in paper_not_indexed) or 'None'}\n\n"
        f"## Possible Orphan Pages\n\n{chr(10).join(f'- {p}' for p in orphan_pages) or 'None'}\n\n"
        f"## Broken Wiki Links\n\n{chr(10).join(f'- {p}' for p in broken_links) or 'None'}\n\n"
        f"## Possibly Stale Topic Pages\n\n{chr(10).join(f'- {p}' for p in stale_topic_pages) or 'None'}\n\n"
        f"## Repeated Concept Candidates\n\n{chr(10).join(repeated_concept_lines) or 'None'}\n"
    )
    report_path.write_text(report, encoding="utf-8")
    append_wiki_log(f"{now_iso()} Wiki lint report created: `{report_path.relative_to(project_path())}`")
    print(f"Wrote {report_path.relative_to(project_path())}")


if __name__ == "__main__":
    main()
