#!/usr/bin/env python3
"""Jellyfin MCP server — read-only library queries + two writes (collection add, favorite set)."""

import json
import os
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

# --- config (env vars) -----------------------------------------------------

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://nas.local:8096")
JELLYFIN_USER = os.environ.get("JELLYFIN_USER", "me")
JELLYFIN_DEFAULT_LIMIT = int(os.environ.get("JELLYFIN_DEFAULT_LIMIT", "50"))
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")

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
        op: favorites | recent | unwatched | lookup | collections | collection_movies | in_collection
        user: account name; defaults to mine
        title: movie title (required for lookup / in_collection)
        collection: collection name — to list its movies (collection_movies) or check membership (in_collection; omit to scan all)
        genre: Jellyfin genre NAME to filter `unwatched`, e.g. "Action" (omit for no filter). This is a genre name — NOT a numeric id
        limit: max movies for a list op (default 50)
        year: release year to disambiguate lookup / in_collection
        offset: 0-based start index for paging list ops; advance it to fetch the next page
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


# --------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
