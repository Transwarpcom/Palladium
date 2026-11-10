"""微信视频号提取器 —— App 内 WebView 登录元宝换 token，再拿 mp4 直链

## 为什么必须这样

视频号网页端有「扫码进微信观看」门禁：
  - 匿名访问 finder-preview 的 get_feed_info，只能拿到作者昵称/头像/
    封面/点赞数，`feedInfo.videoUrl` 恒为空（2026-11 实测确认）
  - 视频流的鉴权 token 只能由「腾讯元宝」的代解析接口下发

所以整条链路是：
  1. App 内 WebView 打开 yuanbao.tencent.com，用户微信扫码登录一次
     （会话落盘，后续复用，不用每次扫）
  2. WebView 里调 POST /api/weixin/get_parse_result
     拿到 playable_url，从中解析 token 与 eid(exportId)
  3. 再调 channels.weixin.qq.com 的 get_feed_info，
     body 传 {baseReq:{generalToken: token}, exportId: eid}
  4. feedInfo.videoUrl 就是 mp4 直链（已经是未加密的）

关键点：第 2、3 步都在 WebView 里用页面自身 origin 发请求，
cookie 自动带上，也不用处理跨域。

## 返回结构（视频）
  feedInfo.videoUrl            主地址
  feedInfo.h264VideoInfo       有则优先（兼容性最好）
  feedInfo.h265VideoInfo       HEVC，体积小
## 返回结构（图集）
  feedInfo.picInfo[].url       图片列表
  feedInfo.bgmInfo.bgmUrl      原声
"""
import json
import re
import urllib.parse

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError, traverse_obj, url_or_none


# 短链形态：https://weixin.qq.com/sph/<短ID>
# 长链形态：https://channels.weixin.qq.com/finder-preview/pages/sph?id=<短ID>
# 两条分支的组名必须不同，否则合进一个正则会报 redefinition of group name
_SHORT_RE = r'https?://weixin\.qq\.com/sph/(?P<id>[A-Za-z0-9_-]+)'
_LONG_RE = (r'https?://channels\.weixin\.qq\.com/finder-preview/pages/sph'
            r'\?(?:[^#]*&)?id=(?P<long_id>[A-Za-z0-9_-]+)')

_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
       'AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148')


def _https(url):
    """把 http 换成 https。

    Info.plist 没有 ATS 例外，App 内所有请求强制 HTTPS。
    视频号的 CDN 偶尔下发 http://，直接用会被系统拦掉。
    """
    if not url or not isinstance(url, str):
        return url
    if url.startswith('http://'):
        return 'https://' + url[len('http://'):]
    return url


_PAGE_SCRIPT_TEMPLATE = r'''
const __shortId = __SHORT_ID__;
const __shareUrl = __SHARE_URL__;
const out = {};

// ---------- 1. 确认元宝登录态 ----------
try {
  const probe = await fetch('https://yuanbao.tencent.com/api/user/info', {
    credentials: 'include',
    headers: { 'Accept': 'application/json' },
  });
  out.loginStatus = probe.status;
  if (probe.status !== 200) {
    return JSON.stringify({ __error: 'not_logged_in', __status: probe.status });
  }
} catch (e) {
  return JSON.stringify({ __error: 'probe_failed', detail: String(e) });
}

// ---------- 2. 元宝代解析，换 token + eid ----------
let parsed;
try {
  const resp = await fetch('https://yuanbao.tencent.com/api/weixin/get_parse_result', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      type: 'video_channel_url',
      url: __shareUrl,
      scene: 1,
    }),
  });
  out.parseStatus = resp.status;
  parsed = await resp.json();
} catch (e) {
  return JSON.stringify({ __error: 'parse_failed', detail: String(e) });
}
out.parsed = parsed;

const playable = parsed?.data?.playable_url || parsed?.playable_url || '';
let token = '';
let eid = '';
if (playable) {
  try {
    const u = new URL(playable, 'https://channels.weixin.qq.com');
    token = u.searchParams.get('token') || u.searchParams.get('generalToken') || '';
    eid = u.searchParams.get('eid') || u.searchParams.get('exportId') || '';
  } catch (e) { /* ignore */ }
}
if (!token) {
  // 有的版本直接给字段
  token = parsed?.data?.token || parsed?.token || '';
  eid = eid || parsed?.data?.export_id || parsed?.data?.exportId || '';
}
out.token = token;
out.eid = eid;
if (!token) {
  // 把原始响应一起带回去，方便判断是「未登录」「接口改了」还是「作品无权限」
  return JSON.stringify({
    __error: 'no_token',
    __parseStatus: out.parseStatus,
    __playable: String(playable).slice(0, 300),
    parsed: parsed,
  });
}

// ---------- 3. 拿 feedInfo ----------
let feed;
try {
  const pageUrl = 'https://channels.weixin.qq.com/finder-preview/pages/sph?id=' + __shortId;
  const apiUrl = 'https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info'
    + '?_rid=' + Date.now()
    + '&_pageUrl=' + encodeURIComponent(pageUrl);
  const resp2 = await fetch(apiUrl, {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      'Referer': pageUrl,
    },
    body: JSON.stringify({
      baseReq: { generalToken: token },
      exportId: eid,
      shortUri: __shortId,
    }),
  });
  out.feedStatus = resp2.status;
  feed = await resp2.json();
} catch (e) {
  return JSON.stringify({ __error: 'feed_failed', detail: String(e) });
}

return JSON.stringify({ __ok: true, feed: feed });
'''


