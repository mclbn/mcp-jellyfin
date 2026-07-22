#!/usr/bin/env python3
"""Jellyfin MCP server — read-only library queries + one write (collection add).

Port of jellyfin.el: same tool names, same arguments, same JSON output.
"""

import json
import os
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

# --- config (env vars mirror the Elisp defcustoms) -------------------------

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://nas.local:8096")
JELLYFIN_USER = os.environ.get("JELLYFIN_USER", "me")
JELLYFIN_DEFAULT_LIMIT = int(os.environ.get("JELLYFIN_DEFAULT_LIMIT", "50"))
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")

_ssl_verify = os.environ.get("JELLYFIN_SSL_VERIFY", "").lower()
_ssl_verify = _ssl_verify if _ssl_verify else False if JELLYFIN_URL.startswith("http://") else True

mcp = FastMCP("jellyfin", host="0.0.0.0", port=int(os.environ.get("MCP_PORT", "8000")))

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


# --- shape (same minimal fields as Elisp `perso/jellyfin--shape`) -----------

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


def _find_movie(uid: str, title: str, year: int | None = None) -> dict | None:
    """Best library match for title (opt. year). Client-side match like the Elisp version."""
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

    # exact name match (with optional year)
    for it in items:
        if norm(it.get("Name")) == norm(title) and (year is None or it.get("ProductionYear") == year):
            return it
    # else top (relevance-ranked) hit
    return items[0] if items else None


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

@mcp.tool()
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
    """Read my Jellyfin movie library (read-only). Ops:
    - favorites: my hearted movies (taste signal)
    - recent: my most-recently-watched movies, newest first
    - unwatched: movies I own but have not watched (optional genre filter)
    - lookup: is a given movie in my library? watched? favorite?
    - collections: list my collections
    - collection_movies: the movies inside a named collection
    - in_collection: which collection(s) contain a given movie
    Rows carry title, year, genres, imdb/tmdb ids, and watched/favorite flags.

    Args:
        op: favorites | recent | unwatched | lookup | collections | collection_movies | in_collection
        user: account name; defaults to mine
        title: movie title (required for lookup / in_collection)
        collection: collection name: to list its movies (collection_movies) or to check membership (in_collection; omit to scan all)
        genre: genre filter for unwatched, e.g. Comedy
        limit: max movies for list ops
        year: release year to disambiguate lookup / in_collection
        offset: 0-based start index for paging list ops; use with limit to fetch the next page
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
            it = _find_movie(uid, title, year)
            if it:
                result = {"in_library": True}
                result.update(_shape(it))
                return json.dumps(result)
            else:
                return json.dumps({"in_library": False, "title": title, "year": year})

        elif op == "collections":
            items = _items(base, {
                "IncludeItemTypes": "BoxSet", "Recursive": "true",
                "SortBy":           "SortName",
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
            it = _find_movie(uid, title, year)
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
            return json.dumps({"title": it.get("Name"), "in_collections": in_list})

        else:
            raise ValueError(f"Unknown op {op!r}")

    except Exception as e:
        return json.dumps({"error": str(e)})


# --- tool: jellyfin_collection_add (the one write exception) ---------------

@mcp.tool()
def jellyfin_collection_add(
    title: str,
    collection: str,
) -> str:
    """Add an OWNED movie to an EXISTING Jellyfin collection. Add-only and reversible.
    Refuses if the movie is not in the library, or if the collection name is missing or
    ambiguous (it never creates collections). Use only when I explicitly ask to add a film.

    Args:
        title: movie to add (must already be in the library)
        collection: exact name of an existing collection
    """
    try:
        uid = _user_id()
        it  = _find_movie(uid, title)
        if not it:
            raise ValueError(f"{title!r} is not in the library; cannot add it")
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


# --------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
