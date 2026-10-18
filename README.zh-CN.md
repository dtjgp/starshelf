<img src="docs/icon.png" width="128" alt="Starshelf 图标">

# starshelf

[English](README.md) · **简体中文**

批量整理 **GitHub Star Lists**。GitHub 网页一次只能改一个仓库的归属；starshelf 可以一次新建、改名、
删除列表，并批量加入、移动、移出几百个已 star 的仓库。可以用本地网页、macOS 小程序或命令行操作。

![全部 star](docs/screenshots/all-stars.png)

## 功能

- **先改草稿，再应用。** 所有修改都先进入本地草稿（自动保存，可以撤销和重做）。
  只有在预览里点「应用到 GitHub」，才会写入 GitHub。
- **先预览。** 预览会列出要新建、改名、删除的列表，以及每个归属有变化的仓库。
- **每次写入前自动备份。** 每次应用前都会保存 GitHub 上的当前状态，可以在「备份」里一键恢复
  （命令行用 `star_lists.py restore`）。
- **可中断、可继续。** 写到一半出错（网络、限流）时，再应用一次即可，会从当前进度继续。
  写完后会重新读取 GitHub 核对结果。
- **不会误取消 star。** 只有你明确标记「取消 star」的仓库才会被取消：预览里单独列出，必须勾选确认，
  并且在最后一步才执行。以后从备份恢复时会重新 star（star 时间会变成恢复当天）。
- **可选的 AI 助手和 AI 建议。** 在右侧对话面板里直接对 AI 说「把量化相关的仓库移到 Finance」
  「把 FunProjects 按主题拆成三个列表」，每次回答都作为一步可撤销的修改写进草稿；也可以让 AI
  一次给出完整的新列表方案。AI 只会改草稿。
  可以用本机的 **Claude Code** 或 **Codex**（使用它们自己的登录和会员额度），也可以用
  **Anthropic API**（需要 API key）。

![预览修改](docs/screenshots/preview.png)

## 需要什么

- Python 3.9 或更新版本。
- 带 `user` 权限的 GitHub token。最简单的方式是安装 [GitHub CLI](https://cli.github.com/)，
  运行 `gh auth login` 登录，再运行 `gh auth refresh -h github.com -s user`。也可以设置 `GH_TOKEN`。
- `pip install pyyaml`：只有命令行的 `layout.yaml` 流程需要。
- 可选（AI 建议）：已登录的 `claude`（Claude Code）或 `codex` 命令；或者 `pip install anthropic`
  加 Anthropic API key。

## 快速开始

```bash
git clone https://github.com/dtjgp/starshelf.git
cd starshelf
python3 star_lists_ui.py          # 在浏览器打开 http://127.0.0.1:8765/#t=<本次运行的密钥>
```

数据（备份、草稿、日志）保存在脚本旁边的 `stars_backup/` 里。用 `--dir` 可以换一个文件夹。

### macOS 小程序

```bash
cd app && ./build_app.sh --install    # 需要 Xcode Command Line Tools；安装为「Starshelf.app」
```

小程序只是一个窗口，里面是同一个本地网页。数据默认保存在
`~/Library/Application Support/Starshelf`。要换文件夹：

```bash
defaults write io.github.dtjgp.starshelf SLDataDir "~/path/to/folder"
```

在 macOS 26 及以后的系统上，如果用 Xcode 26 或更新版本构建，图标会跟随「系统设置 → 外观 → 图标和小组件样式」
（默认、深色、透明、着色）自动变化；更早的 macOS 显示经典图标。构建出的 App 是 ad-hoc 签名，只适合在你自己的 Mac 上用。从别处下载的副本会被 Gatekeeper 拦下，
请自己构建。

### 命令行

```bash
python3 star_lists.py export     # 备份，并生成可编辑的 stars_backup/layout.yaml
# 编辑 layout.yaml：在列表之间移动仓库行，改名、删除或新增列表块；
# 把仓库行剪切到 `unstar:` 下 = 取消它的 star
python3 star_lists.py plan       # 只预览，不写入
python3 star_lists.py apply      # 询问确认 → 备份 → 写入 → 核对
python3 star_lists.py restore stars_backup/snapshots/<文件>.json
```

使用 `--yes` 时，如果计划里有取消 star，还必须加 `--allow-unstar`。

![取消 star 需要确认](docs/screenshots/unstar-confirm.png)

## AI 助手与 AI 建议

- **AI 助手**：点击后在右侧打开对话面板。用自然语言说要怎么改，AI 会回复一段说明，并列出它对草稿
  做的修改，每一步都可以撤销。需求不清楚时，它会先反问。
- **AI 建议**：一次给出完整的新列表方案，或给不在任何列表里的仓库找位置，放进草稿前先预览。

![AI 助手](docs/screenshots/ai-assistant.png)

两者都使用你选择的后端：

| 后端 | 用什么 | 说明 |
|---|---|---|
| Claude Code（默认） | `claude -p`，使用 Claude Code 的登录 | 不开任何工具、不加载 MCP 服务器和用户设置。 |
| Codex | `codex exec`，使用 Codex / ChatGPT 的登录 | 在空文件夹的只读沙箱里运行，但无法完全关掉它的工具；页面会显示安全提示。 |
| Anthropic API | `anthropic` 库 + `ANTHROPIC_API_KEY`，或 macOS 钥匙串里用 `security add-generic-password -s "Starshelf" -a anthropic-api-key -w` 保存的 key | 按用量付费。 |

可以用 `STAR_LISTS_CLAUDE_MODEL`、`STAR_LISTS_CODEX_MODEL`、`STAR_LISTS_AI_MODEL` 更换模型。

## 隐私与安全

- 网页只在 `127.0.0.1` 上提供；每个 API 请求都要带本次运行的密钥，并检查 Host 和 Origin。
  GitHub token 只留在本机的 Python 进程里。
- 数据只会发给 GitHub。使用 AI 建议时，仓库名、语言、简介和列表名会发给你选择的 AI 服务
  （Anthropic 或 OpenAI）。GitHub token 不会发给任何 AI 服务。

## 测试

所有测试都用内存里的假 GitHub，不联网，也不需要 token。

```bash
pip install pyyaml playwright && python3 -m playwright install chromium
python3 tests/test_engine.py
python3 tests/test_lifecycle.py
python3 tests/test_ai.py
python3 tests/test_ui.py          # 在虚构账号上的浏览器点击测试
```

## 限制

- GitHub Lists 仍是公开预览功能，API 可能会变。
- GitHub 限制写入速度：starshelf 每次写入间隔约 1 秒，几百处修改需要几分钟。
- 在 macOS 上测试过。网页和命令行在 Linux 上应该也能用；Windows 没有测试过。

## 许可证

[MIT](LICENSE)
