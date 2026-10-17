// Starshelf — a small native macOS shell around star_lists_ui.py.
//
// The app starts the bundled Python backend (127.0.0.1 only), shows its page in a WKWebView,
// opens GitHub links in your normal browser, and stops the backend when you quit. Quitting
// while GitHub is being written to asks first, because that would interrupt the write.

import Cocoa
import WebKit

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var server: Process?
    private var serverURL: URL?
    private var outData = Data()
    private var errData = Data()
    private var quitting = false
    private var reusedInstance = false

    // MARK: - Launch

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsMagnification = true

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1320, height: 860),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Starshelf"
        window.minSize = NSSize(width: 760, height: 520)
        window.contentView = webView
        window.delegate = self
        window.isReleasedWhenClosed = false
        if !window.setFrameUsingName("StarshelfMain") { window.center() }
        window.setFrameAutosaveName("StarshelfMain")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        showPage(title: "正在启动 Starshelf", body: "正在从 GitHub 读取你的 star 和列表…", spinning: true)
        startServer()
    }

    // MARK: - Backend

    private var resources: String { Bundle.main.resourcePath ?? "." }

    // A setting comes from the user's defaults first (`defaults write <bundle id> SLDataDir ...`),
    // then from Info.plist.
    private func setting(_ key: String) -> String? {
        UserDefaults.standard.string(forKey: key) ?? Bundle.main.object(forInfoDictionaryKey: key) as? String
    }

    private var dataDir: String {
        NSString(string: setting("SLDataDir") ?? "~/Library/Application Support/Starshelf").expandingTildeInPath
    }

    private func findPython() -> String? {
        let home = NSHomeDirectory()
        var candidates: [String] = []
        if let custom = setting("SLPython") { candidates.append(NSString(string: custom).expandingTildeInPath) }
        candidates += ["\(home)/miniconda3/bin/python3", "\(home)/anaconda3/bin/python3",
                       "\(home)/miniforge3/bin/python3", "/opt/homebrew/bin/python3",
                       "/usr/local/bin/python3", "/usr/bin/python3"]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    private func startServer() {
        guard let python = findPython() else {
            showPage(title: "找不到 Python 3",
                     body: "Starshelf 需要 Python 3。请先安装（例如在终端运行 brew install python），然后重新打开。")
            return
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = ["-u", "-B", "\(resources)/star_lists_ui.py",
                             "--no-browser", "--exit-with-parent", "--dir", dataDir]
        var env = ProcessInfo.processInfo.environment
        let home = NSHomeDirectory()
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:\(home)/miniconda3/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONDONTWRITEBYTECODE"] = "1"  // never write __pycache__ into the signed app bundle
        process.environment = env

        let out = Pipe()
        let err = Pipe()
        process.standardOutput = out
        process.standardError = err
        out.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            DispatchQueue.main.async { MainActor.assumeIsolated { self.gotOutput(data) } }
        }
        err.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            DispatchQueue.main.async { MainActor.assumeIsolated { self.errData.append(data) } }
        }
        process.terminationHandler = { proc in
            let status = proc.terminationStatus
            // give the pipes a moment to deliver the last lines before reporting
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                MainActor.assumeIsolated { self.serverEnded(status) }
            }
        }
        do {
            try process.run()
            server = process
        } catch {
            showPage(title: "无法启动后台程序", body: error.localizedDescription)
        }
    }

    private func gotOutput(_ data: Data) {
        outData.append(data)
        guard serverURL == nil else { return }
        let text = String(decoding: outData, as: UTF8.self)
        if let range = text.range(of: #"http://127\.0\.0\.1:\d+/#t=[A-Za-z0-9_\-]+"#, options: .regularExpression),
           let url = URL(string: String(text[range])) {
            serverURL = url
            reusedInstance = text.contains("已在运行")
            webView.load(URLRequest(url: url))
        }
    }

    private func serverEnded(_ status: Int32) {
        server = nil
        if quitting || (reusedInstance && status == 0) { return }
        let log = (String(decoding: errData, as: UTF8.self) + "\n" + String(decoding: outData, as: UTF8.self))
            .trimmingCharacters(in: .whitespacesAndNewlines)
        var hint = "请退出 Starshelf 后重新打开。"
        if log.contains("No GitHub token") || log.contains("HTTP 401") {
            hint = "GitHub 登录信息无效。请在终端运行 gh auth login，然后重新打开 Starshelf。"
        }
        serverURL = nil
        showPage(title: "Starshelf 的后台程序停止了", body: hint,
                 detail: log.isEmpty ? "退出码 \(status)" : String(log.suffix(6000)))
    }

    private func stopServer() {
        quitting = true
        guard let process = server, process.isRunning else { return }
        process.terminate()  // SIGTERM: the backend shuts down cleanly and removes its lock file
        let deadline = Date().addingTimeInterval(3)
        while process.isRunning && Date() < deadline { usleep(50_000) }
        if process.isRunning { kill(process.processIdentifier, SIGKILL) }
    }

    // MARK: - Quit (⌘Q, closing the window)

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        NSApp.terminate(nil)  // same safety check as ⌘Q; the window stays if the user cancels
        return false
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if quitting { return .terminateNow }
        guard let url = serverURL, let port = url.port,
              let jobURL = URL(string: "http://127.0.0.1:\(port)/api/job") else {
            stopServer()
            return .terminateNow
        }
        let token = (url.fragment ?? "").replacingOccurrences(of: "t=", with: "")
        var request = URLRequest(url: jobURL)
        request.setValue(token, forHTTPHeaderField: "X-Session")
        request.timeoutInterval = 3
        URLSession.shared.dataTask(with: request) { data, _, _ in
            var running = false
            if let data, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                running = (obj["status"] as? String) == "running"
            }
            DispatchQueue.main.async { MainActor.assumeIsolated { self.confirmQuit(running: running) } }
        }.resume()
        return .terminateLater
    }

    private func confirmQuit(running: Bool) {
        if running {
            let alert = NSAlert()
            alert.messageText = "正在写入 GitHub"
            alert.informativeText = "现在退出会中断写入。已经写入的部分会保留，下次打开后点「查看剩余修改」就能继续。"
            alert.addButton(withTitle: "继续等待")
            alert.addButton(withTitle: "仍然退出")
            if alert.runModal() == .alertFirstButtonReturn {
                NSApp.reply(toApplicationShouldTerminate: false)
                return
            }
        }
        stopServer()
        NSApp.reply(toApplicationShouldTerminate: true)
    }

    // MARK: - Web view

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping @MainActor @Sendable (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.allow); return }
        if url.scheme == "about" || url.host == "127.0.0.1" || url.host == "localhost" {
            decisionHandler(.allow)
            return
        }
        if url.scheme == "http" || url.scheme == "https" { NSWorkspace.shared.open(url) }
        decisionHandler(.cancel)  // includes files dropped onto the window
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url, url.scheme == "http" || url.scheme == "https" {
            NSWorkspace.shared.open(url)  // repo links (target=_blank) open in your browser
        }
        return nil
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        if let url = serverURL { webView.load(URLRequest(url: url)) }
    }

    private func esc(_ s: String) -> String {
        s.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;").replacingOccurrences(of: "\"", with: "&quot;")
    }

    private func showPage(title: String, body: String, spinning: Bool = false, detail: String? = nil) {
        let pre = detail.map { "<pre>\(esc($0))</pre>" } ?? ""
        let html = """
        <!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>
        :root{--paper:#EDF0F4;--ink:#1B2333;--ink2:#566175;--gold:#B47E0E;--sunk:#F5F7FA;color-scheme:light dark}
        @media (prefers-color-scheme:dark){:root{--paper:#111722;--ink:#E4E8F0;--ink2:#A3ADBF;--gold:#E2B048;--sunk:#171F2D}}
        html,body{height:100%;margin:0}
        body{display:grid;place-items:center;background:var(--paper);color:var(--ink);
             font:14px/1.5 "Avenir Next","PingFang SC",system-ui,sans-serif;-webkit-font-smoothing:antialiased}
        main{max-width:640px;padding:32px;text-align:center}
        svg{width:40px;height:40px} path{fill:var(--gold)}
        .spin{animation:t 2.4s linear infinite} @keyframes t{to{transform:rotate(360deg)}}
        h1{font-size:19px;font-weight:600;margin:14px 0 6px} p{color:var(--ink2);margin:0}
        pre{text-align:left;white-space:pre-wrap;background:var(--sunk);border-radius:8px;padding:12px 14px;
            font-size:12.5px;max-height:50vh;overflow:auto;margin-top:16px}
        @media (prefers-reduced-motion:reduce){.spin{animation:none}}
        </style></head><body><main>
        <svg class="\(spinning ? "spin" : "")" viewBox="0 0 24 24"><path d="M12 2.6l2.83 6.08 6.67.72-4.98 4.5 1.4 6.56L12 17.1l-5.92 3.36 1.4-6.56-4.98-4.5 6.67-.72z"/></svg>
        <h1>\(esc(title))</h1><p>\(esc(body))</p>\(pre)
        </main></body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    // MARK: - Menus

    @objc private func showDataFolder(_ sender: Any?) {
        try? FileManager.default.createDirectory(atPath: dataDir, withIntermediateDirectories: true)
        NSWorkspace.shared.open(URL(fileURLWithPath: dataDir))
    }

    @objc private func openInBrowser(_ sender: Any?) {
        if let url = serverURL { NSWorkspace.shared.open(url) }
    }

    @objc private func reloadPage(_ sender: Any?) {
        if let url = serverURL { webView.load(URLRequest(url: url)) } else { webView.reload() }
    }

    @objc private func zoomIn(_ sender: Any?) { webView.pageZoom = min(webView.pageZoom + 0.1, 2.0) }
    @objc private func zoomOut(_ sender: Any?) { webView.pageZoom = max(webView.pageZoom - 0.1, 0.6) }
    @objc private func zoomReset(_ sender: Any?) { webView.pageZoom = 1.0 }

    private func buildMenu() {
        let main = NSMenu()

        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于 Starshelf",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "在访达中显示数据文件夹", action: #selector(showDataFolder(_:)), keyEquivalent: "")
        appMenu.addItem(withTitle: "在浏览器中打开", action: #selector(openInBrowser(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏 Starshelf", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "退出 Starshelf", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let appItem = NSMenuItem()
        appItem.submenu = appMenu
        main.addItem(appItem)

        // The page handles ⌘Z / ⇧⌘Z / ⌘A itself; these items make them (and copy/paste) work in text fields.
        let edit = NSMenu(title: "编辑")
        edit.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = edit.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(.separator())
        edit.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        let editItem = NSMenuItem()
        editItem.submenu = edit
        main.addItem(editItem)

        let view = NSMenu(title: "显示")
        view.addItem(withTitle: "重新载入", action: #selector(reloadPage(_:)), keyEquivalent: "r")
        view.addItem(.separator())
        view.addItem(withTitle: "实际大小", action: #selector(zoomReset(_:)), keyEquivalent: "0")
        view.addItem(withTitle: "放大", action: #selector(zoomIn(_:)), keyEquivalent: "+")
        view.addItem(withTitle: "缩小", action: #selector(zoomOut(_:)), keyEquivalent: "-")
        let viewItem = NSMenuItem()
        viewItem.submenu = view
        main.addItem(viewItem)

        let win = NSMenu(title: "窗口")
        win.addItem(withTitle: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "关闭", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        let winItem = NSMenuItem()
        winItem.submenu = win
        main.addItem(winItem)

        NSApp.mainMenu = main
        NSApp.windowsMenu = win
    }
}

MainActor.assumeIsolated {
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    app.setActivationPolicy(.regular)
    app.run()
}
