"""Click-through test of star_lists_ui against a fake GitHub seeded with a made-up account."""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).parent))
import star_lists_ai as ai  # noqa: E402
import star_lists_ui as ui  # noqa: E402
from fake_ai import FakeSuggester  # noqa: E402
from fake_github import FakeGitHub  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

# Made-up account (321 repos, 9 lists); regenerate with tests/make_demo_snapshot.py.
SNAP = str(Path(__file__).parent / "fixtures" / "demo_snapshot.json")
OUT = Path(__file__).parent / ".shots"
OUT.mkdir(exist_ok=True)
results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"\n      {detail}"), flush=True)


g = FakeGitHub.from_snapshot(SNAP, page=100)
initial = g.view()
work = Path(tempfile.mkdtemp(prefix="sl_ui_")) / "stars_backup"
fake = FakeSuggester(id="claude-code", label="Claude Code", provider="Anthropic")
fake_codex = FakeSuggester(id="codex", label="Codex", provider="OpenAI", warning=ai.CODEX_WARNING)
app = ui.App(g, work, sleep=0, settle=0, suggesters=[fake, fake_codex])
server = ui.build_server(app, 18765)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{port}/#t={app.token}"


def raw(method: str, path: str, headers: dict[str, str]) -> int:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, headers=headers)
    r = c.getresponse(); r.read(); c.close()
    return r.status


# ---------------------------------------------------------------- security
check("API without session secret -> 401", raw("GET", "/api/state", {}) == 401)
check("foreign Host header -> 403 (DNS rebinding)", raw("GET", "/", {"Host": "evil.example:80"}) == 403)
check("foreign Origin -> 401 (CSRF)", raw("GET", "/api/state", {"X-Session": app.token, "Origin": "http://evil.example"}) == 401)
check("page itself is served", raw("GET", "/", {}) == 200)

starred = {r["id"]: r for r in json.load(open(SNAP))["repos"] if r["starred"]}
paper_hits = sorted(r["full_name"] for r in starred.values()
                    if "paper" in (r["full_name"] + " " + (r["description"] or "")).lower())
errors: list[str] = []

