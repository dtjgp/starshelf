"""Tests for star_lists_ai.py and the /api/suggest endpoint (no network, no API key needed)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).parent))
import star_lists as sl  # noqa: E402
import star_lists_ai as ai  # noqa: E402
import star_lists_ui as ui  # noqa: E402
from fake_ai import FakeSuggester, repo_lines  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"\n      {detail}"), flush=True)


def raises(fn: Any, text: str) -> bool:
    try:
        fn()
    except ai.AIError as exc:
        return text in str(exc)
    return False


# --------------------------------------------------------------------------- synthetic account
def state() -> sl.State:
    repos = {f"R{i}": sl.RepoInfo(f"R{i}", f"alice/repo{i}", starred=i <= 10, language="Python" if i % 2 else "",
                                  description=f"tool number {i} " + ("x" * 300 if i == 1 else ""))
             for i in range(1, 12)}
    lists = {
        "UL_ai": sl.ListInfo("UL_ai", "AI", "machine learning", items=["R1", "R2", "R3"]),
        "UL_tools": sl.ListInfo("UL_tools", "Tools", items=["R4", "R5"]),
        "UL_old": sl.ListInfo("UL_old", "Old", items=["R6", "R11"]),  # R11 is in a list but not starred
    }
    return sl.State("alice", "2026-10-17T00:00:00+00:00", lists, repos)


BASE = state()
DRAFT = [{"key": l.id, "name": l.name, "description": l.description, "items": list(l.items)} for l in BASE.lists.values()]


def req(mode: str, **kw: Any) -> ai.SuggestRequest:
    return ai.parse_request({"mode": mode, "lists": DRAFT, **kw}, BASE)


# --------------------------------------------------------------------------- 1. request validation
r = req("incremental")
check("incremental defaults to starred repos in no list", r.targets == ["R7", "R8", "R9", "R10"], str(r.targets))
r = req("reorganize")
check("reorganize covers starred + listed repos", sorted(r.targets) == sorted(f"R{i}" for i in range(1, 12)), str(r.targets))
r = req("incremental", targets=["R2", "nope", "R2", 5])
check("explicit targets are filtered and deduplicated", r.targets == ["R2"], str(r.targets))
check("excluded repos are never targets", "R1" not in req("reorganize", exclude=["R1"]).targets
      and req("incremental", exclude=["R7"]).targets == ["R8", "R9", "R10"])
check("unknown mode refused", raises(lambda: ai.parse_request({"mode": "x", "lists": DRAFT}, BASE), "未知"))
check("nothing to place refused", raises(lambda: req("incremental", targets=[]), "没有需要归类"))
check("duplicate list key refused", raises(lambda: ai.parse_request({"mode": "reorganize", "lists": DRAFT + DRAFT[:1]}, BASE), "key"))
check("max_lists clamped", req("reorganize", max_lists=999).max_lists == 50 and req("reorganize", max_lists=0).max_lists == 12)
check("no lists and no new lists refused",
      raises(lambda: ai.parse_request({"mode": "incremental", "lists": [], "allow_new": False}, BASE), "还没有任何列表"))

# --------------------------------------------------------------------------- 2. prompt
p = ai.build_prompt(req("reorganize", instructions="按用途分"), BASE.repos)
check("prompt uses short ids for repos and lists", p.repo_refs["r1"] == "R1" and p.list_refs == {"L1": "UL_ai", "L2": "UL_tools", "L3": "UL_old"})
check("prompt shows current membership", any(l.startswith("r1 | alice/repo1 |") and "in: L1" in l for l in p.user.splitlines()))
check("long descriptions are clipped", max(len(l) for l in p.user.splitlines() if l.startswith("r")) < 260)
check("user instructions included", "按用途分" in p.user)
check("reorganize uses the reorganize schema", p.schema is ai.REORG_SCHEMA)
pi = ai.build_prompt(req("incremental", allow_new=False), BASE.repos)
check("incremental lists show example members", "e.g. alice/repo1" in pi.user)
check("incremental without new lists says so", "Do not propose new lists" in pi.user and pi.schema is ai.INCR_SCHEMA)
check("incremental prompt lists only the targets", [x[1] for x in repo_lines(pi)] == [f"alice/repo{i}" for i in (7, 8, 9, 10)])

# --------------------------------------------------------------------------- 3. reorganize answers
R = req("reorganize")
P = ai.build_prompt(R, BASE.repos)
ref = {v: k for k, v in P.repo_refs.items()}
answer = {"lists": [
    {"ref": "L1", "name": "🤖 AI", "description": "ml", "repos": [ref["R1"], ref["R2"], ref["R7"], "r999"]},
    {"ref": "L1", "name": "AI again", "description": "", "repos": [ref["R3"]]},          # L1 reused twice -> new list
    {"ref": "", "name": "Tools", "description": "", "repos": [ref["R4"], ref["R5"], ref["R6"]]},  # adopts UL_tools by name
    {"ref": "", "name": "tools", "description": "", "repos": [ref["R8"]]},             # same name -> merged
    {"ref": "", "name": "Empty", "description": "", "repos": []},
    {"ref": "", "name": "", "description": "", "repos": [ref["R9"]]},                  # no name -> ignored
], "notes": "说明"}
out = ai.interpret(R, P, answer)
by = {e["name"]: e for e in out["lists"]}
check("reorganize result replaces the layout", out["replace"] is True and out["mode"] == "reorganize")
check("ref keeps the list (rename)", by["🤖 AI"]["key"] == "UL_ai" and by["🤖 AI"]["items"] == ["R1", "R2", "R7"])
check("second use of a ref becomes a new list", by["AI again"]["key"] is None)
check("new list named like an unclaimed list adopts it", by["Tools"]["key"] == "UL_tools")
check("same name twice is merged", by["Tools"]["items"] == ["R4", "R5", "R6", "R8"] and "tools" not in by)
check("empty and nameless lists dropped", "Empty" not in by and len(out["lists"]) == 3)
check("unreferenced lists are deleted", out["deleted"] == ["UL_old"])
check("unassigned repos reported", set(out["unassigned"]) == {"R9", "R10", "R11"}
      and any("3 个仓库 AI 没有归类" in w for w in out["warnings"]), json.dumps(out["warnings"], ensure_ascii=False))
check("invalid ids reported", any("1 个无效的编号" in w for w in out["warnings"]))
check("empty new list warning", any("空的新列表" in w for w in out["warnings"]))
check("too many lists warned", any("超过了你设的上限" in w
      for w in ai.interpret(req("reorganize", max_lists=2), P, answer)["warnings"]))
emptied = ai.interpret(R, P, {"lists": [{"ref": "L2", "name": "Tools", "description": "", "repos": []},
                                       {"ref": "L1", "name": "AI", "description": "", "repos": [ref["R1"]]}], "notes": ""})
check("emptied existing list is deleted and named in a warning", "UL_tools" in emptied["deleted"]
      and any("会被删除" in w and "Tools" in w for w in emptied["warnings"]), json.dumps(emptied, ensure_ascii=False))
check("garbage answer refused", raises(lambda: ai.interpret(R, P, ["x"]), "格式不正确"))
check("no usable list refused", raises(lambda: ai.interpret(R, P, {"lists": [], "notes": ""}), "没有给出可用的列表"))
long = ai.interpret(R, P, {"lists": [{"ref": "", "name": "n" * 500, "description": "d" * 900, "repos": [ref["R1"]]}], "notes": ""})
check("names and descriptions are clipped", len(long["lists"][0]["name"]) == ai.NAME_MAX and len(long["lists"][0]["description"]) == ai.DESC_MAX)

# --------------------------------------------------------------------------- 4. incremental answers
I = req("incremental")
PI = ai.build_prompt(I, BASE.repos)
iref = {v: k for k, v in PI.repo_refs.items()}
ans = {"assignments": [
    {"repo": iref["R7"], "lists": ["L1", "N1"]},
    {"repo": iref["R8"], "lists": ["N1", "N2"]},
    {"repo": iref["R9"], "lists": ["N3"]},               # N3 = "tools" -> existing Tools list
    {"repo": iref["R10"], "lists": []},
    {"repo": "r77", "lists": ["L1"]},
    {"repo": iref["R7"], "lists": ["L9"]},
], "new_lists": [{"ref": "N1", "name": "Bucket", "description": "b"}, {"ref": "N2", "name": "bucket", "description": ""},
                 {"ref": "N3", "name": "tools", "description": ""}, {"ref": "N4", "name": "Too many", "description": ""}],
   "notes": ""}
out = ai.interpret(I, PI, ans)
by = {e["name"]: e for e in out["lists"]}
check("incremental only adds", out["replace"] is False and out["deleted"] == [])
check("existing list receives additions only", by["AI"]["key"] == "UL_ai" and by["AI"]["items"] == ["R7"])
check("new list collects its repos (twin names merged)", by["Bucket"]["key"] is None and by["Bucket"]["items"] == ["R7", "R8"]
      and "bucket" not in by, json.dumps(out["lists"], ensure_ascii=False))
check("new list named like an existing list merges into it", by["Tools"]["key"] == "UL_tools" and by["Tools"]["items"] == ["R9"])
check("at most 3 new lists", "Too many" not in by)
check("unplaced target reported", out["unassigned"] == ["R10"])
check("bad ids reported", any("无效的编号" in w for w in out["warnings"]))
named = ai.interpret(I, PI, {**ans, "notes": "r1 放进 L1，L9 和 r99 不存在，URL1 不变"})
check("ids in notes become names", named["notes"] == "alice/repo7 放进 「AI」，L9 和 r99 不存在，URL1 不变", named["notes"])
no_new = ai.interpret(req("incremental", allow_new=False), PI, ans)
check("allow_new=False ignores new lists", all(e["key"] for e in no_new["lists"]))
check("nothing placed refused", raises(lambda: ai.interpret(I, PI, {"assignments": [], "new_lists": [], "notes": ""}), "没有给这些仓库"))
other = ai.interpret(I, PI, {"assignments": [{"repo": iref["R7"], "lists": ["L1"]}], "new_lists": [], "notes": ""})
check("non-target repo cannot be moved", all("R1" not in e["items"] for e in other["lists"]))

# --------------------------------------------------------------------------- 5. CLI backends (fake claude / codex executables)
bindir = Path(tempfile.mkdtemp(prefix="sl_bin_"))
LOG = bindir / "calls.jsonl"
FAKE_CLAUDE = f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
mode = os.environ.get("FAKE_MODE", "")
if args[:2] == ["auth", "status"]:
    print(json.dumps({{"loggedIn": mode != "loggedout", "authMethod": "claude.ai"}}))
    with open({str(LOG)!r}, "a") as f: f.write(json.dumps({{"tool": "claude", "status": True}}) + "\\n")
    sys.exit(0)
stdin = sys.stdin.read()
with open({str(LOG)!r}, "a") as f:
    f.write(json.dumps({{"tool": "claude", "args": args, "stdin": stdin, "cwd": os.getcwd(),
                        "api_key": "ANTHROPIC_API_KEY" in os.environ}}) + "\\n")
if mode == "error":
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": True, "result": "Usage limit reached"}})); sys.exit(1)
if mode == "garbage":
    print("boom", file=sys.stderr); sys.exit(2)
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "",
                  "structured_output": {{"lists": [], "notes": "cc"}},
                  "usage": {{"input_tokens": 5, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 200, "output_tokens": 77}},
                  "modelUsage": {{"claude-opus-5-5": {{}}}}}}))
"""
FAKE_CODEX = f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
mode = os.environ.get("FAKE_MODE", "")
if args[:2] == ["login", "status"]:
    print("Not logged in" if mode == "loggedout" else "Logged in using ChatGPT", file=sys.stderr); sys.exit(0)
