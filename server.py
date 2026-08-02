#!/usr/bin/env python3
"""Jellyfin MCP server — read-only library queries + three writes (collection add, favorite set, watched set)."""

import json
import os
import datetime
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

# --- config (env vars) -----------------------------------------------------

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://nas.local:8096")
JELLYFIN_USER = os.environ.get("JELLYFIN_USER", "me")
JELLYFIN_DEFAULT_LIMIT = int(os.environ.get("JELLYFIN_DEFAULT_LIMIT", "50"))
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")

# Bulk enumeration (the `all` op) pages server-side; these bound it so a wrong
# server count or an ignored StartIndex can never spin forever.
JELLYFIN_PAGE = int(os.environ.get("JELLYFIN_PAGE", "200"))          # rows per page
JELLYFIN_MAX_PAGES = int(os.environ.get("JELLYFIN_MAX_PAGES", "500"))
JELLYFIN_MAX_ITEMS = int(os.environ.get("JELLYFIN_MAX_ITEMS", "100000"))
JELLYFIN_YEAR_FLOOR = int(os.environ.get("JELLYFIN_YEAR_FLOOR", "1880"))  # floor for open-ended year ranges

_ssl_verify = os.environ.get("JELLYFIN_SSL_VERIFY", "").lower()
_ssl_verify = _ssl_verify if _ssl_verify else False if JELLYFIN_URL.startswith("http://") else True

mcp = FastMCP("jellyfin", host="0.0.0.0", port=int(os.environ.get("MCP_PORT", "8000")))

# Tool annotations (hints; a client MAY use readOnlyHint/destructive to decide
# whether to prompt). Version-safe: omitted if this mcp build lacks the type.
try:
    from mcp.types import ToolAnnotations
    _READ_ANN = {"annotations": ToolAnnotations(readOnlyHint=True)}
    _WRITE_ANN = {"annotations": ToolAnnotations(readOnlyHint=False, idempotentHint=True)}
except Exception:
    _READ_ANN, _WRITE_ANN = {}, {}

# --- low-level HTTP -------------------------------------------------------

def _request(method: str, path: str, params: dict | None = None):
    """Call Jellyfin API; return parsed JSON or None for empty body. Raise on HTTP error."""
    url = f"{JELLYFIN_URL}{path}"
    headers = {"Authorization": f'MediaBrowser Token="{JELLYFIN_API_KEY}"'}
    r = httpx.request(method, url, params=params, headers=headers, timeout=30, verify=_ssl_verify)
    r.raise_for_status()
    if r.content:
        return r.json()
    return None


def _items(path: str, params: dict | None = None) -> list:
    """GET path; return the 'Items' list."""
    resp = _request("GET", path, params)
    return resp.get("Items", []) if resp else []


def _fetch_all(path: str, params: dict) -> tuple[list, Optional[int], bool]:
    """Fetch EVERY row for a query by paging server-side.

    Returns (items, total_claimed, consistent):
      - items:         all rows gathered across pages
      - total_claimed: Jellyfin's TotalRecordCount from the first page (or None)
      - consistent:    True unless we gathered a different number of rows than the
                       server claimed (e.g. it returned a short page early, a known
                       Jellyfin count quirk) — the caller surfaces this rather than
                       trusting a number it can't stand behind.

    Terminates on the FIRST of: all rows gathered (>= total_claimed), a short or
    empty page, or the page/item safety cap. So it CANNOT loop forever, even if the
    server miscounts or ignores StartIndex.
    """
    collected: list = []
    total: Optional[int] = None
    start, pages = 0, 0
    while True:
        pages += 1
        p = dict(params)
        p.update({"Limit": str(JELLYFIN_PAGE), "StartIndex": str(start),
                  "EnableTotalRecordCount": "true"})
        resp = _request("GET", path, p) or {}
        items = resp.get("Items", []) or []
        if total is None:
            total = resp.get("TotalRecordCount")
        collected.extend(items)
        if not items:                                   # ran out of rows
            break
        if len(items) < JELLYFIN_PAGE:                  # last (short) page
            break
        if total is not None and len(collected) >= total:
            break
        if pages >= JELLYFIN_MAX_PAGES or len(collected) >= JELLYFIN_MAX_ITEMS:
            break
        start += len(items)
    consistent = (total is None) or (len(collected) == total)
    return collected, total, consistent


