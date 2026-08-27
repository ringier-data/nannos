"""Shared ranking + paging for the runtime tool-discovery searches.

Used by both discovery surfaces so PTC-on (``tools.search``) and PTC-off
(``search_tools``) return the same shape for the same catalog and query:

* :func:`rank` — deterministic token-overlap ranking over precomputed entries;
* :func:`build_page` — slices the ranked hits into a page and wraps it in a
  result object that *says* whether it was truncated.

The result is an object, not a bare list, on purpose. A bare ``top_k`` list gave the
model no way to tell "exactly 10 tools matched" from "10 of 60 shown", so when the
tool it needed ranked 11th it concluded the tool did not exist and gave up on search.
``total_matches``/``truncated``/``next_offset`` make the cap explicit and give it a
way to page.
"""

from __future__ import annotations

import re
from typing import Any, TypedDict

# Default page size for both discovery searches.
DEFAULT_LIMIT = 10
# Upper bound a caller can request per page (keeps a single tool result small).
MAX_LIMIT = 50

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Function words that appear in nearly every tool description. They never count as a
# match on their own: with the full description indexed, "list campaigns for an
# advertiser" would otherwise match the whole catalogue on ``for``/``an`` and report
# ``truncated=True`` for every query — the very confusion the paged result exists to remove.
_STOP_WORDS = frozenset(
    "a an and are as at be by for from in into is it its of on or that the this to via with your".split()
)
# Tokens shorter than this only count as a whole word of the *name* (e.g. ``ad``, ``id``),
# never as a substring anywhere.
_MIN_DESC_TOKEN_LEN = 3


class SearchMatch(TypedDict):
    name: str
    description: str


class SearchResult(TypedDict, total=False):
    matches: list[SearchMatch]
    total_matches: int
    offset: int
    shown: int
    truncated: bool
    next_offset: int | None
    hint: str


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def first_line(description: str | None) -> str:
    if not description:
        return ""
    stripped = description.strip()
    return stripped.splitlines()[0] if stripped else ""


def make_entry(*, name: str, callable_name: str, description: str | None) -> dict[str, Any]:
    """Build a searchable entry.

    ``callable_name`` is what the model types to call the tool (camelCase for PTC,
    the raw name for the native meta-tools). The displayed description is the first
    line only, but matching runs over the whole description so tools whose
    distinguishing words sit on line 2+ stay discoverable.
    """
    desc_full = (description or "").lower()
    name_hay = f"{callable_name} {name}".lower()
    return {
        "name": name,
        "callable": callable_name,
        "desc": first_line(description),
        "name_haystack": name_hay,
        "name_words": set(tokenize(name_hay)),
        "haystack": f"{name_hay} {desc_full}",
        "words": set(tokenize(f"{name_hay} {desc_full}")),
    }


def _score(entry: dict[str, Any], tokens: list[str]) -> tuple[int, int, int] | None:
    """Rank key for one entry, or None if nothing matched.

    Components (all higher-is-better):
    1. weighted overlap — whole-word name hit 3, substring name hit 2, whole-word
       description hit 2, substring description hit 1;
    2. number of distinct query tokens that matched at all (breadth beats one
       token repeated);
    3. number of whole-word hits (``repo`` in ``repo`` beats ``repo`` in ``report``).

    Stop words and very short tokens only match as a whole word of the name, so they
    cannot turn the whole catalogue into "matches".
    """
    weighted = matched = whole = 0
    for tok in tokens:
        name_only = tok in _STOP_WORDS or len(tok) < _MIN_DESC_TOKEN_LEN
        if tok in entry["name_words"]:
            weighted += 3
            whole += 1
        elif name_only:
            continue
        elif tok in entry["name_haystack"]:
            weighted += 2
        elif tok in entry["words"]:
            weighted += 2
            whole += 1
        elif tok in entry["haystack"]:
            weighted += 1
        else:
            continue
        matched += 1
    if matched == 0:
        return None
    return (weighted, matched, whole)


def rank(entries: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """All entries matching ``query``, best first. Ties break on callable name."""
    tokens = list(dict.fromkeys(tokenize(query)))
    if not tokens:
        return []
    scored = []
    for entry in entries:
        key = _score(entry, tokens)
        if key is not None:
            scored.append((key, entry))
    scored.sort(key=lambda pair: (tuple(-c for c in pair[0]), pair[1]["callable"]))
    return [entry for _, entry in scored]


def clamp_limit(limit: int | None) -> int:
    if limit is None or limit < 1:
        return DEFAULT_LIMIT
    return min(limit, MAX_LIMIT)


def build_page(
    hits: list[dict[str, Any]],
    *,
    query: str,
    offset: int | None,
    limit: int | None,
    search_tool_name: str,
) -> SearchResult:
    """Slice ranked ``hits`` into a page and describe the cap explicitly."""
    page_size = clamp_limit(limit)
    start = max(offset or 0, 0)
    total = len(hits)
    page = hits[start : start + page_size]
    end = start + len(page)
    truncated = end < total
    result: SearchResult = {
        "matches": [{"name": e["callable"], "description": e["desc"]} for e in page],
        "total_matches": total,
        "offset": start,
        "shown": len(page),
        "truncated": truncated,
        "next_offset": end if truncated else None,
    }
    if total == 0:
        tokens = tokenize(query)
        if not tokens:
            result["hint"] = "Query contained no searchable words; use a few plain words describing the action."
        else:
            result["hint"] = (
                f"No tool matched any of: {' '.join(tokens)}. Try different or fewer words "
                "(e.g. the object name alone) — the catalog may use other terminology."
            )
    elif start >= total:
        result["hint"] = f"offset {start} is past the last match ({total} total); start from offset 0."
    elif truncated:
        result["hint"] = (
            f"Showing {start + 1}-{end} of {total} matches. If the tool you need is not here, "
            f"call {search_tool_name} again with the same query and offset {end} (next_offset), "
            "or narrow the query."
        )
    else:
        result["hint"] = f"All {total} matching tools shown; no more results for this query."
    return result
