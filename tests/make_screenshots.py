"""Draw the README screenshots from the made-up demo account, in Chinese and in English.

    python3 tests/make_screenshots.py      # writes docs/screenshots/*.png (中文) and docs/screenshots/en/*.png

Uses the fake GitHub and a fake AI (no network, no token); nothing is applied.
Needs: pip install playwright && python3 -m playwright install chromium
"""
from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(Path(__file__).parent)]
import star_lists_ai as ai  # noqa: E402
import star_lists_ui as ui  # noqa: E402
from fake_ai import FakeSuggester, chat_repos, list_names  # noqa: E402
from fake_github import FakeGitHub  # noqa: E402
from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402

SNAP = Path(__file__).parent / "fixtures" / "demo_snapshot.json"
OUT = {"zh": ROOT / "docs" / "screenshots", "en": ROOT / "docs" / "screenshots" / "en"}
TEXT = {
    "zh": {"locale": "zh-CN", "ask": "请把第二个列表里两个学习类的仓库移到第一个",
           "reply": "已把 **2 个仓库** 从 {src} 移到 {dst}：\n- {repo} 更适合 {dst}\n- 另一个也是入门教程，一起移过去了",
           "mac": "💻 Mac & Terminal", "research": "📝 Research & Writing"},
    "en": {"locale": "en-US", "ask": "Move the two tutorial repos in the second list to the first one",
           "reply": "Moved **2 repositories** from {src} to {dst}:\n- {repo} fits {dst} better\n- the other one is a tutorial too, so it moved with it",
           "mac": "💻 Mac & Terminal", "research": "📝 Research & Writing"},
}


def answer(prompt: ai.Prompt) -> dict[str, Any]:
    """Move two repos from the second list to the first, replying in the page's language."""
    refs = list(list_names(prompt))
    src, dst = refs[1], refs[0]
    moving = [r for r, _, inside in chat_repos(prompt) if src in inside and dst not in inside][:2]
    text = TEXT["en" if "in English" in prompt.system else "zh"]["reply"]
    blank = {"repos": [], "list": "", "to": "", "name": "", "description": ""}
    return {"reply": text.format(src=src, dst=dst, repo=moving[0]),
            "operations": [{**blank, "op": "move", "repos": moving, "list": src, "to": dst}]}


def discard(page: Page) -> None:
    page.keyboard.press("Escape")
    if page.locator("#btnDiscard").is_enabled():
        page.click("#btnDiscard")
    page.locator("#toast").evaluate("el => el.hidden = true")


def shoot(page: Page, lang: str, url: str) -> None:
    out, tx = OUT[lang], TEXT[lang]
    out.mkdir(parents=True, exist_ok=True)
    page.goto(url)
    expect(page.locator("#app")).to_be_visible(timeout=15000)
    page.screenshot(path=str(out / "all-stars.png"))

    # preview: a new private list from a search, a rename and a deletion
    page.fill("#q", "data")
    page.check("#selAll")
    page.click("#btnNewList")
    page.fill("#ldName", tx["research"])
    page.check("input[name=vis][value=private]")
    page.click("#ldSave")
    page.fill("#q", "")
    page.locator("#q").dispatch_event("input")
    item = page.locator("#lists .litem", has_text="DailyHelper")
    item.hover(); item.locator("[data-act=edit]").click()
    page.fill("#ldName", tx["mac"]); page.click("#ldSave")
    item = page.locator("#lists .litem", has_text="FunProjects")
    item.hover(); item.locator("[data-act=edit]").click()
    page.click("#ldDelete"); page.click("#ldDelete")
    page.locator("#toast").evaluate("el => el.hidden = true")
    page.click("#btnReview")
    expect(page.locator("#rvActions button.primary")).to_be_visible(timeout=15000)
    page.mouse.move(0, 0)
    page.screenshot(path=str(out / "preview.png"))
    discard(page)

    # unstar: two repos of EdgeAI marked, the preview asks for confirmation
    page.locator("#lists .litem", has_text="EdgeAI").locator("[data-act=view]").click()
    rows = page.locator("#rows .row")
    for i in range(2):
        rows.nth(i).locator("input[type=checkbox]").check()
    page.click("#bUnstar")
    page.locator("#toast").evaluate("el => el.hidden = true")
    page.click("#btnReview")
    expect(page.locator("#rvUnstarOk")).to_be_visible(timeout=15000)
    page.mouse.move(0, 0)
    page.screenshot(path=str(out / "unstar-confirm.png"))
    discard(page)

    # AI assistant: one answer, shown with its change cards
    page.click(".vbtn[data-view=all]")
    page.click("#btnChat")
    expect(page.locator("#chatSend")).to_be_enabled(timeout=10000)
    page.fill("#chatInput", tx["ask"])
    page.click("#chatSend")
    expect(page.locator("#chatLog .changes")).to_be_visible(timeout=15000)
    page.mouse.move(0, 0)
    page.screenshot(path=str(out / "ai-assistant.png"))
    discard(page)


def main() -> int:
    g = FakeGitHub.from_snapshot(str(SNAP), page=100)
    app = ui.App(g, Path(tempfile.mkdtemp(prefix="sl_shots_")) / "data", sleep=0, settle=0,
                 suggesters=[FakeSuggester(answer, id="claude-code", label="Claude Code", provider="Anthropic")])
    server = ui.build_server(app, 18800)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/#t={app.token}"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for lang in ("zh", "en"):
            ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale=TEXT[lang]["locale"])
            shoot(ctx.new_page(), lang, url)
            ctx.close()
        browser.close()
    server.shutdown()
    assert g.mutations == 0, "screenshots must not write to (fake) GitHub"
    for lang, folder in OUT.items():
        print(lang, sorted(f.name for f in folder.glob("*.png")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
