"""End-to-end tests for star_lists.py against an in-memory fake of GitHub's GraphQL API."""
from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import star_lists as sl  # noqa: E402


class FakeGitHub:
    """Simulates the subset of GitHub GraphQL used by star_lists.py (with small pages)."""

    def __init__(self, page: int = 2, max_lists: int = 32, unstar_on_empty: bool = False) -> None:
        self.page, self.max_lists, self.unstar_on_empty = page, max_lists, unstar_on_empty
        self.login = "jun"
        self.repos: dict[str, dict[str, Any]] = {}
        self.starred: list[str] = []          # most recent first
        self.lists: dict[str, dict[str, Any]] = {}
        self.counter = 0
        self.mutations = 0
        self.fail_at: Optional[int] = None

    # ---- setup helpers
    def add_repo(self, name: str, starred: bool = True) -> str:
        rid = f"R_{len(self.repos):04d}"
        self.repos[rid] = {"id": rid, "nameWithOwner": name, "description": f"desc of {name}",
                           "url": f"https://github.com/{name}", "stargazerCount": 1500 * (len(self.repos) + 1),
                           "isArchived": name.endswith("9"), "pushedAt": "2026-01-01T00:00:00Z",
                           "primaryLanguage": {"name": "Python"}}
        if starred:
            self.starred.insert(0, rid)
        return rid

    def add_list(self, name: str, items: list[str], description: str = "", private: bool = False) -> str:
        self.counter += 1
        lid = f"UL_kwDOtest{self.counter:06d}"
        self.lists[lid] = {"name": name, "description": description, "isPrivate": private, "items": list(items)}
        return lid

    def rid(self, name: str) -> str:
        return next(r for r, d in self.repos.items() if d["nameWithOwner"] == name)

    def view(self) -> dict[str, Any]:
        """Name-based picture used for assertions (ids differ after restore)."""
        return {
            "lists": {d["name"]: {"repos": sorted(self.repos[r]["nameWithOwner"] for r in d["items"]),
                                  "description": d["description"], "private": d["isPrivate"]}
                      for d in self.lists.values()},
            "starred": sorted(self.repos[r]["nameWithOwner"] for r in self.starred),
        }

    # ---- GraphQL
    def _page(self, seq: list[Any], after: Optional[str]) -> tuple[list[Any], dict[str, Any]]:
        start = int(after) if after else 0
        chunk = seq[start:start + self.page]
        nxt = start + self.page
        return chunk, {"hasNextPage": nxt < len(seq), "endCursor": str(nxt)}

    def _err(self, msg: str, typ: str = "UNPROCESSABLE") -> None:
        raise sl.GraphQLError([{"message": msg, "type": typ}])

    def graphql(self, query: str, variables: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        v = variables or {}
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
            edges = [{"starredAt": f"2026-0{1 + i % 9}-01T00:00:00Z", "node": self.repos[r]} for i, r in enumerate(chunk)]
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

    def _name_taken(self, name: str, exclude: Optional[str] = None) -> bool:
        return any(d["name"].casefold() == name.casefold() for lid, d in self.lists.items() if lid != exclude)

    def m_CreateList(self, inp: dict[str, Any]) -> dict[str, Any]:
        if len(self.lists) >= self.max_lists:
            self._err(f"You can only have {self.max_lists} lists")
        if self._name_taken(inp["name"]):
            self._err("Name has already been taken")
        lid = self.add_list(inp["name"], [], inp.get("description", ""), inp.get("isPrivate", False))
        return {"createUserList": {"list": {"id": lid, "name": inp["name"]}}}

    def m_UpdateList(self, inp: dict[str, Any]) -> dict[str, Any]:
        lst = self.lists.get(inp["listId"]) or self._err("Could not resolve list", "NOT_FOUND")
        if "name" in inp and self._name_taken(inp["name"], inp["listId"]):
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
        if ids and rid not in self.starred:
            self.starred.insert(0, rid)
        if not ids and self.unstar_on_empty and rid in self.starred:
            self.starred.remove(rid)
        return {"updateUserListsForItem": {"lists": [{"id": i} for i in ids]}}

    def m_AddStar(self, inp: dict[str, Any]) -> dict[str, Any]:
        if inp["starrableId"] not in self.starred:
            self.starred.insert(0, inp["starrableId"])
        return {"addStar": {"starrable": {"viewerHasStarred": True}}}

    def m_RemoveStar(self, inp: dict[str, Any]) -> dict[str, Any]:
        if inp["starrableId"] in self.starred:
            self.starred.remove(inp["starrableId"])
        return {"removeStar": {"starrable": {"viewerHasStarred": False}}}


# --------------------------------------------------------------------------- fixtures
def initial(**kw: Any) -> FakeGitHub:
    g = FakeGitHub(**kw)
    r = {i: g.add_repo(f"alice/repo{i}", starred=i <= 13) for i in range(1, 15)}
    g.add_list("AI", [r[1], r[2], r[3]], "machine learning")
    g.add_list("Tools", [r[4], r[5]])
    g.add_list("Old", [r[6], r[7]])
    g.add_list("Misc", [r[8], r[1]])
    g.add_list("Edge", [r[9]], private=True)
    return g


INITIAL_VIEW = initial().view()

EXPECTED_REORG = {
    "AI": {"repos": ["alice/repo1", "alice/repo2", "alice/repo3", "alice/repo8"], "description": "machine learning", "private": False},
    "Dev Tools": {"repos": ["alice/repo4", "alice/repo5", "alice/repo6"], "description": "", "private": False},
    "Edge": {"repos": ["alice/repo9"], "description": "6G edge intelligence", "private": True},
    "Reading": {"repos": ["alice/repo10", "alice/repo11", "alice/repo2"], "description": "papers & notes", "private": True},
}


def run(g: FakeGitHub, *args: str) -> tuple[int, str]:
    buf_out, buf_err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        code = sl.main(list(args), client=g)
    return code, buf_out.getvalue() + buf_err.getvalue()


def load(d: Path) -> dict[str, Any]:
    return yaml.safe_load((d / "layout.yaml").read_text(encoding="utf-8"))


def save(d: Path, doc: dict[str, Any]) -> None:
    (d / "layout.yaml").write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")


def block(doc: dict[str, Any], name: str) -> dict[str, Any]:
    return next(b for b in doc["lists"] if b["name"] == name)


def reorg(doc: dict[str, Any]) -> dict[str, Any]:
    """rename Tools, delete Old (repo6 -> Dev Tools, repo7 -> nowhere), merge Misc into AI,
    create private Reading, edit Edge description."""
    block(doc, "Tools")["name"] = "Dev Tools"
    block(doc, "Tools" if False else "Dev Tools")["repos"].append("alice/repo6")
    block(doc, "AI")["repos"].append("alice/repo8")
    block(doc, "Edge")["description"] = "6G edge intelligence"
    doc["lists"] = [b for b in doc["lists"] if b["name"] not in ("Old", "Misc")]
    doc["lists"].append({"name": "Reading", "description": "papers & notes", "private": True,
                         "repos": ["alice/repo10", "alice/repo11", "alice/repo2"]})
    doc["unlisted"] = [r for r in doc["unlisted"] if r not in ("alice/repo10", "alice/repo11")]
    return doc


results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def fresh_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="sl_"))