class WeChatChannelsIE(InfoExtractor):
    """微信视频号（短链 / H5 预览长链）。

    依赖 App 内 WebView 里已登录的元宝会话。没登录时会明确报错，
    提示用户先在「常用平台登录」里扫码。
    """

    IE_NAME = 'wechat:channels'
    IE_DESC = '微信视频号（WebView 元宝登录）'
    _VALID_URL = rf'(?:{_SHORT_RE}|{_LONG_RE})'
    _TESTS = [{
        'url': 'https://weixin.qq.com/sph/AzGrUgqzFv',
        'info_dict': {'id': 'AzGrUgqzFv', 'ext': 'mp4', 'title': 're:.+'},
        'skip': '需要 App 内 WebView 元宝登录态',
    }]

    def _real_extract(self, url):
        m = re.match(_SHORT_RE, url) or re.match(_LONG_RE, url)
        short_id = m.group('id') or m.group('long_id')
        share_url = f'https://weixin.qq.com/sph/{short_id}'

        data = self._fetch_via_webview(short_id, share_url)

        feed_data = data.get('feed') or {}
        if feed_data.get('errCode') not in (0, None):
            self.report_warning(
                f"视频号返回错误码 {feed_data.get('errCode')}: "
                f"{feed_data.get('errMsg')}")

        # feedInfo 可能在 data.feedInfo，也可能直接在顶层（接口版本差异），
        # 依次尝试，避免结构一变就全盘失败。
        feed = (
            traverse_obj(feed_data, ('data', 'feedInfo'))
            or traverse_obj(feed_data, ('feedInfo',))
            or traverse_obj(feed_data, ('data',))
            or {}
        )
        if not isinstance(feed, dict):
            feed = {}
        author = (
            traverse_obj(feed_data, ('data', 'authorInfo', 'nickname', {str}))
            or traverse_obj(feed_data, ('authorInfo', 'nickname', {str}))
            or ''
        )
        desc = traverse_obj(feed, ('description', {str})) or ''

        # 视频：h264 优先（兼容性最好），其次主地址，最后 h265
        formats = []
        for key, fid in (('h264VideoInfo', 'h264'),
                         ('videoUrl', 'default'),
                         ('h265VideoInfo', 'h265')):
            node = feed.get(key)
            video_url = None
            if isinstance(node, str):
                video_url = url_or_none(node)
            elif isinstance(node, dict):
                video_url = url_or_none(node.get('videoUrl'))
            if video_url:
                formats.append({
                    'url': _https(video_url),
                    'format_id': fid,
                    'ext': 'mp4',
                    'http_headers': {
                        'User-Agent': _UA,
                        'Referer': 'https://channels.weixin.qq.com/',
                    },
                })

        # 图集作品：没有视频，只有 picInfo
        images = []
        for item in traverse_obj(feed, ('picInfo', ...)) or []:
            u = url_or_none(traverse_obj(item, ('url', {str})))
            if u:
                images.append(_https(u))

        title = (desc or author or short_id).strip()[:200]

        # 封面 / 原声
        cover = _https(url_or_none(traverse_obj(feed, ('coverUrl', {str}))))
        bgm = _https(url_or_none(traverse_obj(feed, ('bgmInfo', 'bgmUrl', {str}))))

        if images and not formats:
            # 图集：做成 playlist，和抖音/小红书图文一致
            entries = [{
                'id': f'{short_id}_{i}',
                'title': f'{title[:60]}_{i + 1}',
                'formats': [{
                    'url': u,
                    'ext': 'jpg',
                    'format_id': 'image',
                }],
                'http_headers': {'Referer': 'https://channels.weixin.qq.com/'},
            } for i, u in enumerate(images)]
            info = {
                'id': short_id,
                '_type': 'playlist',
                'title': title,
                'description': desc,
                'uploader': author,
                'entries': entries,
            }
            if cover:
                info['thumbnails'] = [{'url': cover}]
            return info

        if not formats:
            raise ExtractorError(
                '视频号没解析到视频或图片。可能原因：'
                '(1) 元宝登录已过期，去「常用平台登录」重新扫码；'
                '(2) 这条作品设了权限；'
                '(3) 接口结构又变了。', expected=True)

        info = {
            'id': short_id,
            'title': title,
            'description': desc,
            'uploader': author,
            'formats': formats,
            'http_headers': {'Referer': 'https://channels.weixin.qq.com/'},
        }
        if cover:
            info['thumbnails'] = [{'url': cover}]
        if bgm:
            info['_bgm_url'] = bgm
        return info

    def _fetch_via_webview(self, short_id, share_url):
        """在已登录元宝的 WKWebView 里走完整个解析流程。"""
        try:
            from yt_dlp_plugins.webkit_jsi.lib.easy import (
                WKJSE_Factory, WKJSE_Webview, jsres_to_json)
            from yt_dlp_plugins.webkit_jsi.lib.logging import (
                DefaultLoggerImpl)
        except Exception as e:
            raise ExtractorError(
                '缺少 yt-dlp-apple-webkit-jsi，无法在 App 内解析视频号。'
                f'（{e}）', expected=True)

        script = (_PAGE_SCRIPT_TEMPLATE
                  .replace('__SHORT_ID__', json.dumps(short_id))
                  .replace('__SHARE_URL__', json.dumps(share_url)))

        html = '<!DOCTYPE html><html><head></head><body></body></html>'
        logger = DefaultLoggerImpl()
        try:
            with WKJSE_Factory(logger) as send, WKJSE_Webview(send) as wv:
                # baseURL 用元宝域名，这样 fetch 的 credentials 能带上元宝 cookie
                wv.navigate_to('https://yuanbao.tencent.com/', html)
                raw = jsres_to_json(wv.execute_js(script))
        except Exception as e:
            raise ExtractorError(f'WebView 执行失败: {e}', expected=True)

        if not raw:
            raise ExtractorError('视频号 WebView 没有返回数据', expected=True)

        try:
            payload = json.loads(raw)
        except Exception:
            raise ExtractorError(
                f'视频号 WebView 返回了非 JSON: {str(raw)[:200]}', expected=True)

        err = payload.get('__error')
        if err == 'not_logged_in':
            raise ExtractorError(
                '还没登录腾讯元宝。请到「设置 → Cookies → 常用平台登录」'
                '选「腾讯元宝」扫码登录一次（之后会一直有效）。', expected=True)
        if err == 'no_token':
            # 带上详情：没有 playable_url 通常是登录态过期或作品无权限
            status = payload.get('__parseStatus')
            playable = payload.get('__playable') or ''
            parsed = str(payload.get('parsed', ''))[:300]
            raise ExtractorError(
                f'元宝没给回 token（HTTP {status}）。'
                '可能性：元宝登录已过期（重新扫码）、这条作品需要权限、'
                '或元宝接口改版。'
                f'原始响应：playable_url={playable or "空"}，{parsed}',
                expected=True)
        if err:
            raise ExtractorError(
                f"视频号解析失败：{err} {payload.get('detail', '')} "
                f"{str(payload.get('parsed', ''))[:200]}", expected=True)

        return payload
