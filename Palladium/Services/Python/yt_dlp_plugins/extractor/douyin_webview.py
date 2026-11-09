"""抖音提取器 —— 用 WKWebView 跑官方 SDK 计算 X-Bogus 签名

背景（2026-11 实测）：
  抖音 Web API 现在要求 X-Bogus 签名，由官方 bdms_1.0.1.19_fix.js 生成。
  a_bogus 那套（f2 / ylcangel / JoeanAmier 的 Python 移植）全部已失效——
  实测签名长度、pageId 常量都不对，带上去一律 403。

  真正的签名入口是官方 SDK 暴露的 window.byted_acrawler.frontierSign(path)，
  它返回 {"X-Bogus": "..."}。这个函数做了大量浏览器环境自检
  （navigator、performance、canvas、webdriver 检测等），
  纯 Python / Node 沙箱复现不出来，必须在真实 WebKit 里跑。

方案：
  1. 用 yt-dlp-apple-webkit-jsi 提供的 WKJSE_Webview 建一个真实 WKWebView
  2. 用 loadHTMLString:baseURL: 把 baseURL 设成 https://www.douyin.com/，
     这样 document.origin 正确，SDK 的环境检测能过
  3. 页面里注入 bdms SDK 脚本（从 CDN 拉，和抖音线上完全同一个文件）
  4. 页面内 fetch 抖音 API（credentials: include 自动带 WebView 的 cookie）
  5. 把 JSON 结果传回 Python
"""
import json
import re
import urllib.parse

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError, traverse_obj, url_or_none


# 抖音线上实际使用的 SDK（2026-11 抓自 www.douyin.com）
# 版本随抖音更新会变，这里按顺序尝试，谁先成功用谁
BDMS_SDK_URLS = [
    'https://p-ppc-weboff.byteimg.com/tos-cn-i-9r5gewecjs/bdms_1.0.1.19_fix.js',
    'https://lf-c-flwb.bytetos.com/obj/rc-client-security/webmssdk/1.0.0.20/webmssdk.es5.js',
]

# 抖音网页版请求的固定参数。这些值必须和 SDK 自检的环境一致，
# 否则 frontierSign 内部的环境校验会给出不同的签名。
_COMMON_PARAMS = {
    'device_platform': 'webapp',
    'aid': '6383',
    'channel': 'channel_pc_web',
    'pc_client_type': '1',
    'pc_libra_divert': 'Windows',
    'support_h265': '1',
    'support_dash': '1',
    'version_code': '170400',
    'version_name': '17.4.0',
    'cookie_enabled': 'true',
    'screen_width': '2560',
    'screen_height': '1440',
    'browser_language': 'zh-CN',
    'browser_platform': 'Win32',
    'browser_name': 'Chrome',
    'browser_version': '135.0.0.0',
    'browser_online': 'true',
    'engine_name': 'Blink',
    'engine_version': '135.0.0.0',
    'os_name': 'Windows',
    'os_version': '10',
    'cpu_core_num': '20',
    'device_memory': '8',
    'platform': 'PC',
    'downlink': '0.55',
    'effective_type': '3g',
    'round_trip_time': '500',
}


def _build_query(extra):
    """拼查询串，保持固定参数的顺序（签名对顺序敏感）。"""
    parts = [f'{k}={v}' for k, v in _COMMON_PARAMS.items()]
    parts.extend(f'{k}={v}' for k, v in extra.items())
    return '&'.join(parts)