COMMON = ("--yes", "--sleep", "0", "--settle", "0")

# --------------------------------------------------------------------------- 1. export + no-op plan
g = initial()
d = fresh_dir()
code, out = run(g, "export", "--dir", str(d))
check("export exit 0", code == 0, out)
check("export files", all((d / f).exists() for f in ("layout.yaml", "snapshot.json", "inventory.csv")))
text = (d / "layout.yaml").read_text(encoding="utf-8")
check("layout has comments + unlisted", "# Python · ★" in text and "unlisted:" in text and "ARCHIVED" in text)
doc = load(d)
check("layout parses: 5 lists", len(doc["lists"]) == 5)
check("unlisted = repo10..13", sorted(doc["unlisted"]) == sorted(f"alice/repo{i}" for i in range(10, 14)), doc["unlisted"])
check("pagination: repo1 in AI and Misc", "alice/repo1" in block(doc, "AI")["repos"] and "alice/repo1" in block(doc, "Misc")["repos"])
code, out = run(g, "plan", "--dir", str(d))
check("unchanged layout -> nothing to do", code == 0 and "Nothing to do" in out, out)
code, out = run(g, "export", "--dir", str(d))
check("export refuses to overwrite layout", code == 2 and "already exists" in out)
inv = (d / "inventory.csv").read_text(encoding="utf-8-sig").splitlines()
check("inventory rows", len(inv) == 1 + 13 and inv[0].startswith("repo,lists"), str(len(inv)))

