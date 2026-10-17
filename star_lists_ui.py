#!/usr/bin/env python3
"""Starshelf - a local web app for organising your GitHub Star Lists.

    python3 star_lists_ui.py          # opens http://127.0.0.1:8765 in your browser

Everything you change in the page is a local draft (saved automatically) until you
press "应用到 GitHub". Each apply takes a backup first, and any backup can be restored
from the "备份" dialog. The work itself is done by star_lists.py (same folder), so the
page and the command line behave identically.

Security: the server listens on 127.0.0.1 only, checks the Host/Origin headers and
requires a per-run secret (in the URL after #t=) on every API call. The GitHub token
stays inside this process and is never sent to the browser.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import star_lists as sl  # noqa: E402
import star_lists_ai as ai  # noqa: E402

__version__ = "1.0.0"
HTML_PATH = HERE / "star_lists_ui.html"
MAX_BODY = 8 * 1024 * 1024
SNAPSHOT_NAME = re.compile(r"^[A-Za-z0-9._-]+\.json$")
PROGRESS = re.compile(r"^\s*\[\s*(\d+)/(\d+)\]\s*(.*)$")
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class Busy(sl.StarListsError):
    """Another apply/restore is still running."""


# --------------------------------------------------------------------------- payloads
def state_payload(state: sl.State) -> dict[str, Any]:
    """Everything the page needs to render, without any credentials."""
    return {
        "login": state.login,
        "fetched_at": state.fetched_at,
        "lists": [{"id": l.id, "name": l.name, "description": l.description, "private": l.private,
                   "items": list(l.items)} for l in state.lists.values()],
        "repos": [{"id": r.id, "full_name": r.full_name, "description": r.description,
                   "language": r.language, "stars": r.stars, "archived": r.archived,
                   "pushed_at": r.pushed_at, "starred_at": r.starred_at,
                   "url": r.url or f"https://github.com/{r.full_name}", "starred": r.starred}
                  for r in state.repos.values()],
    }


def layout_from_draft(draft_lists: Any, baseline: sl.State, unstar: Any = None) -> sl.Layout:
    """Turn the page's draft (lists with repo ids, repo ids to unstar) into a star_lists Layout."""
    if not isinstance(draft_lists, list):
        raise sl.StarListsError("草稿格式不正确（缺少 lists）")
    if unstar is not None and not isinstance(unstar, list):
        raise sl.StarListsError("草稿格式不正确（unstar）")
    names = {r.id: r.full_name for r in baseline.repos.values()}
    errors: list[str] = []
    seen: set[str] = set()
    specs: list[sl.ListSpec] = []
    listed: set[str] = set()
    for i, raw in enumerate(draft_lists, 1):
        if not isinstance(raw, dict):
            errors.append(f"第 {i} 个列表格式不正确")
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            errors.append(f"第 {i} 个列表没有名字")
            continue
        if name.casefold() in seen:
            errors.append(f"有两个列表都叫「{name}」，列表名不能重复")
        seen.add(name.casefold())
        items = [rid for rid in dict.fromkeys(raw.get("items") or []) if isinstance(rid, str) and rid in names]
        listed.update(items)
        lid = raw.get("id")
        specs.append(sl.ListSpec(name=name, id=lid if isinstance(lid, str) and lid else None,
                                 description=str(raw.get("description") or ""),
                                 private=bool(raw.get("private")), repos=[names[rid] for rid in items]))
    dropping = [rid for rid in dict.fromkeys(unstar or []) if isinstance(rid, str) and rid in names]
    for rid in dropping:
        if rid in listed:
            errors.append(f"{names[rid]} 还在列表里，不能同时取消 star")
    if errors:
        raise sl.StarListsError("\n".join(errors))
    gone = set(dropping)
    unlisted = [names[rid] for rid, r in baseline.repos.items() if r.starred and rid not in listed and rid not in gone]
    return sl.Layout(lists=specs, unlisted=unlisted, unstar=[names[rid] for rid in dropping])


