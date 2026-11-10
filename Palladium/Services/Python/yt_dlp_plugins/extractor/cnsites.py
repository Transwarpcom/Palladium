"""国内平台补充提取器（Palladium fork）

放在 yt_dlp_plugins/extractor/ 下，由 yt-dlp 插件机制自动加载。
加载路径是 sys.path 里的 yt_dlp_plugins 目录（Palladium 把 Python 脚本
目录插到 sys.path[0]，正好命中）。

覆盖 yt-dlp 原生缺失的部分：
  抖音     v.douyin.com 短链、图文作品（/note/）
  小红书   xhslink.com 短链、图文笔记（纯图片）

原生 DouyinIE 只认 /video/<纯数字>，XiaoHongShuIE 只认
/explore|discovery/item/<十六进制>，两边都不认短链，也都不认图文。
"""
import re
import urllib.parse

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError, js_to_json, traverse_obj, url_or_none


# ---------------------------------------------------------------- 抖音

class DouyinShortLinkIE(InfoExtractor):
    """v.douyin.com 短链：跟随跳转拿真实 ID，再交给原生 DouyinIE。"""

    IE_NAME = 'douyin:short'
    IE_DESC = '抖音短链（v.douyin.com）'
    _VALID_URL = r'https?://v\.douyin\.com/(?P<id>[\w\-]+)'
    _TESTS = [{'url': 'https://v.douyin.com/iRNBho6u/', 'only_matching': True}]

    def _real_extract(self, url):
        video_id = self._match_id(url)
        urlh = self._request_webpage(
            url, video_id, '展开短链', fatal=False)
        final_url = urlh.url if urlh else ''
        self.write_debug(f'短链展开结果: {final_url}')

        real_id = None
        for pattern in (r'/share/video/(\d+)', r'/video/(\d+)',
                        r'/share/slides/(\d+)', r'[?&]mid=(\d+)'):
            m = re.search(pattern, final_url)
            if m:
                real_id = m.group(1)
                break

        if not real_id:
            raise ExtractorError(
                f'短链展开后没找到视频 ID（跳转到 {final_url}）', expected=True)

        return self.url_result(
            f'https://www.douyin.com/video/{real_id}',
            ie='DouyinWebView', video_id=real_id)


class DouyinNoteIE(InfoExtractor):
    """抖音图文作品（/note/<id>）：原生 DouyinIE 不认这个路径。

    2026-11 实测：iesdouyin 分享页已经不再注入 _ROUTER_DATA 里的
    item_list（play_addr 出现 0 次），所以这里不再自己解析，
    直接转给 DouyinWebViewIE —— 它用 App 内 WKWebView 跑官方 SDK
    签名，能拿到正常的 aweme_detail（图文作品的 images 字段在里面）。
    """

    IE_NAME = 'douyin:note'
    IE_DESC = '抖音图文作品'
    _VALID_URL = r'https?://(?:www\.)?douyin\.com/note/(?P<id>\d+)'
    _TESTS = [{
        'url': 'https://www.douyin.com/note/7123456789012345678',
        'only_matching': True,
    }]

    def _real_extract(self, url):
        note_id = self._match_id(url)
        return self.url_result(
            f'https://www.douyin.com/video/{note_id}',
            ie='DouyinNoteWebView', video_id=note_id)


# ---------------------------------------------------------------- 小红书


def _strip_xhs_resize(url):
    """去掉小红书 CDN 的缩放/裁剪参数，拿原图。

    sns-img.xhscdn.com 的图片地址后面常带处理后缀，形如：
      ...jpg?imageView2/2/w/1080/format/webp
      ...jpg!nd_dft_wlteh_webp_3
    这些会让服务端返回压缩版。去掉后拿到原始尺寸。

    只做保守处理：删掉 query 里的 imageView2 参数段、删掉 ! 开头的
    处理后缀。不确定的形态原样返回，宁可拿压缩图也不要 404。
    """
    if not url:
        return url
    out = url.replace('http://', 'https://', 1)

    # 去掉 ! 后面的处理后缀（小红书常见的写法）
    bang = out.find('!')
    if bang > 0:
        out = out[:bang]

    # 去掉 imageView2 这类查询参数
    if '?' in out:
        base, _, query = out.partition('?')
        kept = [p for p in query.split('&')
                if p and not p.startswith(('imageView2', 'imageMogr2'))]
        out = base + ('?' + '&'.join(kept) if kept else '')

    return out


