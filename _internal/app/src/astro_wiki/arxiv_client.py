from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

OAI_URL = "https://oaipmh.arxiv.org/oai"
QUERY_URL = "https://export.arxiv.org/api/query"
ARXIV_NS = {"oai": "http://www.openarchives.org/OAI/2.0/", "ax": "http://arxiv.org/OAI/arXiv/"}
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def polite_headers() -> dict[str, str]:
    email = os.getenv("ASTRO_WIKI_CONTACT_EMAIL", "").strip()
    ua = "Astro-ph-LLM-Wiki/0.1"
    if email:
        ua = f"{ua} (mailto:{email})"
    headers = {"User-Agent": ua}
    if email:
        headers["From"] = email
    return headers


def normalize_arxiv_id(raw_id: str) -> tuple[str, int]:
    raw_id = raw_id.strip().replace("oai:arXiv.org:", "").replace("arXiv:", "")
    match = re.match(r"(?P<base>.+?)v(?P<version>\d+)$", raw_id)
    if match:
        return match.group("base"), int(match.group("version"))
    return raw_id, 1


def _text(node: ET.Element | None, default: str = "") -> str:
    if node is None or node.text is None:
        return default
    return " ".join(node.text.split())


def _parse_author(author: ET.Element) -> str:
    keyname = _text(author.find("ax:keyname", ARXIV_NS))
    forenames = _text(author.find("ax:forenames", ARXIV_NS))
    return " ".join(part for part in [forenames, keyname] if part)


def _record_to_paper(record: ET.Element) -> dict[str, Any] | None:
    header = record.find("oai:header", ARXIV_NS)
    if header is None or header.attrib.get("status") == "deleted":
        return None
    meta = record.find("oai:metadata/ax:arXiv", ARXIV_NS)
    if meta is None:
        return None

    arxiv_id, version = normalize_arxiv_id(_text(meta.find("ax:id", ARXIV_NS)))
    versions = meta.findall("ax:versions/ax:version", ARXIV_NS)
    if versions:
        version_text = _text(versions[-1].find("ax:version", ARXIV_NS))
        version_match = re.search(r"(\d+)", version_text)
        if version_match:
            version = int(version_match.group(1))

    categories = _text(meta.find("ax:categories", ARXIV_NS))
    primary_category = categories.split()[0] if categories else ""
    created = _text(meta.find("ax:created", ARXIV_NS)) or None
    updated = _text(meta.find("ax:updated", ARXIV_NS)) or None
    datestamp = _text(header.find("oai:datestamp", ARXIV_NS)) or None
    authors = [_parse_author(author) for author in meta.findall("ax:authors/ax:author", ARXIV_NS)]
    authors = [author for author in authors if author]
    return {
        "arxiv_id": arxiv_id,
        "version": version,
        "title": _text(meta.find("ax:title", ARXIV_NS)),
        "authors": authors,
        "abstract": _text(meta.find("ax:abstract", ARXIV_NS)),
        "categories": categories,
        "primary_category": primary_category,
        "published": created,
        "updated": updated or datestamp,
        "announced_date": datestamp,
        "abs_url": f"https://arxiv.org/abs/{arxiv_id}",
        "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}.pdf",
    }