with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: m.type == "error" and errors.append(f"console: {m.text}"))
    page.goto(URL)
    expect(page.locator("#app")).to_be_visible(timeout=15000)
    rows = page.locator("#rows .row")
    check("all view shows 321 repos", rows.count() == 321, str(rows.count()))
    check("sidebar counts", page.locator("#nAll").inner_text() == "321" and page.locator("#nNone").inner_text() == "12")
    check("9 lists in sidebar", page.locator("#lists .litem").count() == 9)
    page.screenshot(path=str(OUT / "01-all.png"))

    page.click(".vbtn[data-view=none]")
    check("'not in any list' shows 12", rows.count() == 12, str(rows.count()))

    # -- create a private list
    page.click("#btnNewList")
    page.fill("#ldName", "📝 Research & Writing")
    page.fill("#ldDesc", "论文写作、作图、文献")
    page.check("input[name=vis][value=private]")
    page.screenshot(path=str(OUT / "02-new-list.png"))
    page.click("#ldSave")
    new_item = page.locator("#lists .litem", has_text="Research & Writing")
    check("new list appears with badge", new_item.count() == 1 and "新建" in new_item.inner_text())

    # -- duplicate name is rejected
    page.click("#btnNewList")
    page.fill("#ldName", "edgeai")
    page.click("#ldSave")
    check("duplicate name rejected", page.locator("#ldErr").is_visible() and "EdgeAI" in page.locator("#ldErr").inner_text())
    page.click("#ldCancel")

    # -- bulk add search results
    page.click(".vbtn[data-view=all]")
    page.fill("#q", "paper")
    check("search filters rows", rows.count() == len(paper_hits), f"{rows.count()} vs {len(paper_hits)}")
    page.check("#selAll")
    check("select all visible", page.locator("#countLabel").inner_text() == f"已选 {len(paper_hits)} 个")
    page.click("#bAdd")
    page.locator("#popover .opt", has_text="Research & Writing").click()
    check("bulk add updates list count", new_item.locator(".n").text_content() == str(len(paper_hits)))
    page.screenshot(path=str(OUT / "03-bulk-added.png"))
    page.fill("#q", "")
    page.click("#bDeselect")

    # -- rename DailyHelper
    page.locator("#lists .litem", has_text="DailyHelper").hover()
    page.locator("#lists .litem", has_text="DailyHelper").locator("[data-act=edit]").click()
    page.fill("#ldName", "💻 Mac & Terminal")
    page.click("#ldSave")
    renamed = page.locator("#lists .litem", has_text="Mac & Terminal")
    check("rename shows badge", renamed.count() == 1 and "改名" in renamed.inner_text())

    # -- delete FunProjects (two-step confirm)
    fun = page.locator("#lists .litem", has_text="FunProjects")
    fun.hover(); fun.locator("[data-act=edit]").click()
    page.click("#ldDelete")
    check("delete asks for confirmation", "确认删除" in page.locator("#ldDelete").inner_text())
    page.click("#ldDelete")
    check("deleted list marked", "将删除" in fun.inner_text())

    # -- chip remove + toggle picker + undo/redo
    page.locator("#lists .litem", has_text="EdgeAI").locator("[data-act=view]").click()
    qlib = page.locator("#rows .row", has_text="microsoft/qlib")
    qlib.locator(".chip [data-act=chip-remove]").first.click()
    check("removed row stays visible as 'gone'", "gone" in (qlib.get_attribute("class") or ""))
    qlib.locator("[data-act=pick-lists]").click()
    page.locator("#popover .opt", has_text="Finance").click()
    page.keyboard.press("Escape")
    check("qlib now has Finance chip", qlib.locator(".chip.added", has_text="Finance").count() == 1)
    page.click("#btnUndo")
    check("undo removes Finance", qlib.locator(".chip", has_text="Finance").count() == 0)
    page.click("#btnRedo")
    check("redo re-adds Finance", qlib.locator(".chip.added", has_text="Finance").count() == 1)

    # -- move papers-we-love from EdgeAI to the new list
    pwl = page.locator("#rows .row", has_text="papers-we-love/papers-we-love")
    pwl.locator("input[type=checkbox]").check()
    page.click("#bMove")
    page.locator("#popover .opt", has_text="Research & Writing").click()
    check("moved row is gone from EdgeAI", "gone" in (pwl.get_attribute("class") or ""))
    page.screenshot(path=str(OUT / "04-edgeai-edits.png"))

    # -- shift-click range selection
    page.click(".vbtn[data-view=all]")
    page.locator("#rows .row").nth(2).locator("input[type=checkbox]").click()
    page.locator("#rows .row").nth(6).locator("input[type=checkbox]").click(modifiers=["Shift"])
    check("shift-click selects a range of 5", page.locator("#countLabel").inner_text() == "已选 5 个", page.locator("#countLabel").inner_text())
    page.keyboard.press("Escape")

    # -- drag & drop codecarbon onto SmartGrid
    page.fill("#q", "codecarbon")
    page.locator("#rows .row").first.drag_to(page.locator("#lists .litem", has_text="SmartGrid"))
    check("drag & drop adds to list", page.locator("#rows .row").first.locator(".chip.added", has_text="SmartGrid").count() == 1)
    page.fill("#q", "")

    count_before = page.locator("#changeCount").inner_text()
    # -- reload: draft must survive (no waiting for the save indicator on purpose)
    page.reload()
    expect(page.locator("#app")).to_be_visible(timeout=15000)
    check("draft survives reload", page.locator("#changeCount").inner_text() == count_before and
          page.locator("#lists .litem", has_text="Research & Writing").count() == 1, count_before)
    check("refresh disabled with pending changes", page.locator("#btnRefresh").is_disabled())

    edge_color = page.locator("#lists .litem", has_text="EdgeAI").get_attribute("style")
    # -- review + apply
    page.click("#btnReview")
    expect(page.locator("#rvBody h2")).to_have_text("确认要写入 GitHub 的修改", timeout=15000)
    lead = page.locator("#rvBody .lead").inner_text()
    check("plan summary", "新建 1 个列表" in lead and "修改 1 个列表" in lead and "删除 1 个列表" in lead, lead)
    page.screenshot(path=str(OUT / "05-review.png"), full_page=False)
    check("GitHub untouched before apply", g.mutations == 0)
    page.locator("#rvActions button.primary").click()
    expect(page.locator("#rvBody h2")).to_have_text("已应用到 GitHub", timeout=60000)
    page.screenshot(path=str(OUT / "06-done.png"))
    page.locator("#rvActions button.primary").click()

    v = g.view()
    L = v["lists"]
    research = sorted(set(paper_hits) | {"papers-we-love/papers-we-love"})
    check("FunProjects deleted", "FunProjects" not in L)
    check("DailyHelper renamed, members kept", L.get("💻 Mac & Terminal") == initial["lists"]["DailyHelper"])
    check("new private list with expected repos", L.get("📝 Research & Writing") == research
          and v["meta"]["📝 Research & Writing"] == ("论文写作、作图、文献", True), str(L.get("📝 Research & Writing")))
    check("EdgeAI lost qlib + papers-we-love", sorted(set(initial["lists"]["EdgeAI"]) - {"microsoft/qlib", "papers-we-love/papers-we-love"}) == L["EdgeAI"])
    check("Finance gained qlib", L["Finance"] == sorted(initial["lists"]["Finance"] + ["microsoft/qlib"]))
    check("SmartGrid gained codecarbon", L["SmartGrid"] == sorted(initial["lists"]["SmartGrid"] + ["mlco2/codecarbon"]))
    untouched = [n for n in ("⭐️⭐️LearnSomething", "ClaudeCode&Codex", "LLM related", "GreenAI")]
    check("other lists unchanged", all(L[n] == initial["lists"][n] for n in untouched))
    check("no star lost", v["starred"] == initial["starred"])
    check("list colours stable after deleting another list", page.locator("#lists .litem", has_text="EdgeAI").get_attribute("style") == edge_color)
    check("UI reset after apply", page.locator("#changeCount").inner_text() == "0" and not page.locator("#btnRefresh").is_disabled())
    check("draft file removed", not (work / "ui_draft.json").exists())
    page.locator("#lists .litem", has_text="Research & Writing").locator("[data-act=view]").click()
    check("new list shows its repos after apply", rows.count() == len(research), str(rows.count()))

    # -- drift: a list created on github.com meanwhile is left alone
    g.lists["UL_drift"] = {"name": "Made on github.com", "description": "", "isPrivate": False, "items": [g.rid("ollama/ollama")]}
    page.locator("#rows .row").first.locator("[data-act=pick-lists]").click()
    page.locator("#popover .opt", has_text="Finance").click()
    page.keyboard.press("Escape")
    page.click("#btnReview")
    expect(page.locator("#rvBody h2")).to_have_text("确认要写入 GitHub 的修改", timeout=15000)
    check("drift warning shown", "Made on github.com" in page.locator("#rvBody").inner_text())
    page.locator("#rvActions button.primary").click()
    expect(page.locator("#rvBody h2")).to_have_text("已应用到 GitHub", timeout=60000)
    page.locator("#rvActions button.primary").click()
    check("drift list untouched", g.view()["lists"]["Made on github.com"] == ["ollama/ollama"])
    del g.lists["UL_drift"]

    # -- restore the first backup (state before the first apply)
    page.click("#btnRefresh")
    expect(page.locator("#toast")).to_contain_text("重新读取", timeout=10000)
    page.click("#btnBackups")
    expect(page.locator("#bkList .snap").first).to_be_visible(timeout=10000)
    page.screenshot(path=str(OUT / "07-backups.png"))
    snaps = page.locator("#bkList .snap")
    check("two pre-apply backups listed", snaps.count() == 2, str(snaps.count()))
    snaps.nth(1).locator("button").click()
    expect(page.locator("#rvBody h2")).to_contain_text("恢复到", timeout=15000)
    page.locator("#rvActions button.primary").click()
    expect(page.locator("#rvBody h2")).to_have_text("已恢复", timeout=60000)
    page.locator("#rvActions button.primary").click()
    check("restore brings back the original lists exactly", g.view() == initial,
          json.dumps({k: v for k, v in g.view()["lists"].items() if initial["lists"].get(k) != v}, ensure_ascii=False)[:600])
    check("UI shows 9 lists again", page.locator("#lists .litem").count() == 9)

    # -- failure + resume
    page.locator("#lists .litem", has_text="GreenAI").locator("[data-act=view]").click()
    page.check("#selAll")
    page.click("#bAdd")
    page.locator("#popover .opt", has_text="SmartGrid").click()
    page.click(".vbtn[data-view=none]")
    page.check("#selAll")
    page.click("#bAdd")
    page.locator("#popover .opt", has_text="EdgeAI").click()
    g.fail_at = g.mutations + 3
    page.click("#btnReview")
    expect(page.locator("#rvBody h2")).to_have_text("确认要写入 GitHub 的修改", timeout=15000)
    page.locator("#rvActions button.primary").click()
    expect(page.locator("#rvBody h2")).to_have_text("没有全部完成", timeout=60000)
    fail_text = page.locator("#rvBody").inner_text()
    check("failure explains how to continue (in Chinese)", "从当前进度继续" in fail_text and "第 3/" in fail_text and "run `apply`" not in fail_text, fail_text)
    page.screenshot(path=str(OUT / "08-failed.png"))
    g.fail_at = None
    page.locator("#rvActions button.primary").click()  # 查看剩余修改
    expect(page.locator("#rvBody h2")).to_have_text("确认要写入 GitHub 的修改", timeout=15000)
    page.locator("#rvActions button.primary").click()
    expect(page.locator("#rvBody h2")).to_have_text("已应用到 GitHub", timeout=60000)
    page.locator("#rvActions button.primary").click()
    L = g.view()["lists"]
    check("resume completes", set(initial["lists"]["GreenAI"]) <= set(L["SmartGrid"]) and len(L["EdgeAI"]) == len(initial["lists"]["EdgeAI"]) + 12,
          f'{len(L["EdgeAI"])}')
    check("no stars lost overall", g.view()["starred"] == initial["starred"])

    # -- AI backends in the assistant panel: setup help, backend picker, Codex warning
    check("the 「AI 建议」 button and dialog are gone", page.locator("#btnAI").count() == 0
          and page.locator("#aiDialog").count() == 0)
    fake.available = fake_codex.available = False
    page.click("#btnChat")
    expect(page.locator("#chatNote .warn")).to_contain_text("还没有登录", timeout=10000)
    check("AI unavailable: setup shown, send disabled", page.locator("#chatSend").is_disabled()
          and "claude" in page.locator("#chatNote .setup").inner_text())
    fake.available = fake_codex.available = True
    page.reload()  # the page caches the backend status until it is reloaded
    expect(page.locator("#app")).to_be_visible(timeout=15000)
    page.click("#btnChat")
    expect(page.locator("#chatBackend option").first).to_have_text("Claude Code", timeout=10000)
    check("backend picker lists both, Claude Code first", page.locator("#chatBackend option").all_inner_texts() == ["Claude Code", "Codex"]
          and page.locator("#chatBackend").input_value() == "claude-code" and page.locator("#chatNote .warn").count() == 0)
    page.select_option("#chatBackend", "codex")
    check("Codex shows the security warning", "读取你电脑上的文件" in page.locator("#chatNote").inner_text())
    page.screenshot(path=str(OUT / "12-ai-codex-warning.png"))
    page.select_option("#chatBackend", "claude-code")
    check("switching back hides the warning", page.locator("#chatNote .warn").count() == 0)
    page.click("#chatClose")

    # -- unstar: draft mark -> preview checkbox -> applied last -> restore stars again
    names = {r.id: r.full_name for r in app.baseline.repos.values()}
    starred_before = g.view()["starred"]
    fin = page.locator("#lists .litem", has_text="Finance")
    fin.locator("[data-act=view]").click()
    fin_n = int(fin.locator(".n").text_content())
    ids = [rows.nth(i).get_attribute("data-id") for i in range(2)]
    for i in range(2):
        rows.nth(i).locator("input[type=checkbox]").check()
    check("unstar button shown for starred selection", page.locator("#bUnstar").is_visible() and not page.locator("#bKeep").is_visible())
    page.click("#bUnstar")
    check("marked repos leave their lists and get a view", page.locator("#vUnstar").is_visible()
          and page.locator("#nUnstar").inner_text() == "2" and fin.locator(".n").text_content() == str(fin_n - 2))
    page.locator("#toast").wait_for(state="visible")
    page.click("#toastUndo")
    check("undo removes the unstar mark", page.locator("#vUnstar").is_hidden() and fin.locator(".n").text_content() == str(fin_n))
    page.click("#btnRedo")
    page.click("#vUnstar")
    check("unstar view lists the marked repos", rows.count() == 2 and rows.first.locator(".tag.del").inner_text() == "将取消 star")
    page.screenshot(path=str(OUT / "13-unstar-view.png"))
    rows.first.locator("[data-act=keep-star]").click()
    check("keep star puts the repo back in its lists", page.locator("#nUnstar").inner_text() == "1"
          and fin.locator(".n").text_content() == str(fin_n - 1))
    kept = [i for i in ids if page.locator(f'#rows .row[data-id="{i}"]').count() == 0]
    gone_id = next(i for i in ids if i not in kept)
    page.reload()
    expect(page.locator("#app")).to_be_visible(timeout=15000)
    check("unstar mark survives reload", page.locator("#nUnstar").inner_text() == "1")
    edge = page.locator("#lists .litem", has_text="EdgeAI")
    edge.locator("[data-act=view]").click()
    second_id = rows.first.get_attribute("data-id")
    rows.first.locator("[data-act=pick-lists]").click()
    page.locator("#popover .opt.danger").click()
    check("unstar from the row menu", page.locator("#nUnstar").inner_text() == "2")
    marked = sorted(names[i] for i in (gone_id, second_id))
    muts = g.mutations
    page.click("#btnReview")
    expect(page.locator("#rvBody h2")).to_have_text("确认要写入 GitHub 的修改", timeout=15000)
    go = page.locator("#rvActions button.primary")
    check("preview lists unstars separately", "取消 star（2" in page.locator("#rvUnstarHead").inner_text()
          and sorted(page.locator("#rvUnstar .prepo").all_inner_texts()) == marked
          and "取消 2 个仓库的 star" in page.locator("#rvBody .lead").inner_text())
    check("apply disabled until the unstar is confirmed", go.is_disabled())
    page.screenshot(path=str(OUT / "14-unstar-preview.png"))
    page.check("#rvUnstarOk")
    check("confirming enables apply", go.is_enabled())
    go.click()
    expect(page.locator("#rvBody h2")).to_have_text("已应用到 GitHub", timeout=60000)
    page.locator("#rvActions button.primary").click()
    v = g.view()
    check("stars removed on GitHub", sorted(set(starred_before) - set(v["starred"])) == marked, str(sorted(set(starred_before) - set(v["starred"]))))
    check("unstarred repos are in no list", all(n not in l for n in marked for l in v["lists"].values()))
    check("apply wrote to GitHub and cleared the unstar view", g.mutations > muts and page.locator("#vUnstar").is_hidden())

    # the server refuses an unstar that was not confirmed, even if the page is bypassed
    lists_now = [{"id": l["id"], "name": l["name"], "description": l["description"], "private": l["private"], "items": l["items"]}
                 for l in ui.state_payload(app.baseline)["lists"]]
    victim = next(r.id for r in app.baseline.repos.values() if r.starred)
    for l in lists_now:
        l["items"] = [i for i in l["items"] if i != victim]
    muts = g.mutations
    app.apply({"lists": lists_now, "unstar": [victim]})
    for _ in range(100):
        if app.job.status != "running":
            break
        time.sleep(0.05)
    check("server refuses unconfirmed unstar", app.job.status == "failed" and "勾选确认" in (app.job.error or "")
          and g.mutations == muts, str(app.job.error))
    page.reload()
    expect(page.locator("#app")).to_be_visible(timeout=15000)

    page.click("#btnBackups")
    expect(page.locator("#bkList .snap").first).to_be_visible(timeout=10000)
    page.locator("#bkList .snap").first.locator("button").click()
    expect(page.locator("#rvBody h2")).to_contain_text("恢复到", timeout=15000)
    check("restore preview re-stars them first", "重新 star（2" in page.locator("#rvBody").inner_text()
          and page.locator("#rvActions button.primary").is_enabled())
    page.locator("#rvActions button.primary").click()
    expect(page.locator("#rvBody h2")).to_have_text("已恢复", timeout=60000)
    page.locator("#rvActions button.primary").click()
    check("restore brings the stars back", g.view()["starred"] == starred_before)
    # -- AI assistant drawer (fake AI): each answer edits the draft as one undoable step
    muts = g.mutations
    def count(name: str) -> int:  # text_content: the count is hidden (not removed) while the mouse is over the list
        return int(page.locator("#lists .litem", has_text=name).first.locator(".n").text_content())
    l1, l2 = page.locator("#lists .litem .lname").all_inner_texts()[:2]
    n1, n2 = count(l1), count(l2)
    page.click("#btnChat")
    expect(page.locator("#chat")).to_be_visible()
    expect(page.locator("#chatBackend option").first).to_have_text("Claude Code", timeout=10000)
    check("drawer opens beside the list with examples", page.locator("#chatLog .chat-empty li").count() == 4
          and page.locator("#main").is_visible())
    ai_msgs = page.locator("#chatLog .msg.ai:not(.pending)")
    page.fill("#chatInput", "请把第二个列表的两个仓库移到第一个")
    page.keyboard.press("Enter")
    expect(ai_msgs).to_have_count(1, timeout=15000)
    reply = ai_msgs.first.inner_text()
    check("reply names lists instead of ids", f"「{l1}」" in reply and " L1" not in reply and "r1" not in reply, reply)
    check("move applied to the draft", count(l1) == n1 + 2 and count(l2) == n2 - 2
          and int(page.locator("#changeCount").inner_text()) > 0, f"{count(l1)} {count(l2)}")
    first = ai_msgs.first
    check("reply rendered as light Markdown", first.locator(".md strong").inner_text() == "2 个仓库"
          and first.locator(".md li").count() == 2, first.inner_html()[:400])
    card = first.locator(".changes")
    head = card.locator(".changes-head").inner_text()
    check("changes shown as cards, one row per list", "改动了 2 个列表" in head and "加入 2" in head and "移出 2" in head
          and card.locator(".change").count() == 2, head)
    row1 = card.locator(".change", has_text=l1)
    check("each row has the list's tags and the repos", row1.locator(".tagc.add").inner_text() == "+2"
          and row1.locator("li.add").count() == 2 and card.locator(".change", has_text=l2).locator(".tagc.del").inner_text() == "−2")
    check("draft-only note under the cards", "还没有写入 GitHub" in first.inner_text())
    page.screenshot(path=str(OUT / "15-ai-assistant.png"))
    row1.locator(".cname").click()
    check("clicking a list name opens that list", l1 in page.locator("#viewTitle").inner_text())
    ai_msgs.first.locator(".undo-turn").click()
    check("undo this step restores the draft", count(l1) == n1 and count(l2) == n2 and "已撤销这一步" in ai_msgs.first.inner_text())
    page.fill("#chatInput", "新建一个列表放没归类的")
    page.click("#chatSend")
    expect(ai_msgs).to_have_count(2, timeout=15000)
    check("create_list adds a new draft list", page.locator("#lists .litem", has_text="Chat Bucket").count() == 1
          and "新建" in page.locator("#lists .litem", has_text="Chat Bucket").inner_text())
    check("history is sent back to the AI", "Conversation so far:" in fake.calls[-1].user
          and "（已改草稿：" in fake.calls[-1].user and "随后被撤销" in fake.calls[-1].user)
    page.fill("#chatInput", "取消第一个仓库的 star")
    page.keyboard.press("Enter")
    expect(ai_msgs).to_have_count(3, timeout=15000)
    check("unstar via chat marks the repo", page.locator("#nUnstar").inner_text() == "1")
    page.click("#vUnstar")
    check("unstar view says it is not on GitHub yet", "还没有写入 GitHub" in page.locator("#unstarPending").inner_text())
    page.fill("#chatInput", "怎么移？")
    page.keyboard.press("Enter")
    expect(ai_msgs).to_have_count(4, timeout=15000)
    check("a question changes nothing", ai_msgs.nth(3).locator(".changes").count() == 0)
    chat_w = lambda: round(page.locator("#chat").bounding_box()["width"])  # noqa: E731
    w0 = chat_w()
    edge = page.locator("#chatResize").bounding_box()
    page.mouse.move(edge["x"] + edge["width"] / 2, edge["y"] + 300)
    page.mouse.down()
    page.mouse.move(edge["x"] - 200, edge["y"] + 300, steps=6)
    page.mouse.up()
    w1 = chat_w()
    check("drag the left edge to widen the panel", w0 == 400 and 195 <= w1 - w0 <= 205, f"{w0} -> {w1}")
    page.locator("#chatResize").focus()
    page.keyboard.press("ArrowRight")
    w2 = chat_w()
    check("arrow key narrows it by 24 px", w1 - w2 == 24, f"{w1} -> {w2}")
    page.screenshot(path=str(OUT / "16-ai-assistant-wide.png"))
    page.reload()
    expect(page.locator("#app")).to_be_visible(timeout=15000)
    check("drawer closed after reload", page.locator("#chat").is_hidden())
    page.click("#btnChat")
    check("conversation kept after reload", page.locator("#chatLog .msg").count() == 8
          and page.locator("#chatLog .changes").count() == 3)
    check("panel width remembered after reload", chat_w() == w2, str(chat_w()))
    page.locator("#chatResize").dblclick()
    check("double-click restores the default width", chat_w() == 400, str(chat_w()))
    page.click("#chatClear")
    check("clear empties the conversation", page.locator("#chatLog .msg").count() == 0)
    page.click("#chatClose")
    check("close hides the drawer", page.locator("#chat").is_hidden())
    page.click("#btnDiscard")
    check("AI assistant never writes to GitHub", g.mutations == muts)
    check("no JS errors", not errors, "\n".join(errors[:10]))

    # -- screenshots: dark + narrow
    dark = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    dp = dark.new_page(); dp.goto(URL); expect(dp.locator("#app")).to_be_visible(timeout=15000)
    dp.locator("#lists .litem", has_text="EdgeAI").locator("[data-act=view]").click()
    dp.screenshot(path=str(OUT / "09-dark.png"))
    narrow = browser.new_context(viewport={"width": 390, "height": 844})
    np_ = narrow.new_page(); np_.goto(URL); expect(np_.locator("#app")).to_be_visible(timeout=15000)
    np_.screenshot(path=str(OUT / "10-narrow.png"))
    sw = np_.evaluate("document.documentElement.scrollWidth")
    check("no horizontal scroll at 390px", sw <= 390, str(sw))
    browser.close()

server.shutdown()
fails = [r for r in results if not r[1]]
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
sys.exit(1 if fails else 0)
