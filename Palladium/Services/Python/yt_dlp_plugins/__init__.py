"""yt-dlp 插件包（Palladium fork 自建）。

这里放国内平台的补充提取器。yt-dlp 的插件发现机制会扫描
sys.path 上的 `yt_dlp_plugins/extractor/` 目录，把里面的
InfoExtractor 子类注册进去。

放空文件是为了确保它被当成正规包（而不是依赖命名空间包推断），
在打包进 .app bundle 后目录层级会被 Xcode 展平，
由 PythonScripts.swift 按 relativePath 重建，所以这个文件不能省。
"""
