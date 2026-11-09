"""Developer diagnostics for the debug menu. Every probe returns a JSON object."""

import ast
import contextlib
import io
import json
import os
import platform
import sys
import tempfile
import time
import traceback

from .shared import TRACKED_PACKAGES

EXTRA_DIAGNOSTIC_PACKAGES = ("cffi", "certifi", "websockets", "brotli", "requests", "urllib3")
RESPONSE_PREVIEW_CHARS = 2000


def diagnostic_result(ok, output="", details=None, error=None):
    return {"ok": bool(ok), "output": str(output or ""), "details": details or {}, "error": error}


def diagnostic_error(error):
    return diagnostic_result(False, traceback.format_exc(), error=f"{type(error).__name__}: {error}")


def package_versions():
    from .packages import installed_version

    versions = {}
    for name in (*TRACKED_PACKAGES, *EXTRA_DIAGNOSTIC_PACKAGES):
        versions[name] = installed_version(name) or "not installed"
    return versions


def python_info(_argument):
    import ssl

    details = {
        "version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "openssl": ssl.OPENSSL_VERSION,
        "prefix": sys.prefix,
        "sys_path": list(sys.path),
        "environment": {
            key: value for key, value in sorted(os.environ.items()) if key.startswith(("PALLADIUM_", "PYTHON"))
        },
        "packages": package_versions(),
    }
    return diagnostic_result(True, sys.version, details)


def run_python_source(source):
    tree = ast.parse(str(source or ""), "<debug>", "exec")
    trailing_expression = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        trailing_expression = ast.Expression(tree.body.pop().value)

    namespace = {"__name__": "__palladium_debug__"}
    captured = io.StringIO()
    value = None
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        exec(compile(tree, "<debug>", "exec"), namespace)
        if trailing_expression is not None:
            value = eval(compile(trailing_expression, "<debug>", "eval"), namespace)

    output = captured.getvalue()
    if value is not None:
        output += repr(value) + "\n"
    return output


def python_eval(argument):
    output = run_python_source(argument.get("source", ""))
    return diagnostic_result(True, output)


def curl_cffi_info(_argument):
    import cffi
    import curl_cffi

    targets = []
    browser_type = getattr(curl_cffi, "BrowserType", None)
    if browser_type is not None:
        targets = [member.value for member in browser_type]
    details = {
        "curl_cffi": getattr(curl_cffi, "__version__", "unknown"),
        "libcurl": getattr(curl_cffi, "__curl_version__", "unknown"),
        "cffi": getattr(cffi, "__version__", "unknown"),
        "module_path": getattr(curl_cffi, "__file__", ""),
        "impersonate_targets": targets,
        "ytdlp_impersonate_targets": ytdlp_impersonate_targets(),
    }
    output = f"curl_cffi {details['curl_cffi']} ({details['libcurl']})"
    return diagnostic_result(True, output, details)


def ytdlp_impersonate_targets():
    try:
        import yt_dlp

        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            available = ydl._get_available_impersonate_targets()
        return sorted({f"{target} via {handler}" for target, handler in available})
    except Exception as error:
        return [f"unavailable: {type(error).__name__}: {error}"]


