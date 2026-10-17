"""In-memory fake of the GitHub GraphQL subset used by star_lists.py, seedable from a snapshot."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import threading
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import star_lists as sl  # noqa: E402


class FakeGitHub:
    def __init__(self, page: int = 100, max_lists: int = 32) -> None:
        self.page, self.max_lists = page, max_lists
        self.login = "jun"
        self.repos: dict[str, dict[str, Any]] = {}
        self.starred: list[str] = []
        self.starred_at: dict[str, str] = {}
        self.lists: dict[str, dict[str, Any]] = {}
        self.counter = 0
        self.mutations = 0
        self.fail_at: Optional[int] = None
        self.lock = threading.Lock()

    @classmethod
    def from_snapshot(cls, path: str, **kw: Any) -> "FakeGitHub":
        snap = json.load(open(path, encoding="utf-8"))
        g = cls(**kw)
        g.login = snap["login"]
        for r in snap["repos"]:
            g.repos[r["id"]] = {"id": r["id"], "nameWithOwner": r["full_name"], "description": r["description"],
                                "url": r["url"], "stargazerCount": r["stars"], "isArchived": r["archived"],
                                "pushedAt": r["pushed_at"], "primaryLanguage": {"name": r["language"]} if r["language"] else None}
            if r["starred"]:
                g.starred.append(r["id"])
                g.starred_at[r["id"]] = r["starred_at"]
        for l in snap["lists"]:
            g.lists[l["id"]] = {"name": l["name"], "description": l["description"], "isPrivate": l["private"], "items": list(l["items"])}
        return g

    def view(self) -> dict[str, Any]:
        return {"lists": {d["name"]: sorted(self.repos[r]["nameWithOwner"] for r in d["items"]) for d in self.lists.values()},
                "meta": {d["name"]: (d["description"], d["isPrivate"]) for d in self.lists.values()},
                "starred": sorted(self.repos[r]["nameWithOwner"] for r in self.starred)}

    def rid(self, name: str) -> str:
        return next(r for r, d in self.repos.items() if d["nameWithOwner"] == name)

    def lid(self, name: str) -> str:
        return next(l for l, d in self.lists.items() if d["name"] == name)

    def _page(self, seq: list[Any], after: Optional[str]) -> tuple[list[Any], dict[str, Any]]:
        start = int(after) if after else 0
        nxt = start + self.page
        return seq[start:nxt], {"hasNextPage": nxt < len(seq), "endCursor": str(nxt)}

    def _err(self, msg: str, typ: str = "UNPROCESSABLE") -> None:
        raise sl.GraphQLError([{"message": msg, "type": typ}])

    def graphql(self, query: str, variables: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        with self.lock:
            return self._graphql(query, variables or {})

    def _graphql(self, query: str, v: dict[str, Any]) -> dict[str, Any]:
        kind, op = re.search(r"(query|mutation)\s+(\w+)", query).groups()
        if kind == "mutation":
            self.mutations += 1
            if self.fail_at is not None and self.mutations == self.fail_at:
                raise sl.StarListsError("simulated network failure")
            return getattr(self, "m_" + op)(v["input"])
        if op == "Viewer":
            return {"viewer": {"login": self.login}}
        if op == "Stars":
            chunk, info = self._page(self.starred, v.get("after"))
            edges = [{"starredAt": self.starred_at.get(r, "2026-01-01T00:00:00Z"), "node": self.repos[r]} for r in chunk]
            return {"viewer": {"starredRepositories": {"edges": edges, "pageInfo": info}}}
        if op == "Lists":
            chunk, info = self._page(list(self.lists), v.get("after"))
            nodes = [{"id": lid, "name": self.lists[lid]["name"], "description": self.lists[lid]["description"],
                      "isPrivate": self.lists[lid]["isPrivate"]} for lid in chunk]
            return {"viewer": {"lists": {"nodes": nodes, "pageInfo": info}}}
        if op == "ListItems":
            lst = self.lists.get(v["id"])
            if lst is None:
                return {"node": None}
            chunk, info = self._page(lst["items"], v.get("after"))
            nodes = [{"__typename": "Repository", "id": r, "nameWithOwner": self.repos[r]["nameWithOwner"]} for r in chunk]
            return {"node": {"items": {"nodes": nodes, "pageInfo": info}}}
        raise AssertionError(op)

    def _taken(self, name: str, exclude: Optional[str] = None) -> bool:
        return any(d["name"].casefold() == name.casefold() for lid, d in self.lists.items() if lid != exclude)

    def m_CreateList(self, inp: dict[str, Any]) -> dict[str, Any]:
        if len(self.lists) >= self.max_lists:
            self._err(f"You can only have {self.max_lists} lists")
        if self._taken(inp["name"]):
            self._err("Name has already been taken")
        self.counter += 1
        lid = f"UL_fake{self.counter:06d}"
        self.lists[lid] = {"name": inp["name"], "description": inp.get("description", ""),
                           "isPrivate": inp.get("isPrivate", False), "items": []}
        return {"createUserList": {"list": {"id": lid, "name": inp["name"]}}}

    def m_UpdateList(self, inp: dict[str, Any]) -> dict[str, Any]:
        lst = self.lists.get(inp["listId"]) or self._err("Could not resolve list", "NOT_FOUND")
        if "name" in inp and self._taken(inp["name"], inp["listId"]):
            self._err("Name has already been taken")
        for k in ("name", "description", "isPrivate"):
            if k in inp:
                lst[k] = inp[k]
        return {"updateUserList": {"list": {"id": inp["listId"], "name": lst["name"]}}}

    def m_DeleteList(self, inp: dict[str, Any]) -> dict[str, Any]:
        if inp["listId"] not in self.lists:
            self._err("Could not resolve list", "NOT_FOUND")
        del self.lists[inp["listId"]]
        return {"deleteUserList": {"clientMutationId": None}}

    def m_SetItemLists(self, inp: dict[str, Any]) -> dict[str, Any]:
        rid, ids = inp["itemId"], inp["listIds"]
        if rid not in self.repos or any(i not in self.lists for i in ids):
            self._err("Could not resolve", "NOT_FOUND")
        for lid, d in self.lists.items():
            if lid in ids and rid not in d["items"]:
                d["items"].append(rid)
            if lid not in ids and rid in d["items"]:
                d["items"].remove(rid)
        return {"updateUserListsForItem": {"lists": [{"id": i} for i in ids]}}

    def m_AddStar(self, inp: dict[str, Any]) -> dict[str, Any]:
        if inp["starrableId"] not in self.starred:
            self.starred.insert(0, inp["starrableId"])
        return {"addStar": {"starrable": {"viewerHasStarred": True}}}

    def m_RemoveStar(self, inp: dict[str, Any]) -> dict[str, Any]:
        if inp["starrableId"] in self.starred:
            self.starred.remove(inp["starrableId"])
        return {"removeStar": {"starrable": {"viewerHasStarred": False}}}