def harvest_oai(
    *,
    from_date: date,
    until_date: date,
    request_delay_seconds: float = 3.0,
    max_records: int | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    params: dict[str, str] = {
        "verb": "ListRecords",
        "metadataPrefix": "arXiv",
        "set": "physics:astro-ph",
        "from": from_date.isoformat(),
        "until": until_date.isoformat(),
    }
    resumption_token: str | None = None
    with httpx.Client(headers=polite_headers(), follow_redirects=True, timeout=60.0) as client:
        while True:
            if resumption_token:
                response = client.get(OAI_URL, params={"verb": "ListRecords", "resumptionToken": resumption_token})
            else:
                response = client.get(OAI_URL, params=params)
            response.raise_for_status()
            root = ET.fromstring(response.text)
            error = root.find("oai:error", ARXIV_NS)
            if error is not None:
                code = error.attrib.get("code", "")
                if code == "noRecordsMatch":
                    return records
                raise RuntimeError(f"OAI-PMH error {code}: {_text(error)}")
            for record in root.findall(".//oai:record", ARXIV_NS):
                paper = _record_to_paper(record)
                if paper:
                    records.append(paper)
                    if max_records and len(records) >= max_records:
                        return records
            token_node = root.find(".//oai:resumptionToken", ARXIV_NS)
            resumption_token = _text(token_node) if token_node is not None else ""
            if not resumption_token:
                return records
            time.sleep(request_delay_seconds)


def query_atom(
    *,
    categories: list[str],
    query: str | None = None,
    max_results: int = 100,
    request_delay_seconds: float = 3.0,
) -> list[dict[str, Any]]:
    category_query = " OR ".join(f"cat:{category}" for category in categories)
    search_query = atom_search_query(category_query, query)
    params = {
        "search_query": search_query,
        "start": "0",
        "max_results": str(max_results),
        "sortBy": "lastUpdatedDate",
        "sortOrder": "descending",
    }
    with httpx.Client(headers=polite_headers(), timeout=60.0) as client:
        response = client.get(QUERY_URL, params=params)
        response.raise_for_status()
    time.sleep(request_delay_seconds)
    try:
        import feedparser
    except ModuleNotFoundError:
        return _parse_atom_xml(response.text)

    feed = feedparser.parse(response.text)
    papers: list[dict[str, Any]] = []
    for entry in feed.entries:
        arxiv_id, version = normalize_arxiv_id(entry.id.rsplit("/", 1)[-1])
        categories_list = [tag.term for tag in getattr(entry, "tags", []) if getattr(tag, "term", "")]
        pdf_url = next((link.href for link in getattr(entry, "links", []) if getattr(link, "type", "") == "application/pdf"), None)
        updated = getattr(entry, "updated", None)
        announced_date = None
        if updated:
            try:
                announced_date = datetime.fromisoformat(updated.replace("Z", "+00:00")).date().isoformat()
            except ValueError:
                announced_date = None
        papers.append(
            {
                "arxiv_id": arxiv_id,
                "version": version,
                "title": " ".join(getattr(entry, "title", "").split()),
                "authors": [author.name for author in getattr(entry, "authors", [])],
                "abstract": " ".join(getattr(entry, "summary", "").split()),
                "categories": " ".join(categories_list),
                "primary_category": categories_list[0] if categories_list else "",
                "published": getattr(entry, "published", None),
                "updated": updated,
                "announced_date": announced_date,
                "abs_url": f"https://arxiv.org/abs/{arxiv_id}",
                "pdf_url": pdf_url or f"https://arxiv.org/pdf/{arxiv_id}.pdf",
            }
        )
    return papers


def atom_search_query(category_query: str, query: str | None = None) -> str:
    cleaned = " ".join((query or "").split())
    if not cleaned:
        return category_query
    has_arxiv_field = bool(re.search(r"\b(?:all|ti|abs|au|cat|id|jr|co|rn):", cleaned, flags=re.IGNORECASE))
    if has_arxiv_field or " AND " in cleaned.upper() or " OR " in cleaned.upper():
        text_query = cleaned
    else:
        terms = re.findall(r"[A-Za-z0-9][A-Za-z0-9.+_-]*", cleaned)
        text_query = " AND ".join(f"all:{term}" for term in terms)
    if not text_query:
        return category_query
    return f"({category_query}) AND ({text_query})"


def _parse_atom_xml(xml_text: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    papers: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ATOM_NS):
        entry_id = _text(entry.find("atom:id", ATOM_NS))
        arxiv_id, version = normalize_arxiv_id(entry_id.rsplit("/", 1)[-1])
        categories_list = [node.attrib.get("term", "") for node in entry.findall("atom:category", ATOM_NS)]
        categories_list = [category for category in categories_list if category]
        pdf_url = None
        for link in entry.findall("atom:link", ATOM_NS):
            if link.attrib.get("type") == "application/pdf":
                pdf_url = link.attrib.get("href")
                break
        updated = _text(entry.find("atom:updated", ATOM_NS)) or None
        announced_date = None
        if updated:
            try:
                announced_date = datetime.fromisoformat(updated.replace("Z", "+00:00")).date().isoformat()
            except ValueError:
                announced_date = None
        papers.append(
            {
                "arxiv_id": arxiv_id,
                "version": version,
                "title": _text(entry.find("atom:title", ATOM_NS)),
                "authors": [_text(author.find("atom:name", ATOM_NS)) for author in entry.findall("atom:author", ATOM_NS)],
                "abstract": _text(entry.find("atom:summary", ATOM_NS)),
                "categories": " ".join(categories_list),
                "primary_category": categories_list[0] if categories_list else "",
                "published": _text(entry.find("atom:published", ATOM_NS)) or None,
                "updated": updated,
                "announced_date": announced_date,
                "abs_url": f"https://arxiv.org/abs/{arxiv_id}",
                "pdf_url": pdf_url or f"https://arxiv.org/pdf/{arxiv_id}.pdf",
            }
        )
    return papers


def default_harvest_window(
    last_successful_utc: str | None,
    target_date: date | None = None,
    *,
    current_utc_date: date | None = None,
) -> tuple[date, date]:
    utc_today = current_utc_date or datetime.now(timezone.utc).date()
    until = min(target_date or utc_today, utc_today)
    if last_successful_utc:
        try:
            last = datetime.fromisoformat(last_successful_utc.replace("Z", "+00:00")).date()
        except ValueError:
            last = until - timedelta(days=7)
    else:
        last = until - timedelta(days=7)
    return last - timedelta(days=1), until
