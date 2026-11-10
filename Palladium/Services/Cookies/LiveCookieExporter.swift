import Foundation
import WebKit

extension Notification.Name {
    /// App 内 WebView 登录完成。ContentView 收到后会把 cookie 文件刷新一遍。
    static let palladiumWebLoginCompleted = Notification.Name("palladiumWebLoginCompleted")
}

/// 从 App 内登录 WebView 的持久会话里实时导出 cookies。
///
/// ## 为什么需要这个
///
/// 原来的实现是：用户在 WebView 里登录 → 点「导入」→ 导出一次性快照存成
/// `xxx-cookies.txt` → 之后每次下载都用这个文件。
///
/// 问题是快照不刷新。抖音的 X-Bogus 会话、小红书的 xsec_token、
/// 视频号元宝的 hy_token 都是短时效的，几小时后快照就废了，
/// 但文件内容永远停在导出那一刻，于是「导入成功却下不了」。
///
/// 正确做法：登录态的**唯一真源**是 `WKWebsiteDataStore.default()`（落盘、
/// 平台自己在刷新），cookie 文件只是给 yt-dlp 用的投影。
/// 每次下载前从 WebKit 重新导出一次，文件就永远是最新的。
enum LiveCookieExporter {

    /// 登录 WebView 使用的持久会话。和 CookieWebsiteImportView 里那套是同一个，
    /// 所以用户在里面登录过的站点，这里都能读到。
    static var dataStore: WKWebsiteDataStore { .default() }

    /// 从 WebKit 读出当前所有 cookie，按主机名过滤后写成 Netscape 格式。
    ///
    /// - Parameters:
    ///   - hosts: 只导出这些站点的 cookie（含子域）。传 nil 表示全部导出。
    ///   - now: 判定过期用的时间点，测试时可注入。
    /// - Returns: Netscape cookie 文本；没有任何匹配 cookie 时返回 nil。
    static func netscapeText(
        matching hosts: [String]? = nil,
        now: Date = Date()
    ) async -> String? {
        let cookies: [HTTPCookie] = await withCheckedContinuation { continuation in
            dataStore.httpCookieStore.getAllCookies { all in
                continuation.resume(returning: all)
            }
        }
        guard !cookies.isEmpty else { return nil }

        let filtered: [HTTPCookie]
        if let hosts, !hosts.isEmpty {
            let normalized = hosts.map { $0.lowercased() }
            filtered = cookies.filter { cookie in
                let domain = cookie.domain.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "."))
                return normalized.contains { host in
                    domain == host || domain.hasSuffix("." + host)
                }
            }
        } else {
            filtered = cookies
        }
        guard !filtered.isEmpty else { return nil }

        // 复用项目已有的 Netscape 序列化（它会过滤过期项、处理 #HttpOnly_ 前缀）
        return try? NetscapeCookieExporter.text(from: filtered, now: now)
    }

    /// 把某个域名的登录 cookie 刷新到指定文件（覆盖写）。
    ///
    /// 下载前调用一次，yt-dlp 读到的就是最新的会话。
    /// 返回是否写入成功（没登录/没有匹配 cookie 时返回 false，调用方应保留旧文件）。
    ///
    /// 覆盖保护：WebView 会话里的 cookie 条数比原文件少时**不覆盖**。
    /// 用户从浏览器手动导出的文件可能含 WebView 里没有的字段
    /// （比如某些设备指纹 cookie），盲目覆盖会把能用的配置刷坏。
    @discardableResult
    static func refreshCookieFile(
        at fileURL: URL,
        matching hosts: [String]
    ) async -> Bool {
        guard let text = await netscapeText(matching: hosts) else { return false }

        // 数一下新导出的有效行数，和原文件比
        let newCount = countCookieLines(in: text)
        if let existing = try? String(contentsOf: fileURL, encoding: .utf8) {
            let oldCount = countCookieLines(in: existing)
            if newCount < oldCount {
                return false
            }
        }
        guard newCount > 0 else { return false }

        do {
            try FileManager.default.createDirectory(
                at: fileURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try text.write(to: fileURL, atomically: true, encoding: .utf8)
            return true
        } catch {
            return false
        }
    }

    /// 数一下 Netscape cookie 文本里有多少条有效记录。
    /// 跳过注释行（`#` 开头，但 `#HttpOnly_` 开头的算数据行）和空行。
    static func countCookieLines(in text: String) -> Int {
        text.split(separator: "\n", omittingEmptySubsequences: true).reduce(0) { count, rawLine in
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            if line.isEmpty { return count }
            if line.hasPrefix("#"), !line.hasPrefix("#HttpOnly_") { return count }
            return count + 1
        }
    }

    /// 判断某个 cookie 文件名对应哪些站点。
    ///
    /// 文件名来自 `importBrowserCookies`：`<host>-cookies.txt`。
    /// 但重复导入时 uniqueCookieDestinationURL 会生成
    /// `<host>-cookies 2.txt`，所以解析顺序必须是：
    ///   1. 去掉 .txt
    ///   2. 去掉末尾的 ` N`（重名序号）
    ///   3. 去掉 -cookies
    /// 顺序错了就会把 `www.douyin.com-cookies 2` 当成主机名，刷新失效。
    static func hosts(forCookieFileName fileName: String) -> [String] {
        var name = fileName

        // 1) 去扩展名
        if name.lowercased().hasSuffix(".txt") {
            name = String(name.dropLast(4))
        }

        // 2) 去重名序号：结尾是 " 2" ~ " 999"
        if let spaceRange = name.range(of: " ", options: .backwards) {
            let suffix = name[spaceRange.upperBound...]
            if !suffix.isEmpty, suffix.allSatisfy(\.isNumber) {
                name = String(name[name.startIndex..<spaceRange.lowerBound])
            }
        }

        // 3) 去 -cookies 后缀
        if name.hasSuffix("-cookies") {
            name = String(name.dropLast("-cookies".count))
        }

        name = name.trimmingCharacters(in: .whitespaces)
        guard !name.isEmpty else { return [] }

        // 只处理从浏览器登录导出的文件（名字里含点，说明是主机名）。
        // 粘贴导入的文件叫 pasted-cookies.txt，反推出 "pasted" 不是域名，
        // 拿它去匹配 WebView cookie 只会白跑一趟。
        guard name.contains(".") else { return [] }

        // 特例：元宝的视频号凭据存在 yuanbao 域，但下载走 channels 域，
        // 两个都要带上，否则视频号拿不到 token。
        var hosts = [name]
        if name.contains("yuanbao") {
            hosts.append(contentsOf: ["channels.weixin.qq.com", "weixin.qq.com"])
        }
        return hosts
    }
}