class XiaoHongShuShortIE(InfoExtractor):
    """xhslink.com / xhslink.cn 短链（小红书 App 分享的默认形式）。"""

    IE_NAME = 'xiaohongshu:short'
    IE_DESC = '小红书短链'
    _VALID_URL = r'https?://(?:xhslink\.com|xhslink\.cn)/(?P<id>[\w\-]+)'
    _TESTS = [{
        'url': 'https://xhslink.com/a/abcdefg',
        'only_matching': True,
    }, {
        'url': 'https://xhslink.com/o/abcdefg',
        'only_matching': True,
    }]

    def _real_extract(self, url):
        short_id = self._match_id(url)
        urlh = self._request_webpage(
            url, short_id, '展开短链', fatal=False)
        final_url = urlh.url if urlh else ''
        self.write_debug(f'短链展开结果: {final_url}')

        m = re.search(r'/(?:explore|discovery/item)/([\da-f]+)', final_url)
        if not m:
            raise ExtractorError(
                f'短链展开后没找到笔记 ID（跳转到 {final_url}）', expected=True)

        note_id = m.group(1)

        # 关键：把展开后 URL 里的 xsec_token 原样带过去。
        # 不带的话服务端会重定向到 /404，之前短链解析一直失败就是这个原因。
        query = urllib.parse.parse_qs(urllib.parse.urlparse(final_url).query)
        xsec_token = (query.get('xsec_token') or [''])[0]
        xsec_source = (query.get('xsec_source') or ['pc_feed'])[0]

        target = f'https://www.xiaohongshu.com/explore/{note_id}'
        if xsec_token:
            target += (f'?xsec_token={urllib.parse.quote(xsec_token, safe="")}'
                       f'&xsec_source={urllib.parse.quote(xsec_source, safe="")}')
            self.write_debug(f'携带 xsec_token: {xsec_token[:16]}…')
        else:
            self.write_debug('展开后的 URL 里没有 xsec_token')

        # 交给自己的图片提取器，不是原生的 XiaoHongShu。
        # 原生只认 note.video.media.stream，图文笔记没有 video 字段，
        # 会直接抛 "No video formats found"。而 XiaoHongShuImagesIE
        # 内部会判断：有视频就转回原生，没视频才走 imageList。
        return self.url_result(target, ie='XiaoHongShuImages', video_id=note_id)


