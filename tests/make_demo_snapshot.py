"""Write tests/fixtures/demo_snapshot.json: a made-up account for the UI test and screenshots.

    python3 tests/make_demo_snapshot.py

Deterministic. Shape: 321 starred repos, 9 lists (same sizes and overlaps as a real account),
12 repos in no list. Almost every repo is fictional; four well-known public repos sit in fixed
places because the UI test drags them around.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

OUT = Path(__file__).parent / "fixtures" / "demo_snapshot.json"
LISTS = [  # name, size, description
    ("⭐️⭐️LearnSomething", 56, "Good repos to learn something"),
    ("FunProjects", 139, ""),
    ("ClaudeCode&Codex", 88, "Coding agents and their tooling"),
    ("LLM related", 56, ""),
    ("DailyHelper", 15, "Small tools for every day"),
    ("EdgeAI", 28, "Repos related to model compression"),
    ("SmartGrid", 7, ""),
    ("Finance", 6, ""),
    ("GreenAI", 3, "Energy-aware machine learning"),
]
N_REPOS, N_UNLISTED = 321, 12
SPECIAL = {  # name -> (exactly the lists it is in, description, language)
    "microsoft/qlib": ({"EdgeAI"}, "AI-oriented quantitative investment platform", "Python"),
    "papers-we-love/papers-we-love": ({"EdgeAI"}, "Papers from the computer science community to read and discuss", ""),
    "mlco2/codecarbon": ({"GreenAI"}, "Track emissions from compute and recommend ways to reduce their impact", "Python"),
    "ollama/ollama": ({"FunProjects", "LLM related"}, "Get up and running with large language models", "Go"),
}
ORGS = ["acme-labs", "example-ai", "sample-dev", "demo-works", "northwind", "contoso-research",
        "fabrikam", "tailspin", "wingtip-tools", "lumen-oss"]
WORDS = ["atlas", "beacon", "cobalt", "delta", "ember", "fjord", "grove", "harbor", "iris", "juniper",
         "kestrel", "lagoon", "meadow", "nimbus", "orbit", "prism", "quartz", "ripple", "summit", "tundra"]
TOPICS = {
    "⭐️⭐️LearnSomething": ["course notes on deep learning", "a beginner-friendly algorithms book", "paper reading notes and summaries"],
    "FunProjects": ["a terminal toy", "a tiny game engine", "a generative art playground", "a web scraper"],
    "ClaudeCode&Codex": ["prompts and skills for coding agents", "an MCP server", "agent workflow templates"],
    "LLM related": ["LLM inference server", "fine-tuning recipes", "a RAG starter kit", "paper implementations of LLM methods"],
    "DailyHelper": ["a menu-bar utility", "a note-taking helper", "a clipboard manager"],
    "EdgeAI": ["model quantization toolkit", "pruning experiments", "on-device inference runtime", "paper list on efficient deep learning"],
    "SmartGrid": ["power-grid simulator", "demand-response dataset tools"],
    "Finance": ["market data downloader", "backtesting framework"],
    "GreenAI": ["energy measurement for GPUs"],
    "": ["a small utility", "a dataset collection", "an experiment log"],
}
LANGS = ["Python", "Python", "Python", "TypeScript", "Rust", "Go", "Jupyter Notebook", "Shell", "C++", ""]


def main() -> None:
    rnd = random.Random(20261017)
    # memberships: repos 0..11 are in no list; lists take consecutive (wrapping) runs of the rest
    pool = list(range(N_UNLISTED, N_REPOS))
    members: dict[str, list[int]] = {}
    pos = 0
    for name, size, _ in LISTS:
        members[name] = [pool[(pos + k) % len(pool)] for k in range(size)]
        pos += size
    names: dict[int, str] = {}
    for special, (wanted, _, _) in SPECIAL.items():
        # pin one repo to exactly `wanted`; swap it with another repo where needed (sizes unchanged)
        first = sorted(wanted)[0]
        i = next(i for i in members[first] if i not in names)
        names[i] = special
        for name, idx in members.items():
            if name in wanted and i not in idx:
                k = next(k for k in range(len(idx) - 1, -1, -1) if idx[k] not in names)
                idx[k] = i
            elif name not in wanted and i in idx:
                idx[idx.index(i)] = next(j for j in pool if j not in idx and j not in names)
    in_lists: dict[int, set[str]] = {}
    for name, idx in members.items():
        for i in idx:
            in_lists.setdefault(i, set()).add(name)
    used = set(names.values())
    for i in range(N_REPOS):
        while i not in names:
            cand = f"{rnd.choice(ORGS)}/{rnd.choice(WORDS)}-{rnd.choice(WORDS)}"
            if cand not in used:
                names[i] = cand
                used.add(cand)

    repos = []
    for i in range(N_REPOS):
        full = names[i]
        home = sorted(in_lists.get(i, {""}))[0]
        if full in SPECIAL:
            desc, lang = SPECIAL[full][1], SPECIAL[full][2]
        else:
            desc = f"Demo repository: {rnd.choice(TOPICS[home])}."
            lang = rnd.choice(LANGS)
        day = 1 + (N_REPOS - i) * 2  # repo 0 is the most recently starred
        repos.append({
            "id": f"R_demo{i:04d}", "full_name": full, "starred": True, "description": desc, "language": lang,
            "stars": rnd.choice([12, 85, 430, 1200, 5600, 23000, 98000]) + i, "archived": i % 41 == 7,
            "pushed_at": f"2026-{1 + i % 9:02d}-{1 + i % 27:02d}T08:00:00Z",
            "starred_at": f"{2026 - day // 360}-{1 + (day // 30) % 12:02d}-{1 + day % 28:02d}T09:00:00Z",
            "url": f"https://github.com/{full}",
        })
    repos.sort(key=lambda r: r["starred_at"], reverse=True)
    lists = [{"id": f"UL_demo{k:02d}", "name": name, "description": desc, "private": False,
              "items": [f"R_demo{i:04d}" for i in members[name]]}
             for k, (name, _, desc) in enumerate(LISTS, 1)]
    doc = {"format": 1, "tool": "tests/make_demo_snapshot.py", "login": "demo",
           "fetched_at": "2026-10-17T12:00:00+00:00", "lists": lists, "repos": repos}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    listed = {i for l in lists for i in l["items"]}
    print(f"wrote {OUT}: {len(repos)} repos, {len(lists)} lists, {sum(r['id'] not in listed for r in repos)} in no list")


if __name__ == "__main__":
    main()
