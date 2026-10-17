#!/usr/bin/env python3
"""Declaratively reorganise your GitHub *Star Lists* (delete / rename / create / regroup).

Workflow
--------
    python star_lists.py export      # 1. back up lists + stars, write an editable layout.yaml
    #                                  2. edit stars_backup/layout.yaml  (the state you WANT)
    python star_lists.py plan        # 3. dry run: print every change, touch nothing
    python star_lists.py apply       # 4. execute (asks first, takes a fresh backup first)
    python star_lists.py restore stars_backup/snapshots/<file>.json   # roll back any time

Guarantees
----------
* Never unstars anything you did not put under ``unstar:``. If GitHub drops a star as a side
  effect of a list edit, the repository is re-starred automatically during verification.
  Repos under ``unstar:`` are first taken out of every list, and their stars are removed as the
  very last step; ``restore`` stars them again (with a new star date).
* Idempotent / resumable: ``apply`` recomputes the diff from the live state every time,
  so after an interruption (network, rate limit, Ctrl-C) you simply run it again.
* Conservative scope: only lists and repos present in the export snapshot (or named in
  the layout) are touched. Lists created on github.com after exporting are left alone
  unless you pass ``--strict``.
* ``updateUserListsForItem`` sets the *complete* set of lists for a repo; the tool always
  sends the full target set, so memberships in lists you did not touch are preserved.

Authentication: ``GH_TOKEN`` / ``GITHUB_TOKEN`` env var, or a logged-in GitHub CLI
(``gh auth login``). If GitHub answers INSUFFICIENT_SCOPES, run
``gh auth refresh -h github.com -s user``.

Requirements: Python >= 3.9 and PyYAML (``pip install pyyaml``). Nothing else.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Protocol

__version__ = "1.0.0"
SNAPSHOT_FORMAT = 1
GRAPHQL_URL = "https://api.github.com/graphql"
NEW = "new:"  # key prefix for lists that do not exist on GitHub yet
_EMPTY_CONN: dict[str, Any] = {"nodes": [], "edges": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}


# --------------------------------------------------------------------------- errors
class StarListsError(Exception):
    """User-facing error: printed without a traceback."""


class GraphQLError(StarListsError):
    """GitHub returned a GraphQL ``errors`` array."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors
        self.types = {str(e.get("type", "")) for e in errors}
        msg = "; ".join(str(e.get("message", e)) for e in errors)
        if "INSUFFICIENT_SCOPES" in self.types:
            msg += ("\n  -> your token lacks a scope. With the GitHub CLI run:"
                    "\n       gh auth refresh -h github.com -s user")
        super().__init__(msg)


class ApplyAborted(StarListsError):
    """Raised when a mutation fails mid-way through ``apply``.

    Attributes describe the failing step so front-ends can phrase their own message.
    """

    step: int = 0
    total: int = 0
    op: str = ""
    cause: str = ""
    list_limit_hint: bool = False


# --------------------------------------------------------------------------- GraphQL documents
Q_VIEWER = "query Viewer { viewer { login } }"

