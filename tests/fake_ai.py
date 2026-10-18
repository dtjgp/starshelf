"""Stand-in for the Claude Code / Codex backends: answers from the prompt text, without any network."""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional, Union

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import star_lists_ai as ai  # noqa: E402

def list_names(prompt: ai.Prompt) -> dict[str, str]:
    """{"L1": name, ...} in prompt order."""
    return dict(line.split(" | ", 2)[:2] for line in prompt.user.splitlines() if re.match(r"^L\d+ \| ", line))


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
    refs = list(list_names(prompt))
    repos = chat_repos(prompt)
    blank = {"repos": [], "list": "", "to": "", "name": "", "description": ""}
    if "?" in msg or "？" in msg:
        return {"reply": "你是想移动哪个列表里的仓库？", "operations": []}
    if "移到" in msg:
        src, dst = refs[1], refs[0]
        moving = [r for r, _, inside in repos if src in inside and dst not in inside][:2]
        return {"reply": f"已把 **2 个仓库** 从 {src} 移到 {dst}：\n- {moving[0]} 更适合 {dst}\n- 另一个也一起移过去了",
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
                 id: str = "fake", label: str = "Fake AI", provider: str = "Nobody",
                 warning: Union[str, Callable[[], str]] = "") -> None:
        self.id, self.label, self.provider, self.warning = id, label, provider, warning
        self.model = "fake-model"
        self.answer = answer
        self.delay = delay
        self.available = True
        self.calls: list[ai.Prompt] = []
        self.error: Optional[Exception] = None

    def status(self) -> dict[str, Any]:
        tr = ai.sl.tr  # like the real backends: messages in the language of the request
        base = {"id": self.id, "label": self.label, "provider": self.provider, "model": self.model,
                "note": tr(f"{self.label} 的说明", f"About {self.label}"),
                "warning": self.warning() if callable(self.warning) else self.warning}
        if self.available:
            return {**base, "available": True, "reason": "", "setup": []}
        return {**base, "available": False, "reason": tr(f"{self.label} 还没有登录。", f"{self.label} is not signed in."),
                "setup": tr("在终端运行：\nclaude\n然后按提示登录。", "Run this in a terminal:\nclaude\nand sign in.").split("\n")}

    def complete(self, prompt: ai.Prompt) -> tuple[dict[str, Any], dict[str, Any]]:
        self.calls.append(prompt)
        time.sleep(self.delay)
        if self.error:
            raise self.error
        answer = self.answer or chat_answer
        return answer(prompt), {"backend": self.id, "model": self.model, "input_tokens": len(prompt.user) // 4, "output_tokens": 321}