def _count_movies(uid: str, year: int | None = None, genre: str | None = None,
                  year_from: int | None = None, year_to: int | None = None) -> dict:
    """Count movies matching an optional year (or year range) and/or genre.

    Uses Jellyfin's TotalRecordCount, which is reliable for the `Years=`/`Genres=`
    query filters (unlike the watched/favorite post-filters — hence `count` accepts
    only these axes). A single `year` wins if given; otherwise `year_from`/`year_to`
    define an INCLUSIVE range, expanded to a comma-list of years (Jellyfin has no
    native range filter but accepts a comma-delimited `Years=` list, which keeps the
    count on the trusted path). An open-ended range fills the missing bound
    (floor = JELLYFIN_YEAR_FLOOR, ceiling = current year).

    Verifies exactly when the whole result fits on one page; for multi-page results
    it trusts the documented-reliable total. Never enumerates a large library just
    to count, and reports a single-page count/rows mismatch instead of a number it
    can't back.
    """
    filters: dict = {}
    years_param: str | None = None

    if year is not None:                                # single year wins
        years_param = str(year)
        filters["year"] = year
    elif year_from is not None or year_to is not None:  # inclusive range
        lo = year_from if year_from is not None else JELLYFIN_YEAR_FLOOR
        hi = year_to if year_to is not None else datetime.date.today().year
        if lo > hi:
            raise ValueError(f"year_from ({lo}) is after year_to ({hi})")
        if hi - lo > 300:
            raise ValueError(f"year range {lo}-{hi} is too wide")
        years_param = ",".join(str(y) for y in range(lo, hi + 1))
        filters["year_from"], filters["year_to"] = lo, hi   # echo effective bounds

    params = {
        "IncludeItemTypes": "Movie", "Recursive": "true",
        "EnableTotalRecordCount": "true",
        "Limit": str(JELLYFIN_PAGE), "StartIndex": "0",
    }
    if years_param is not None:
        params["Years"] = years_param
    if genre:
        params["Genres"] = genre
        filters["genre"] = genre

    resp = _request("GET", f"/Users/{uid}/Items", params) or {}
    rows = len(resp.get("Items", []) or [])
    total = resp.get("TotalRecordCount")
    out: dict = {"op": "count", "filters": filters}

    # For the year/genre filters this tool accepts, Jellyfin's TotalRecordCount is
    # reliable, so it simply IS the count. We add fields ONLY to raise a genuine
    # problem; the absence of a warning means the number can be trusted as-is.
    if total is None:                                   # server gave no total at all
        out.update({"count": rows,
                    "note": "server reported no total; count reflects the rows "
                            "returned and may be incomplete"})
    elif rows < JELLYFIN_PAGE and total != rows:        # whole set on one page, yet total disagrees
        out.update({"count": rows, "total_claimed": total, "consistent": False,
                    "note": "server's reported total disagrees with the rows it "
                            "returned on a single page; reporting the rows actually seen"})
    else:                                               # exact single-page match, or trusted multi-page total
        out["count"] = total
    return out


# --- shape (minimal fields returned for each item) -------------------------

def _shape(item: dict) -> dict:
    pids = item.get("ProviderIds") or {}
    ud   = item.get("UserData") or {}
    return {
        "title":    item.get("Name"),
        "year":     item.get("ProductionYear"),
        "genres":   item.get("Genres") or [],
        "imdb":     pids.get("Imdb"),
        "tmdb":     pids.get("Tmdb"),
        "id":       item.get("Id"),
        "watched":  bool(ud.get("Played"))     if ud else False,
        "favorite": bool(ud.get("IsFavorite")) if ud else False,
    }


def _shape_min(item: dict) -> dict:
    """Compact projection of `_shape` for bulk enumeration: drops `tmdb` (redundant
    once `imdb` is present) and `id`, to keep a whole-library dump small."""
    s = _shape(item)
    return {k: s[k] for k in ("title", "year", "genres", "imdb", "watched", "favorite")}


# --- resolvers ------------------------------------------------------------

