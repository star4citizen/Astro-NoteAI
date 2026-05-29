from __future__ import annotations

import re
from dataclasses import dataclass

from .config import project_path
from .qmd_client import collections_for, qmd_search

KOREAN_QUERY_EXPANSIONS: dict[str, list[str]] = {
    "환경": ["environment", "environmental", "density", "cluster", "group", "void", "filament"],
    "환경효과": ["environment", "environmental", "density", "cluster", "group", "void", "filament"],
    "밀도": ["density", "overdensity", "cluster", "environment"],
    "퀜칭": ["quenching", "quenched", "passive", "quiescent"],
    "소광": ["quenching", "quenched", "passive", "quiescent"],
    "수동화": ["quenching", "quenched", "passive", "quiescent"],
    "별형성": ["star formation", "star-forming", "sfr"],
    "별 형성": ["star formation", "star-forming", "sfr"],
    "항성질량": ["stellar mass", "mass function"],
    "항성 질량": ["stellar mass", "mass function"],
    "질량": ["stellar mass", "mass function"],
    "형태": ["morphology", "morphological", "sersic", "disk", "early-type"],
    "가스": ["gas", "h i", "neutral hydrogen", "cold gas"],
    "먼지": ["dust", "dust attenuation"],
    "금속": ["metallicity", "metal"],
    "병합": ["merger", "post-merger", "dual agn"],
    "합병": ["merger", "post-merger", "dual agn"],
    "고적색편이": ["high-redshift", "high redshift", "jwst"],
    "적색편이": ["redshift", " z "],
    "관측": ["observation", "observational", "survey", "spectroscopy", "photometry"],
    "자료": ["data", "dataset", "survey", "sample"],
    "방법": ["method", "methodology", "model", "simulation"],
    "시뮬레이션": ["simulation", "simulations", "cosmological simulation"],
    "기계학습": ["machine learning", "deep learning", "neural network"],
    "머신러닝": ["machine learning", "deep learning", "neural network"],
    "은하": ["galaxy", "galaxies"],
    "외부은하": ["galaxy", "galaxies", "extragalactic"],
    "군집": ["cluster", "group", "environment", "overdensity"],
    "위성은하": ["satellite galaxy", "satellite", "environment"],
    "중심은하": ["central galaxy", "central", "halo"],
    "필라멘트": ["filament", "cosmic web", "large-scale structure"],
    "은하단": ["cluster", "galaxy cluster", "environment"],
}


@dataclass
class RetrievedPage:
    path: str
    score: float
    excerpt: str


