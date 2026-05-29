from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .config import api_provider_config, chat_model, llm_provider, load_local_settings, project_path
from .ollama_client import chat
from .wiki_io import paper_page_path

DEFAULT_SECTIONS = [
    "Scientific Question",
    "Data",
    "Method",
    "Main Results",
    "Limitations",
    "Follow-up Questions",
]

CUSTOM_PROMPT_SETTINGS = {
    "ingest_reduce_paper.md": "upload_work_prompt",
}


@dataclass(frozen=True)
class TextChunk:
    label: str
    text: str


@dataclass
class NumericValidationResult:
    checked_count: int
    warning_count: int
    findings: list[dict[str, Any]]
    sources_checked: list[str]


@dataclass
class IngestResult:
    body: str
    source_type: str
    source_material_path: Path
    chunk_count: int
    chunk_cache_dir: Path
    validation: NumericValidationResult | None
    validation_markdown_path: Path | None
    validation_json_path: Path | None
    generation_mode: str
    map_model: str
    reduce_model: str
    generation_error: str = ""


class StructuralValidationError(RuntimeError):
    pass


def row_value(row: Mapping[str, Any] | Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def safe_id(arxiv_id: str) -> str:
    return arxiv_id.replace("/", "_")


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "default"


def read_text(path: Path | None) -> str:
    return path.read_text(encoding="utf-8", errors="ignore") if path and path.exists() else ""


def project_rel(path: Path) -> str:
    if not path.is_absolute():
        return str(path).replace("\\", "/").lstrip("./")
    try:
        return str(path.resolve().relative_to(project_path().resolve())).replace("\\", "/")
    except ValueError:
        return path.name


def prompt_text(name: str) -> str:
    settings_key = CUSTOM_PROMPT_SETTINGS.get(name)
    if settings_key:
        custom_prompt = str(load_local_settings().get(settings_key) or "").strip()
        if custom_prompt:
            return custom_prompt
    return project_path("config", "prompts", name).read_text(encoding="utf-8")


def source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def split_fixed(text: str, max_chars: int) -> list[TextChunk]:
    chunks: list[TextChunk] = []
    start = 0
    index = 1
    while start < len(text):
        stop = min(len(text), start + max_chars)
        if stop < len(text):
            boundary = text.rfind("\n\n", start, stop)
            if boundary > start + max_chars // 2:
                stop = boundary
        piece = text[start:stop].strip()
        if piece:
            chunks.append(TextChunk(f"chunk {index}", piece))
        start = max(stop, start + 1)
        index += 1
    return chunks


def split_extracted_text(text: str, max_chunk_chars: int, max_chunks: int | None) -> list[TextChunk]:
    page_re = re.compile(r"\n*--- Page (\d+) ---\n*", re.IGNORECASE)
    matches = list(page_re.finditer(text))
    if not matches:
        chunks = split_fixed(text, max_chunk_chars)
        return chunks[:max_chunks] if max_chunks else chunks

    pages: list[tuple[int, str]] = []
    for index, match in enumerate(matches):
        page_num = int(match.group(1))
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        page_text = text[start:end].strip()
        if page_text:
            pages.append((page_num, page_text))

    chunks: list[TextChunk] = []
    current_pages: list[int] = []
    current_parts: list[str] = []
    current_len = 0
    for page_num, page_text in pages:
        page_block = f"--- Page {page_num} ---\n{page_text}"
        if current_parts and current_len + len(page_block) > max_chunk_chars:
            chunks.append(TextChunk(f"pages {current_pages[0]}-{current_pages[-1]}", "\n\n".join(current_parts)))
            current_pages = []
            current_parts = []
            current_len = 0
        current_pages.append(page_num)
        current_parts.append(page_block)
        current_len += len(page_block)
    if current_parts:
        chunks.append(TextChunk(f"pages {current_pages[0]}-{current_pages[-1]}", "\n\n".join(current_parts)))
    return chunks[:max_chunks] if max_chunks else chunks


def split_markdown_text(markdown: str, max_chunk_chars: int, max_chunks: int | None) -> list[TextChunk]:
    heading_re = re.compile(r"^(#{1,4})\s+(.+?)\s*$", re.MULTILINE)
    matches = list(heading_re.finditer(markdown))
    if not matches:
        chunks = split_fixed(markdown, max_chunk_chars)
        return chunks[:max_chunks] if max_chunks else chunks

    chunks: list[TextChunk] = []
    preamble = markdown[: matches[0].start()].strip()
    if preamble:
        chunks.append(TextChunk("markdown preamble", preamble))
    for index, match in enumerate(matches):
        level = len(match.group(1))
        title = " ".join(match.group(2).split())
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        section_text = markdown[start:end].strip()
        if not section_text:
            continue
        label = f"markdown h{level}: {title[:80]}"
        if len(section_text) <= max_chunk_chars:
            chunks.append(TextChunk(label, section_text))
            continue
        for sub_index, subchunk in enumerate(split_fixed(section_text, max_chunk_chars), start=1):
            chunks.append(TextChunk(f"{label} part {sub_index}", subchunk.text))
    return chunks[:max_chunks] if max_chunks else chunks


ANCHOR_COMMENT_RE = re.compile(r"^<!--\s*astro-note-anchor:\s*paragraph-\d+\s*-->\s*\n?", flags=re.MULTILINE)


def strip_source_reference_anchors(markdown: str) -> str:
    return ANCHOR_COMMENT_RE.sub("", markdown)


def ensure_source_reference_anchors(source_path: Path, source_text: str, chunks: list[TextChunk]) -> str:
    if source_path.suffix.lower() not in {".md", ".markdown"}:
        return source_text
    text = source_text
    positions: list[tuple[int, str]] = []
    search_start = 0
    for index, chunk in enumerate(chunks, start=1):
        anchor = paragraph_anchor(index)
        comment = f"<!-- astro-note-anchor: {anchor} -->"
        if comment in text:
            continue
        needle = chunk.text.strip()
        if not needle:
            continue
        position = text.find(needle, search_start)
        if position < 0:
            compact_start = needle[:240].strip()
            position = text.find(compact_start, search_start) if compact_start else -1
        if position < 0:
            continue
        positions.append((position, comment))
        search_start = position + max(1, len(needle))
    if not positions:
        return text
    for position, comment in reversed(positions):
        prefix = "" if position == 0 or text[position - 1] == "\n" else "\n"
        text = text[:position] + f"{prefix}{comment}\n" + text[position:]
    source_path.write_text(text.rstrip() + "\n", encoding="utf-8")
    return text


def strip_markdown_inline(text: str) -> str:
    cleaned = text.replace("\\_", "_")
    cleaned = re.sub(r"[*_`]+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" :-")


def paragraph_label(fallback_index: int) -> str:
    return f"paragraph {max(1, fallback_index)}"


def paragraph_anchor(fallback_index: int) -> str:
    return f"paragraph-{max(1, fallback_index)}"


def citation_anchor(label: str) -> str:
    normalized = " ".join(str(label or "").split()).lower()
    section = re.fullmatch(r"section\s+(\d+(?:\.\d+)*)", normalized)
    if section:
        return "section-" + section.group(1).replace(".", "-")
    paragraph = re.fullmatch(r"paragraph\s+(\d+)", normalized)
    if paragraph:
        return f"paragraph-{paragraph.group(1)}"
    if normalized in {"abstract", "abstract-level evidence"}:
        return "abstract"
    return ""


def citation_label_from_source_label(label: str, fallback_index: int = 1) -> str:
    raw = " ".join(str(label or "").split())
    if not raw:
        return paragraph_label(fallback_index)

    lower = raw.lower()
    if lower in {"markdown preamble", "preamble"}:
        return paragraph_label(fallback_index)

    pages = re.fullmatch(r"pages?\s+\d+(?:\s*[-–]\s*\d+)?", raw, flags=re.IGNORECASE)
    if pages:
        return paragraph_label(fallback_index)

    chunk = re.fullmatch(r"chunk\s+(\d+)", raw, flags=re.IGNORECASE)
    if chunk:
        return paragraph_label(int(chunk.group(1)))

    markdown_heading = re.match(r"markdown h\d:\s*(.+)", raw, flags=re.IGNORECASE)
    heading = strip_markdown_inline(markdown_heading.group(1) if markdown_heading else raw)
    if not heading:
        return paragraph_label(fallback_index)

    heading_lower = heading.lower()
    if heading_lower == "abstract":
        return paragraph_label(fallback_index)
    if heading_lower.startswith("keywords"):
        return paragraph_label(fallback_index)
    if heading_lower == "acknowledgments":
        return paragraph_label(fallback_index)
    if heading_lower == "references":
        return paragraph_label(fallback_index)

    section = re.match(r"(?P<number>\d{1,2}(?:\.\d+)*)(?:\.|\s+)(?P<title>.+)", heading)
    if section and not re.fullmatch(r"(?:19|20)\d{2}", section.group("number")):
        number = section.group("number")
        return f"section {number}"

    if markdown_heading:
        return paragraph_label(fallback_index)
    return paragraph_label(fallback_index)


def citation_label_for_extract(extract: Mapping[str, Any], fallback_index: int) -> str:
    label = str(extract.get("chunk_label") or extract.get("source") or "")
    return citation_label_from_source_label(label, fallback_index)


def extracts_for_reduce_prompt(extracts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prompt_extracts: list[dict[str, Any]] = []
    for fallback_index, extract in enumerate(extracts, start=1):
        prompt_extract = dict(extract)
        citation_label = citation_label_for_extract(extract, fallback_index)
        prompt_extract["source"] = citation_label
        prompt_extract["citation_label"] = citation_label
        prompt_extract.pop("chunk_index", None)
        prompt_extract.pop("chunk_label", None)
        prompt_extracts.append(prompt_extract)
    return prompt_extracts


def compact_text_value(value: str, max_chars: int) -> str:
    compact = re.sub(r"\s+", " ", str(value or "")).strip()
    return compact[:max_chars].rstrip()


def compact_extract_for_reduce(extract: Mapping[str, Any]) -> dict[str, Any]:
    kept: dict[str, Any] = {
        "source": extract.get("source") or "",
        "citation_label": extract.get("citation_label") or extract.get("source") or "",
    }
    limits = {
        "scientific_question": 3,
        "data": 5,
        "method": 5,
        "main_results": 5,
        "limitations": 4,
        "figures_tables": 3,
        "follow_up_questions": 3,
    }
    for key, limit in limits.items():
        value = extract.get(key)
        if isinstance(value, list):
            items = [compact_text_value(item, 360) for item in value if str(item or "").strip()]
            if items:
                kept[key] = items[:limit]
        elif value:
            kept[key] = compact_text_value(str(value), 360)
    excerpt = compact_text_value(str(extract.get("evidence_excerpt") or ""), 700)
    if excerpt:
        kept["evidence_excerpt"] = excerpt
    return kept


def shrink_extract_for_reduce(extract: Mapping[str, Any], max_chars: int) -> dict[str, Any]:
    base = {
        "source": extract.get("source") or "",
        "citation_label": extract.get("citation_label") or extract.get("source") or "",
    }
    keys = [
        "scientific_question",
        "data",
        "method",
        "main_results",
        "limitations",
        "figures_tables",
        "follow_up_questions",
    ]
    variants: list[dict[str, Any]] = []
    for item_limit, char_limit, include_excerpt in [(2, 180, True), (1, 140, False), (1, 80, False)]:
        candidate = dict(base)
        for key in keys:
            value = extract.get(key)
            if isinstance(value, list):
                items = [compact_text_value(item, char_limit) for item in value if str(item or "").strip()]
                if items:
                    candidate[key] = items[:item_limit]
            elif value:
                candidate[key] = compact_text_value(str(value), char_limit)
        if include_excerpt and extract.get("evidence_excerpt"):
            candidate["evidence_excerpt"] = compact_text_value(str(extract.get("evidence_excerpt")), 240)
        variants.append(candidate)
    variants.append(dict(base))
    for candidate in variants:
        if len(json.dumps([candidate], indent=2, ensure_ascii=False)) <= max_chars:
            return candidate
    return dict(base)


def reduce_extracts_json(extracts: list[dict[str, Any]], max_chars: int) -> str:
    full = json.dumps(extracts, indent=2, ensure_ascii=False)
    if len(full) <= max_chars:
        return full
    compacted = [compact_extract_for_reduce(extract) for extract in extracts]
    compact = json.dumps(compacted, indent=2, ensure_ascii=False)
    if len(compact) <= max_chars:
        return compact
    packed: list[dict[str, Any]] = []
    for extract in compacted:
        candidate = packed + [extract]
        encoded = json.dumps(candidate, indent=2, ensure_ascii=False)
        if len(encoded) <= max_chars:
            packed = candidate
            continue
        if packed:
            break
        shrunk = shrink_extract_for_reduce(extract, max_chars)
        if len(json.dumps([shrunk], indent=2, ensure_ascii=False)) <= max_chars:
            packed.append(shrunk)
        break
    return json.dumps(packed, indent=2, ensure_ascii=False)


def chunk_reference_map(extracts: list[dict[str, Any]]) -> dict[int, str]:
    labels: dict[int, str] = {}
    for fallback_index, extract in enumerate(extracts, start=1):
        try:
            index = int(extract.get("chunk_index") or fallback_index)
        except (TypeError, ValueError):
            index = fallback_index
        labels[index] = citation_label_for_extract(extract, fallback_index)
    return labels


def source_label_alias_map(extracts: list[dict[str, Any]]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for fallback_index, extract in enumerate(extracts, start=1):
        source_label = str(extract.get("chunk_label") or extract.get("source") or "")
        citation_label = citation_label_for_extract(extract, fallback_index)
        markdown_heading = re.match(r"markdown h\d:\s*(.+)", source_label, flags=re.IGNORECASE)
        if not markdown_heading:
            continue
        heading = strip_markdown_inline(markdown_heading.group(1))
        if not heading:
            continue
        aliases[f"section {heading}"] = citation_label
        if heading.endswith("."):
            aliases[f"section {heading.rstrip('.')}"] = citation_label
    return aliases


def normalize_source_label_aliases(markdown: str, extracts: list[dict[str, Any]]) -> str:
    normalized = markdown
    aliases = source_label_alias_map(extracts)
    for alias in sorted(aliases, key=len, reverse=True):
        replacement = aliases[alias]
        if alias != replacement:
            normalized = normalized.replace(alias, replacement)
    normalized = re.sub(r"\bsource passages?\s+(\d+)\b", r"paragraph \1", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\b(?:paper opening|paper title)\b", "paragraph 1", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bsection\s+(\d+)\.0\b", r"section \1", normalized, flags=re.IGNORECASE)
    normalized = re.sub(
        r"\(([^)]*\bsection\b[^)]*)\)",
        lambda match: "(" + re.sub(r"(?<![\d.])(\d+)\.0\b", r"\1", match.group(1)) + ")",
        normalized,
        flags=re.IGNORECASE,
    )
    return normalized


def expand_reference_numbers(text: str) -> list[int]:
    refs = re.sub(r"\bchunks?\b", "", text, flags=re.IGNORECASE)
    numbers: list[int] = []
    for match in re.finditer(r"\d+(?:\s*[-–]\s*\d+)?", refs):
        token = match.group(0)
        if "-" in token or "–" in token:
            start_text, end_text = re.split(r"\s*[-–]\s*", token, maxsplit=1)
            start = int(start_text)
            end = int(end_text)
            if start <= end and end - start <= 20:
                numbers.extend(range(start, end + 1))
            continue
        numbers.append(int(token))
    return numbers


def normalize_chunk_references(markdown: str, extracts: list[dict[str, Any]]) -> str:
    labels_by_index = chunk_reference_map(extracts)
    markdown = normalize_source_label_aliases(markdown, extracts)
    if not labels_by_index:
        return markdown

    def replace(match: re.Match[str]) -> str:
        numbers = expand_reference_numbers(match.group(1))
        labels: list[str] = []
        for number in numbers:
            label = labels_by_index.get(number)
            if label and label not in labels:
                labels.append(label)
        if not labels:
            return match.group(0)
        return f"({'; '.join(labels)})"

    return re.sub(r"\(([^)]*\bchunks?\b[^)]*)\)", replace, markdown, flags=re.IGNORECASE)


def source_markdown_target(source_path: Path, anchor: str) -> str:
    rel = project_rel(source_path)
    target = f"../../{rel}" if not rel.startswith("../") else rel
    return f"{target}#{anchor}" if anchor else target


def source_reference_targets(extracts: list[dict[str, Any]], source_path: Path) -> dict[str, str]:
    targets: dict[str, str] = {
        "abstract": source_markdown_target(source_path, "abstract"),
        "abstract-level evidence": source_markdown_target(source_path, "abstract"),
    }
    for fallback_index, extract in enumerate(extracts, start=1):
        label = citation_label_for_extract(extract, fallback_index)
        anchor = citation_anchor(label)
        if anchor:
            targets.setdefault(label.lower(), source_markdown_target(source_path, anchor))
    return targets


SOURCE_CITATION_RE = re.compile(
    r"\b(?:section\s+\d+(?:\.\d+)*|paragraph\s+\d+|abstract(?:-level evidence)?)\b",
    flags=re.IGNORECASE,
)


def link_source_citations(markdown: str, extracts: list[dict[str, Any]], source_path: Path | None) -> str:
    if not source_path or source_path.suffix.lower() not in {".md", ".markdown"}:
        return markdown
    targets = source_reference_targets(extracts, source_path)
    if not targets:
        return markdown
    masked_links: list[str] = []

    def mask_existing_link(match: re.Match[str]) -> str:
        masked_links.append(match.group(0))
        return f"@@ASTRO_NOTE_LINK_{len(masked_links) - 1}@@"

    masked = re.sub(r"\[[^\]]+\]\([^)]+\)", mask_existing_link, markdown)

    def replace_parenthetical(match: re.Match[str]) -> str:
        content = match.group(1)
        changed = False

        def replace_label(label_match: re.Match[str]) -> str:
            nonlocal changed
            label = " ".join(label_match.group(0).split())
            target = targets.get(label.lower())
            if not target:
                return label_match.group(0)
            changed = True
            return f"[{label}]({target})"

        linked = SOURCE_CITATION_RE.sub(replace_label, content)
        return f"({linked})" if changed else match.group(0)

    linked = re.sub(
        r"\(([^()\n]*(?:section\s+\d|paragraph\s+\d|abstract)[^()\n]*)\)",
        replace_parenthetical,
        masked,
        flags=re.IGNORECASE,
    )
    for index, original in enumerate(masked_links):
        linked = linked.replace(f"@@ASTRO_NOTE_LINK_{index}@@", original)
    return re.sub(r"`(\([^`]*\[[^\]]+\]\([^)]+\)[^`]*\))`", r"\1", linked)


def strip_same_paper_arxiv_citations(markdown: str, arxiv_id: str) -> str:
    escaped_id = re.escape(arxiv_id)
    normalized = re.sub(
        rf"\s*\((?:arXiv:\s*)?{escaped_id}\)",
        "",
        markdown,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        rf"\s*\[(?:arXiv:\s*)?{escaped_id}\]\(https://arxiv\.org/abs/{escaped_id}\)",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    return normalized


def parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        stripped = fence.group(1).strip()
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else {"raw_response": stripped}
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(stripped[start : end + 1])
            return parsed if isinstance(parsed, dict) else {"raw_response": stripped}
        except json.JSONDecodeError:
            pass
    return {"raw_response": stripped}


def heuristic_chunk_extract(chunk: TextChunk) -> dict[str, Any]:
    compact = " ".join(chunk.text.split())
    return {
        "source": chunk.label,
        "scientific_question": [],
        "data": [],
        "method": [],
        "main_results": [],
        "limitations": [],
        "figures_tables": [],
        "follow_up_questions": [],
        "evidence_excerpt": compact[:1200],
        "notes": "Heuristic fallback extract; no LLM claims were generated.",
    }


def use_llm_map_stage() -> bool:
    provider = llm_provider()
    provider_cfg = api_provider_config(provider)
    configured = provider_cfg.get("map_llm")
    if configured is not None:
        return bool(configured)
    if provider == "gemini":
        return False
    return True


def llm_chunk_extract(row: Mapping[str, Any] | Any, chunk: TextChunk, model: str) -> dict[str, Any]:
    user_content = (
        f"Paper ID: {row_value(row, 'arxiv_id')}\n"
        f"Title: {row_value(row, 'title')}\n"
        f"Abstract: {row_value(row, 'abstract') or ''}\n"
        f"Chunk source: {chunk.label}\n\n"
        f"Chunk text:\n{chunk.text}"
    )
    response = chat(
        [
            {"role": "system", "content": prompt_text("ingest_map_extract.md")},
            {"role": "user", "content": user_content},
        ],
        model=model,
        format_json=True,
        timeout=180.0,
        options={"num_predict": 1200, "temperature": 0.1},
        think=False,
    )
    parsed = parse_json_object(response)
    parsed.setdefault("source", chunk.label)
    return parsed


def cache_key(index: int, chunk: TextChunk, model: str, use_llm: bool) -> str:
    payload = json.dumps(
        {
            "index": index,
            "label": chunk.label,
            "text_hash": source_hash(chunk.text),
            "model": model,
            "prompt_hash": source_hash(prompt_text("ingest_map_extract.md")) if use_llm else "no-llm",
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def extract_chunks(
    row: Mapping[str, Any] | Any,
    chunks: list[TextChunk],
    *,
    model: str,
    use_llm: bool,
    use_llm_map: bool,
    cache_dir: Path,
    parallel_map: int,
) -> list[dict[str, Any]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    results: dict[int, dict[str, Any]] = {}

    def build_extract(index: int, chunk: TextChunk) -> tuple[int, dict[str, Any]]:
        map_with_llm = use_llm and use_llm_map
        path = cache_dir / f"{index:03d}-{cache_key(index, chunk, model, map_with_llm)}.json"
        if path.exists():
            return index, json.loads(path.read_text(encoding="utf-8"))
        if map_with_llm:
            try:
                extract = llm_chunk_extract(row, chunk, model)
            except Exception as exc:
                extract = heuristic_chunk_extract(chunk)
                extract["llm_error"] = str(exc)
        else:
            extract = heuristic_chunk_extract(chunk)
            if use_llm and not use_llm_map:
                extract["notes"] = "Heuristic chunk extract; LLM map stage was skipped to avoid API quota fan-out."
        extract["chunk_index"] = index
        extract["chunk_label"] = chunk.label
        path.write_text(json.dumps(extract, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return index, extract

    if parallel_map <= 1 or len(chunks) <= 1:
        completed = [build_extract(index, chunk) for index, chunk in enumerate(chunks, start=1)]
    else:
        completed = []
        with ThreadPoolExecutor(max_workers=parallel_map) as executor:
            futures = [executor.submit(build_extract, index, chunk) for index, chunk in enumerate(chunks, start=1)]
            for future in as_completed(futures):
                completed.append(future.result())

    for index, extract in completed:
        results[index] = extract
    return [results[index] for index in sorted(results)]


def strip_top_title(markdown: str) -> str:
    text = markdown.strip()
    text = re.sub(r"^---\n.*?\n---\n+", "", text, count=1, flags=re.DOTALL)
    text = re.sub(r"^# .+\n+", "", text, count=1, flags=re.MULTILINE)
    return text.strip()


def deterministic_body(row: Mapping[str, Any] | Any, extracts: list[dict[str, Any]]) -> str:
    lines = [
        "## Scientific Question",
        "",
        "Requires LLM reduce or human review.",
        "",
        "## Data",
        "",
        "Requires LLM reduce or human review.",
        "",
        "## Method",
        "",
        "Requires LLM reduce or human review.",
        "",
        "## Main Results",
        "",
        row_value(row, "abstract") or "No abstract available.",
        "",
        "## Limitations",
        "",
        "Requires LLM reduce or human review.",
        "",
        "## Follow-up Questions",
        "",
        "- Which section or page contains the strongest quantitative result?",
        "- Which limitations are stated in the discussion or conclusion?",
        "",
        "## Full-text Evidence Inventory",
        "",
    ]
    for extract in extracts:
        lines.extend([f"### {extract.get('chunk_label') or extract.get('source')}", "", str(extract.get("evidence_excerpt") or "").strip(), ""])
    return "\n".join(lines).strip()


def reduce_body(
    row: Mapping[str, Any] | Any,
    existing_wiki: str,
    extracts: list[dict[str, Any]],
    model: str,
    *,
    max_extract_chars: int = 120000,
    num_predict: int = 4096,
    timeout: float = 600.0,
) -> str:
    prompt_extracts = extracts_for_reduce_prompt(extracts)
    arxiv_id = str(row_value(row, "arxiv_id") or "")
    user_content = (
        f"Paper ID: {row_value(row, 'arxiv_id')}\n"
        f"Title: {row_value(row, 'title')}\n"
        f"Categories: {row_value(row, 'categories') or ''}\n"
        f"Abstract:\n{row_value(row, 'abstract') or ''}\n\n"
        "Source extracts JSON:\n"
        f"{reduce_extracts_json(prompt_extracts, max_extract_chars)}"
    )
    response = chat(
        [
            {"role": "system", "content": prompt_text("ingest_reduce_paper.md")},
            {"role": "user", "content": user_content},
        ],
        model=model,
        timeout=timeout,
        options={"num_predict": num_predict, "temperature": 0.1},
        think=False,
    )
    return strip_same_paper_arxiv_citations(normalize_chunk_references(strip_top_title(response), extracts), arxiv_id)


def markdown_sections(markdown: str) -> dict[str, str]:
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", markdown, flags=re.MULTILINE))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        sections[match.group(1).strip()] = markdown[start:end].strip()
    return sections


SECTION_EXTRACT_FIELDS = {
    "Scientific Question": "scientific_question",
    "Data": "data",
    "Method": "method",
    "Main Results": "main_results",
    "Limitations": "limitations",
    "Follow-up Questions": "follow_up_questions",
}


def section_bullets_from_extracts(
    extracts: list[dict[str, Any]],
    section: str,
    *,
    limit: int = 8,
) -> list[str]:
    field = SECTION_EXTRACT_FIELDS[section]
    bullets: list[str] = []
    seen: set[str] = set()
    for fallback_index, extract in enumerate(extracts, start=1):
        raw_values = extract.get(field) or []
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        citation = citation_label_for_extract(extract, fallback_index)
        for value in values:
            text = strip_markdown_inline(str(value))
            if not text or text.lower() in seen:
                continue
            seen.add(text.lower())
            bullets.append(f"- {text} ({citation})")
            if len(bullets) >= limit:
                return bullets
    return bullets


def fallback_section_bullets(row: Mapping[str, Any] | Any, section: str, extracts: list[dict[str, Any]]) -> list[str]:
    if section == "Limitations":
        return ["- Explicit limitations are not stated in the extracted passages; review the full discussion before reusing these claims."]
    if section == "Follow-up Questions":
        title = strip_markdown_inline(str(row_value(row, "title") or "this paper"))
        return [f"- Which additional observations would most directly test the main claims in {title}?"]
    if section == "Main Results" and row_value(row, "abstract"):
        return [f"- {strip_markdown_inline(str(row_value(row, 'abstract')))} (abstract)"]
    return section_bullets_from_extracts(extracts, section, limit=3) or ["- Not confirmed in the extracted source context."]


def coerce_required_sections(row: Mapping[str, Any] | Any, markdown: str, extracts: list[dict[str, Any]]) -> str:
    if not structural_validation_errors(markdown):
        return markdown
    sections = markdown_sections(markdown)
    lines: list[str] = []
    for section in DEFAULT_SECTIONS:
        body = sections.get(section, "").strip()
        if body:
            bullets = [body]
        else:
            bullets = section_bullets_from_extracts(extracts, section)
            if not bullets:
                bullets = fallback_section_bullets(row, section, extracts)
        lines.extend([f"## {section}", "", *bullets, ""])
    return "\n".join(lines).strip()


def unescaped_count(text: str, char: str) -> int:
    return len(re.findall(rf"(?<!\\){re.escape(char)}", text))


def structural_validation_errors(markdown: str) -> list[str]:
    sections = markdown_sections(markdown)
    errors: list[str] = []
    for section in DEFAULT_SECTIONS:
        body = sections.get(section, "")
        if not body:
            errors.append(f"missing_or_empty_section:{section}")
    section_names = list(sections)
    if section_names and section_names[-1] != "Follow-up Questions":
        errors.append(f"last_section:{section_names[-1]}")
    stripped = markdown.rstrip()
    last_line = stripped.splitlines()[-1].strip() if stripped else ""
    if re.search(r"[_\^]\{[^}]*$", last_line):
        errors.append("open_tex_brace_at_eof")
    if len(re.findall(r"(?<!\\)\*\*", last_line)) % 2 == 1:
        errors.append("open_bold_marker_at_eof")
    if unescaped_count(last_line, "`") % 2 == 1:
        errors.append("open_code_marker_at_eof")
    if re.search(r"[-+*/=,;:({\[_\\]$", last_line):
        errors.append("dangling_punctuation_at_eof")
    if re.fullmatch(r"[-*+]\s*", last_line):
        errors.append("empty_bullet_at_eof")
    if unescaped_count(markdown, "$") % 2 == 1 and (
        re.search(r"\$[^$]*$", last_line) or any(error.startswith("missing_or_empty_section") for error in errors)
    ):
        errors.append("open_inline_math")
    return errors


def validate_structural_markdown(markdown: str) -> None:
    errors = structural_validation_errors(markdown)
    if errors:
        raise StructuralValidationError(", ".join(errors))


NUMERIC_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("redshift", re.compile(r"\bz\s*(?:=|~|≈|<|>|≤|≥|\\sim|\\lesssim|\\gtrsim|\s)*\d+(?:\.\d+)?(?:\s*(?:-|–|to)\s*\d+(?:\.\d+)?)?", re.IGNORECASE)),
    ("percentage", re.compile(r"(?:~|≈|<|>|≤|≥|\\sim|\\lesssim|\\gtrsim|\\ge|\\le)?\s*\d+(?:\.\d+)?\s*\\?%", re.IGNORECASE)),
    ("unit_range", re.compile(r"\b\d+(?:\.\d+)?\s*(?:-|–|to)\s*\d+(?:\.\d+)?\s*(?:Å|Angstroms?|mag|kpc|Mpc|pc|arcsec|dex|Gyr|Myr|yr)\b", re.IGNORECASE)),
    ("unit_value", re.compile(r"(?:~|≈|<|>|≤|≥|\\sim|\\lesssim|\\gtrsim|\\ge|\\le)?\s*\b\d+(?:\.\d+)?\s*(?:Å|Angstroms?|mag|kpc|Mpc|pc|arcsec|dex|Gyr|Myr|yr)\b", re.IGNORECASE)),
    ("sn_threshold", re.compile(r"\bS/N\s*(?:=|<|>|<=|>=|≤|≥)\s*\d+(?:\.\d+)?", re.IGNORECASE)),
    ("comma_number", re.compile(r"(?<![\w.])\d{1,3}(?:,\d{1,3})+(?![\w.])")),
    ("count", re.compile(r"\b(?:\d{1,3}(?:,\d{1,3})+|\d+)\s+(?:galaxies|galaxy|ETGs?|spirals?|S0s|indices|samples|objects)\b", re.IGNORECASE)),
]


def candidate_validation_text(candidate: str) -> str:
    sections = markdown_sections(candidate)
    selected = [sections.get(section, "") for section in DEFAULT_SECTIONS]
    return "\n\n".join(part for part in selected if part) or candidate


def normalize_numeric_text(text: str) -> str:
    replacements = {
        "−": "-",
        "–": "-",
        "—": "-",
        "\\%": "%",
        "\\sim": "~",
        "\\gtrsim": ">=",
        "\\lesssim": "<=",
        "\\ge": ">=",
        "\\le": "<=",
        "≥": ">=",
        "≤": "<=",
        "≳": ">=",
        "≲": "<=",
        "≈": "~",
        "∼": "~",
        "\u00a0": " ",
    }
    normalized = text
    for old, new in replacements.items():
        normalized = normalized.replace(old, new)
    normalized = re.sub(r"[$*_`{}]", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip().lower()


def compact_numeric_text(text: str) -> str:
    return re.sub(r"[\s,_{}$*`\\]+", "", normalize_numeric_text(text))


def numeric_variants(token: str, *, preserve_comma_groups: bool = False) -> set[str]:
    normalized = normalize_numeric_text(token)
    variants = {normalized, normalized.replace(",", " ")}
    stripped = re.sub(r"^(?:[<>=~]+\s*)+", "", normalized)
    if stripped and stripped != normalized:
        variants.add(stripped)
    if not preserve_comma_groups:
        variants.add(normalized.replace(",", ""))
        variants.add(normalized.replace(" ", ""))
    variants.add(normalized.replace("angstrom", "å").replace("angstroms", "å"))
    return {variant for variant in variants if variant}


def has_bad_comma_grouping(token: str) -> bool:
    comma_numbers = re.findall(r"\b\d{1,3}(?:,\d{1,3})+\b", token)
    return any(not re.fullmatch(r"\d{1,3}(?:,\d{3})+", number) for number in comma_numbers)


def source_contains_variant(source: str, variant: str) -> bool:
    if not variant:
        return False
    if variant[0].isdigit() or variant[-1].isdigit():
        return re.search(rf"(?<!\d){re.escape(variant)}(?!\d)", source) is not None
    return variant in source


def source_contains_count(source: str, token: str) -> bool:
    normalized = normalize_numeric_text(token)
    match = re.fullmatch(
        r"(?P<number>\d{1,3}(?:,\d{1,3})+|\d+)\s+(?P<noun>galaxies|galaxy|etgs?|spirals?|s0s|indices|samples|objects)",
        normalized,
        flags=re.IGNORECASE,
    )
    if not match:
        return False
    number = match.group("number")
    noun = match.group("noun")
    number_variants = {number, number.replace(",", " "), number.replace(",", "")}
    number_pattern = "|".join(re.escape(variant) for variant in sorted(number_variants, key=len, reverse=True))
    return re.search(rf"(?<!\d)(?:{number_pattern})(?!\d)(?:\s+\w+){{0,2}}\s+{re.escape(noun)}\b", source) is not None


def extract_numeric_tokens(text: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for kind, pattern in NUMERIC_PATTERNS:
        for match in pattern.finditer(text):
            token = " ".join(match.group(0).split())
            if not token:
                continue
            start = max(0, match.start() - 90)
            end = min(len(text), match.end() + 90)
            context = " ".join(text[start:end].split())
            key = (kind, normalize_numeric_text(token), context)
            if key in seen:
                continue
            seen.add(key)
            findings.append({"kind": kind, "token": token, "context": context, "bad_comma_grouping": has_bad_comma_grouping(token)})
    return findings


def validate_numeric_claims(candidate: str, source_blobs: dict[str, str]) -> NumericValidationResult:
    raw_findings = extract_numeric_tokens(candidate_validation_text(candidate))
    source_indexes = {
        name: {"normalized": normalize_numeric_text(text), "compact": compact_numeric_text(text)}
        for name, text in source_blobs.items()
        if text
    }
    checked: list[dict[str, Any]] = []
    for finding in raw_findings:
        token = finding["token"]
        preserve_comma_groups = finding["kind"] in {"comma_number", "count"} and "," in token
        variants = numeric_variants(token, preserve_comma_groups=preserve_comma_groups)
        use_compact = finding["kind"] not in {"comma_number", "count"}
        compact_variants = {compact_numeric_text(variant) for variant in variants} if use_compact else set()
        found_in: list[str] = []
        for source_name, source_index in source_indexes.items():
            if any(source_contains_variant(source_index["normalized"], variant) for variant in variants):
                found_in.append(source_name)
                continue
            if finding["kind"] == "count" and source_contains_count(source_index["normalized"], token):
                found_in.append(source_name)
                continue
            if use_compact and any(source_contains_variant(source_index["compact"], variant) for variant in compact_variants):
                found_in.append(source_name)
        note = ""
        if not found_in:
            note = "not found in validation sources"
        if finding["bad_comma_grouping"]:
            note = (note + "; " if note else "") + "nonstandard comma grouping"
        checked.append({**finding, "status": "ok" if found_in and not finding["bad_comma_grouping"] else "warning", "found_in": found_in, "note": note})
    return NumericValidationResult(
        checked_count=len(checked),
        warning_count=sum(1 for finding in checked if finding["status"] != "ok"),
        findings=checked,
        sources_checked=sorted(source_indexes),
    )


def write_numeric_validation_report(arxiv_id: str, validation: NumericValidationResult) -> tuple[Path, Path]:
    safe = safe_id(arxiv_id)
    report_dir = project_path("reports", "ingest-validation")
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / f"{safe}.validation.json"
    md_path = report_dir / f"{safe}.validation.md"
    json_path.write_text(
        json.dumps(
            {
                "checked_count": validation.checked_count,
                "warning_count": validation.warning_count,
                "sources_checked": validation.sources_checked,
                "findings": validation.findings,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    warning_rows = [finding for finding in validation.findings if finding["status"] != "ok"]
    rows = []
    for finding in warning_rows[:50]:
        rows.append(
            "| {status} | `{kind}` | `{token}` | {found} | {note} | {context} |".format(
                status=finding["status"],
                kind=finding["kind"],
                token=finding["token"].replace("|", "\\|"),
                found=", ".join(finding["found_in"]) if finding["found_in"] else "-",
                note=(finding["note"] or "-").replace("|", "\\|"),
                context=finding["context"].replace("|", "\\|"),
            )
        )
    table = "\n".join(rows) if rows else "| ok | - | - | - | - | No warnings. |"
    md_path.write_text(
        "# Numeric Validation Report\n\n"
        f"- Paper ID: {arxiv_id}\n"
        f"- Checked numeric tokens: {validation.checked_count}\n"
        f"- Warnings: {validation.warning_count}\n"
        f"- Sources checked: {', '.join(validation.sources_checked) or 'none'}\n\n"
        "| Status | Type | Token | Found in | Note | Context |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        f"{table}\n",
        encoding="utf-8",
    )
    return json_path, md_path


def markdown_from_pdf(row: Mapping[str, Any] | Any, *, strict: bool = False) -> tuple[Path, str] | None:
    out_path = project_path("data", "markdown", f"{safe_id(str(row_value(row, 'arxiv_id')))}.md")
    if out_path.exists():
        text = read_text(out_path)
        if text.strip():
            return out_path, text
    pdf_rel = row_value(row, "pdf_path")
    if not pdf_rel:
        return None
    pdf_path = project_path(str(pdf_rel))
    if not pdf_path.exists():
        return None
    try:
        import pymupdf4llm
    except Exception as exc:
        if strict:
            raise RuntimeError(f"PDF-to-Markdown conversion dependency is unavailable: {exc}") from exc
        return None
    try:
        markdown = pymupdf4llm.to_markdown(str(pdf_path))
    except Exception as exc:
        if strict:
            raise RuntimeError(f"PDF-to-Markdown conversion failed: {exc}") from exc
        return None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")
    return out_path, markdown


def source_material(row: Mapping[str, Any] | Any, requested_source: str) -> tuple[str, Path, str]:
    requested = requested_source.lower()
    if requested in {"auto", "markdown"}:
        markdown = markdown_from_pdf(row, strict=requested == "markdown")
        if markdown:
            path, text = markdown
            return "markdown", path, text
        if requested == "markdown":
            raise RuntimeError("Markdown source requested, but PDF-to-Markdown conversion is unavailable.")
    text_rel = row_value(row, "text_path")
    if not text_rel:
        raise RuntimeError("Extracted text path is missing.")
    path = project_path(str(text_rel))
    text = read_text(path)
    if not text.strip():
        raise RuntimeError(f"Extracted text is empty: {path}")
    return "text", path, text


def improve_paper_body(
    row: Mapping[str, Any] | Any,
    *,
    source: str = "auto",
    map_model: str | None = None,
    reduce_model: str | None = None,
    use_llm: bool = True,
    max_chunk_chars: int = 9000,
    max_chunks: int | None = None,
    parallel_map: int = 4,
    validate_numeric: bool = True,
    reduce_input_max_chars: int = 120000,
    reduce_num_predict: int = 4096,
    reduce_timeout: float = 600.0,
    reduce_retries: int = 1,
) -> IngestResult:
    arxiv_id = str(row_value(row, "arxiv_id"))
    map_model = map_model or chat_model()
    reduce_model = reduce_model or map_model
    source_type, source_path, source_text = source_material(row, source)
    if source_type == "markdown":
        source_text = strip_source_reference_anchors(source_text)
    chunks = (
        split_markdown_text(source_text, max_chunk_chars, max_chunks)
        if source_type == "markdown"
        else split_extracted_text(source_text, max_chunk_chars, max_chunks)
    )
    if not chunks:
        raise RuntimeError(f"No source chunks found for {arxiv_id}")
    if source_type == "markdown":
        ensure_source_reference_anchors(source_path, source_text, chunks)
    existing_wiki = read_text(
        paper_page_path(
            arxiv_id,
            title=row_value(row, "title"),
            authors=row_value(row, "authors_json"),
            year=row_value(row, "published") or row_value(row, "announced_date") or row_value(row, "updated"),
        )
    ) or read_text(paper_page_path(arxiv_id))
    cache_dir = project_path(
        "data",
        "cache",
        "wiki_ingest",
        safe_id(arxiv_id),
        f"{source_type}-{source_hash(source_text)}-{safe_name(map_model)}",
    )
    extracts = extract_chunks(
        row,
        chunks,
        model=map_model,
        use_llm=use_llm,
        use_llm_map=use_llm_map_stage(),
        cache_dir=cache_dir,
        parallel_map=max(1, parallel_map),
    )
    if use_llm:
        generation_error = ""
        try:
            last_error: Exception | None = None
            for attempt in range(reduce_retries + 1):
                try:
                    body = reduce_body(
                        row,
                        existing_wiki,
                        extracts,
                        reduce_model,
                        max_extract_chars=reduce_input_max_chars,
                        num_predict=reduce_num_predict * (2 if attempt else 1),
                        timeout=reduce_timeout,
                    )
                    body = coerce_required_sections(row, body, extracts)
                    validate_structural_markdown(body)
                    break
                except Exception as exc:
                    last_error = exc
            else:
                raise last_error or StructuralValidationError("structural validation failed")
            generation_mode = "llm_map_reduce"
        except Exception as exc:
            generation_error = str(exc)
            body = deterministic_body(row, extracts)
            body += f"\n\n## Generation Error\n\nLLM reduce failed: `{exc}`\n"
            generation_mode = "map_llm_reduce_fallback"
    else:
        generation_error = "LLM disabled"
        body = deterministic_body(row, extracts)
        generation_mode = "deterministic_no_llm"

    body = link_source_citations(body, extracts, source_path)

    validation = None
    validation_json_path = None
    validation_markdown_path = None
    if validate_numeric:
        text_path = project_path(str(row_value(row, "text_path"))) if row_value(row, "text_path") else None
        validation = validate_numeric_claims(
            body,
            {
                "source_material": source_text,
                "chunk_extracts": json.dumps(extracts, ensure_ascii=False),
                "extracted_text": read_text(text_path),
            },
        )
        validation_json_path, validation_markdown_path = write_numeric_validation_report(arxiv_id, validation)

    return IngestResult(
        body=body,
        source_type=source_type,
        source_material_path=source_path,
        chunk_count=len(chunks),
        chunk_cache_dir=cache_dir,
        validation=validation,
        validation_markdown_path=validation_markdown_path,
        validation_json_path=validation_json_path,
        generation_mode=generation_mode,
        map_model=map_model,
        reduce_model=reduce_model,
        generation_error=generation_error,
    )
