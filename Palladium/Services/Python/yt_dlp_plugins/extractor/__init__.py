"""国内平台提取器集合。

- cnsites.py        抖音短链/图文、小红书短链/图文
- douyin_webview.py 抖音视频（WKWebView 跑官方 bdms SDK 算 X-Bogus）
- wechat_channels.py 微信视频号（WebView 登录元宝换 token）

yt-dlp 会扫描本目录下的 InfoExtractor 子类并注册。
"""
