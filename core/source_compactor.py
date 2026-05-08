from __future__ import annotations

from typing import Any


def _truncate(text: Any, max_chars: int) -> str:
    s = "" if text is None else str(text)
    return s[:max_chars]


def _priority(source: dict) -> int:
    st = str(source.get("source_type", "")).lower()
    sn = str(source.get("source_name", "")).lower()
    ms = str(source.get("market_scope", "")).lower()
    if ms == "il" and ("official" in st or "importer" in st or "official" in sn or "importer" in sn):
        return 0
    if ms == "il" and ("review" in st or "spec" in st):
        return 1
    if ms == "il" and "price" in st:
        return 2
    if ms == "il" and "archived" in st:
        return 3
    return 4


def compact_sources_for_model(sources, max_sources=6, max_snippets_per_source=2, max_snippet_chars=220) -> list[dict]:
    seen_urls = set()
    compacted = []
    norm=[x for x in (sources or []) if isinstance(x, dict)]
    for src in sorted(norm, key=_priority):
        if len(compacted) >= max_sources:
            break
        url = src.get("url")
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        snippets = src.get("evidence_snippet", [])
        if isinstance(snippets, str):
            snippets = [snippets]
        snippets = [_truncate(s, max_snippet_chars) for s in (snippets or [])[:max_snippets_per_source]]
        compacted.append(
            {
                "source_id": src.get("source_id"),
                "url": url,
                "title": src.get("title"),
                "source_name": src.get("source_name"),
                "source_type": src.get("source_type"),
                "market_scope": src.get("market_scope"),
                "reliability_score": src.get("reliability_score"),
                "fields_supported": src.get("fields_supported", []),
                "evidence_snippet": snippets,
            }
        )
    return compacted
