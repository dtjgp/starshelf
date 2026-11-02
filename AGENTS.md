# Starshelf agent instructions

## Product

Starshelf reorganises GitHub **Star Lists** in bulk (create, rename and delete lists; add, move and
remove starred repos) through the GraphQL API, because GitHub's web UI edits one item at a time.
It has a CLI (`star_lists.py`), a local web UI (`star_lists_ui.py` + `star_lists_ui.html`) and a
native macOS app (`app/`, a WKWebView shell that starts the Python backend). An optional AI
assistant (「AI 助手」) edits the draft through the local Claude Code or Codex CLI.

## Read first

1. This file, then the nearest test and source files.
2. `local-notes/STATUS.md`, if present, for the current account state, PR history and open work.
   It is a local, gitignored file: re-read live state before you rely on its numbers.

## One-way doors

Ask the owner before each of these. Tests never do them.

- **Writes to the real GitHub account:** `apply`, `restore`, unstar, and list deletion. Each real
  write needs its own approval. Tests use the in-memory fake GitHub (`tests/fake_github.py`).
- **Real AI turns:** they use the owner's Claude Code or Codex subscription quota. Use demo data
  (`tests/fixtures/demo_snapshot.json`) and ask first.
- **Personal data:** never delete or rewrite `stars_backup/` (snapshots, journals, draft, baseline),
  and never commit it.
- **Publication:** push, PR merge and release. Merging is always the owner's call; never enable
  auto-merge.

## Architecture invariants (do not break)