# 页面内执行的 JS：加载 SDK → 签名 → fetch → 返回 JSON
_PAGE_SCRIPT_TEMPLATE = r'''
const __sdkUrls = __SDK_URLS__;
const __apiPath = __API_PATH__;

// 1. 依次尝试加载 SDK，直到 window.byted_acrawler 出现
for (const u of __sdkUrls) {
  try {
    await new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = u;
      s.onload = resolve;
      s.onerror = () => reject(new Error('load fail'));
      document.head.appendChild(s);
    });
    if (window.byted_acrawler) break;
  } catch (e) { /* 换下一个 */ }
}
if (!window.byted_acrawler) {
  return JSON.stringify({__error: 'sdk_not_loaded'});
}
if (typeof window.byted_acrawler.init === 'function') {
  try { window.byted_acrawler.init({ aid: 6383, dfp: false, pageId: 6241 }); } catch (e) {}
}

// 2. 算签名
let sig;
try {
  sig = window.byted_acrawler.frontierSign(__apiPath);
} catch (e) {
  return JSON.stringify({__error: 'sign_failed', detail: String(e)});
}
const qs = Object.keys(sig).map(k => k + '=' + encodeURIComponent(sig[k])).join('&');

// 3. 发请求（credentials: include 会带上 WebView 里的抖音 cookie）
let resp, text;
try {
  resp = await fetch(__apiPath + '&' + qs, {
    credentials: 'include',
    headers: { 'Accept': 'application/json, text/plain, */*' },
  });
  text = await resp.text();
} catch (e) {
  return JSON.stringify({__error: 'fetch_failed', detail: String(e)});
}
return JSON.stringify({__status: resp.status, __body: text});
'''


