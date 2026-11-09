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
            ie='Douyin', video_id=real_id)


class DouyinNoteIE(InfoExtractor):
    """抖音图文作品（/note/<id>）：原生 DouyinIE 不认这个路径。"""

    IE_NAME = 'douyin:note'
    IE_DESC = '抖音图文作品'
    _VALID_URL = r'https?://(?:www\.)?douyin\.com/note/(?P<id>\d+)'
    _TESTS = []

    def _real_extract(self, url):
        note_id = self._match_id(url)
        webpage = self._download_webpage(
            f'https://www.iesdouyin.com/share/slides/{note_id}/',
            note_id, '下载图文分享页', fatal=False) or ''

        router = self._search_json(
            r'(?:window\.)?_ROUTER_DATA\s*=', webpage, 'router data', note_id,
            transform_source=js_to_json, fatal=False)

        item = None
        if isinstance(router, dict):
            for value in (router.get('loaderData') or {}).values():
                if not isinstance(value, dict):
                    continue
                for key in ('videoInfoRes', 'video_info_res'):
                    res = value.get(key)
                    if isinstance(res, dict):
                        items = res.get('item_list') or res.get('itemList') or []
                        if items:
                            item = items[0]
                            break
                if item:
                    break

        if not item:
            raise ExtractorError(
                '图文页面没解析出内容（可能需要 cookies，或页面结构已变）',
                expected=True)

        images = [u for u in (traverse_obj(item, ('images', ..., 'url_list', 0)) or [])
                  if url_or_none(u)]
        cleaned = [re.sub(r'[?&](?:watermark|wm)=\d+', '', u) for u in images]
        if not cleaned:
            raise ExtractorError('这个图文作品没有可用图片', expected=True)

        desc = traverse_obj(item, ('desc', {str})) or f'douyin_{note_id}'
        author = traverse_obj(item, ('author', 'nickname', {str})) or ''

        return {
            'id': note_id,
            'title': desc.strip()[:200] or note_id,
            'description': desc,
            'uploader': author,
            'thumbnails': [{'url': u} for u in cleaned],
            'entries': [{
                'id': f'{note_id}_{i}',
                'title': f'{desc[:60]}_{i + 1}',
                'url': u,
                'ext': 'jpg',
                'http_headers': {'Referer': 'https://www.douyin.com/'},
            } for i, u in enumerate(cleaned)],
        }


# ---------------------------------------------------------------- 小红书

class XiaoHongShuShortIE(InfoExtractor):
    """xhslink.com / xhslink.cn 短链（小红书 App 分享的默认形式）。"""

    IE_NAME = 'xiaohongshu:short'
    IE_DESC = '小红书短链'
    _VALID_URL = r'https?://(?:xhslink\.com|xhslink\.cn)/(?P<id>[\w\-]+)'
    _TESTS = []

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
        return self.url_result(
            f'https://www.xiaohongshu.com/explore/{note_id}',
            ie='XiaoHongShu', video_id=note_id)


class XiaoHongShuImagesIE(InfoExtractor):
    """小红书图文笔记（纯图片）。

    原生 XiaoHongShuIE 只遍历 note.video.media.stream，纯图片笔记没有
    video 字段，formats 为空，抛 No video formats found。
    这里单独解析 note.imageList。
    """

    IE_NAME = 'xiaohongshu:images'
    IE_DESC = '小红书图文笔记'
    _VALID_URL = r'https?://(?:www\.)?xiaohongshu\.com/(?:explore|discovery/item)/(?P<id>[\da-f]+)'
    _TESTS = []

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

        images = [u for u in (traverse_obj(note, ('imageList', ..., 'urlDefault')) or [])
                  if url_or_none(u)]
        if not images:
            raise ExtractorError('这条笔记没有图片也没有视频', expected=True)

        desc = traverse_obj(note, ('desc', {str})) or ''
        title = traverse_obj(note, ('title', {str})) or desc[:60] or note_id
        uploader = traverse_obj(note, ('user', 'nickname', {str})) or ''

        return {
            'id': note_id,
            'title': title.strip()[:200],
            'description': desc,
            'uploader': uploader,
            'thumbnails': [{'url': u} for u in images],
            'entries': [{
                'id': f'{note_id}_{i}',
                'title': f'{title[:60]}_{i + 1}',
                'url': u,
                'ext': 'jpg',
                'http_headers': {'Referer': 'https://www.xiaohongshu.com/'},
            } for i, u in enumerate(images)],
        }