def ytdlp_plugins_report(_argument):
    """列出已注册的自建提取器，用来确认插件在 App 内是否真的生效。

    排查「装了包却下不了国内平台」时先跑这个：
    如果 expected 里的提取器没出现在 registered 里，
    说明插件没加载（白名单没登记、文件没进 bundle、或 sys.path 顺序不对），
    而不是平台反爬的问题。
    """
    try:
        import sys

        import yt_dlp

        ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
        ydl.add_default_info_extractors()
        # _ies 是私有属性，不同 yt-dlp 版本结构可能不同
        # （有的版本键是 IE_NAME，有的是类名），两种都收。
        ies = getattr(ydl, "_ies", None) or {}
        registered = set(ies.keys())
        try:
            registered |= {cls.__name__ for cls in ies.values()}
        except Exception:
            pass
        registered = sorted(registered)

        expected = [
            "DouyinWebView", "DouyinNoteWebView", "DouyinShortLink",
            "XiaoHongShuImages", "XiaoHongShuShort", "WeChatChannels",
        ]
        found = [name for name in expected if name in registered]
        missing = [name for name in expected if name not in registered]

        details = {
            "script_dir_on_sys_path": next(
                (p for p in sys.path[:5] if p and os.path.isdir(
                    os.path.join(p, "yt_dlp_plugins"))), None),
            "registered_total": len(registered),
            "expected_found": found,
            "expected_missing": missing,
            "has_webkit_jsi": _module_available(
                "yt_dlp_plugins.webkit_jsi.lib.easy"),
        }
        ok = not missing
        text = json.dumps(details, indent=2, ensure_ascii=False)
        if missing:
            text += f"\n\n缺失: {', '.join(missing)}\n插件未加载，先查打包白名单和 sys.path。"
        return diagnostic_result(ok, text, details)
    except Exception as error:
        return diagnostic_result(
            False, f"{type(error).__name__}: {error}")


def douyin_sign_probe(argument):
    """在真机上跑一遍抖音 WebView 签名全流程，逐步报告卡在哪。

    这是唯一没法在电脑上验证的环节：桌面 Chrome 里 frontierSign 能跑通，
    但 iOS 的 WKWebView 环境自检可能不一样。装包后先跑这个：

    - webview_ok=false         → webkit_jsi 不可用（依赖没装进包）
    - sdk_not_loaded           → 拿不到 bdms SDK（网络/域名问题）
    - sign_failed              → SDK 装上了但 frontierSign 抛异常
    - http != 200 且无业务数据  → 签名算出来但服务端不认（环境值缺失）
    - aweme_detail 为空         → 签名通了，是作品本身的问题（已删/权限）
    - 全部通过                  → 拿到 playable_url 才说明真跑通了
    """
    video_id = str((argument or {}).get("video_id") or "6961737553342991651")
    probe = {"video_id": video_id, "steps": []}

    def step(name, ok, detail=""):
        probe["steps"].append({"step": name, "ok": bool(ok), "detail": str(detail)[:400]})
        return ok

    # 1) 依赖是否可用
    if not _module_available("yt_dlp_plugins.webkit_jsi.lib.easy"):
        step("import_webkit_jsi", False, "模块不存在")
        return diagnostic_result(False, json.dumps(probe, indent=2, ensure_ascii=False), probe)
    step("import_webkit_jsi", True)

    # 2) 提取器是否注册
    try:
        import yt_dlp

        ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
        ydl.add_default_info_extractors()
        ies = getattr(ydl, "_ies", None) or {}
        names = set(ies.keys())
        try:
            names |= {cls.__name__ for cls in ies.values()}
        except Exception:
            pass
        registered = ("DouyinWebView" in names) or ("douyin:webview" in names)
    except Exception as error:
        registered = False
        step("extractor_registered", False, f"{type(error).__name__}: {error}")
    else:
        step("extractor_registered", registered, "DouyinWebView 在册" if registered else "未注册")
    if not registered:
        return diagnostic_result(False, json.dumps(probe, indent=2, ensure_ascii=False), probe)

    # 3) 真正跑一次签名 + 请求
    try:
        from yt_dlp_plugins.extractor.douyin_webview import DouyinWebViewIE
    except Exception as error:
        step("import_extractor", False, f"{type(error).__name__}: {error}")
        return diagnostic_result(False, json.dumps(probe, indent=2, ensure_ascii=False), probe)
    step("import_extractor", True)

    try:
        ie = DouyinWebViewIE()
        ie.set_downloader(ydl)
        detail = ie._fetch_detail(video_id)
    except Exception as error:
        step("sign_and_fetch", False, f"{type(error).__name__}: {error}")
        return diagnostic_result(False, json.dumps(probe, indent=2, ensure_ascii=False), probe)

    if not detail:
        step("sign_and_fetch", False, "没拿到 aweme_detail（看上面的 report_warning）")
        return diagnostic_result(False, json.dumps(probe, indent=2, ensure_ascii=False), probe)

    step("sign_and_fetch", True)
    play = ((detail.get("video") or {}).get("play_addr") or {}).get("url_list") or []
    probe["result"] = {
        "desc": str(detail.get("desc"))[:60],
        "author": str((detail.get("author") or {}).get("nickname"))[:30],
        "play_url_count": len(play),
        "first_play_url": (play[0][:120] if play else None),
    }
    ok = bool(play)
    step("have_play_url", ok, f"{len(play)} 个地址" if ok else "没有播放地址")
    return diagnostic_result(ok, json.dumps(probe, indent=2, ensure_ascii=False), probe)