# --------------------------------------------------------------------------- 2. reorganisation
save(d, reorg(load(d)))
code, out = run(g, "plan", "--dir", str(d), "--limit", "0")
check("plan exit 0", code == 0, out)
check("plan summary", "1 create · 2 rename/edit · 2 delete" in out and "5 membership change(s)" in out, out)
check("plan: repo7 ends in no list", "1 repo(s) will end up in no list" in out, out)
check("plan did not mutate", g.mutations == 0)
code, out = run(g, "apply", "--dir", str(d), *COMMON)
check("apply exit 0", code == 0, out)
check("apply: 10 steps", "[10/10]" in out, out)
check("final state == expected", g.view()["lists"] == EXPECTED_REORG, json.dumps(g.view()["lists"], indent=1))
check("no stars lost", g.view()["starred"] == INITIAL_VIEW["starred"])
check("pre-apply backup + journal", any((d / "snapshots").glob("preapply-*.json")) and any((d / "logs").glob("apply-*.jsonl")))
code, out = run(g, "apply", "--dir", str(d), *COMMON)
check("idempotent second apply", code == 0 and "Nothing to do" in out and g.mutations == 10, out)

# --------------------------------------------------------------------------- 3. restore to export snapshot
code, out = run(g, "restore", str(d / "snapshot.json"), "--dir", str(d), *COMMON)
check("restore exit 0", code == 0, out)
check("restore == initial (names, members, flags)", g.view() == INITIAL_VIEW, json.dumps(g.view(), indent=1))

# --------------------------------------------------------------------------- 4. interrupted apply + resume
g = initial()
d = fresh_dir()
run(g, "export", "--dir", str(d))
save(d, reorg(load(d)))
g.fail_at = 4
code, out = run(g, "apply", "--dir", str(d), *COMMON)
check("interrupted apply exits 2 with resume hint", code == 2 and "step 4/10 failed" in out and "run `apply` again" in out, out)
g.fail_at = None
code, out = run(g, "apply", "--dir", str(d), *COMMON)
check("resume finishes", code == 0 and g.view()["lists"] == EXPECTED_REORG, out + json.dumps(g.view()["lists"], indent=1))
check("resume did not duplicate lists", len(g.lists) == 4)
journal = next((d / "logs").glob("apply-*.jsonl")).read_text().splitlines()
check("journal records failure", any('"ok": false' in line for line in journal))

# --------------------------------------------------------------------------- 5. name swap (AI <-> Edge)
g = initial()
d = fresh_dir()
run(g, "export", "--dir", str(d))
doc = load(d)
block(doc, "AI")["name"], block(doc, "Edge")["name"] = "__x__", "AI"
block(doc, "__x__")["name"] = "Edge"
save(d, doc)
code, out = run(g, "apply", "--dir", str(d), *COMMON)
v = g.view()["lists"]
check("swap succeeds via temp names", code == 0 and "temporary" in out, out)
check("swap result", v["Edge"]["repos"] == INITIAL_VIEW["lists"]["AI"]["repos"] and v["AI"]["repos"] == ["alice/repo9"], json.dumps(v))

# --------------------------------------------------------------------------- 6. drift: list created on github.com after export
g = initial()
d = fresh_dir()
run(g, "export", "--dir", str(d))
g.add_list("Z-new", [g.rid("alice/repo12")])
save(d, reorg(load(d)))
code, out = run(g, "apply", "--dir", str(d), *COMMON)
check("drift list untouched (non-strict)", code == 0 and g.view()["lists"].get("Z-new", {}).get("repos") == ["alice/repo12"], out)
check("drift warning shown", "created after the export" in out, out)
code, out = run(g, "plan", "--dir", str(d), "--strict")
check("--strict would delete drift list", '- delete "Z-new"' in out, out)

# --------------------------------------------------------------------------- 7. star dropped as side effect -> re-starred
g = initial(unstar_on_empty=True)
d = fresh_dir()
run(g, "export", "--dir", str(d))
doc = load(d)
block(doc, "Edge")["repos"] = []
save(d, doc)
code, out = run(g, "apply", "--dir", str(d), *COMMON)
check("lost star restored", code == 0 and "re-starred alice/repo9" in out and "alice/repo9" in g.view()["starred"], out)

# --------------------------------------------------------------------------- 8. typo -> error, no writes
g = initial()
d = fresh_dir()
run(g, "export", "--dir", str(d))
doc = load(d)
block(doc, "AI")["repos"].append("alice/repo-typo")
save(d, doc)
code, out = run(g, "apply", "--dir", str(d), *COMMON)
check("typo blocks apply", code == 1 and "unknown repository 'alice/repo-typo'" in out and g.mutations == 0, out)

