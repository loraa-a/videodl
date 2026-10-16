import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import xinpianchang as xpc


class Response(io.BytesIO):
    def __init__(self, content, headers=None):
        super().__init__(content)
        self.headers = headers or {}


class SimpleDownloaderTests(unittest.TestCase):
    def test_extracts_work_from_share_text(self):
        self.assertEqual(xpc.article_id('分享作品 https://www.xinpianchang.com/a13019115?from=search'), '13019115')
        self.assertEqual(xpc.article_id('www.xinpianchang.com/a13019115/'), '13019115')

    def test_rejects_other_domains_and_stock(self):
        for url in ('https://www.xinpianchang.com.evil.test/a123', 'https://stock.xinpianchang.com/a123', 'https://example.com/a123'):
            with self.subTest(url=url), self.assertRaises(xpc.DownloadError):
                xpc.article_id(url)

    def test_resolves_without_requesting_the_blocked_web_page(self):
        article = {'video': {'vid': 'video-123', 'appKey': 'key-123'}, 'title': '作品'}
        media = {'resource': {'progressive': [{'url': 'https://cdn.example/video.mp4', 'quality': '720p'}]}}
        with patch.object(xpc, 'request_json', side_effect=[article, media]) as fetch:
            result = xpc.resolve_video('https://www.xinpianchang.com/a123')
        self.assertEqual(fetch.call_args_list[0].args[0], 'https://app.xinpianchang.com/article/123')
        self.assertTrue(fetch.call_args_list[1].args[0].startswith('https://mod-api.xinpianchang.com/mod/api/v2/media/video-123?'))
        self.assertEqual(result['title'], '作品')

    def test_selects_highest_available_quality_and_backup(self):
        resources = {'progressive': [
            {'url': '', 'width': 3840, 'height': 2160},
            {'url': 'https://cdn.example/360.mp4', 'width': 640, 'height': 360},
            {'backupUrl': 'https://cdn.example/720.mp4', 'width': 1280, 'height': 720},
        ]}
        self.assertEqual(xpc.select_resource(resources)['url'], 'https://cdn.example/720.mp4')

    def test_selects_hls_when_no_mp4_is_available(self):
        resource = xpc.select_resource({'progressive': [], 'hls': {'url': 'https://cdn.example/index.m3u8'}})
        self.assertTrue(resource['stream'])

    def test_reports_when_no_playable_resource_is_available(self):
        with self.assertRaises(xpc.DownloadError):
            xpc.select_resource({'progressive': [{'url': '', 'backupUrl': ''}]})

    def test_sanitizes_windows_names(self):
        self.assertEqual(xpc.safe_title('CON'), '_CON')
        self.assertEqual(xpc.safe_title('../小米:SU7? '), '_小米_SU7_')
        self.assertLessEqual(len(xpc.safe_title('长' * 200)), 100)

    def test_preserves_existing_and_partial_downloads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / '作品.mp4').write_bytes(b'existing')
            (path / '作品 (2).mp4.part').write_bytes(b'partial')
            self.assertEqual(xpc.output_path(path, '作品').name, '作品 (3).mp4')
            self.assertEqual((path / '作品.mp4').read_bytes(), b'existing')

    def test_downloads_mp4_without_ffmpeg(self):
        video = {'title': '作品', 'url': 'https://cdn.example/video.mp4', 'size': 4, 'stream': False}
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            with patch.object(xpc, 'urlopen', return_value=Response(b'test', {'Content-Length': '4'})) as fetch, patch.object(xpc, 'find_ffmpeg', side_effect=AssertionError('MP4 does not need FFmpeg')):
                saved = xpc.save_video(video, Path(directory))
                self.assertEqual(fetch.call_args.args[0].get_header('Range'), 'bytes=0-')
            self.assertEqual(saved.read_bytes(), b'test')
            self.assertFalse(saved.with_suffix('.mp4.part').exists())

    def test_truncated_response_is_not_published_as_complete(self):
        video = {'title': '作品', 'url': 'https://cdn.example/video.mp4', 'size': 10, 'stream': False}
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            with patch.object(xpc, 'urlopen', return_value=Response(b'short', {'Content-Length': '10'})):
                with self.assertRaises(xpc.DownloadError):
                    xpc.save_video(video, Path(directory))
            self.assertFalse((Path(directory) / '作品.mp4').exists())

    def test_rejects_error_page_instead_of_saving_video(self):
        video = {'title': '作品', 'url': 'https://cdn.example/video.mp4', 'size': 0, 'stream': False}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(xpc, 'urlopen', return_value=Response(b'<html>Error</html>', {'Content-Type': 'text/html'})):
                with self.assertRaises(xpc.DownloadError):
                    xpc.save_video(video, Path(directory))
            self.assertFalse(list(Path(directory).iterdir()))

    def test_api_error_has_clear_message(self):
        payload = json.dumps({'status': 1, 'message': '需要登录', 'data': {}}).encode()
        with patch.object(xpc, 'urlopen', return_value=Response(payload)):
            with self.assertRaisesRegex(xpc.DownloadError, '需要登录'):
                xpc.request_json('https://example.com')

    def test_quit_and_single_download_exit_codes(self):
        with patch('builtins.input', return_value='q'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(xpc.main([]), 0)
        with patch.object(xpc, 'run_download', return_value=False):
            self.assertEqual(xpc.main(['https://www.xinpianchang.com/a123']), 1)


if __name__ == '__main__':
    unittest.main()