def _user_id(name: str | None = None) -> str:
    """Resolve username to Jellyfin user id (case-insensitive exact match)."""
    name = name or JELLYFIN_USER
    users = _request("GET", "/Users") or []
    for u in users:
        if (u.get("Name") or "").lower() == name.lower():
            return u["Id"]
    raise ValueError(f"No Jellyfin user named {name!r}")


def _find_movie(uid: str, title: str, year: int | None = None):
    """Best library match for title (opt. year), client-side.

    Returns (item, match) where match is "exact" or "fuzzy"; (None, None) if the
    library search returned nothing. A "fuzzy" result is only the top relevance
    hit and MUST NOT be treated as confirmed ownership by callers.
    """
    items = _items(
        f"/Users/{uid}/Items",
        {
            "IncludeItemTypes": "Movie",
            "Recursive":         "true",
            "searchTerm":        title,
            "Limit":             "10",
            "EnableUserData":    "true",
            "Fields":            "ProviderIds,Genres,ProductionYear",
        },
    )
    def norm(s):
        return (s or "").strip().lower()

    # exact (normalized) name match, with optional year — the only confident case
    for it in items:
        if norm(it.get("Name")) == norm(title) and (year is None or it.get("ProductionYear") == year):
            return it, "exact"
    # no exact match: expose the top hit ONLY as a fuzzy candidate (never as owned)
    if items:
        return items[0], "fuzzy"
    return None, None


def _boxset_id(uid: str, name: str) -> str:
    """Resolve collection (BoxSet) name to id; raise if missing/ambiguous."""
    sets = _items(
        f"/Users/{uid}/Items",
        {"IncludeItemTypes": "BoxSet", "Recursive": "true"},
    )
    norm = lambda s: (s or "").strip().lower()
    hits = [b for b in sets if norm(b.get("Name")) == norm(name)]
    if not hits:
        raise ValueError(f"No collection named {name!r}")
    if len(hits) > 1:
        raise ValueError(f"Collection {name!r} is ambiguous ({len(hits)} matches)")
    return hits[0]["Id"]


# --- tool: jellyfin (read-only) -------------------------------------------