class XiaoHongShuImagesIE(InfoExtractor):
    """小红书图文笔记（纯图片）。

    原生 XiaoHongShuIE 只遍历 note.video.media.stream，纯图片笔记没有
    video 字段，formats 为空，抛 No video formats found。
    这里单独解析 note.imageList。
    """

    IE_NAME = 'xiaohongshu:images'
    IE_DESC = '小红书图文笔记'
    _VALID_URL = r'https?://(?:www\.)?xiaohongshu\.com/(?:explore|discovery/item)/(?P<id>[\da-f]+)'
    _TESTS = [{
        'url': 'https://www.xiaohongshu.com/explore/6ad78427000000000c00f895',
        'only_matching': True,
    }, {
        'url': 'https://www.xiaohongshu.com/discovery/item/6ad78427000000000c00f895'
               '?xsec_token=ABC&xsec_source=pc_feed',
        'only_matching': True,
    }]

    def _real_extract(self, url):
        note_id = self._match_id(url)
        # 分享链接里的 xsec_token 决定能不能访问，必须原样带过去
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        xsec_token = (query.get('xsec_token') or [''])[0]
        xsec_source = (query.get('xsec_source') or ['pc_feed'])[0]

        fetch_url = url
        if not xsec_token:
            # 没有 token 时先访问首页拿一个，否则大概率被重定向到 /404
            self.write_debug('链接里没有 xsec_token，尝试从首页取')
            home = self._download_webpage(
                'https://www.xiaohongshu.com/explore', note_id,
                '获取首页 token', fatal=False) or ''
            m = re.search(
                r'xsec_token["\']?\s*[:=]\s*["\']([^"\']+)', home)
            if m:
                xsec_token = m.group(1)
                self.write_debug(f'拿到 xsec_token: {xsec_token[:16]}…')
            else:
                # 2026-11 实测：登录后 explore 首页只有 40KB 的 SPA 外壳，
                # 数据走 XHR（需要 x-s 签名），页面里根本没有 xsec_token。
                # 与其下去撞 404 让人困惑，不如直接说清怎么拿对的链接。
                raise ExtractorError(
                    '小红书链接缺 xsec_token，打开后会被重定向到 404。'
                    '正确做法：在小红书 App 里点「分享 → 复制链接」，'
                    '得到的是 xhslink.com 短链（本插件会自动展开并带上 token），'
                    '或者带 xsec_token 参数的完整链接。'
                    '直接复制浏览器地址栏的 explore/<id> 通常不带 token。',
                    expected=True)
        if xsec_token:
            fetch_url = (f'https://www.xiaohongshu.com/explore/{note_id}'
                         f'?xsec_token={xsec_token}&xsec_source={xsec_source}')

        webpage = self._download_webpage(fetch_url, note_id, '下载笔记页')

        initial_state = self._search_json(
            r'(?:window\.)?__INITIAL_STATE__\s*=', webpage, 'initial state',
            note_id, transform_source=js_to_json, fatal=False)
        if not initial_state:
            raise ExtractorError(
                '页面里没有 __INITIAL_STATE__，可能需要 cookies', expected=True)

        note = traverse_obj(initial_state, ('note', 'noteDetailMap', note_id, 'note'))
        if not note:
            raise ExtractorError(
                '没解析到笔记内容。小红书对未登录请求会重定向到 /404，'
                '请在 Palladium 设置里导入 cookies（浏览器登录后导出 xiaohongshu.com 的 cookies.txt）',
                expected=True)

        # 有视频就让原生提取器处理
        if traverse_obj(note, ('video', 'media', 'stream')):
            return self.url_result(url, ie='XiaoHongShu', video_id=note_id)

        # 优先取原图，其次默认图。imageList 里不同笔记字段不同，
        # 按质量从高到低依次尝试。
        raw_images = traverse_obj(note, ('imageList', ...)) or []
        images = []
        for item in raw_images:
            if not isinstance(item, dict):
                continue
            for key in ('urlDefault', 'urlPre', 'url'):
                v = item.get(key)
                if url_or_none(v):
                    images.append(_strip_xhs_resize(v))
                    break
        if not images:
            raise ExtractorError('这条笔记没有图片也没有视频', expected=True)

        desc = traverse_obj(note, ('desc', {str})) or ''
        title = traverse_obj(note, ('title', {str})) or desc[:60] or note_id
        uploader = traverse_obj(note, ('user', 'nickname', {str})) or ''

        return {
            'id': note_id,
            '_type': 'playlist',
            'title': title.strip()[:200],
            'description': desc,
            'uploader': uploader,
            'thumbnails': [{'url': u} for u in images],
            'entries': [{
                'id': f'{note_id}_{i}',
                'title': f'{title[:60]}_{i + 1}',
                'formats': [{
                    'url': u,
                    'ext': 'jpg',
                    'format_id': 'image',
                }],
                'http_headers': {'Referer': 'https://www.xiaohongshu.com/'},
            } for i, u in enumerate(images)],
        }