Q_LISTS = """
query Lists($after: String) {
  viewer {
    lists(first: 100, after: $after) {
      nodes { id name description isPrivate }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""

Q_LIST_ITEMS = """
query ListItems($id: ID!, $after: String) {
  node(id: $id) {
    ... on UserList {
      items(first: 100, after: $after) {
        nodes { __typename ... on Repository { id nameWithOwner } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}"""

Q_STARS = """
query Stars($after: String) {
  viewer {
    starredRepositories(first: 100, after: $after,
                        orderBy: {field: STARRED_AT, direction: DESC}) {
      edges {
        starredAt
        node {
          id nameWithOwner description url stargazerCount isArchived pushedAt
          primaryLanguage { name }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""

M_CREATE = """
mutation CreateList($input: CreateUserListInput!) {
  createUserList(input: $input) { list { id name } }
}"""

M_UPDATE = """
mutation UpdateList($input: UpdateUserListInput!) {
  updateUserList(input: $input) { list { id name } }
}"""

M_DELETE = """
mutation DeleteList($input: DeleteUserListInput!) {
  deleteUserList(input: $input) { clientMutationId }
}"""

M_SET_LISTS = """
mutation SetItemLists($input: UpdateUserListsForItemInput!) {
  updateUserListsForItem(input: $input) { lists { id } }
}"""

M_ADD_STAR = """
mutation AddStar($input: AddStarInput!) {
  addStar(input: $input) { starrable { viewerHasStarred } }
}"""

M_REMOVE_STAR = """
mutation RemoveStar($input: RemoveStarInput!) {
  removeStar(input: $input) { starrable { viewerHasStarred } }
}"""


# --------------------------------------------------------------------------- client
class Client(Protocol):
    """Anything that can run a GraphQL document and return its ``data`` member."""

    def graphql(self, query: str, variables: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        ...


def resolve_token() -> str:
    """Return a token from GH_TOKEN / GITHUB_TOKEN, falling back to ``gh auth token``."""
    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=20)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    raise StarListsError("No GitHub token found. Run `gh auth login` (GitHub CLI) "
                         "or export GH_TOKEN=<personal access token>.")


def _wait_hint(headers: Any, body: str = "") -> Optional[float]:
    """Seconds to wait according to Retry-After / x-ratelimit-* headers (capped at 15 min)."""
    if headers is None:
        return None
    retry_after = headers.get("Retry-After")
    if retry_after and retry_after.isdigit():
        return min(float(retry_after), 900.0)
    if headers.get("x-ratelimit-remaining") == "0" and (headers.get("x-ratelimit-reset") or "").isdigit():
        return min(max(float(headers["x-ratelimit-reset"]) - time.time(), 1.0) + 2.0, 900.0)
    if "secondary rate limit" in body.lower():
        return 60.0
    return None


class GitHubClient:
    """Minimal stdlib GraphQL client with retry and back-off."""

    def __init__(self, token: str, url: str = GRAPHQL_URL, max_retries: int = 6) -> None:
        self.token = token
        self.url = url
        self.max_retries = max_retries

    def graphql(self, query: str, variables: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        backoff = 2.0
        for attempt in range(self.max_retries + 1):
            last = attempt == self.max_retries
            req = urllib.request.Request(self.url, data=body, method="POST", headers={
                "Authorization": f"bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": f"star-lists/{__version__}",
            })
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    headers = resp.headers
                    payload = json.loads(resp.read().decode())
            except urllib.error.HTTPError as exc:
                text = exc.read().decode(errors="replace")
                if exc.code == 401:
                    raise StarListsError("GitHub rejected the token (HTTP 401). Re-run `gh auth login`.") from None
                hint = _wait_hint(exc.headers, text)
                transient = exc.code in (429, 500, 502, 503, 504) or (exc.code == 403 and hint is not None)
                if transient and not last:
                    wait = hint or backoff
                    _echo(f"    ... HTTP {exc.code}, waiting {wait:.0f}s and retrying")
                    time.sleep(wait)
                    backoff = min(backoff * 2, 120.0)
                    continue
                raise StarListsError(f"HTTP {exc.code} from GitHub: {text[:400]}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                    raise StarListsError(
                        "TLS certificate check failed. On macOS with a python.org build, run "
                        "'/Applications/Python 3.x/Install Certificates.command' once.") from None
                if not last:
                    _echo(f"    ... network error ({exc}), retrying in {backoff:.0f}s")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 120.0)
                    continue
                raise StarListsError(f"Network error: {exc}") from None
            errors = payload.get("errors")
            if errors:
                types = {str(e.get("type", "")) for e in errors}
                if "RATE_LIMITED" in types and not last:
                    wait = _wait_hint(headers) or 60.0
                    _echo(f"    ... GraphQL rate limit, waiting {wait:.0f}s")
                    time.sleep(wait)
                    continue
                raise GraphQLError(errors)
            return payload["data"]
        raise StarListsError("Gave up after repeated retries.")


# --------------------------------------------------------------------------- state model
@dataclass
class RepoInfo:
    id: str
    full_name: str
    starred: bool = True
    description: str = ""
    language: str = ""
    stars: int = 0
    archived: bool = False
    pushed_at: str = ""
    starred_at: str = ""
    url: str = ""


@dataclass
class ListInfo:
    id: str
    name: str
    description: str = ""
    private: bool = False
    items: list[str] = field(default_factory=list)  # repo node ids, API order


@dataclass
class State:
    """A full picture of one account's stars and lists at a point in time."""

    login: str
    fetched_at: str
    lists: dict[str, ListInfo]
    repos: dict[str, RepoInfo]

    def memberships(self) -> dict[str, set[str]]:
        """repo id -> ids of the lists that contain it."""
        out: dict[str, set[str]] = {}
        for lst in self.lists.values():
            for rid in lst.items:
                out.setdefault(rid, set()).add(lst.id)
        return out

    def repo_ids_by_name(self) -> dict[str, str]:
        return {r.full_name.casefold(): r.id for r in self.repos.values()}

    def to_json(self) -> dict[str, Any]:
        return {
            "format": SNAPSHOT_FORMAT,
            "tool": f"star_lists.py {__version__}",
            "login": self.login,
            "fetched_at": self.fetched_at,
            "lists": [asdict(lst) for lst in self.lists.values()],
            "repos": [asdict(r) for r in self.repos.values()],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "State":
        if data.get("format") != SNAPSHOT_FORMAT:
            raise StarListsError(f"Unsupported snapshot format: {data.get('format')!r}")
        lists = {d["id"]: ListInfo(**d) for d in data["lists"]}
        repos = {d["id"]: RepoInfo(**d) for d in data["repos"]}
        return cls(login=data["login"], fetched_at=data["fetched_at"], lists=lists, repos=repos)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "State":
        if not path.exists():
            raise StarListsError(f"Snapshot not found: {path}  (run `export` first)")
        return cls.from_json(json.loads(path.read_text(encoding="utf-8")))


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _echo(msg: str = "") -> None:
    print(msg, flush=True)


def _paginate(client: Client, query: str, variables: dict[str, Any],
              conn_of: Callable[[dict[str, Any]], dict[str, Any]], key: str = "nodes") -> Iterator[dict[str, Any]]:
    after: Optional[str] = None
    while True:
        conn = conn_of(client.graphql(query, {**variables, "after": after})) or _EMPTY_CONN
        yield from conn.get(key) or []
        info = conn.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return
        after = info["endCursor"]


def fetch_state(client: Client, workers: int = 4) -> State:
    """Read every starred repo and every list (with members) of the authenticated user.

    The starred-repo pages must be read in order (cursor pagination), but that chain runs
    in parallel with the per-list reads, which themselves run ``workers`` at a time.
    """
    from concurrent.futures import ThreadPoolExecutor

    def read_stars() -> dict[str, RepoInfo]:
        out: dict[str, RepoInfo] = {}
        for edge in _paginate(client, Q_STARS, {}, lambda d: d["viewer"]["starredRepositories"], key="edges"):
            n = edge["node"]
            out[n["id"]] = RepoInfo(
                id=n["id"], full_name=n["nameWithOwner"], starred=True,
                description=n.get("description") or "",
                language=(n.get("primaryLanguage") or {}).get("name") or "",
                stars=int(n.get("stargazerCount") or 0), archived=bool(n.get("isArchived")),
                pushed_at=n.get("pushedAt") or "", starred_at=edge.get("starredAt") or "",
                url=n.get("url") or "")
        return out

    def read_items(list_id: str) -> list[dict[str, Any]]:
        return [item for item in _paginate(client, Q_LIST_ITEMS, {"id": list_id},
                                           lambda d: (d.get("node") or {}).get("items"))
                if item.get("__typename") == "Repository"]

    login = client.graphql(Q_VIEWER)["viewer"]["login"]
    with ThreadPoolExecutor(max_workers=max(1, workers) + 1) as pool:
        stars_job = pool.submit(read_stars)
        lists: dict[str, ListInfo] = {}
        for n in _paginate(client, Q_LISTS, {}, lambda d: d["viewer"]["lists"]):
            lists[n["id"]] = ListInfo(id=n["id"], name=n["name"], description=n.get("description") or "",
                                      private=bool(n.get("isPrivate")))
        item_jobs = {lid: pool.submit(read_items, lid) for lid in lists}
        repos = stars_job.result()
        for lid, job in item_jobs.items():
            for item in job.result():
                lists[lid].items.append(item["id"])
                repos.setdefault(item["id"], RepoInfo(id=item["id"], full_name=item["nameWithOwner"],
                                                      starred=False))
    return State(login=login, fetched_at=_now(), lists=lists, repos=repos)


# --------------------------------------------------------------------------- layout file
LIST_KEYS = {"name", "id", "description", "private", "repos"}
_PLAIN_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+$")

LAYOUT_HEADER = """\
# GitHub Star Lists 目标布局 (这个文件 = 你想要的最终状态)
#   预览:  python star_lists.py plan      执行:  python star_lists.py apply
#
# 编辑规则
#   - 删除整个列表块                 -> 删除该列表 (其中的仓库仍保持 star)
#   - 改 name, 保留 id               -> 重命名, 成员不变
#   - 新增一个块且不写 id            -> 新建列表
#   - 剪切/复制仓库行到其他列表下    -> 调整归属 (同一仓库可以出现在多个列表)
#   - 仓库不出现在任何列表下         -> 不属于任何列表 (仍保持 star)
#   - unlisted 只是暂存区: 放在这里 = 不属于任何列表
#   - description / private 省略     -> 已有列表保持原值; 新列表默认无描述、公开
#   - 行尾 # 之后是注释 (语言 · star 数 · 简介), 可随意删改
#   - 仓库行剪切到 unstar 下         -> 取消 star (先移出所有列表, 最后一步才取消 star)
# 除了 unstar 下的仓库, 本工具不会取消任何 star.
"""


def _q(text: str) -> str:
    """Quote a scalar as a JSON string, which is also a valid YAML double-quoted scalar."""
    return json.dumps(text, ensure_ascii=False)


def _human(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _oneline(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _repo_line(r: RepoInfo, indent: str) -> str:
    name = r.full_name if _PLAIN_REPO.match(r.full_name) else _q(r.full_name)
    bits = [b for b in (r.language, f"★{_human(r.stars)}" if r.stars else "",
                        "ARCHIVED" if r.archived else "", "" if r.starred else "NOT STARRED",
                        _oneline(r.description, 70) if r.description else "") if b]
    return f"{indent}- {name}" + (f"   # {' · '.join(bits)}" if bits else "")


def render_layout(state: State) -> str:
    """Hand-written YAML so every repo line can carry a helpful trailing comment."""
    out = [LAYOUT_HEADER + f"# 导出自 @{state.login} · {state.fetched_at}", "", "lists:"]
    for lst in state.lists.values():
        out += [f"  - name: {_q(lst.name)}",
                f"    id: {_q(lst.id)}",
                f"    description: {_q(lst.description)}",
                f"    private: {'true' if lst.private else 'false'}"]
        if lst.items:
            out.append("    repos:")
            out += [_repo_line(state.repos[rid], "      ") for rid in lst.items]
        else:
            out.append("    repos: []")
        out.append("")
    listed = {rid for lst in state.lists.values() for rid in lst.items}
    unlisted = sorted((r for r in state.repos.values() if r.starred and r.id not in listed),
                      key=lambda r: r.starred_at, reverse=True)
    if not state.lists:
        out[-1] = "lists: []"
        out.append("")
    out.append("# 已 star 但不在任何列表中的仓库 (按 star 时间倒序)")
    if unlisted:
        out.append("unlisted:")
        out += [_repo_line(r, "  ") for r in unlisted]
    else:
        out.append("unlisted: []")
    out += ["", "# 要取消 star 的仓库: 把仓库行剪切到这里 (不能同时留在列表里).",
            "# 应用时先把它移出所有列表, 最后一步才取消 star. 以后可以用 restore 从备份恢复,",
            "# 但 star 时间会变成恢复当天.",
            "unstar: []"]
    return "\n".join(out) + "\n"


@dataclass
class ListSpec:
    name: str
    id: Optional[str] = None
    description: Optional[str] = None  # None = keep current value
    private: Optional[bool] = None     # None = keep current value
    repos: list[str] = field(default_factory=list)


@dataclass
class Layout:
    lists: list[ListSpec]
    unlisted: list[str]
    warnings: list[str] = field(default_factory=list)
    unstar: list[str] = field(default_factory=list)  # repos whose star is removed (after leaving all lists)


def parse_layout(text: str, source: str = "layout") -> Layout:
    """Parse and validate a layout document; all problems are reported at once."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        raise StarListsError("PyYAML is required to read the layout: pip install pyyaml") from None
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise StarListsError(f"{source}: YAML syntax error:\n{exc}") from None
    errs: list[str] = []
    warns: list[str] = []
    if not isinstance(doc, dict):
        raise StarListsError(f"{source}: top level must be a mapping with `lists:` and `unlisted:`")
    for key in set(doc) - {"lists", "unlisted", "unstar"}:
        errs.append(f"unknown top-level key {key!r} (allowed: lists, unlisted, unstar)")

    def str_list(value: Any, where: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            errs.append(f"{where} must be a list of 'owner/repo' strings")
            return []
        return [v.strip() for v in value]

    specs: list[ListSpec] = []
    raw_lists = doc.get("lists") or []
    if not isinstance(raw_lists, list):
        errs.append("`lists` must be a list of list blocks")
        raw_lists = []
    for i, raw in enumerate(raw_lists, 1):
        where = f"lists[{i}]"
        if not isinstance(raw, dict):
            errs.append(f"{where} must be a mapping (name/id/description/private/repos)")
            continue
        for key in set(raw) - LIST_KEYS:
            errs.append(f"{where}: unknown key {key!r} (allowed: {', '.join(sorted(LIST_KEYS))})")
        name = raw.get("name")
        if isinstance(name, (int, float)) and not isinstance(name, bool):
            name = str(name)
        if not isinstance(name, str) or not name.strip():
            errs.append(f"{where}: `name` is required")
            continue
        where = f'list "{name}"'
        lid = raw.get("id")
        if lid is not None and (not isinstance(lid, str) or not lid.strip()):
            errs.append(f"{where}: `id` must be a string (or omit it to create a new list)")
            lid = None
        desc = raw.get("description")
        if desc is not None and not isinstance(desc, str):
            desc = str(desc)
        private = raw.get("private")
        if private is not None and not isinstance(private, bool):
            errs.append(f"{where}: `private` must be true or false")
            private = None
        repos = str_list(raw.get("repos"), f"{where}: `repos`")
        seen: set[str] = set()
        unique: list[str] = []
        for r in repos:
            if r.casefold() in seen:
                warns.append(f"{where}: {r} listed twice (kept once)")
                continue
            seen.add(r.casefold())
            unique.append(r)
        specs.append(ListSpec(name=name.strip(), id=lid.strip() if lid else None,
                              description=desc, private=private, repos=unique))

    names: dict[str, str] = {}
    ids: set[str] = set()
    for s in specs:
        if s.name.casefold() in names:
            errs.append(f'two list blocks are named "{s.name}" (names must be unique)')
        names[s.name.casefold()] = s.name
        if s.id:
            if s.id in ids:
                errs.append(f"id {s.id} appears in two list blocks (copy-paste? remove one `id:`)")
            ids.add(s.id)
    unlisted = str_list(doc.get("unlisted"), "`unlisted`")
    unstar = list({r.casefold(): r for r in str_list(doc.get("unstar"), "`unstar`")}.values())
    in_lists = {r.casefold(): s.name for s in specs for r in s.repos}
    parked = {r.casefold() for r in unlisted}
    for r in unstar:
        if r.casefold() in in_lists:
            errs.append(f'{r} is under `unstar` and also in list "{in_lists[r.casefold()]}" - keep only one of them')
        elif r.casefold() in parked:
            warns.append(f"{r} is under both `unlisted` and `unstar` - its star will be removed")
    if errs:
        raise StarListsError(f"{source} has {len(errs)} problem(s):\n  - " + "\n  - ".join(errs))
    return Layout(lists=specs, unlisted=unlisted, warnings=warns, unstar=unstar)


def layout_from_state(state: State) -> Layout:
    """The layout that reproduces ``state`` exactly (used by ``restore``)."""
    specs = [ListSpec(name=l.name, id=l.id, description=l.description, private=l.private,
                      repos=[state.repos[r].full_name for r in l.items]) for l in state.lists.values()]
    return Layout(lists=specs, unlisted=[])


# --------------------------------------------------------------------------- planning
@dataclass
class Membership:
    repo_id: str
    repo: str
    before: list[str]  # list keys (live ids)
    after: list[str]   # list keys (live ids or NEW+name)


@dataclass
class Plan:
    login: str
    temp_renames: list[tuple[str, str, str]] = field(default_factory=list)  # (list id, current, temp)
    updates: list[tuple[str, dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    creates: list[ListSpec] = field(default_factory=list)
    memberships: list[Membership] = field(default_factory=list)
    deletes: list[tuple[str, str, int]] = field(default_factory=list)  # (list id, name, #repos)
    unstars: list[tuple[str, str]] = field(default_factory=list)   # (repo id, name): star removed last
    restars: list[tuple[str, str]] = field(default_factory=list)   # (repo id, name): restore stars it first
    cur_names: dict[str, str] = field(default_factory=dict)    # live id -> current name
    final_names: dict[str, str] = field(default_factory=dict)  # key -> final name
    untouched: list[str] = field(default_factory=list)
    emptied: int = 0          # repos that had >=1 list and will end up in none
    starred_total: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def n_ops(self) -> int:
        return (len(self.temp_renames) + len(self.updates) + len(self.creates)
                + len(self.memberships) + len(self.deletes) + len(self.unstars) + len(self.restars))


def build_plan(layout: Layout, live: State, snap: Optional[State], *,
               strict: bool = False, recreate_missing: bool = False) -> Plan:
    """Diff the desired ``layout`` against the ``live`` state.

    Args:
        layout: desired end state.
        live: freshly fetched state.
        snap: the export snapshot; defines which lists/repos this run is allowed to touch.
        strict: manage *every* live list and repo (lists absent from the layout get deleted).
        recreate_missing: ``restore`` mode - a block whose ``id`` no longer exists is re-created, and
            repos that are starred in ``snap`` but not any more are starred again.
    """
    p = Plan(login=live.login, warnings=list(layout.warnings))
    p.starred_total = sum(1 for r in live.repos.values() if r.starred)
    p.cur_names = {lid: l.name for lid, l in live.lists.items()}
    if snap is not None and snap.login.casefold() != live.login.casefold():
        p.errors.append(f"the snapshot belongs to @{snap.login} but you are logged in as @{live.login}")
        return p

    specs = [ListSpec(**asdict(s)) for s in layout.lists]
    spec_key: dict[int, str] = {}
    claimed: dict[str, ListSpec] = {}
    for i, s in enumerate(specs):  # pass 1: blocks that carry an id
        if not s.id:
            continue
        if s.id in live.lists:
            claimed[s.id] = s
            spec_key[i] = s.id
        elif recreate_missing:
            s.id = None
        else:
            p.errors.append(f'list "{s.name}": id {s.id} no longer exists on GitHub '
                            f"(deleted?). Remove its `id:` line to create it again.")
    by_name: dict[str, list[str]] = {}
    for lst in live.lists.values():
        by_name.setdefault(lst.name.casefold(), []).append(lst.id)
    for i, s in enumerate(specs):  # pass 2: blocks without id -> adopt same-named list or create
        if i in spec_key or s.id:
            continue
        free = [lid for lid in by_name.get(s.name.casefold(), []) if lid not in claimed]
        if free:
            claimed[free[0]] = s
            spec_key[i] = free[0]
        else:
            spec_key[i] = NEW + s.name
            p.creates.append(s)

    known_lists = set(live.lists) if (strict or snap is None) else set(snap.lists) & set(live.lists)
    to_delete = [lid for lid in live.lists if lid in known_lists and lid not in claimed]
    unmanaged = [lid for lid in live.lists if lid not in known_lists and lid not in claimed]
    p.untouched = [live.lists[lid].name for lid in unmanaged]
    if unmanaged:
        p.warnings.append(f"{len(unmanaged)} list(s) were created after the export and are not in the "
                          f"layout; they are left untouched: {', '.join(_q(n) for n in p.untouched)} "
                          f"(use --strict to manage them too)")

    final: dict[str, str] = {spec_key[i]: s.name for i, s in enumerate(specs) if i in spec_key}
    final.update({lid: live.lists[lid].name for lid in unmanaged})
    p.final_names = final
    owner_of: dict[str, str] = {}
    for key, name in final.items():
        if name.casefold() in owner_of:
            p.errors.append(f'two lists would both be named "{name}" (one of them is not in the layout)')
        owner_of[name.casefold()] = key

    # ---- list edits; holders of a name another list wants are moved to a temp name first
    for lid, s in claimed.items():
        cur = live.lists[lid]
        before = {"name": cur.name, "description": cur.description, "isPrivate": cur.private}
        after = {"name": s.name,
                 "description": cur.description if s.description is None else s.description,
                 "isPrivate": cur.private if s.private is None else s.private}
        if after != before:
            p.updates.append((lid, before, after))
    wanted = {after["name"].casefold(): lid for lid, before, after in p.updates if after["name"] != before["name"]}
    for lid, cur in live.lists.items():
        taker = wanted.get(cur.name.casefold())
        if taker is not None and taker != lid:
            p.temp_renames.append((lid, cur.name, f"tmp-{lid[-10:]}"))
    p.deletes = [(lid, live.lists[lid].name, len(live.lists[lid].items)) for lid in to_delete]

    # ---- stars: restore re-stars what the snapshot had; `unstar:` removes stars (last step)
    live_ids = live.repo_ids_by_name()
    snap_ids = snap.repo_ids_by_name() if snap else {}
    repo_name = {rid: r.full_name for rid, r in live.repos.items()}
    if recreate_missing and snap is not None:
        p.restars = [(r.id, r.full_name) for r in snap.repos.values()
                     if r.starred and not (r.id in live.repos and live.repos[r.id].starred)]
        for rid, name in p.restars:
            repo_name.setdefault(rid, name)
    restar_ids = {rid for rid, _ in p.restars}

    def resolve(name: str) -> Optional[str]:
        cf = name.casefold()
        if cf in live_ids:
            return live_ids[cf]
        rid = snap_ids.get(cf)
        if rid and rid in live.repos:
            p.warnings.append(f"{name} is now called {live.repos[rid].full_name} on GitHub (renamed/transferred)")
            return rid
        if rid in restar_ids:
            return rid
        if rid:
            p.warnings.append(f"skipped {name}: it is no longer starred (star it again to put it in a list)")
            return None
        p.errors.append(f"unknown repository {name!r} - typo, or not starred? (star it on GitHub first)")
        return None

    dropping: set[str] = set()  # repos under `unstar:` that still have a star or a list
    for name in layout.unstar:
        cf = name.casefold()
        rid = live_ids.get(cf) or snap_ids.get(cf)
        if rid is None:
            p.errors.append(f"unknown repository {name!r} under `unstar`")
        elif rid in live.repos:  # absent from live = already unstarred and in no list: nothing to do
            dropping.add(rid)
            if live.repos[rid].starred:
                p.unstars.append((rid, live.repos[rid].full_name))

    mentioned: dict[str, set[str]] = {}
    for i, s in enumerate(specs):
        key = spec_key.get(i)
        if key is None:
            continue
        for name in s.repos:
            rid = resolve(name)
            if rid:
                mentioned.setdefault(rid, set()).add(key)
    for rid in sorted(dropping & set(mentioned)):
        p.errors.append(f"{repo_name[rid]} is under `unstar` but also in a list - keep only one of them")
    parked = {rid for rid in (resolve(n) for n in layout.unlisted) if rid}
    both = sorted(live.repos[r].full_name for r in parked if r in mentioned)
    if both:
        p.warnings.append(f"{len(both)} repo(s) are under `unlisted` and also in a list - the list wins: "
                          + ", ".join(both[:5]) + (" ..." if len(both) > 5 else ""))

    current = live.memberships()
    if strict or snap is None:
        scope = set(live.repos)
    else:
        known = set(snap.repos) | {rid for l in snap.lists.values() for rid in l.items}
        scope = known & set(live.repos)
    scope |= set(mentioned) | dropping
    deleted, keep = set(to_delete), set(unmanaged)
    silently_emptied = 0
    for rid in sorted(scope, key=lambda r: repo_name[r].casefold()):
        cur = current.get(rid, set())
        if rid in dropping:  # leaves every list, including ones made after the export
            target: set[str] = set()
        else:
            target = set(mentioned.get(rid, set())) | (cur & keep)
            if cur and not target:
                p.emptied += 1
        if target == cur - deleted and not (rid in dropping and cur):
            continue  # nothing to do, or only membership in lists that will be deleted anyway
        if rid not in mentioned and rid not in parked and rid not in dropping and cur - deleted:
            silently_emptied += 1
        p.memberships.append(Membership(rid, repo_name[rid], sorted(cur), sorted(target)))
    if silently_emptied:
        p.warnings.append(f"{silently_emptied} repo(s) are not mentioned anywhere in the layout and will be "
                          f"removed from their lists (they stay starred). If unintended, put them back.")
    return p


# --------------------------------------------------------------------------- presentation
def _names(keys: list[str], names: dict[str, str]) -> str:
    if not keys:
        return "(no list)"
    return "{" + ", ".join(_q(names.get(k, k[len(NEW):] if k.startswith(NEW) else k)) for k in keys) + "}"


def _describe_update(before: dict[str, Any], after: dict[str, Any]) -> str:
    parts = []
    if after["name"] != before["name"]:
        parts.append(f'rename {_q(before["name"])} -> {_q(after["name"])}')
    else:
        parts.append(f'edit {_q(after["name"])}:')
    if after["description"] != before["description"]:
        parts.append(f'description -> {_q(_oneline(after["description"], 50))}')
    if after["isPrivate"] != before["isPrivate"]:
        parts.append("private" if after["isPrivate"] else "public")
    return " ".join(parts)


def print_plan(p: Plan, limit: int = 60) -> None:
    _echo(f"\n=== Plan for @{p.login} ===")
    if p.errors:
        _echo(f"\nERRORS ({len(p.errors)}) - nothing will be changed until these are fixed:")
        for e in p.errors:
            _echo(f"  x {e}")
        return
    adds = {s.name: 0 for s in p.creates}
    for m in p.memberships:
        for k in m.after:
            if k.startswith(NEW):
                adds[k[len(NEW):]] += 1
    _echo(f"  lists : {len(p.creates)} create · {len(p.updates)} rename/edit · {len(p.deletes)} delete"
          + (f" · {len(p.untouched)} untouched" if p.untouched else ""))
    _echo(f"  repos : {len(p.memberships)} membership change(s) out of {p.starred_total} starred")
    if p.unstars or p.restars:
        _echo(f"  stars : {len(p.unstars)} unstar · {len(p.restars)} re-star")
    if p.temp_renames:
        _echo(f"  ({len(p.temp_renames)} list(s) get a temporary name first so names can be swapped)")
    if p.updates or p.creates or p.deletes:
        _echo("")
    for lid, before, after in p.updates:
        _echo(f"  ~ {_describe_update(before, after)}")
    for s in p.creates:
        _echo(f"  + create {_q(s.name)}{' (private)' if s.private else ''} with {adds[s.name]} repo(s)")
    for lid, name, n in p.deletes:
        _echo(f"  - delete {_q(name)} ({n} repo(s) currently)")
    if p.restars:
        _echo("\n  re-star (first step):")
        for _, name in p.restars:
            _echo(f"    * {name}")
    if p.unstars:
        _echo("\n  UNSTAR (last step; they leave every list, a later restore gives a new star date):")
        for _, name in p.unstars:
            _echo(f"    x {name}")
    if p.memberships:
        _echo("\n  membership changes:")
        for m in p.memberships[: limit if limit > 0 else None]:
            _echo(f"    {m.repo}: {_names(m.before, p.cur_names)} -> {_names(m.after, p.final_names)}")
        if 0 < limit < len(p.memberships):
            _echo(f"    ... and {len(p.memberships) - limit} more (use --limit 0 to show all)")
    if p.emptied:
        _echo(f"\n  note: {p.emptied} repo(s) will end up in no list (they stay starred)")
    warnings = list(dict.fromkeys(p.warnings))
    if warnings:
        _echo(f"\n  warnings ({len(warnings)}):")
        for w in warnings:
            _echo(f"    ! {w}")
    if p.n_ops == 0:
        _echo("\n  Nothing to do - GitHub already matches the layout.")


# --------------------------------------------------------------------------- execution
def execute(client: Client, plan: Plan, *, sleep: float, delete_first: bool, journal: Path) -> None:
    """Run the plan's mutations in a safe order, journaling every step."""
    key_to_id: dict[str, str] = {}

    def mutate(doc: str, inp: dict[str, Any]) -> dict[str, Any]:
        return client.graphql(doc, {"input": inp})

    def create(s: ListSpec) -> None:
        inp: dict[str, Any] = {"name": s.name, "isPrivate": bool(s.private)}
        if s.description:
            inp["description"] = s.description
        key_to_id[NEW + s.name] = mutate(M_CREATE, inp)["createUserList"]["list"]["id"]

    def set_lists(m: Membership) -> None:
        ids = [key_to_id.get(k, k) for k in m.after]
        missing = [k for k in ids if k.startswith(NEW)]
        if missing:
            raise StarListsError(f"internal: list {missing[0][len(NEW):]!r} was not created")
        mutate(M_SET_LISTS, {"itemId": m.repo_id, "listIds": ids})

    deleting = {lid for lid, _, _ in plan.deletes}
    deletes = [(f"delete {_q(name)}", (lambda lid=lid: mutate(M_DELETE, {"listId": lid})))
               for lid, name, _ in plan.deletes]
    temp = [(f"rename {_q(old)} -> {_q(tmp)} (temporary)",
             (lambda lid=lid, tmp=tmp: mutate(M_UPDATE, {"listId": lid, "name": tmp})))
            for lid, old, tmp in plan.temp_renames if not (delete_first and lid in deleting)]
    updates = [(_describe_update(before, after),
                (lambda lid=lid, after=after: mutate(M_UPDATE, {"listId": lid, **after})))
               for lid, before, after in plan.updates]
    creates = [(f"create {_q(s.name)}", (lambda s=s: create(s))) for s in plan.creates]
    members = [(f"{m.repo} -> {_names(m.after, plan.final_names)}", (lambda m=m: set_lists(m)))
               for m in plan.memberships]
    restars = [(f"re-star {name}", (lambda rid=rid: mutate(M_ADD_STAR, {"starrableId": rid})))
               for rid, name in plan.restars]
    unstars = [(f"unstar {name}", (lambda rid=rid: mutate(M_REMOVE_STAR, {"starrableId": rid})))
               for rid, name in plan.unstars]
    # re-stars come first (a repo must be starred to sit in a list); unstars always come last
    steps = (deletes + restars + temp + updates + creates + members + unstars) if delete_first \
        else (restars + temp + updates + creates + members + deletes + unstars)

    journal.parent.mkdir(parents=True, exist_ok=True)
    total = len(steps)
    width = len(str(total))
    with journal.open("a", encoding="utf-8") as jf:
        for i, (desc, run) in enumerate(steps, 1):
            try:
                run()
            except StarListsError as exc:
                jf.write(json.dumps({"t": _now(), "step": i, "of": total, "op": desc, "ok": False,
                                     "error": str(exc)}, ensure_ascii=False) + "\n")
                hint = ""
                if desc.startswith("create") and not delete_first and plan.deletes:
                    hint = ("\n  If GitHub refused because you have too many lists, re-run with "
                            "--delete-first (deletions happen before creations).")
                err = ApplyAborted(
                    f"step {i}/{total} failed: {desc}\n  {exc}\n  Steps 1..{i - 1} are done "
                    f"(see {journal}). Fix the cause and run `apply` again - it continues from the "
                    f"live state.{hint}")
                err.step, err.total, err.op, err.cause, err.list_limit_hint = i, total, desc, str(exc), bool(hint)
                raise err from None
            jf.write(json.dumps({"t": _now(), "step": i, "of": total, "op": desc, "ok": True},
                                ensure_ascii=False) + "\n")
            jf.flush()
            _echo(f"  [{i:>{width}}/{total}] {desc}")
            if i < total and sleep > 0:
                time.sleep(sleep)


def apply_with_verify(client: Client, layout: Layout, snap: Optional[State], plan: Plan, before: State, *,
                      strict: bool, recreate_missing: bool, sleep: float, delete_first: bool,
                      journal: Path, settle: float = 2.0) -> int:
    """Execute, then re-read GitHub, restore any lost stars and finish leftover steps (max 2 passes).

    Stars removed on purpose (``layout.unstar``) are not "lost" and are not put back."""
    execute(client, plan, sleep=sleep, delete_first=delete_first, journal=journal)
    wanted_gone = {n.casefold() for n in layout.unstar}
    intended = {rid for rid, _ in plan.unstars} | {r.id for r in before.repos.values()
                                                    if r.full_name.casefold() in wanted_gone}
    for attempt in (1, 2):
        if settle > 0:
            time.sleep(settle)
        _echo("\nVerifying against GitHub ...")
        after = fetch_state(client)
        lost = [r for r in before.repos.values() if r.starred and r.id not in intended
                and not (r.id in after.repos and after.repos[r.id].starred)]
        for r in lost:
            client.graphql(M_ADD_STAR, {"input": {"starrableId": r.id}})
            _echo(f"  re-starred {r.full_name} (the star was dropped as a side effect)")
        if lost:
            after = fetch_state(client)
        residual = build_plan(layout, after, snap, strict=strict, recreate_missing=recreate_missing)
        if residual.errors:
            print_plan(residual)
            return 1
        if residual.n_ops == 0:
            n_lists = len(after.lists)
            n_star = sum(1 for r in after.repos.values() if r.starred)
            _echo(f"  OK - GitHub matches the layout ({n_lists} lists, {n_star} starred repos, "
                  f"{len(lost)} star(s) restored, {len(intended)} unstarred on purpose).")
            return 0
        if attempt == 1:
            _echo(f"  {residual.n_ops} step(s) still pending - applying them once more")
            execute(client, residual, sleep=sleep, delete_first=delete_first, journal=journal)
    _echo("  Some changes are still pending (GitHub may be slow to update). "
          "Wait a minute and run `plan` again.")
    return 1


def _confirm(yes: bool, plan: Plan, sleep: float, allow_unstar: bool = False) -> None:
    minutes = plan.n_ops * (sleep + 0.5) / 60
    _echo(f"\n{plan.n_ops} API call(s), about {max(minutes, 0.1):.1f} min.")
    n = len(plan.unstars)
    if yes:
        if n and not allow_unstar:
            raise StarListsError(f"this plan removes {n} star(s); add --allow-unstar to confirm "
                                 "(or run without --yes to be asked)")
        return
    if not sys.stdin.isatty():
        raise StarListsError("refusing to change GitHub without confirmation (no terminal); add --yes")
    if input("Apply these changes to GitHub? Type 'yes' to continue: ").strip().lower() != "yes":
        raise StarListsError("aborted - nothing was changed")
    if n and input(f"This also REMOVES {n} star(s) (a later restore gives them a new star date). "
                   "Type 'unstar' to continue: ").strip().lower() != "unstar":
        raise StarListsError("aborted - nothing was changed")


# --------------------------------------------------------------------------- commands
def _write_inventory(state: State, path: Path) -> None:
    m = state.memberships()
    with path.open("w", newline="", encoding="utf-8-sig") as fh:  # BOM so Excel reads UTF-8
        w = csv.writer(fh)
        w.writerow(["repo", "lists", "language", "stars", "archived", "pushed_at", "starred_at",
                    "description", "url"])
        for r in sorted(state.repos.values(), key=lambda r: r.starred_at, reverse=True):
            lists = sorted(state.lists[lid].name for lid in m.get(r.id, ()))
            w.writerow([r.full_name, "; ".join(lists), r.language, r.stars, r.archived, r.pushed_at[:10],
                        r.starred_at[:10], _oneline(r.description, 300), r.url])


def cmd_export(args: argparse.Namespace, client: Client) -> int:
    out = Path(args.dir)
    layout_path = out / "layout.yaml"
    if layout_path.exists() and not args.force:
        raise StarListsError(f"{layout_path} already exists (your edits?). "
                             "Move it away or pass --force to overwrite it.")
    _echo("Reading your stars and lists from GitHub ...")
    state = fetch_state(client)
    out.mkdir(parents=True, exist_ok=True)
    state.save(out / "snapshot.json")
    archived = out / "snapshots" / f"export-{_stamp()}.json"
    state.save(archived)
    layout_path.write_text(render_layout(state), encoding="utf-8")
    _write_inventory(state, out / "inventory.csv")
    listed = {rid for l in state.lists.values() for rid in l.items}
    n_star = sum(1 for r in state.repos.values() if r.starred)
    n_unlisted = sum(1 for r in state.repos.values() if r.starred and r.id not in listed)
    _echo(f"@{state.login}: {n_star} starred repos, {len(state.lists)} lists, {n_unlisted} starred repos in no list.")
    _echo(f"  backup     : {out / 'snapshot.json'}  (+ {archived.name})")
    _echo(f"  edit this  : {layout_path}")
    _echo(f"  overview   : {out / 'inventory.csv'}  (open in Excel/Numbers to decide categories)")
    _echo("Next: edit layout.yaml, then run `plan`.")
    return 0


def _inputs(args: argparse.Namespace) -> tuple[Layout, State]:
    out = Path(args.dir)
    layout_path = Path(args.layout) if args.layout else out / "layout.yaml"
    snap_path = Path(args.snapshot) if args.snapshot else out / "snapshot.json"
    if not layout_path.exists():
        raise StarListsError(f"Layout not found: {layout_path}  (run `export` first)")
    layout = parse_layout(layout_path.read_text(encoding="utf-8"), str(layout_path))
    return layout, State.load(snap_path)


def cmd_plan(args: argparse.Namespace, client: Client) -> int:
    layout, snap = _inputs(args)
    live = fetch_state(client)
    plan = build_plan(layout, live, snap, strict=args.strict)
    print_plan(plan, args.limit)
    return 1 if plan.errors else 0


def _run(client: Client, args: argparse.Namespace, layout: Layout, snap: State, *,
         strict: bool, recreate_missing: bool, label: str) -> int:
    live = fetch_state(client)
    plan = build_plan(layout, live, snap, strict=strict, recreate_missing=recreate_missing)
    print_plan(plan, args.limit)
    if plan.errors:
        return 1
    if plan.n_ops == 0:
        return 0
    _confirm(args.yes, plan, args.sleep, getattr(args, "allow_unstar", False))
    stamp = _stamp()
    backup = Path(args.dir) / "snapshots" / f"pre{label}-{stamp}.json"
    live.save(backup)
    _echo(f"Backup of the current state: {backup}\n")
    return apply_with_verify(client, layout, snap, plan, live, strict=strict, recreate_missing=recreate_missing,
                             sleep=args.sleep, delete_first=args.delete_first,
                             journal=Path(args.dir) / "logs" / f"{label}-{stamp}.jsonl",
                             settle=args.settle)


def cmd_apply(args: argparse.Namespace, client: Client) -> int:
    layout, snap = _inputs(args)
    return _run(client, args, layout, snap, strict=args.strict, recreate_missing=False, label="apply")


def cmd_restore(args: argparse.Namespace, client: Client) -> int:
    snap = State.load(Path(args.snapshot))
    _echo(f"Restoring @{snap.login} to the state of {snap.fetched_at} ...")
    return _run(client, args, layout_from_state(snap), snap, strict=True, recreate_missing=True,
                label="restore")


def main(argv: Optional[list[str]] = None, client: Optional[Client] = None) -> int:
    ap = argparse.ArgumentParser(prog="star_lists.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", default="stars_backup", help="working folder (default: stars_backup)")
    runner = argparse.ArgumentParser(add_help=False)
    runner.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    runner.add_argument("--sleep", type=float, default=1.0, help="seconds between API writes (default 1.0)")
    runner.add_argument("--delete-first", action="store_true",
                        help="delete lists before creating new ones (if you hit the list limit)")
    runner.add_argument("--settle", type=float, default=2.0, help=argparse.SUPPRESS)
    runner.add_argument("--allow-unstar", action="store_true",
                        help="with --yes: allow removing the stars of repos listed under `unstar:`")
    shown = argparse.ArgumentParser(add_help=False)
    shown.add_argument("--limit", type=int, default=60, help="max membership lines to print (0 = all)")
    target = argparse.ArgumentParser(add_help=False)
    target.add_argument("--layout", help="layout file (default: <dir>/layout.yaml)")
    target.add_argument("--snapshot", help="export snapshot (default: <dir>/snapshot.json)")
    target.add_argument("--strict", action="store_true",
                        help="also manage lists/repos that are not in the export snapshot")

    e = sub.add_parser("export", parents=[common], help="back up stars + lists and write layout.yaml")
    e.add_argument("--force", action="store_true", help="overwrite an existing layout.yaml")
    sub.add_parser("plan", parents=[common, target, shown], help="dry run: show what apply would change")
    sub.add_parser("apply", parents=[common, target, shown, runner], help="make GitHub match layout.yaml")
    r = sub.add_parser("restore", parents=[common, shown, runner], help="roll back to a snapshot file")
    r.add_argument("snapshot", help="snapshot JSON (e.g. stars_backup/snapshots/preapply-*.json)")
    args = ap.parse_args(argv)
    try:
        client = client or GitHubClient(resolve_token())
        handler = {"export": cmd_export, "plan": cmd_plan, "apply": cmd_apply, "restore": cmd_restore}[args.cmd]
        return handler(args, client)
    except StarListsError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted. Run the same command again to continue from the live state.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