def _module_available(name):
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def curl_cffi_request(argument):
    from curl_cffi import requests

    url = str(argument.get("url") or "https://tls.browserleaks.com/json")
    impersonate = str(argument.get("impersonate") or "").strip() or None
    started = time.monotonic()
    response = requests.get(url, impersonate=impersonate, timeout=20)
    details = {
        "url": str(response.url),
        "status_code": response.status_code,
        "http_version": str(getattr(response, "http_version", "")),
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "impersonate": impersonate or "none",
        "headers": dict(response.headers),
    }
    ok = 200 <= response.status_code < 400
    return diagnostic_result(ok, response.text[:RESPONSE_PREVIEW_CHARS], details)


def ffmpeg_bridge(_argument):
    from .ffmpeg_bridge import SwiftFFmpegBridge

    bridge = SwiftFFmpegBridge()
    capabilities = bridge.probe_capabilities()
    with tempfile.TemporaryDirectory(prefix="palladium-debug-") as directory:
        output_path = os.path.join(directory, "bridge-test.mp4")
        encode = bridge.run_ffmpeg([
            "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=15",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "1",
            "-c:v", "mpeg4", "-c:a", "aac", output_path,
        ])
        if encode.exit_code != 0:
            error = f"ffmpeg exited with {encode.exit_code}"
            return diagnostic_result(False, encode.combined_output, capabilities, error)
        metadata = bridge.probe_metadata(output_path)

    streams = [stream.get("codec_name") for stream in metadata.get("streams", [])]
    details = {**capabilities, "encoded_streams": streams, "duration": metadata.get("format", {}).get("duration")}
    return diagnostic_result(streams == ["mpeg4", "aac"], json.dumps(details, indent=2), details)


def quickjs_bridge(argument):
    from .quickjs_bridge import QuickJSBridge

    bridge = QuickJSBridge()
    source = str(argument.get("source") or "console.log([1, 2, 3].map(value => value * 2).join(','))")
    output = bridge.evaluate(source)
    return diagnostic_result(True, output, {"version": bridge.version})


DIAGNOSTICS = {
    "python_info": python_info,
    "python_eval": python_eval,
    "curl_cffi_info": curl_cffi_info,
    "curl_cffi_request": curl_cffi_request,
    "ffmpeg_bridge": ffmpeg_bridge,
    "quickjs_bridge": quickjs_bridge,
    # 自建提取器自检：确认抖音/小红书/视频号插件在 App 内真的加载了
    "ytdlp_plugins_report": ytdlp_plugins_report,
    # 抖音签名全流程实测：这一步只能在真机上验
    "douyin_sign_probe": douyin_sign_probe,
}


def run_debug_diagnostic(name, argument_json=""):
    started = time.monotonic()
    try:
        diagnostic = DIAGNOSTICS.get(str(name))
        if diagnostic is None:
            raise ValueError(f"unknown diagnostic: {name}")
        argument = json.loads(argument_json) if argument_json else {}
        if not isinstance(argument, dict):
            raise ValueError("diagnostic argument must be a JSON object")
        result = diagnostic(argument)
    except (Exception, SystemExit) as error:
        result = diagnostic_error(error)
    result["duration_ms"] = round((time.monotonic() - started) * 1000)
    return json.dumps(result, default=str)