- **GraphQL operations** (checked against GitHub's public schema): queries
  `viewer.starredRepositories`, `viewer.lists`, `node(id){... on UserList{items}}`; mutations
  `createUserList`, `updateUserList`, `deleteUserList`, `updateUserListsForItem`, `addStar`,
  `removeStar`. `UserListItems` is a union of `Repository` only.
- **`updateUserListsForItem` sets the COMPLETE set of lists for a repo** (it does not append). The
  planner always sends the full target set; otherwise memberships in untouched lists are wiped.
- **Never unstar, except what the user marked.** Only repos under `unstar:` (layout) or marked
  「取消 star」 (page) lose their star. They leave every list first, and `removeStar` runs as the LAST
  step. Verification re-stars any other lost star but skips intended unstars. `restore`
  (recreate-missing) re-stars repos as the FIRST step. CLI needs a second prompt (type `unstar`) or
  `--allow-unstar` with `--yes`; the page needs the checkbox, and the server refuses
  `confirm_unstar != true`. An unstar mark and list membership are mutually exclusive. Marked repos
  are excluded from AI input.
- **Declarative diff:** desired layout vs fresh live state → `Plan` (temporary renames for name
  swaps, updates, creates, membership changes, deletes). Order: renames → creates → memberships →
  deletes; `--delete-first` when the list cap is hit.
- **Scope = the snapshot the edit was based on.** Lists or repos that appeared on GitHub after the
  export are left alone (with a warning) unless `--strict`. Restore uses strict + recreate-missing.
- **Idempotent and resumable:** apply recomputes from live state each run; a failed step raises
  `ApplyAborted` (`step/total/op/cause/list_limit_hint`), and a rerun continues.
- **Backups:** every apply and restore saves the pre-change live state to `stars_backup/snapshots/`.
- **UI server security:** binds 127.0.0.1; checks Host (DNS rebinding) and Origin (CSRF); every
  `/api/*` call needs header `X-Session` = the per-run secret from the URL hash `#t=`; strict CSP;
  untrusted text is rendered with `textContent` only. The GitHub token never reaches the browser.
- **Draft model:** the page edits a draft relative to `ui_baseline.json` and autosaves it to
  `ui_draft.json` (also on `pagehide`). The server fingerprints the plan; apply refuses if GitHub
  changed after the preview.
- **Single instance per data folder:** `ui_server.json` lock (mode 600, pid + port + url). A second
  launch prints the running URL and exits 0. `--exit-with-parent` stops the backend when the app is
  gone, never mid-write.
- **macOS app:** runs `python3 -u -B Resources/star_lists_ui.py --no-browser --exit-with-parent
  --dir <data dir>` and loads the printed URL. It bundles its own copies of the `.py` and `.html`
  files. Bundle id `io.github.dtjgp.starshelf`; data dir = user default `SLDataDir`, else
  Info.plist, else `~/Library/Application Support/Starshelf`. Ad-hoc signed, universal, macOS 14+.
- **AI assistant** (`star_lists_ai.py`, `GET/POST /api/chat`): one turn runs in the background.
  Backends:
  - `claude-code`: `claude -p` with `--tools ""`, `--strict-mcp-config`, `--setting-sources project`,
    stdin prompt, empty temp cwd. The child env drops `ANTHROPIC_*` keys and parent-session vars
    (`CLAUDECODE`, `CLAUDE_CODE_*`, `CLAUDE_AGENT_SDK_*`); without that, a run started inside a
    Claude Code session attaches to that session.
  - `codex`: `codex exec --sandbox read-only --ephemeral --ignore-user-config --ignore-rules`. Its
    tools and MCP cannot be fully disabled, so the page shows a prompt-injection warning; the owner
    accepted this with the warning.
  The AI sees ids `r1…` / `L1…` only; `interpret_chat()` maps them back and drops invalid steps.
  The page applies the operations to the draft as ONE undoable edit and never writes to GitHub.

## Decisions already made

Do not reopen these without a new reason from the owner.

- AI runs only through the local Claude Code or Codex subscription. There is no Anthropic API
  backend (removed in PR #3).
- 「AI 建议」 was removed; only the 「AI 助手」 chat remains (PR #5).
- Gold only means "changed"; never use gold as a list colour.
- UI copy is bilingual: every page string lives in the `STR` table in `star_lists_ui.html` as
  `[zh, en]`; server and AI messages use `sl.tr(zh, en)`. Add both forms for new text.
- The owner reorganises the taxonomy in the app. Pre-fill a draft only when asked.
- Ask before you add a feature, setting, toggle or UI text that the owner did not request.

## Verification

```bash
python3 tests/test_engine.py     # needs PyYAML
python3 tests/test_lifecycle.py
python3 tests/test_ai.py         # fake SDK, no key needed
python3 tests/test_ui.py         # needs playwright + chromium
```

- All tests use the fake GitHub and fake AI; no network or token is needed.
- After you change `star_lists.py`, `star_lists_ui.py`, `star_lists_ai.py` or the HTML, run all
  four, then rebuild the app: `cd app && ./build_app.sh --install`.
- CI runs the four tests on Python 3.9 and 3.12. Keep the code Python 3.9-compatible.
- Screenshot changes: `tests/make_screenshots.py` redraws `docs/screenshots/` (zh and en) from
  demo data.

## Workflow

- **Before code:** state what will change, which features it affects, and how you will verify it.
- **Behavior first:** for new behavior, turn the requirement into tests first. For user-visible
  behavior, let the owner confirm the test points, then implement until they pass.
- **Small slices:** one feature branch and PR per usable step.
- **Bugs:** find the cause before you change code. After two failed attempts, stop guessing: add
  temporary diagnostics and read the real runtime data (never print the token). After three failed
  attempts, stop: write a dated handoff in `local-notes/STATUS.md` (problem, expected and actual
  behavior, reproduction, each attempt and why it failed, current hypothesis), revert unverified
  attempt changes, and restart in a fresh session.
- **Same root cause:** search for the same pattern and fix every occurrence with that cause in the
  same change. Record other similar problems as follow-ups.
- **Regression tests:** every fixed bug, including user-reported ones, gets a test that stays in
  the suite.
- **Review:** a separate agent with fresh context reviews the diff. Limit review to three rounds:
  round one reviews the full diff; later rounds review only the changes since the previous round.
  If a critical or high finding is still open after round three, stop and report it.
- **Parallel agents:** each agent works in its own worktree and stages only its own paths. Run all
  four tests after the work is merged.

## Git and release

- Feature branch → `gh pr create` → wait for GitHub Actions → owner merges (squash) →
  `git switch main && git pull --ff-only` → rebuild the app from `main`.
- Conventional Commits. Except for typo or formatting-only commits, the body has three parts:

  ```text
  What changed: <the behavior or files that changed, and why>
  Verification: <the commands or checks that ran, and their results>
  Rollback: <how to undo it; say if a GitHub write or a backup restore is involved>
  ```

- Before every push, check the diff for secrets and personal data. `stars_backup/`,
  `tests/fixtures/snapshot.json`, `CLAUDE.md` and `local-notes/` are gitignored.

## Gotchas

- GitHub Lists are still "public preview"; the API may change.
- GitHub secondary rate limits on writes: the executor sleeps 1 s between mutations and backs off
  on 403/429/`RATE_LIMITED`. Hundreds of membership changes take minutes.
- `gh auth status` must show the `user` scope, else `INSUFFICIENT_SCOPES`
  (`gh auth refresh -h github.com -s user`).
- Windows: the lock check skips `os.kill(pid, 0)` and SIGHUP is optional; untested on Windows.
- WKWebView storage (list colours) is keyed by bundle id; a bundle id change resets it.
- After a new icon, the Dock may keep the old one: `touch /Applications/Starshelf.app; killall Dock`.
- Squash merges leave the local feature branch "unmerged" for git; check that its tree equals
  `main`'s, then `git branch -D`.
- The `.app` is ad-hoc signed, so Gatekeeper blocks a downloaded copy; users build with
  `build_app.sh`.