@mcp.tool(**_READ_ANN)
def jellyfin(
    op: str,
    user: Optional[str] = None,
    title: Optional[str] = None,
    collection: Optional[str] = None,
    genre: Optional[str] = None,
    limit: Optional[int] = None,
    year: Optional[int] = None,
    offset: Optional[int] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
) -> str:
    """Read my Jellyfin movie library (read-only). Pick exactly one op.

    Ops:
    - favorites: my hearted movies (taste signal)
    - recent: my most-recently-watched, newest first
    - unwatched: movies I own but have not watched (optional genre filter)
    - lookup: is ONE specific movie in my library? watched? favorite? (needs title)
    - collections: list my collections (name + id)
    - collection_movies: movies inside a named collection (needs collection)
    - in_collection: which collection(s) contain a movie (needs title)
    - all: my ENTIRE movie library — every owned film, gathered by server-side
        auto-paging in ONE call (no manual limit/offset needed). Each movie has
        {title, year, genres, imdb, watched, favorite}. Use this for
        completeness / coverage questions that need the whole library —
        distributions, "do I own any <language/country> films", listing
        everything of a kind — and as the fallback for anything `count` can't do.
        Prefer `count` when a plain number for a year/genre is all that's needed.
    - count: HOW MANY movies match a year and/or genre, as an authoritative number
        WITHOUT listing them (needs year and/or genre; omit both for the library
        total). e.g. "how many 1985 films", "how many horror movies do I own".
        For a span (a decade, "between X and Y", "before/after YYYY") use
        year_from/year_to (inclusive) INSTEAD of listing years or dumping `all` —
        e.g. "horror films from the 80s" -> genre="Horror", year_from=1980,
        year_to=1989 in ONE call. Only year/genre are supported here because
        Jellyfin counts those reliably; for watched/favorite counts use `all`
        (or favorites/unwatched) instead. The returned `count` is exact for these
        filters — report it directly. The result carries an extra
        `consistent:false`/`note` ONLY if the server gave an inconsistent answer;
        if there is no such field, do not hedge the number.

    Returns JSON. List ops -> array of {title, year, genres, imdb, tmdb, id,
    watched, favorite}. `lookup` returns one of:
      {"in_library": true,  "match": "exact", ...movie fields}   -> owned, confirmed.
      {"in_library": false, "match": "fuzzy", "candidate": {...}, "note": ...}
          -> NO exact title match. `candidate` is the closest library title; do NOT
             treat it as owned. To confirm, re-call lookup with the candidate's exact
             title + year.
      {"in_library": false, "match": null}   -> nothing close in the library.
    `in_collection` similarly tags "match": "exact"|"fuzzy" (verify a fuzzy result).

    Paging (list ops): `limit` (default 50) + `offset` (0-based); advance `offset`
    by `limit` to fetch the next page.

    Args:
        op: favorites | recent | unwatched | lookup | collections | collection_movies | in_collection | all | count
        user: account name; defaults to mine
        title: movie title (required for lookup / in_collection)
        collection: collection name — to list its movies (collection_movies) or check membership (in_collection; omit to scan all)
        genre: Jellyfin genre NAME to filter `unwatched` or `count`, e.g. "Action" (omit for no filter). This is a genre name — NOT a numeric id
        limit: max movies for a list op (default 50); ignored by `all` (which fetches everything) and `count`
        year: release year — disambiguates lookup / in_collection, and filters `count` (a SINGLE year)
        offset: 0-based start index for paging list ops; advance it to fetch the next page. Not needed for `all` (it pages internally)
        year_from: inclusive start year of a range for `count` (e.g. 1980 for "the 80s"); omit for an open start. Ignored if `year` is given
        year_to: inclusive end year of a range for `count` (e.g. 1989 for "the 80s"); omit for an open end (defaults to the current year). Ignored if `year` is given
    """
    try:
        uid  = _user_id(user)
        lim  = str(limit if limit is not None else JELLYFIN_DEFAULT_LIMIT)
        off  = str(offset if offset is not None else 0)
        base = f"/Users/{uid}/Items"
        flds = "ProviderIds,Genres,ProductionYear"

        if op == "favorites":
            items = _items(base, {
                "IncludeItemTypes": "Movie", "Recursive": "true",
                "Filters":          "IsFavorite", "EnableUserData": "true",
                "Fields":           flds, "Limit": lim, "StartIndex": off,
                "SortBy":           "SortName",
            })
            return json.dumps([_shape(i) for i in items])

        elif op == "recent":
            items = _items(base, {
                "IncludeItemTypes": "Movie", "Recursive": "true",
                "Filters":          "IsPlayed", "EnableUserData": "true",
                "Fields":           flds, "Limit": lim, "StartIndex": off,
                "SortBy":           "DatePlayed", "SortOrder": "Descending",
            })
            return json.dumps([_shape(i) for i in items])

        elif op == "unwatched":
            params = {
                "IncludeItemTypes": "Movie", "Recursive": "true",
                "Filters":          "IsUnplayed", "EnableUserData": "true",
                "Fields":           flds, "Limit": lim, "StartIndex": off,
                "SortBy":           "SortName",
            }
            if genre:
                params["Genres"] = genre
            items = _items(base, params)
            return json.dumps([_shape(i) for i in items])

        elif op == "lookup":
            if not title:
                raise ValueError("lookup needs a title")
            it, match = _find_movie(uid, title, year)
            if it and match == "exact":
                result = {"in_library": True, "match": "exact"}
                result.update(_shape(it))
                return json.dumps(result)
            elif it:  # fuzzy: never assert ownership; surface an unconfirmed candidate
                return json.dumps({
                    "in_library": False,
                    "match": "fuzzy",
                    "query": {"title": title, "year": year},
                    "candidate": _shape(it),
                    "note": "no exact title match; closest library title returned as a candidate "
                            "— confirm before treating as owned",
                })
            else:
                return json.dumps({"in_library": False, "match": None, "title": title, "year": year})

        elif op == "collections":
            items = _items(base, {
                "IncludeItemTypes": "BoxSet", "Recursive": "true",
                "SortBy":           "SortName",
                "Limit":            lim, "StartIndex": off,
            })
            return json.dumps([{"name": b.get("Name"), "id": b.get("Id")} for b in items])

        elif op == "collection_movies":
            if not collection:
                raise ValueError("collection_movies needs a collection name")
            cid = _boxset_id(uid, collection)
            items = _items(base, {
                "ParentId":         cid,
                "IncludeItemTypes": "Movie", "Recursive": "true",
                "EnableUserData":   "true",
                "Fields":           flds, "Limit": lim, "StartIndex": off,
                "SortBy":           "SortName",
            })
            return json.dumps([_shape(i) for i in items])

        elif op == "in_collection":
            if not title:
                raise ValueError("in_collection needs a title")
            it, match = _find_movie(uid, title, year)
            if not it:
                raise ValueError(f"{title!r} is not in the library")
            mid = it["Id"]
            if collection:
                sets = [{"Id": _boxset_id(uid, collection), "Name": collection}]
            else:
                sets = _items(base, {
                    "IncludeItemTypes": "BoxSet", "Recursive": "true",
                })
            in_list = []
            for b in sets:
                kids = _items(base, {
                    "ParentId":         b["Id"],
                    "IncludeItemTypes": "Movie", "Recursive": "true",
                })
                if any(k.get("Id") == mid for k in kids):
                    in_list.append(b.get("Name"))
            out = {"title": it.get("Name"), "match": match, "in_collections": in_list}
            if match == "fuzzy":
                out["note"] = ("no exact title match; resolved to the closest library title "
                               "— verify this is the intended film")
            return json.dumps(out)

        elif op == "all":
            movies, total, consistent = _fetch_all(base, {
                "IncludeItemTypes": "Movie", "Recursive": "true",
                "EnableUserData":   "true", "Fields": flds,
                "SortBy":           "SortName",
            })
            out = {
                "op":        "all",
                "count":     len(movies),
                "consistent": consistent,
                "movies":    [_shape_min(m) for m in movies],
            }
            if not consistent:
                out["total_claimed"] = total
                out["note"] = ("stopped before reaching the server's claimed total "
                               "(count/total mismatch); returning every row actually "
                               "fetched — treat the set as possibly incomplete")
            return json.dumps(out)

        elif op == "count":
            return json.dumps(_count_movies(uid, year, genre, year_from, year_to))

        else:
            raise ValueError(f"Unknown op {op!r}")

    except Exception as e:
        return json.dumps({"error": str(e)})


