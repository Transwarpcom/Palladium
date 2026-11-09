import Combine
import SwiftUI
import WebKit

struct CookieWebsiteImportView: View {
    let onImport: (_ cookies: [HTTPCookie], _ sourceURL: URL) throws -> Void
    /// 预设网址：给了就跳过输入框，直接进登录浏览器。
    /// 用于「小红书 / 抖音 / B站」这类固定站点的快捷入口。
    var presetURL: URL? = nil

    @Environment(\.dismiss) private var dismiss
    @State private var navigationPath: [URL] = []

    var body: some View {
        NavigationStack(path: $navigationPath) {
            CookieWebsiteURLView { sourceURL in
                navigationPath.append(sourceURL)
            }
            .navigationDestination(for: URL.self) { sourceURL in
                CookieLoginBrowserView(
                    sourceURL: sourceURL,
                    onCancel: { dismiss() },
                    onImport: { cookies in
                        try onImport(cookies, sourceURL)
                        dismiss()
                    }
                )
            }
        }
        .interactiveDismissDisabled()
        .onAppear {
            if let presetURL, navigationPath.isEmpty {
                navigationPath.append(presetURL)
            }
        }
    }
}

private struct CookieWebsiteURLView: View {
    let onOpen: (_ sourceURL: URL) -> Void

    @Environment(\.dismiss) private var dismiss
    @FocusState private var isURLFieldFocused: Bool
    @State private var urlText = ""
    @State private var errorMessage: String?

    var body: some View {
        Form {
            Section {
                TextField("cookies.web.url.placeholder", text: $urlText)
                    .keyboardType(.URL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .focused($isURLFieldFocused)
                    .onSubmit(openWebsite)

                Button(action: openWebsite) {
                    Label("cookies.web.open", systemImage: "globe")
                }
                .disabled(urlText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            } header: {
                Text("cookies.web.url.message")
            } footer: {
                Text("cookies.web.privacy")
            }
        }
        .navigationTitle("cookies.web.url.title")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .cancellationAction) {
                Button("common.cancel") {
                    dismiss()
                }
            }
        }
        .alert(String(localized: "common.result", bundle: .app), isPresented: Binding(
            get: { errorMessage != nil },
            set: { isPresented in
                if !isPresented {
                    errorMessage = nil
                }
            }
        )) {
            Button(String(localized: "common.ok", bundle: .app), role: .cancel) {
                errorMessage = nil
            }
        } message: {
            Text(errorMessage ?? "")
        }
        .onAppear {
            isURLFieldFocused = true
        }
    }

    private func openWebsite() {
        guard let sourceURL = normalizedWebsiteURL(from: urlText) else {
            errorMessage = String(localized: "cookies.error.invalid_url", bundle: .app)
            return
        }
        isURLFieldFocused = false
        onOpen(sourceURL)
    }

    private func normalizedWebsiteURL(from rawValue: String) -> URL? {
        let trimmedValue = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedValue.isEmpty else { return nil }

        let valueWithScheme = trimmedValue.contains("://") ? trimmedValue : "https://\(trimmedValue)"
        guard let components = URLComponents(string: valueWithScheme),
              let scheme = components.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              components.host?.isEmpty == false else {
            return nil
        }
        return components.url
    }
}

private struct CookieLoginBrowserView: View {
    let sourceURL: URL
    let onCancel: () -> Void
    let onImport: (_ cookies: [HTTPCookie]) throws -> Void

    @StateObject private var browser = CookieLoginBrowserModel()
    @State private var isImporting = false
    @State private var errorMessage: String?

    var body: some View {
        CookieLoginWebView(sourceURL: sourceURL, browser: browser)
            .overlay(alignment: .top) {
                if browser.isLoading {
                    ProgressView()
                        .padding(10)
                        .background(.regularMaterial, in: Capsule())
                        .padding()
                }
            }
            .safeAreaInset(edge: .bottom) {
                VStack(alignment: .leading, spacing: 4) {
                    if let loadErrorMessage = browser.loadErrorMessage {
                        Text(loadErrorMessage)
                            .foregroundStyle(.red)
                    }

                    Text("cookies.web.browser.help")
                        .foregroundStyle(.secondary)
                }
                .font(.caption)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal)
                .padding(.vertical, 10)
                .background(.bar)
            }
            .navigationTitle("cookies.web.browser.title")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("common.cancel", action: onCancel)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("cookies.web.import", action: importCookies)
                        .disabled(isImporting)
                }
            }
            .alert(String(localized: "common.result", bundle: .app), isPresented: Binding(
                get: { errorMessage != nil },
                set: { isPresented in
                    if !isPresented {
                        errorMessage = nil
                    }
                }
            )) {
                Button(String(localized: "common.ok", bundle: .app), role: .cancel) {
                    errorMessage = nil
                }
            } message: {
                Text(errorMessage ?? "")
            }
    }

    private func importCookies() {
        isImporting = true
        browser.getAllCookies { cookies in
            isImporting = false
            do {
                try onImport(cookies)
            } catch {
                errorMessage = error.localizedDescription
            }
        }
    }
}

private final class CookieLoginBrowserModel: ObservableObject {
    // 用 default() 而不是 nonPersistent()：
    // 后者每次登录都是一次性会话，关掉就没了，等于每回都要重新登录。
    // default() 把 cookie 和 localStorage 落到 App 的 WebKit 数据目录，
    // 下次打开还在，会话能自己续期（这正是解决 cookies 过期问题的关键）。
    let websiteDataStore = WKWebsiteDataStore.default()

    @Published var isLoading = false
    @Published var loadErrorMessage: String?

    func getAllCookies(completion: @escaping ([HTTPCookie]) -> Void) {
        websiteDataStore.httpCookieStore.getAllCookies(completion)
    }
}

private struct CookieLoginWebView: UIViewRepresentable {
    let sourceURL: URL
    @ObservedObject var browser: CookieLoginBrowserModel

    func makeCoordinator() -> Coordinator {
        Coordinator(browser: browser)
    }

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = browser.websiteDataStore

        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true
        webView.load(URLRequest(url: sourceURL))
        return webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {}

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        private let browser: CookieLoginBrowserModel

        init(browser: CookieLoginBrowserModel) {
            self.browser = browser
        }

        func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation?) {
            browser.isLoading = true
            browser.loadErrorMessage = nil
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation?) {
            browser.isLoading = false
        }

        func webView(
            _ webView: WKWebView,
            didFail navigation: WKNavigation?,
            withError error: Error
        ) {
            browser.isLoading = false
            setLoadError(error)
        }

        func webView(
            _ webView: WKWebView,
            didFailProvisionalNavigation navigation: WKNavigation?,
            withError error: Error
        ) {
            browser.isLoading = false
            setLoadError(error)
        }

        func webView(
            _ webView: WKWebView,
            createWebViewWith configuration: WKWebViewConfiguration,
            for navigationAction: WKNavigationAction,
            windowFeatures: WKWindowFeatures
        ) -> WKWebView? {
            if navigationAction.targetFrame == nil {
                webView.load(navigationAction.request)
            }
            return nil
        }

        private func setLoadError(_ error: Error) {
            let nsError = error as NSError
            if nsError.domain == NSURLErrorDomain && nsError.code == NSURLErrorCancelled {
                return
            }
            browser.loadErrorMessage = error.localizedDescription
        }
    }
}