def tokenize(text: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[\w.+-]+", text, flags=re.UNICODE) if len(token) > 1}


def expand_query_terms(query: str) -> set[str]:
    terms = tokenize(query)
    lowered = query.lower()
    for korean, expansions in KOREAN_QUERY_EXPANSIONS.items():
        if korean in lowered:
            for expansion in expansions:
                terms.update(tokenize(expansion))
    return terms


def contains_korean(text: str) -> bool:
    return bool(re.search(r"[가-힣]", text))


def normalize_korean_query_for_lexical_search(query: str) -> str:
    stopwords = {
        "알려주세요",
        "알려줘",
        "찾아줘",
        "찾아주세요",
        "무엇인가요",
        "뭔가요",
        "있나요",
        "있습니까",
        "해주세요",
        "논문",
    }
    particles = ("께서는", "에서는", "으로는", "이라는", "라는", "에서", "에게", "한테", "부터", "까지", "으로", "로", "을", "를", "이", "가", "은", "는", "의", "와", "과", "도", "만")
    normalized: list[str] = []
    for token in re.findall(r"[가-힣]+|[A-Za-z0-9.+-]+", query):
        if token in stopwords:
            continue
        if re.fullmatch(r"[가-힣]+", token):
            for particle in particles:
                if len(token) > len(particle) + 1 and token.endswith(particle):
                    token = token[: -len(particle)]
                    break
        if len(token) > 1 and token not in stopwords:
            normalized.append(token)
    return " ".join(normalized)


def excerpt_for(text: str, terms: set[str], max_chars: int = 1600) -> str:
    lowered = text.lower()
    first_hit = min((lowered.find(term) for term in terms if lowered.find(term) >= 0), default=0)
    start = max(0, first_hit - 400)
    return text[start : start + max_chars].strip()


def score_text(query: str, text: str, title: str = "") -> int:
    terms = expand_query_terms(query)
    if not terms:
        return 0
    lowered_text = text.lower()
    lowered_title = title.lower()
    tokens = tokenize(text)
    score = 0
    for term in terms:
        in_title = term in lowered_title
        exact_count = lowered_text.count(term)
        token_hit = term in tokens
        if in_title:
            score += 8
        if exact_count:
            score += min(exact_count, 6) * 2
        elif token_hit:
            score += 1
    query_lower = query.lower()
    for phrase in re.findall(r"[A-Za-z][A-Za-z -]{4,}", query_lower):
        phrase = phrase.strip()
        if phrase and phrase in lowered_text:
            score += 5
    if "environment" in terms and any(word in lowered_text for word in ["cluster", "group", "overdensity", "filament"]):
        score += 4
    return score


def retrieval_query(query: str) -> str:
    terms = expand_query_terms(query)
    original_terms = tokenize(query)
    expanded = sorted(terms - original_terms)
    if not expanded:
        return query
    suffix = " ".join(expanded)
    return f"{query} {suffix}"[:800]


def qmd_search_pages(query: str, max_pages: int = 8, collections: list[str] | None = None) -> list[RetrievedPage]:
    qmd_collections = collections if collections is not None else collections_for("general_collections")
    queries = []
    if contains_korean(query):
        normalized_query = normalize_korean_query_for_lexical_search(query)
        if normalized_query:
            queries.append(normalized_query)
    queries.append(query)
    expanded_query = retrieval_query(query)
    if expanded_query != query:
        queries.append(expanded_query)
    pages: list[RetrievedPage] = []
    seen: set[str] = set()
    terms = expand_query_terms(query)
    for qmd_query in queries:
        hits = qmd_search(
            qmd_query,
            max_results=max(max_pages * 2, max_pages),
            collections=qmd_collections,
        )
        for hit in hits:
            if hit.path in seen:
                continue
            seen.add(hit.path)
            excerpt = hit.snippet
            if not excerpt and not hit.path.startswith("qmd://"):
                path = project_path(hit.path)
                if path.exists():
                    excerpt = excerpt_for(path.read_text(encoding="utf-8", errors="ignore"), terms)
            if not excerpt:
                continue
            pages.append(RetrievedPage(path=hit.path, score=hit.score * 1000, excerpt=excerpt))
            if len(pages) >= max_pages:
                return pages
    return pages


def search_wiki(query: str, max_pages: int = 8) -> list[RetrievedPage]:
    terms = expand_query_terms(query)
    pages: list[RetrievedPage] = []
    wiki_root = project_path("wiki")
    if not wiki_root.exists():
        return []
    for path in wiki_root.rglob("*.md"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        score = score_text(query, text, path.stem)
        if score == 0:
            continue
        rel = str(path.relative_to(project_path())).replace("\\", "/")
        pages.append(RetrievedPage(path=rel, score=score, excerpt=excerpt_for(text, terms)))
    pages.sort(key=lambda item: item.score, reverse=True)
    qmd_pages = qmd_search_pages(query, max_pages=max_pages)
    if qmd_pages:
        merged: dict[str, RetrievedPage] = {page.path: page for page in qmd_pages}
        for page in pages:
            if page.path in merged:
                current = merged[page.path]
                merged[page.path] = RetrievedPage(page.path, current.score + page.score, current.excerpt)
            else:
                merged[page.path] = page
        return sorted(merged.values(), key=lambda item: item.score, reverse=True)[:max_pages]
    return pages[:max_pages]


def build_context(pages: list[RetrievedPage]) -> str:
    chunks = []
    for page in pages:
        chunks.append(f"Source: {page.path}\n{page.excerpt}")
    return "\n\n---\n\n".join(chunks)