class DouyinWebViewIE(InfoExtractor):
    """抖音视频 —— 走 WKWebView 里的官方 SDK 签名。

    这是目前唯一能在 iOS 上跑通的路子：
    纯 Python / Node 的 a_bogus 实现全部已失效（实测一律 403），
    而官方 SDK 的环境自检无法在沙箱里复现。
    """

    IE_NAME = 'douyin:webview'
    IE_DESC = '抖音（WKWebView 官方 SDK 签名）'
    _VALID_URL = r'https?://(?:www\.)?douyin\.com/video/(?P<id>\d+)'
    _TESTS = [{
        'url': 'https://www.douyin.com/video/6961737553342991651',
        'info_dict': {
            'id': '6961737553342991651',
            'ext': 'mp4',
            'title': 're:.+',
        },
        'skip': '需要 iOS WKWebView 环境',
    }]

    def _real_extract(self, url):
        video_id = self._match_id(url)
        detail = self._fetch_detail(video_id)

        if not detail:
            raise ExtractorError(
                '抖音没返回视频数据。可能原因：作品已删除/仅自己可见，'
                '或 WebView 环境没初始化好。', expected=True)

        desc = traverse_obj(detail, ('desc', {str})) or f'douyin_{video_id}'
        author = traverse_obj(detail, ('author', 'nickname', {str})) or ''
        duration = traverse_obj(detail, ('video', 'duration', {int}))
        if duration:
            duration = duration // 1000  # 抖音给的是毫秒

        # 优先用 bit_rate 里的多档清晰度，回退到 play_addr
        formats = []
        for br in traverse_obj(detail, ('video', 'bit_rate', ...)) or []:
            play_urls = traverse_obj(br, ('play_addr', 'url_list', ...)) or []
            play_urls = [u for u in play_urls if url_or_none(u)]
            if not play_urls:
                continue
            gear = traverse_obj(br, ('gear_name', {str})) or 'unknown'
            bitrate = traverse_obj(br, ('bit_rate', {int}))
            height = None
            m = re.search(r'(\d{3,4})', gear)
            if m:
                height = int(m.group(1))
            formats.append({
                'url': play_urls[0],
                'format_id': gear,
                'ext': 'mp4',
                'height': height,
                'tbr': bitrate // 1000 if bitrate else None,
                'http_headers': {
                    'Referer': 'https://www.douyin.com/',
                    'User-Agent': self.get_param('http_headers', {}).get(
                        'User-Agent', _COMMON_PARAMS['browser_version']),
                },
            })

        if not formats:
            play_urls = traverse_obj(detail, ('video', 'play_addr', 'url_list', ...)) or []
            for i, u in enumerate(u for u in play_urls if url_or_none(u)):
                formats.append({
                    'url': u,
                    'format_id': f'play_{i}',
                    'ext': 'mp4',
                    'http_headers': {'Referer': 'https://www.douyin.com/'},
                })

        if not formats:
            raise ExtractorError('没解析到播放地址（作品可能需要登录才能看）', expected=True)


        # 图文作品：images 字段有图
        images = traverse_obj(detail, ('images', ..., 'url_list', 0)) or []

        return {
            'id': video_id,
            'title': desc.strip()[:200] or video_id,
            'description': desc,
            'uploader': author,
            'duration': duration,
            'formats': formats,
            'thumbnails': [
                {'url': u} for u in (traverse_obj(
                    detail, ('video', 'cover', 'url_list', ...)) or [])
                if url_or_none(u)
            ],
            'http_headers': {'Referer': 'https://www.douyin.com/'},
            '__images': images,
        }

    def _fetch_detail(self, video_id):
        """在 WKWebView 里签名并请求 detail API，返回 aweme_detail 字典。"""
        api_path = '/aweme/v1/web/aweme/detail/?' + _build_query(
            {'aweme_id': video_id})

        script = (_PAGE_SCRIPT_TEMPLATE
                  .replace('__SDK_URLS__', json.dumps(BDMS_SDK_URLS))
                  .replace('__API_PATH__', json.dumps(api_path)))

        raw = self._run_in_webview(script)
        if not raw:
            return None

        try:
            payload = json.loads(raw)
        except Exception:
            self.report_warning(f'WebView 返回了非 JSON 内容: {str(raw)[:200]}')
            return None

        if '__error' in payload:
            self.report_warning(f"WebView 内签名失败: {payload['__error']} "
                                f"{payload.get('detail', '')}")
            return None

        status = payload.get('__status')
        body = payload.get('__body') or ''
        if status != 200:
            self.report_warning(f'抖音 API 返回 HTTP {status}')
            return None

        try:
            data = json.loads(body)
        except Exception:
            self.report_warning(f'抖音返回了非 JSON: {body[:200]}')
            return None

        detail = data.get('aweme_detail')
        if not detail:
            filter_info = data.get('filter_detail') or {}
            msg = filter_info.get('detail_msg') or '没有 aweme_detail'
            self.report_warning(f'抖音没给视频数据: {msg}')
            return None
        return detail

    def _run_in_webview(self, script):
        """在 WKWebView（baseURL 为 douyin.com）里执行 JS 并拿返回值。

        只有在 iOS 上、且 yt-dlp-apple-webkit-jsi 已安装时才能用。
        """
        try:
            from yt_dlp_plugins.webkit_jsi.lib.easy import (
                WKJSE_Factory, WKJSE_Webview, jsres_to_json)
            from yt_dlp_plugins.webkit_jsi.lib.logging import (
                DefaultLoggerImpl)
        except Exception as e:
            raise ExtractorError(
                '缺少 yt-dlp-apple-webkit-jsi，无法在 App 内计算抖音签名。'
                f'（{e}）', expected=True)

        html = '<!DOCTYPE html><html><head></head><body></body></html>'
        logger = DefaultLoggerImpl()
        try:
            with WKJSE_Factory(logger) as send, WKJSE_Webview(send) as wv:
                # baseURL 必须是 douyin.com，否则 SDK 的 origin 检测不过
                wv.navigate_to('https://www.douyin.com/', html)
                result = wv.execute_js(script)
                return jsres_to_json(result)
        except Exception as e:
            raise ExtractorError(
                f'WebView 执行失败: {e}', expected=True)


class DouyinNoteWebViewIE(DouyinWebViewIE):
    """抖音图文作品（/note/<id>）——复用视频提取器，images 走 entries。"""

    IE_NAME = 'douyin:note:webview'
    IE_DESC = '抖音图文作品（WebView 签名）'
    _VALID_URL = r'https?://(?:www\.)?douyin\.com/note/(?P<id>\d+)'
    _TESTS = []

    def _real_extract(self, url):
        info = super()._real_extract(url)
        images = info.pop('__images', None) or []
        if images:
            info['_type'] = 'playlist'
            info['entries'] = [{
                'id': f"{info['id']}_{i}",
                'title': f"{info['title'][:60]}_{i + 1}",
                'formats': [{'url': u, 'ext': 'jpg', 'format_id': 'image'}],
                'http_headers': {'Referer': 'https://www.douyin.com/'},
            } for i, u in enumerate(images)]
            info.pop('formats', None)
        return info
