"""Tests for star_lists_ai.py and the /api/chat endpoint (no network, no API key needed)."""
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
from fake_ai import FakeSuggester  # noqa: E402

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


# --------------------------------------------------------------------------- 1. chat request, prompt and answers
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
md = ai.interpret_chat(c, cp, {"reply": "已移动  **2 个**：\n- r1 到 L1\n\n\n\n- 完成   了", "operations": []})["reply"]
check("chat reply keeps line breaks (Markdown) and maps ids", md == "已移动 **2 个**：\n- alice/repo1 到 「AI」\n\n- 完成 了", repr(md))
check("chat: a question without operations is fine", ai.interpret_chat(c, cp, {"reply": "哪个列表？", "operations": []})["operations"] == [])
check("chat: nothing at all refused", raises(lambda: ai.interpret_chat(c, cp, {"reply": "", "operations": []}), "没有给出回答"))

P = cp  # the CLI backend checks below send this chat prompt

# --------------------------------------------------------------------------- 2. CLI backends (fake claude / codex executables)
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
          and json.loads(a[a.index("--json-schema") + 1]) == ai.CHAT_SCHEMA)
    check("claude-code: no tools, no MCP, no user settings, no saved session",
          a[a.index("--tools") + 1] == "" and "--strict-mcp-config" in a and a[a.index("--setting-sources") + 1] == "project"
          and "--no-session-persistence" in a, str(a))
    check("claude-code: our system prompt and model/effort", a[a.index("--system-prompt") + 1] == ai.CHAT_SYSTEM
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
    check("codex: schema file and stdin prompt", json.loads(c["schema"]) == ai.CHAT_SCHEMA and a[-1] == "-"
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

# --------------------------------------------------------------------------- 3. /api/chat in the app
work = Path(tempfile.mkdtemp(prefix="sl_ai_"))
fake = FakeSuggester(delay=0.2)
other = FakeSuggester(id="other", label="Other")
app = ui.App(None, work, suggesters=[fake, other])  # type: ignore[arg-type]  # no GitHub calls in this section
app.baseline = BASE


def wait(app: ui.App) -> dict[str, Any]:
    for _ in range(100):
        j = app.chat_status({})
        if j["status"] != "running":
            return j
        time.sleep(0.05)
    return j


j = app.chat_status({})
check("idle status lists the backends with a default", j["status"] == "idle"
      and [b["id"] for b in j["ai"]["backends"]] == ["fake", "other"] and j["ai"]["default"] == "fake")
started = app.start_chat({"lists": DRAFT, "message": "hi"})
check("start returns a running job", started["status"] == "running" and started["mode"] == "chat" and started["n_repos"] == 10 and started["id"] == 1
      and started["backend"] == "fake" and started["label"] == "Fake AI")
check("running status skips the availability check", "ai" not in app.chat_status({}))
j = wait(app)
check("job finishes with a reply", j["status"] == "done" and j["result"]["reply"] == "好的，我在。" and j["result"]["operations"] == []
      and j["result"]["usage"]["model"] == "fake-model", json.dumps(j, ensure_ascii=False)[:500])
first = app.start_chat({"lists": DRAFT, "message": "hi"})
second = app.start_chat({"lists": DRAFT, "message": "hi"})
j = wait(app)
check("a newer run replaces an older one", second["id"] == first["id"] + 1 and j["id"] == second["id"] and j["status"] == "done")
fake.error = ai.AIError("模拟失败")
app.start_chat({"lists": DRAFT, "message": "hi"})
j = wait(app)
check("failure is reported", j["status"] == "failed" and j["error"] == "模拟失败")
fake.error = None
picked = app.start_chat({"lists": DRAFT, "message": "hi", "backend": "other"})
wait(app)
check("request picks the backend", picked["backend"] == "other" and len(other.calls) == 1)
check("unknown backend refused", raises(lambda: app.start_chat({"lists": DRAFT, "message": "hi", "backend": "x"}), "没有这个"))
fake.available = False
j = app.chat_status({})
check("default falls back to the first available backend", j["ai"]["default"] == "other")
fake.available = True
fake.available = False
try:
    app.start_chat({"lists": DRAFT, "message": "hi", "backend": "fake"})
    ok = False
except ai.AIError as exc:
    ok = "还没有登录" in str(exc) and "claude" in str(exc)
check("unavailable AI refuses to start with setup help", ok)
fake.available = True
check("nothing was written to the data folder", not any(work.iterdir()))

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

# --------------------------------------------------------------------------- report
fails = [r for r in results if not r[1]]
for name, ok, detail in fails:
    print(f"FAIL  {name}\n      {detail[:2000]}")
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
sys.exit(1 if fails else 0)