# --- tool: jellyfin_collection_add (the one write exception) ---------------

@mcp.tool(**_WRITE_ANN)
def jellyfin_collection_add(
    title: str,
    collection: str,
    year: Optional[int] = None,
) -> str:
    """Add an OWNED movie to an EXISTING Jellyfin collection. Add-only and reversible.
    The movie must match a library title EXACTLY (a fuzzy/near match is refused, never
    guessed) and the collection must exist (it never creates collections). Use only when
    I explicitly ask to add a film. Pass year to disambiguate same-title films.

    Returns {"added", "year", "to"} on success, or {"error": ...}. On a non-exact
    title the error names the closest library title, so you can re-call with that
    exact title + year.

    Args:
        title: movie to add (must already be in the library; matched EXACTLY)
        collection: exact name of an existing collection
        year: release year to disambiguate the title (recommended)
    """
    try:
        uid = _user_id()
        it, match = _find_movie(uid, title, year)
        if not it:
            raise ValueError(f"{title!r} is not in the library; cannot add it")
        if match != "exact":
            raise ValueError(
                f"{title!r} did not exactly match a library title "
                f"(closest: {it.get('Name')!r}, {it.get('ProductionYear')}); refusing to add. "
                f"Re-issue with the exact title (and year) to confirm."
            )
        mid = it["Id"]
        cid = _boxset_id(uid, collection)
        _request("POST", f"/Collections/{cid}/Items", {"ids": mid})
        return json.dumps({
            "added": it.get("Name"),
            "year":  it.get("ProductionYear"),
            "to":    collection,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# --- tool: jellyfin_favorite_set (write — the heart button) ----------------

@mcp.tool(**_WRITE_ANN)
def jellyfin_favorite_set(
    title: str,
    favorite: bool = True,
    year: Optional[int] = None,
) -> str:
    """Set an OWNED movie's favorite (heart) state to an EXPLICIT value — favorite=True
    hearts it, favorite=False removes the heart. This is NOT a toggle: it sets the state
    you pass, so it is reversible and idempotent (setting the state it already has is a
    no-op, never a flip). The movie must match a library title EXACTLY (a fuzzy/near
    match is refused, never guessed). Use only when I explicitly ask to favorite or
    un-favorite a film. Pass year to disambiguate same-title films.

    `favorite` defaults to True (heart it); pass favorite=false to remove the heart. To
    un-heart a movie you must pass favorite=false — calling with no favorite arg hearts
    it. Favorited movies are what the read tool's `favorites` op returns.

    Returns {"favorite", "title", "year"} reflecting the movie's state AFTER the call,
    or {"error": ...}. On a non-exact title the error names the closest library title,
    so you can re-call with that exact title + year.

    Args:
        title: movie to (un)favorite (must already be in the library; matched EXACTLY)
        favorite: True to heart it (default), False to remove the heart
        year: release year to disambiguate the title (recommended)
    """
    try:
        uid = _user_id()
        it, match = _find_movie(uid, title, year)
        if not it:
            raise ValueError(f"{title!r} is not in the library; cannot favorite it")
        if match != "exact":
            raise ValueError(
                f"{title!r} did not exactly match a library title "
                f"(closest: {it.get('Name')!r}, {it.get('ProductionYear')}); refusing to set favorite. "
                f"Re-issue with the exact title (and year) to confirm."
            )
        mid    = it["Id"]
        method = "POST" if favorite else "DELETE"
        resp   = _request(method, f"/Users/{uid}/FavoriteItems/{mid}")
        # Jellyfin returns a UserItemDataDto; trust its IsFavorite, else echo intent.
        actual = bool((resp or {}).get("IsFavorite", favorite))
        return json.dumps({
            "favorite": actual,
            "title":    it.get("Name"),
            "year":     it.get("ProductionYear"),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# --- tool: jellyfin_watched_set (write — the played/watched flag) ----------

@mcp.tool(**_WRITE_ANN)
def jellyfin_watched_set(
    title: str,
    watched: bool = True,
    year: Optional[int] = None,
) -> str:
    """Set an OWNED movie's watched (played) state to an EXPLICIT value — watched=True
    marks it watched, watched=False marks it unwatched. This is NOT a toggle: it sets
    the state you pass, so it is reversible and idempotent (setting the state it already
    has is a no-op, never a flip). The movie must match a library title EXACTLY (a
    fuzzy/near match is refused, never guessed). Use only when I explicitly ask to mark
    a film watched or unwatched. Pass year to disambiguate same-title films.

    `watched` defaults to True (mark it watched); pass watched=false to clear it. To
    mark a movie unwatched you must pass watched=false — calling with no watched arg
    marks it watched. Watched state is what the read tool's `recent` and `unwatched`
    ops key off of.

    Returns {"watched", "title", "year"} reflecting the movie's state AFTER the call,
    or {"error": ...}. On a non-exact title the error names the closest library title,
    so you can re-call with that exact title + year.

    Args:
        title: movie to mark (un)watched (must already be in the library; matched EXACTLY)
        watched: True to mark watched (default), False to mark unwatched
        year: release year to disambiguate the title (recommended)
    """
    try:
        uid = _user_id()
        it, match = _find_movie(uid, title, year)
        if not it:
            raise ValueError(f"{title!r} is not in the library; cannot mark it watched")
        if match != "exact":
            raise ValueError(
                f"{title!r} did not exactly match a library title "
                f"(closest: {it.get('Name')!r}, {it.get('ProductionYear')}); refusing to set watched. "
                f"Re-issue with the exact title (and year) to confirm."
            )
        mid    = it["Id"]
        method = "POST" if watched else "DELETE"
        resp   = _request(method, f"/Users/{uid}/PlayedItems/{mid}")
        # Jellyfin returns a UserItemDataDto; trust its Played, else echo intent.
        actual = bool((resp or {}).get("Played", watched))
        return json.dumps({
            "watched": actual,
            "title":   it.get("Name"),
            "year":    it.get("ProductionYear"),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# --------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