def plan_fingerprint(plan: sl.Plan) -> str:
    """Stable digest of what a plan would do (used to detect drift between preview and apply)."""
    doc = {
        "temp": sorted(lid for lid, _, _ in plan.temp_renames),
        "updates": sorted([lid, after] for lid, _, after in plan.updates),
        "creates": sorted(s.name for s in plan.creates),
        "deletes": sorted(lid for lid, _, _ in plan.deletes),
        "members": sorted([m.repo_id, sorted(m.after)] for m in plan.memberships),
        "unstars": sorted(rid for rid, _ in plan.unstars),
        "restars": sorted(rid for rid, _ in plan.restars),
    }
    return hashlib.sha1(json.dumps(doc, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def plan_payload(plan: sl.Plan) -> dict[str, Any]:
    def final(key: str) -> str:
        return plan.final_names.get(key, key[len(sl.NEW):] if key.startswith(sl.NEW) else key)

    def current(key: str) -> str:
        return plan.cur_names.get(key, key)

    members = []
    new_counts = {s.name: 0 for s in plan.creates}
    for m in plan.memberships:
        before, after = set(m.before), set(m.after)
        for k in after:
            if k.startswith(sl.NEW):
                new_counts[k[len(sl.NEW):]] += 1
        members.append({
            "repo": m.repo,
            "kept": sorted(final(k) for k in before & after),
            "added": sorted(final(k) for k in after - before),
            "removed": sorted(current(k) for k in before - after),
        })
    return {
        "login": plan.login,
        "n_ops": plan.n_ops,
        "fingerprint": plan_fingerprint(plan),
        "errors": plan.errors,
        "warnings": list(dict.fromkeys(plan.warnings)),
        "creates": [{"name": s.name, "private": bool(s.private), "description": s.description or "",
                     "repos": new_counts.get(s.name, 0)} for s in plan.creates],
        "updates": [{"before": before, "after": after} for _, before, after in plan.updates],
        "deletes": [{"name": name, "repos": n} for _, name, n in plan.deletes],
        "temp_renames": len(plan.temp_renames),
        "memberships": members,
        "unstars": sorted(name for _, name in plan.unstars),
        "restars": sorted(name for _, name in plan.restars),
        "emptied": plan.emptied,
        "untouched": plan.untouched,
        "starred_total": plan.starred_total,
    }


# --------------------------------------------------------------------------- background job
class Job:
    """Progress of one apply/restore run, shared with the polling page."""

    def __init__(self, kind: str = "", status: str = "idle") -> None:
        self.kind = kind
        self.status = status  # idle | running | done | failed
        self.phase = ""
        self.step = 0
        self.total = 0
        self.current = ""
        self.lines: list[str] = []
        self.error: Optional[str] = None
        self.backup: Optional[str] = None
        self.started = time.time()
        self.finished: Optional[float] = None
        self._lock = threading.Lock()

    def add(self, line: str) -> None:
        with self._lock:
            match = PROGRESS.match(line)
            if match:
                self.step, self.total, self.current = int(match[1]), int(match[2]), match[3]
                self.phase = "正在写入 GitHub"
            elif line.startswith("Verifying"):
                self.phase = "正在核对 GitHub 上的结果"
            self.lines.append(line)
            del self.lines[:-400]

    def set(self, **kw: Any) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def to_json(self) -> dict[str, Any]:
        with self._lock:
            return {"kind": self.kind, "status": self.status, "phase": self.phase, "step": self.step,
                    "total": self.total, "current": self.current, "log": self.lines[-60:],
                    "error": self.error, "backup": self.backup, "started": self.started,
                    "finished": self.finished}


class SuggestJob:
    """One AI suggestion run. A newer run replaces an older one; the older thread's result is dropped."""

    def __init__(self, seq: int = 0, mode: str = "", n_repos: int = 0, status: str = "idle",
                 backend: str = "", label: str = "") -> None:
        self.id = seq
        self.mode = mode
        self.backend = backend
        self.label = label
        self.n_repos = n_repos
        self.status = status  # idle | running | done | failed
        self.result: Optional[dict[str, Any]] = None
        self.error: Optional[str] = None
        self.started = time.time()
        self.finished: Optional[float] = None

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "mode": self.mode, "backend": self.backend, "label": self.label, "n_repos": self.n_repos, "status": self.status,
                "result": self.result, "error": self.error, "started": self.started, "finished": self.finished,
                "elapsed": (self.finished or time.time()) - self.started}


# --------------------------------------------------------------------------- application
class App:
    def __init__(self, client: sl.Client, workdir: Path, *, sleep: float = 1.0, settle: float = 2.0,
                 suggesters: Optional[list[ai.Suggester]] = None) -> None:
        self.client = client
        self.dir = workdir
        self.sleep = sleep
        self.settle = settle
        self.token = secrets.token_urlsafe(18)
        self.lock = threading.RLock()
        self.baseline: Optional[sl.State] = None
        self.job = Job()
        self.suggesters: list[ai.Suggester] = suggesters if suggesters is not None else ai.default_backends()
        self.suggest_job = SuggestJob()
        self.chat_job = SuggestJob()

    # ---- files
    @property
    def draft_path(self) -> Path:
        return self.dir / "ui_draft.json"

    @property
    def baseline_path(self) -> Path:
        return self.dir / "ui_baseline.json"

    def _read_draft(self) -> Optional[dict[str, Any]]:
        try:
            data = json.loads(self.draft_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _write_draft(self, draft: Optional[dict[str, Any]]) -> None:
        if draft is None:
            self.draft_path.unlink(missing_ok=True)
            return
        tmp = self.draft_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.draft_path)

    def _snapshot_path(self, name: Any) -> Path:
        if not isinstance(name, str) or not SNAPSHOT_NAME.match(name):
            raise sl.StarListsError("备份文件名不正确")
        path = self.dir / "snapshots" / name
        if not path.is_file():
            raise sl.StarListsError(f"找不到备份 {name}")
        return path

    def _ensure_idle(self) -> None:
        if self.job.status == "running":
            raise Busy("正在写入 GitHub，请等它完成后再操作")

    # ---- endpoints
    def get_state(self, _: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            draft = self._read_draft()
            if self.baseline is None:
                if draft is not None and self.baseline_path.exists():
                    self.baseline = sl.State.load(self.baseline_path)
                else:
                    self._refresh_locked()
                    draft = None
            if draft is not None and draft.get("fetched_at") != self.baseline.fetched_at:
                draft = None
            return {"baseline": state_payload(self.baseline), "draft": draft, "job": self.job.to_json()}

    def refresh(self, _: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self._ensure_idle()
            self._refresh_locked()
            self._write_draft(None)
            return {"baseline": state_payload(self.baseline), "draft": None}

    def _refresh_locked(self) -> None:
        state = sl.fetch_state(self.client)
        self.dir.mkdir(parents=True, exist_ok=True)
        state.save(self.baseline_path)
        self.baseline = state

    def save_draft(self, body: dict[str, Any]) -> dict[str, Any]:
        draft = body.get("draft")
        if draft is not None and not isinstance(draft, dict):
            raise sl.StarListsError("草稿格式不正确")
        with self.lock:
            self._write_draft(draft)
        return {"saved": draft is not None, "at": time.time()}

    def _resolve(self, body: dict[str, Any]) -> tuple[sl.Layout, sl.State, bool, bool, str]:
        if body.get("restore"):
            snap = sl.State.load(self._snapshot_path(body["restore"]))
            return sl.layout_from_state(snap), snap, True, True, "restore"
        if self.baseline is None:
            raise sl.StarListsError("还没有读取 GitHub 状态，请刷新页面")
        return (layout_from_draft(body.get("lists"), self.baseline, body.get("unstar")), self.baseline,
                False, False, "apply")

    def plan(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self._ensure_idle()
            layout, snap, strict, recreate, kind = self._resolve(body)
        live = sl.fetch_state(self.client)
        plan = sl.build_plan(layout, live, snap, strict=strict, recreate_missing=recreate)
        return {**plan_payload(plan), "kind": kind}

    def apply(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self._ensure_idle()
            layout, snap, strict, recreate, kind = self._resolve(body)
            self.job = Job(kind=kind, status="running")
            self.job.phase = "正在读取 GitHub 当前状态"
            args = (self.job, layout, snap, strict, recreate, kind, body.get("fingerprint"),
                    bool(body.get("delete_first")), body.get("confirm_unstar") is True)
            threading.Thread(target=self._run, args=args, daemon=True, name="star-lists-apply").start()
            return self.job.to_json()

    def job_status(self, _: dict[str, Any]) -> dict[str, Any]:
        return self.job.to_json()

    def snapshots(self, _: dict[str, Any]) -> dict[str, Any]:
        out = []
        folder = self.dir / "snapshots"
        for path in sorted(folder.glob("*.json")) if folder.exists() else []:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                out.append({"file": path.name, "login": data.get("login"), "fetched_at": data.get("fetched_at"),
                            "lists": len(data.get("lists") or []),
                            "starred": sum(1 for r in data.get("repos") or [] if r.get("starred")),
                            "kind": path.name.split("-", 1)[0]})
            except (OSError, json.JSONDecodeError):
                continue
        out.sort(key=lambda s: s.get("fetched_at") or "", reverse=True)
        return {"snapshots": out}

    # ---- AI suggestions (draft only: nothing here writes to GitHub)
    def _backends(self) -> dict[str, Any]:
        backends = [s.status() for s in self.suggesters]
        default = next((b["id"] for b in backends if b.get("available")), backends[0]["id"] if backends else "")
        return {"backends": backends, "default": default}

    def suggest_status(self, _: dict[str, Any]) -> dict[str, Any]:
        job = self.suggest_job
        out = job.to_json()
        if job.status != "running":  # status checks start local commands; skip them while polling a run
            out["ai"] = self._backends()
        return out

    def _pick_backend(self, body: dict[str, Any]) -> tuple[ai.Suggester, dict[str, Any]]:
        wanted = body.get("backend") or self._backends()["default"]
        suggester = next((s for s in self.suggesters if s.id == wanted), None)
        if suggester is None:
            raise ai.AIError("没有这个 AI 选项")
        status = suggester.status()
        if not status.get("available"):
            raise ai.AIError("\n".join([status.get("reason", "AI 不可用")] + list(status.get("setup") or [])))
        return suggester, status

    def start_suggest(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.baseline is None:
                raise sl.StarListsError("还没有读取 GitHub 状态，请刷新页面")
            suggester, status = self._pick_backend(body)
            req = ai.parse_request(body, self.baseline)
            baseline = self.baseline
            job = SuggestJob(self.suggest_job.id + 1, req.mode, len(req.targets), "running",
                             suggester.id, status.get("label", suggester.id))
            self.suggest_job = job
        threading.Thread(target=self._run_ai, args=(job, lambda: ai.suggest(req, baseline, suggester)),
                         daemon=True, name="star-lists-suggest").start()
        return job.to_json()

    # ---- AI assistant chat: one turn at a time; the page applies the returned operations to its draft
    def chat_status(self, _: dict[str, Any]) -> dict[str, Any]:
        job = self.chat_job
        out = job.to_json()
        if job.status != "running":
            out["ai"] = self._backends()
        return out

    def start_chat(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.baseline is None:
                raise sl.StarListsError("还没有读取 GitHub 状态，请刷新页面")
            suggester, status = self._pick_backend(body)
            req = ai.parse_chat(body, self.baseline)
            baseline = self.baseline
            n_repos = sum(1 for r in baseline.repos.values() if r.starred)
            job = SuggestJob(self.chat_job.id + 1, "chat", n_repos, "running",
                             suggester.id, status.get("label", suggester.id))
            self.chat_job = job
        threading.Thread(target=self._run_ai, args=(job, lambda: ai.chat(req, baseline, suggester)),
                         daemon=True, name="star-lists-chat").start()
        return job.to_json()

    def _run_ai(self, job: SuggestJob, work: Callable[[], dict[str, Any]]) -> None:
        try:
            result, error = work(), None
        except sl.StarListsError as exc:
            result, error = None, str(exc)
        except Exception as exc:  # noqa: BLE001 - report anything to the page
            traceback.print_exc()
            result, error = None, f"{type(exc).__name__}: {exc}"
        job.result, job.error = result, error
        job.finished = time.time()
        job.status = "done" if error is None else "failed"

    # ---- worker
    def _run(self, job: Job, layout: sl.Layout, snap: sl.State, strict: bool, recreate: bool, kind: str,
             fingerprint: Optional[str], delete_first: bool, confirm_unstar: bool = False) -> None:
        original_echo = sl._echo

        def echo(msg: str = "") -> None:
            original_echo(msg)
            for line in str(msg).splitlines():
                job.add(line)

        sl._echo = echo
        try:
            live = sl.fetch_state(self.client)
            plan = sl.build_plan(layout, live, snap, strict=strict, recreate_missing=recreate)
            if plan.errors:
                raise sl.StarListsError("\n".join(plan.errors))
            if fingerprint and fingerprint != plan_fingerprint(plan):
                raise sl.StarListsError("预览之后 GitHub 上的内容有变化（可能你在网页上改过）。"
                                        "没有做任何修改，请重新预览后再应用。")
            if plan.unstars and not confirm_unstar:
                raise sl.StarListsError(f"这次会取消 {len(plan.unstars)} 个仓库的 star，需要先在预览里勾选确认。"
                                        "没有做任何修改。")
            if plan.n_ops == 0:
                job.set(status="done", phase="GitHub 已经和草稿一致，无需修改", finished=time.time())
                with self.lock:
                    self.baseline = live
                    live.save(self.baseline_path)
                    if kind == "apply" or not self._read_draft():
                        self._write_draft(None)
                return
            stamp = sl._stamp()
            label = "preapply" if kind == "apply" else "prerestore"
            backup = self.dir / "snapshots" / f"{label}-ui-{stamp}.json"
            live.save(backup)
            job.set(backup=backup.name, total=plan.n_ops, phase="正在写入 GitHub")
            journal = self.dir / "logs" / f"{kind}-ui-{stamp}.jsonl"
            code = sl.apply_with_verify(self.client, layout, snap, plan, live, strict=strict,
                                        recreate_missing=recreate, sleep=self.sleep, delete_first=delete_first,
                                        journal=journal, settle=self.settle)
            fresh = sl.fetch_state(self.client)
            with self.lock:
                if code == 0:
                    self.baseline = fresh
                    fresh.save(self.baseline_path)
                    self._write_draft(None)
            if code == 0:
                job.set(status="done", phase="完成", finished=time.time())
            else:
                job.set(status="failed", finished=time.time(),
                        error="大部分修改已写入，但仍有少量没有生效（GitHub 可能还没更新完）。"
                              "等一分钟后再点「再次应用」即可补齐。")
        except sl.ApplyAborted as exc:
            msg = (f"第 {exc.step}/{exc.total} 步没有成功：{exc.op}\n原因：{exc.cause}\n\n"
                   f"前 {max(exc.step - 1, 0)} 步已经写入 GitHub 并会保留。处理好原因后点「查看剩余修改」，"
                   f"再点「应用到 GitHub」，会从当前进度继续。")
            if exc.list_limit_hint:
                msg += "\n\n如果原因是列表数量已达上限：在预览里勾选「先删除旧列表，再新建」再试。"
            job.set(status="failed", finished=time.time(), error=msg)
        except sl.StarListsError as exc:
            job.set(status="failed", error=str(exc), finished=time.time())
        except Exception as exc:  # noqa: BLE001 - report anything to the page
            traceback.print_exc()
            job.set(status="failed", error=f"{type(exc).__name__}: {exc}", finished=time.time())
        finally:
            sl._echo = original_echo


# --------------------------------------------------------------------------- HTTP
def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    routes: dict[tuple[str, str], Callable[[dict[str, Any]], dict[str, Any]]] = {
        ("GET", "/api/state"): app.get_state,
        ("POST", "/api/refresh"): app.refresh,
        ("PUT", "/api/draft"): app.save_draft,
        ("POST", "/api/plan"): app.plan,
        ("POST", "/api/apply"): app.apply,
        ("GET", "/api/job"): app.job_status,
        ("GET", "/api/snapshots"): app.snapshots,
        ("GET", "/api/suggest"): app.suggest_status,
        ("POST", "/api/suggest"): app.start_suggest,
        ("GET", "/api/chat"): app.chat_status,
        ("POST", "/api/chat"): app.start_chat,
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = f"Starshelf/{__version__}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # keep the terminal quiet
            pass

        def _origins(self) -> tuple[str, str]:
            port = self.server.server_address[1]
            return f"127.0.0.1:{port}", f"localhost:{port}"

        def _host_ok(self) -> bool:
            return (self.headers.get("Host") or "").lower() in self._origins()

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            if ctype.startswith("text/html"):
                self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _authorized(self) -> bool:
            origin = self.headers.get("Origin")
            if origin and origin.lower() not in {f"http://{o}" for o in self._origins()}:
                return False
            return secrets.compare_digest(self.headers.get("X-Session", ""), app.token)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("request too large")
            raw = self.rfile.read(length) if length else b""
            data = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(data, dict):
                raise ValueError("JSON object expected")
            return data

        def _handle(self) -> None:
            path = urlparse(self.path).path
            if not self._host_ok():
                return self._json(403, {"error": "forbidden"})
            if self.command in ("GET", "HEAD") and path in ("/", "/index.html"):
                try:
                    return self._send(200, HTML_PATH.read_bytes(), "text/html; charset=utf-8")
                except FileNotFoundError:
                    return self._json(500, {"error": f"缺少 {HTML_PATH.name}，请把它放在 {HERE}"})
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            route = routes.get((self.command, path))
            if route is None:
                return self._json(404, {"error": "not found"})
            if not self._authorized():
                return self._json(401, {"error": "链接缺少会话密钥。请使用终端里显示的完整链接打开（末尾带 #t=…）。"})
            try:
                body = self._body() if self.command in ("POST", "PUT") else {}
                self._json(200, route(body))
            except Busy as exc:
                self._json(409, {"error": str(exc)})
            except sl.StarListsError as exc:
                self._json(400, {"error": str(exc)})
            except (ValueError, UnicodeDecodeError) as exc:
                self._json(400, {"error": f"请求格式不正确：{exc}"})
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        do_GET = do_HEAD = do_POST = do_PUT = _handle

    return Handler


LOCK_NAME = "ui_server.json"


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":  # os.kill(pid, 0) would TERMINATE the process on Windows; the port check decides
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def running_instance(workdir: Path) -> Optional[str]:
    """URL of another live server that uses the same data folder, if any."""
    try:
        info = json.loads((workdir / LOCK_NAME).read_text(encoding="utf-8"))
        pid, url, port = int(info["pid"]), str(info["url"]), int(info["port"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    if pid != os.getpid() and _pid_alive(pid) and _port_open(port):
        return url
    return None


def write_lock(workdir: Path, url: str, port: int) -> None:
    path = workdir / LOCK_NAME
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "port": port, "url": url, "started": time.time()}, fh)
    os.chmod(path, 0o600)


def remove_lock(workdir: Path) -> None:
    path = workdir / LOCK_NAME
    try:
        if int(json.loads(path.read_text(encoding="utf-8")).get("pid", -1)) == os.getpid():
            path.unlink()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass


def _interrupt(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def watch_parent(server: ThreadingHTTPServer, app: App, parent: int) -> None:
    """Stop when the process that started us (e.g. the macOS app) is gone, unless GitHub is being written."""
    while True:
        time.sleep(2)
        if os.getppid() == parent or app.job.status == "running":
            continue
        print("启动 Starshelf 的应用已经退出，后台随之关闭。", flush=True)
        server.shutdown()
        return


def build_server(app: App, port: int, tries: int = 20) -> ThreadingHTTPServer:
    """Bind to 127.0.0.1 on the first free port in [port, port + tries)."""
    last: Optional[OSError] = None
    for candidate in range(port, port + tries) if port else [0]:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), make_handler(app))
            server.daemon_threads = True
            return server
        except OSError as exc:
            last = exc
    raise sl.StarListsError(f"无法监听本地端口 {port}-{port + tries - 1}: {last}")


def main(argv: Optional[list[str]] = None, client: Optional[sl.Client] = None) -> int:
    ap = argparse.ArgumentParser(prog="star_lists_ui.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=str(HERE / "stars_backup"),
                    help="backup/draft folder (default: stars_backup next to this script)")
    ap.add_argument("--port", type=int, default=8765, help="first port to try (default 8765)")
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    ap.add_argument("--exit-with-parent", action="store_true",
                    help="shut down when the launching process exits (used by the macOS app)")
    ap.add_argument("--sleep", type=float, default=1.0, help="seconds between GitHub writes (default 1.0)")
    ap.add_argument("--settle", type=float, default=2.0, help=argparse.SUPPRESS)
    ap.add_argument("--ai-model", default=None,
                    help=f"model for the Anthropic API backend (default: $STAR_LISTS_AI_MODEL or {ai.DEFAULT_MODEL})")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args(argv)
    try:
        if not HTML_PATH.exists():
            raise sl.StarListsError(f"缺少 {HTML_PATH.name}，请把它和本脚本放在同一个文件夹")
        workdir = Path(args.dir).expanduser().resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        existing = running_instance(workdir)
        if existing:  # one server per data folder, so two windows never edit the same draft
            print(f"Starshelf 已在运行: {existing}", flush=True)
            if not args.no_browser:
                webbrowser.open(existing)
            return 0
        client = client or sl.GitHubClient(sl.resolve_token())
        app = App(client, workdir, sleep=args.sleep, settle=args.settle,
                  suggesters=ai.default_backends(args.ai_model))
        server = build_server(app, args.port)
    except sl.StarListsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/#t={app.token}"
    write_lock(workdir, url, port)
    for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):  # no SIGHUP on Windows
        if sig is not None:
            signal.signal(sig, _interrupt)
    if args.exit_with_parent:
        threading.Thread(target=watch_parent, args=(server, app, os.getppid()), daemon=True).start()
    print(f"Starshelf 已启动: {url}\n数据文件夹: {workdir}\n关闭: 在这个窗口按 Ctrl+C", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        if app.job.status == "running":
            print("\n注意：写入 GitHub 时被中断。已完成的部分会保留，下次打开后点「查看剩余修改」继续。", flush=True)
        print("\n已关闭。", flush=True)
    finally:
        server.server_close()
        remove_lock(workdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