# --------------------------------------------------------------------------- 9. list cap -> --delete-first
g = initial(max_lists=5)
d = fresh_dir()
run(g, "export", "--dir", str(d))
save(d, reorg(load(d)))
code, out = run(g, "apply", "--dir", str(d), *COMMON)
check("cap: create fails with hint", code == 2 and "--delete-first" in out, out)
code, out = run(g, "apply", "--dir", str(d), "--delete-first", *COMMON)
check("cap: --delete-first succeeds", code == 0 and g.view()["lists"] == EXPECTED_REORG, out + json.dumps(g.view()["lists"]))

# --------------------------------------------------------------------------- 10. repo renamed / unstarred after export
g = initial()
d = fresh_dir()
run(g, "export", "--dir", str(d))
g.repos[g.rid("alice/repo3")]["nameWithOwner"] = "alice/repo3-renamed"
g.starred.remove(g.rid("alice/repo11"))
save(d, reorg(load(d)))
code, out = run(g, "apply", "--dir", str(d), *COMMON)
v = g.view()["lists"]
check("renamed repo resolved", code == 0 and "now called alice/repo3-renamed" in out and "alice/repo3-renamed" in v["AI"]["repos"], out)
check("unstarred repo skipped, not re-starred", "skipped alice/repo11" in out and "alice/repo11" not in v["Reading"]["repos"]
      and "alice/repo11" not in g.view()["starred"], out)

# --------------------------------------------------------------------------- 11. layout validation
bad = """
lists:
  - name: A
    repo: [x/y]
  - name: a
    private: "yes"
  - id: UL_1
unlisted: nope
"""
try:
    sl.parse_layout(bad)
    check("validation raises", False)
except sl.StarListsError as exc:
    msg = str(exc)
    check("validation collects all errors",
          all(s in msg for s in ("unknown key 'repo'", "`private` must be true or false", "`name` is required",
                                 "two list blocks are named", "`unlisted` must be a list")), msg)

# --------------------------------------------------------------------------- 12. raw text edit (cut/paste lines, keep comments)
g = initial()
d = fresh_dir()
run(g, "export", "--dir", str(d))
txt = (d / "layout.yaml").read_text(encoding="utf-8")
line12 = next(l for l in txt.splitlines() if l.strip().startswith("- alice/repo12"))
txt = txt.replace(line12 + "\n", "")
txt = txt.replace('  - name: "Edge"', '  - name: "新列表 中文"\n    repos:\n    ' + line12.strip().replace("- ", "  - ", 1) + '\n\n  - name: "Edge"')
(d / "layout.yaml").write_text(txt, encoding="utf-8")
code, out = run(g, "apply", "--dir", str(d), *COMMON)
check("hand-edited YAML with Chinese name works", code == 0 and g.view()["lists"].get("新列表 中文", {}).get("repos") == ["alice/repo12"], out + txt[-900:])

# --------------------------------------------------------------------------- 13. wrong account
g = initial()
d = fresh_dir()
run(g, "export", "--dir", str(d))
g.login = "someone-else"
code, out = run(g, "plan", "--dir", str(d))
check("wrong account refused", code == 1 and "belongs to @jun" in out, out)

# --------------------------------------------------------------------------- 14. unstar via layout.yaml, then restore
def journal_ops(d: Path, label: str) -> list[str]:
    path = sorted((d / "logs").glob(f"{label}-*.jsonl"))[-1]
    return [json.loads(l)["op"] for l in path.read_text(encoding="utf-8").splitlines()]


g = initial()
d = fresh_dir()
run(g, "export", "--dir", str(d))
check("export writes an empty unstar block", load(d)["unstar"] == [] and "取消 star" in (d / "layout.yaml").read_text(encoding="utf-8"))
doc = load(d)
block(doc, "AI")["repos"].remove("alice/repo1")
block(doc, "Misc")["repos"].remove("alice/repo1")
doc["unlisted"].remove("alice/repo10")
doc["unstar"] = ["alice/repo1", "alice/repo10"]
save(d, doc)
code, out = run(g, "plan", "--dir", str(d))
check("plan shows unstars in their own section", code == 0 and "stars : 2 unstar · 0 re-star" in out
      and "x alice/repo1" in out and "x alice/repo10" in out, out)
