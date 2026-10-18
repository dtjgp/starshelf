"""Stand-in for the Claude Code / Codex backends: answers from the prompt text, without any network."""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import star_lists_ai as ai  # noqa: E402

LINE = re.compile(r"^(r\d+) \| ([^|]+) \|.*?(?:\| in: ([L\d,]+))?(?: \| archived)?$")


def repo_lines(prompt: ai.Prompt) -> list[tuple[str, str, list[str]]]:
    """[(ref, owner/name, [list refs it is in today])] in prompt order."""
    out = []
    for line in prompt.user.splitlines():
        m = LINE.match(line)
        if m:
            out.append((m[1], m[2].strip(), m[3].split(",") if m[3] else []))
    return out


def list_names(prompt: ai.Prompt) -> dict[str, str]:
    """{"L1": name, ...} in prompt order."""
    return dict(line.split(" | ", 2)[:2] for line in prompt.user.splitlines() if re.match(r"^L\d+ \| ", line))


def list_refs(prompt: ai.Prompt) -> list[str]:
    return list(list_names(prompt))


def keep_and_split(prompt: ai.Prompt) -> dict[str, Any]:
    """Reorganize: rename L1, drop the last list (its repos go to L1), add a new list with the
    first three repos, keep everything else where it is, and forget the last repo."""
    refs = list_refs(prompt)
    lines = repo_lines(prompt)
    last = refs[-1]
    groups: dict[str, list[str]] = {r: [] for r in refs[:-1]}
    for ref, _, inside in lines[3:-1]:
        home = [x for x in inside if x != last][:1] or [refs[0]]
        groups[home[0]].append(ref)
    names = list_names(prompt)
    lists = [{"ref": r, "name": ("Renamed by AI" if r == refs[0] else names[r]), "description": "", "repos": groups[r]}
             for r in refs[:-1]]
    lists.append({"ref": "", "name": "AI New", "description": "made by the fake", "repos": [l[0] for l in lines[:3]]})
    return {"lists": lists, "notes": "假的建议：改了一个名字，删了一个列表，新建了一个列表。"}


def first_list_plus_new(prompt: ai.Prompt) -> dict[str, Any]:
    """Incremental: first target -> L2, the others -> a new list N1."""
    lines = repo_lines(prompt)
    refs = list_refs(prompt)
    out = [{"repo": lines[0][0], "lists": [refs[1] if len(refs) > 1 else "N1"]}]
    out += [{"repo": ref, "lists": ["N1"]} for ref, _, _ in lines[1:]]
    return {"assignments": out, "new_lists": [{"ref": "N1", "name": "AI Bucket", "description": "new"}],
            "notes": "假的增量建议。"}


def chat_repos(prompt: ai.Prompt) -> list[tuple[str, str, list[str]]]:
    """Chat prompt repo lines: [(ref, owner/name, [list refs])]."""
    out = []
    for line in prompt.user.splitlines():
        if not re.match(r"^r\d+ \| ", line):
            continue
        parts = line.split(" | ")
        inside = next((x[4:] for x in parts if x.startswith("in: ")), "none")
        out.append((parts[0], parts[1], [] if inside == "none" else inside.split(",")))
    return out


def chat_answer(prompt: ai.Prompt) -> dict[str, Any]:
    """Reacts to keywords in the newest message, like a (very literal) assistant."""
    msg = prompt.user.rsplit("Newest message from the person:\n", 1)[1]
    names = list_names(prompt)
    refs = list(names)
    repos = chat_repos(prompt)
    blank = {"repos": [], "list": "", "to": "", "name": "", "description": ""}
    if "?" in msg or "？" in msg:
        return {"reply": "你是想移动哪个列表里的仓库？", "operations": []}
    if "移到" in msg:
        src, dst = refs[1], refs[0]
        moving = [r for r, _, inside in repos if src in inside and dst not in inside][:2]
        return {"reply": f"已把 2 个仓库从 {src} 移到 {dst}（{moving[0]}）。",
                "operations": [{**blank, "op": "move", "repos": moving, "list": src, "to": dst}]}
    if "新建" in msg:
        loose = [r for r, _, inside in repos if not inside][:2]
        return {"reply": "新建了 N1，放进 2 个还没归类的仓库。",
                "operations": [{**blank, "op": "create_list", "list": "N1", "name": "Chat Bucket", "description": "made in chat"},
                               {**blank, "op": "add", "repos": loose, "list": "N1"}]}
    if "取消" in msg:
        first = next(r for r, _, inside in repos if refs[0] in inside)
        return {"reply": "已标记取消 star。", "operations": [{**blank, "op": "unstar", "repos": [first]}]}
    if "改名" in msg:
        return {"reply": "改好了。", "operations": [{**blank, "op": "rename_list", "list": refs[0], "name": "Renamed via chat"}]}
    return {"reply": "好的，我在。", "operations": []}


class FakeSuggester:
    def __init__(self, answer: Optional[Callable[[ai.Prompt], Any]] = None, delay: float = 0.0, *,
                 id: str = "fake", label: str = "Fake AI", provider: str = "Nobody", warning: str = "") -> None:
        self.id, self.label, self.provider, self.warning = id, label, provider, warning
        self.model = "fake-model"
        self.answer = answer
        self.delay = delay
        self.available = True
        self.calls: list[ai.Prompt] = []
        self.error: Optional[Exception] = None

    def status(self) -> dict[str, Any]:
        base = {"id": self.id, "label": self.label, "provider": self.provider, "model": self.model,
                "note": f"{self.label} 的说明", "warning": self.warning}
        if self.available:
            return {**base, "available": True, "reason": "", "setup": []}
        return {**base, "available": False, "reason": f"{self.label} 还没有登录。", "setup": ["在终端运行：", "claude", "然后按提示登录。"]}

    def complete(self, prompt: ai.Prompt) -> tuple[dict[str, Any], dict[str, Any]]:
        self.calls.append(prompt)
        time.sleep(self.delay)
        if self.error:
            raise self.error
        answer = self.answer or (keep_and_split if prompt.schema is ai.REORG_SCHEMA
                                 else chat_answer if prompt.schema is ai.CHAT_SCHEMA else first_list_plus_new)
        return answer(prompt), {"backend": self.id, "model": self.model, "input_tokens": len(prompt.user) // 4, "output_tokens": 321}