stdin = sys.stdin.read()
schema = open(args[args.index("--output-schema") + 1]).read()
with open({str(LOG)!r}, "a") as f:
    f.write(json.dumps({{"tool": "codex", "args": args, "stdin": stdin, "cwd": os.getcwd(), "schema": schema,
                        "api_key": "OPENAI_API_KEY" in os.environ, "path0": os.environ["PATH"].split(os.pathsep)[0]}}) + "\\n")
print(json.dumps({{"type": "thread.started", "thread_id": "t"}}))
if mode == "error":
    print(json.dumps({{"type": "turn.failed", "error": {{"message": "usage limit"}}}})); sys.exit(1)
open(args[args.index("-o") + 1], "w").write(json.dumps({{"lists": [], "notes": "cx"}}))
print("not json at all")
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 900, "output_tokens": 40}}}}))
"""
for name, text in (("claude", FAKE_CLAUDE), ("codex", FAKE_CODEX)):
    (bindir / name).write_text(text)
    (bindir / name).chmod(0o755)


def log() -> list[dict[str, Any]]:
    return [json.loads(l) for l in LOG.read_text().splitlines()] if LOG.exists() else []


saved_env = {k: os.environ.get(k) for k in ("STAR_LISTS_CLAUDE_BIN", "STAR_LISTS_CODEX_BIN", "ANTHROPIC_API_KEY",
                                            "OPENAI_API_KEY", "FAKE_MODE")}
try:
    os.environ.update({"STAR_LISTS_CLAUDE_BIN": str(bindir / "claude"), "STAR_LISTS_CODEX_BIN": str(bindir / "codex"),
                       "ANTHROPIC_API_KEY": "sk-should-not-leak", "OPENAI_API_KEY": "sk-should-not-leak"})
    cc, cx = ai.ClaudeCodeSuggester(), ai.CodexSuggester()
    st = cc.status()
    check("claude-code: logged in via subscription", st["available"] and "会员额度" in st["note"] and st["model"] == "opus", str(st))
    cc.status()
    check("claude-code: status is cached", sum(1 for c in log() if c.get("status")) == 1)
    data, usage = cc.complete(P)
    c = log()[-1]
    a = c["args"]
    check("claude-code: headless with schema", a[:3] == ["-p", "--output-format", "json"]
          and json.loads(a[a.index("--json-schema") + 1]) == ai.REORG_SCHEMA)
    check("claude-code: no tools, no MCP, no user settings, no saved session",
          a[a.index("--tools") + 1] == "" and "--strict-mcp-config" in a and a[a.index("--setting-sources") + 1] == "project"
          and "--no-session-persistence" in a, str(a))
    check("claude-code: our system prompt and model/effort", a[a.index("--system-prompt") + 1] == ai.SYSTEM
          and a[a.index("--model") + 1] == "opus" and a[a.index("--effort") + 1] == "medium")
    check("claude-code: prompt on stdin, temp folder, API key not passed", c["stdin"] == P.user
          and "starshelf-ai-" in c["cwd"] and c["api_key"] is False, str({k: c[k] for k in ("cwd", "api_key")}))
    check("claude-code: answer and usage", data == {"lists": [], "notes": "cc"} and usage == {
        "backend": "claude-code", "model": "claude-opus-5-5", "input_tokens": 1205, "output_tokens": 77}, str(usage))
    os.environ["FAKE_MODE"] = "error"
    check("claude-code: CLI error shown", raises(lambda: cc.complete(P), "Usage limit reached"))
    os.environ["FAKE_MODE"] = "garbage"
    check("claude-code: crash shows stderr", raises(lambda: cc.complete(P), "boom"))
    os.environ["FAKE_MODE"] = "loggedout"
    st = ai.ClaudeCodeSuggester().status()
    check("claude-code: logged out explains login", not st["available"] and "claude" in st["setup"])
    st = ai.CodexSuggester().status()
    check("codex: logged out explains login", not st["available"] and "codex login" in st["setup"])
    os.environ["FAKE_MODE"] = ""
    st = cx.status()
    check("codex: logged in via ChatGPT, with security warning", st["available"] and "ChatGPT" in st["note"]
          and "读取你电脑上的文件" in st["warning"], str(st))
    data, usage = cx.complete(P)
    c = log()[-1]
    a = c["args"]
    check("codex: read-only, ephemeral, no user config", a[0] == "exec" and a[a.index("--sandbox") + 1] == "read-only"
          and {"--ephemeral", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "--json"} <= set(a), str(a))
    check("codex: schema file and stdin prompt", json.loads(c["schema"]) == ai.REORG_SCHEMA and a[-1] == "-"
          and c["stdin"].startswith(ai.CodexSuggester.PREFACE) and P.user in c["stdin"])
    check("codex: temp folder, its own folder first on PATH, API key not passed",
          "starshelf-ai-" in c["cwd"] and c["path0"] == str(bindir) and c["api_key"] is False)
    check("codex: answer and usage from events", data == {"lists": [], "notes": "cx"}
          and usage["input_tokens"] == 900 and usage["output_tokens"] == 40)
    os.environ["FAKE_MODE"] = "error"
    check("codex: failure message shown", raises(lambda: cx.complete(P), "usage limit"))
    os.environ["STAR_LISTS_CLAUDE_BIN"] = str(bindir / "missing")
    check("missing CLI reported", not ai.ClaudeCodeSuggester().status()["available"]
          and raises(lambda: ai.ClaudeCodeSuggester().complete(P), "没有找到"))
    os.environ["CLAUDE_CODE_SESSION_ID"] = "parent"
    os.environ["CLAUDECODE"] = "1"
    env = ai.cli_env(str(bindir / "claude"), ai.ClaudeCodeSuggester.DROP_ENV)
    os.environ.pop("CLAUDE_CODE_SESSION_ID"); os.environ.pop("CLAUDECODE")
    check("parent Claude session variables are not passed on", "CLAUDE_CODE_SESSION_ID" not in env and "CLAUDECODE" not in env
          and "ANTHROPIC_API_KEY" not in env)
    check("only subscription backends: Claude Code, then Codex", [b.id for b in ai.default_backends()] == ["claude-code", "codex"])
finally:
    for k, v in saved_env.items():
        os.environ.pop(k, None)
        if v is not None:
            os.environ[k] = v

# --------------------------------------------------------------------------- 6. /api/suggest in the app
work = Path(tempfile.mkdtemp(prefix="sl_ai_"))
fake = FakeSuggester(delay=0.2)
other = FakeSuggester(id="other", label="Other")
app = ui.App(None, work, suggesters=[fake, other])  # type: ignore[arg-type]  # no GitHub calls in this section
app.baseline = BASE


def wait(app: ui.App) -> dict[str, Any]:
    for _ in range(100):
        j = app.suggest_status({})
        if j["status"] != "running":
            return j
        time.sleep(0.05)
    return j


j = app.suggest_status({})
check("idle status lists the backends with a default", j["status"] == "idle"
      and [b["id"] for b in j["ai"]["backends"]] == ["fake", "other"] and j["ai"]["default"] == "fake")
started = app.start_suggest({"mode": "reorganize", "lists": DRAFT})
check("start returns a running job", started["status"] == "running" and started["n_repos"] == 11 and started["id"] == 1
      and started["backend"] == "fake" and started["label"] == "Fake AI")
check("running status skips the availability check", "ai" not in app.suggest_status({}))
j = wait(app)
check("job finishes with a proposal", j["status"] == "done" and j["result"]["replace"] is True
      and j["result"]["usage"]["model"] == "fake-model", json.dumps(j, ensure_ascii=False)[:500])
first = app.start_suggest({"mode": "incremental", "lists": DRAFT})
second = app.start_suggest({"mode": "incremental", "lists": DRAFT})
j = wait(app)
check("a newer run replaces an older one", second["id"] == first["id"] + 1 and j["id"] == second["id"] and j["status"] == "done")
fake.error = ai.AIError("模拟失败")
app.start_suggest({"mode": "incremental", "lists": DRAFT})
j = wait(app)
check("failure is reported", j["status"] == "failed" and j["error"] == "模拟失败")
fake.error = None
picked = app.start_suggest({"mode": "incremental", "lists": DRAFT, "backend": "other"})
wait(app)
check("request picks the backend", picked["backend"] == "other" and len(other.calls) == 1)
check("unknown backend refused", raises(lambda: app.start_suggest({"mode": "incremental", "lists": DRAFT, "backend": "x"}), "没有这个"))
fake.available = False
j = app.suggest_status({})
check("default falls back to the first available backend", j["ai"]["default"] == "other")
fake.available = True
fake.available = False
try:
    app.start_suggest({"mode": "incremental", "lists": DRAFT, "backend": "fake"})
    ok = False
except ai.AIError as exc:
    ok = "还没有登录" in str(exc) and "claude" in str(exc)
check("unavailable AI refuses to start with setup help", ok)
fake.available = True
check("nothing was written to the data folder", not any(work.iterdir()))

# --------------------------------------------------------------------------- 7. AI assistant chat
def chat_req(message: str = "hi", **kw: Any) -> ai.ChatRequest:
    return ai.parse_chat({"lists": DRAFT, "message": message, **kw}, BASE)


c = chat_req("  把它们移到 Tools  ", unstar=["R7", "R11", "nope"],
             history=[{"role": "user", "text": "你好"}, {"role": "assistant", "text": "在"}, {"role": "system", "text": "x"},
                      {"role": "user", "text": "   "}] + [{"role": "user", "text": f"m{i}"} for i in range(20)])
check("chat: message trimmed, unstar only starred known repos", c.message == "把它们移到 Tools" and c.unstar == ["R7"], str(c.unstar))
check("chat: history keeps user/assistant turns, newest 12", len(c.history) == ai.CHAT_HISTORY_MAX
      and c.history[-1] == ("user", "m19") and all(r in ("user", "assistant") for r, _ in c.history))
check("chat: empty message refused", raises(lambda: chat_req("   "), "请先输入"))
cp = ai.build_chat_prompt(c, BASE.repos)
check("chat prompt: lists, repos with memberships, unstar mark", "L1 | AI | machine learning | 3" in cp.user
      and any(l.startswith("r") and "alice/repo7" in l and "in: none" in l and "MARKED TO UNSTAR" in l for l in cp.user.splitlines())
      and any("alice/repo11" in l and "in: L3" in l for l in cp.user.splitlines()), cp.user[:800])
check("chat prompt: conversation then newest message last", cp.user.rstrip().endswith("Newest message from the person:\n把它们移到 Tools")
      and "person: m19" in cp.user and cp.schema is ai.CHAT_SCHEMA and cp.system is ai.CHAT_SYSTEM)
cref = {v: k for k, v in cp.repo_refs.items()}
B = {"repos": [], "list": "", "to": "", "name": "", "description": ""}
res = ai.interpret_chat(c, cp, {"reply": "把 r1 从 L1 移到 N1，L9 不存在", "operations": [
    {**B, "op": "create_list", "list": "N1", "name": "New One", "description": "d"},
    {**B, "op": "move", "repos": [cref["R1"], "r999"], "list": "L1", "to": "N1"},
    {**B, "op": "add", "repos": [cref["R8"]], "list": "L2"},
    {**B, "op": "remove", "repos": [cref["R4"]], "list": "L2"},
    {**B, "op": "rename_list", "list": "L3", "name": "Archive"},
    {**B, "op": "delete_list", "list": "L3"},
    {**B, "op": "unstar", "repos": [cref["R9"]]},
    {**B, "op": "keep_star", "repos": [cref["R7"]]},
    {**B, "op": "add", "repos": [cref["R2"]], "list": "L9"},          # unknown list -> skipped
    {**B, "op": "move", "repos": [cref["R2"]], "list": "L1", "to": "L1"},  # same list -> skipped
    {**B, "op": "create_list", "list": "L1", "name": "clash"},        # reuses an L id -> skipped
    {**B, "op": "fly", "repos": []},                                   # unknown op -> skipped
]})
kinds = [o["op"] for o in res["operations"]]
check("chat ops: valid ones kept in order", kinds == ["create_list", "move", "add", "remove", "rename_list", "delete_list", "unstar", "keep_star"], str(kinds))
mv = res["operations"][1]
check("chat ops: refs mapped to draft keys, repo ids and new-list refs", mv["list"] == "UL_ai" and mv["to"] == "new:N1"
      and mv["repos"] == ["R1"] and res["operations"][0]["list"] == "new:N1" and res["operations"][6]["repos"] == ["R9"])
check("chat ops: invalid steps and ids reported", any("1 个无效的仓库编号" in w for w in res["warnings"])
      and any("4 步操作" in w for w in res["warnings"]), str(res["warnings"]))
check("chat reply: ids become names", res["reply"] == "把 alice/repo1 从 「AI」 移到 「New One」，L9 不存在", res["reply"])
check("chat: a question without operations is fine", ai.interpret_chat(c, cp, {"reply": "哪个列表？", "operations": []})["operations"] == [])
check("chat: nothing at all refused", raises(lambda: ai.interpret_chat(c, cp, {"reply": "", "operations": []}), "没有给出回答"))

j = app.chat_status({})
check("chat status idle with backends", j["status"] == "idle" and j["ai"]["default"] == "fake")
started = app.start_chat({"lists": DRAFT, "message": "请把 L2 的仓库移到 L1", "backend": "fake"})
check("chat start returns a running job", started["status"] == "running" and started["mode"] == "chat" and started["label"] == "Fake AI")
for _ in range(100):
    j = app.chat_status({})
    if j["status"] != "running":
        break
    time.sleep(0.05)
ops = (j.get("result") or {}).get("operations") or []
check("chat job finishes with operations", j["status"] == "done" and ops and ops[0]["op"] == "move"
      and ops[0]["list"] == "UL_tools" and ops[0]["to"] == "UL_ai", json.dumps(j, ensure_ascii=False)[:500])
check("chat start refuses an empty message", raises(lambda: app.start_chat({"lists": DRAFT, "message": ""}), "请先输入"))
check("chat and suggest jobs are separate", app.suggest_job is not app.chat_job)

# --------------------------------------------------------------------------- report
fails = [r for r in results if not r[1]]
for name, ok, detail in fails:
    print(f"FAIL  {name}\n      {detail[:2000]}")
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
sys.exit(1 if fails else 0)