check("plan writes nothing", g.mutations == 0)
code, out = run(g, "apply", "--dir", str(d), *COMMON)
check("--yes without --allow-unstar refused", code == 2 and "--allow-unstar" in out and g.mutations == 0, out)
code, out = run(g, "apply", "--dir", str(d), *COMMON, "--allow-unstar")
v = g.view()
check("unstarred repos lose their star", code == 0 and "alice/repo1" not in v["starred"] and "alice/repo10" not in v["starred"], out)
check("unstarred repo left every list", all("alice/repo1" not in l["repos"] for l in v["lists"].values()), str(v["lists"]))
check("all other stars kept", v["starred"] == sorted(set(INITIAL_VIEW["starred"]) - {"alice/repo1", "alice/repo10"}))
check("verification does not re-star them", "0 star(s) restored, 2 unstarred on purpose" in out, out)
ops = journal_ops(d, "apply")
check("unstar steps come last", ops[-2:] == ["unstar alice/repo1", "unstar alice/repo10"]
      and not any(o.startswith("unstar") for o in ops[:-2]), str(ops))
code, out = run(g, "plan", "--dir", str(d))
check("plan after unstar: nothing to do", code == 0 and "Nothing to do" in out, out)
snap = sorted((d / "snapshots").glob("preapply-*.json"))[-1]
code, out = run(g, "restore", str(snap), "--dir", str(d), *COMMON)
check("restore re-stars and puts repos back in their lists", code == 0 and g.view() == INITIAL_VIEW,
      json.dumps(g.view(), ensure_ascii=False)[:600] + out)
ops = journal_ops(d, "restore")
check("re-star steps come first", sorted(ops[:2]) == ["re-star alice/repo1", "re-star alice/repo10"], str(ops))
check("restore plan shows re-stars", "stars : 0 unstar · 2 re-star" in out, out)

# --------------------------------------------------------------------------- 15. unstar: validation, resume, side effects
d = fresh_dir()
g = initial()
run(g, "export", "--dir", str(d))
doc = load(d)
doc["unstar"] = ["alice/repo2"]  # still in AI
save(d, doc)
code, out = run(g, "plan", "--dir", str(d))
check("unstar + still in a list -> error", code == 2 and "also in list" in out, out)
doc["unstar"] = ["alice/nope"]
save(d, doc)
code, out = run(g, "plan", "--dir", str(d))
check("unknown repo under unstar -> error", code == 1 and "under `unstar`" in out and g.mutations == 0, out)
doc["unstar"] = ["alice/repo11"]  # also still under unlisted
save(d, doc)
code, out = run(g, "plan", "--dir", str(d))
check("unstar + unlisted -> warning, unstar wins", code == 0 and "will be removed" in out and "x alice/repo11" in out, out)

g = initial()
d = fresh_dir()
run(g, "export", "--dir", str(d))
doc = load(d)
block(doc, "Tools")["repos"].remove("alice/repo4")
doc["unstar"] = ["alice/repo4"]
save(d, doc)
g.fail_at = 1  # the membership change fails before anything is unstarred
code, out = run(g, "apply", "--dir", str(d), *COMMON, "--allow-unstar")
check("failed step stops before the unstar", code == 2 and "alice/repo4" in g.view()["starred"], out)
g.fail_at = None
code, out = run(g, "apply", "--dir", str(d), *COMMON, "--allow-unstar")
check("rerun finishes the unstar", code == 0 and "alice/repo4" not in g.view()["starred"]
      and "alice/repo4" not in g.view()["lists"]["Tools"]["repos"], out)

g = initial(unstar_on_empty=True)  # GitHub drops the star as soon as the repo leaves its last list
d = fresh_dir()
run(g, "export", "--dir", str(d))
doc = load(d)
block(doc, "Edge")["repos"] = []
doc["unstar"] = ["alice/repo9"]
save(d, doc)
code, out = run(g, "apply", "--dir", str(d), *COMMON, "--allow-unstar")
check("star already dropped by GitHub: unstar still succeeds, not re-starred", code == 0
      and "alice/repo9" not in g.view()["starred"] and "1 unstarred on purpose" in out, out)

g = initial()
d = fresh_dir()
run(g, "export", "--dir", str(d))
doc = load(d)
doc["unlisted"].remove("alice/repo12")
doc["unstar"] = ["alice/repo12"]
save(d, doc)
g.starred.remove(g.rid("alice/repo12"))  # unstarred on github.com meanwhile
code, out = run(g, "plan", "--dir", str(d))
check("already unstarred on GitHub: nothing to do", code == 0 and "Nothing to do" in out, out)

# --------------------------------------------------------------------------- report
width = max(len(n) for n, _, _ in results)
fails = 0
for name, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        fails += 1
        print("      " + detail.replace("\n", "\n      ")[:3000])
print(f"\n{len(results) - fails}/{len(results)} passed")
sys.exit(1 if fails else 0)
