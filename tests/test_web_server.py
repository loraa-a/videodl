import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import build_opener, ProxyHandler, Request

import web_server as web


class Response(io.BytesIO):
    headers = {'Content-Length': '4', 'Content-Type': 'video/mp4'}


class WebDownloadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = web.DownloadService(self.directory.name)
        self.server = web.DownloadServer(('127.0.0.1', 0), self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.address = f'http://127.0.0.1:{self.server.server_port}'
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.service.executor.shutdown(wait=True)
        self.directory.cleanup()

    def request(self, path, payload=None, token=None):
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {'X-Download-Token': token or self.service.token, 'Content-Type': 'application/json'}
        try:
            response = self.opener.open(Request(self.address + path, body, headers), timeout=3)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.read(), response.headers

    def tasks(self):
        return json.loads(self.request('/api/tasks')[1])['tasks']

    def wait_for_terminal(self, count):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            tasks = self.tasks()
            if len(tasks) == count and all(t['status'] in ('completed', 'failed') for t in tasks):
                return tasks
            time.sleep(0.02)
        self.fail('Downloads did not finish')

    def test_serves_page_and_only_exposes_download_routes(self):
        status, page, _ = self.request('/')
        self.assertEqual(status, 200)
        self.assertIn(self.service.token.encode(), page)
        self.assertNotIn(b'__API_TOKEN__', page)
        self.assertEqual(self.request('/xinpianchang.py')[0], 404)
        self.assertEqual(self.request('/files/../README.md')[0], 404)
        self.assertEqual(self.tasks(), [])

    def test_rejects_invalid_links_and_cross_site_submission(self):
        for payload in ({'url': 'https://example.com/a123'}, {'url': 123}, ['invalid']):
            self.assertEqual(self.request('/api/tasks', payload)[0], 400)
        self.assertEqual(self.request('/api/tasks', {'url': 'https://www.xinpianchang.com/a123'}, 'wrong-token')[0], 403)
        self.assertEqual(self.tasks(), [])

    def test_download_progress_queue_unique_names_and_file_retrieval(self):
        started, release = threading.Event(), threading.Event()

        def resolve(url):
            started.set()
            if not release.wait(timeout=3):
                raise RuntimeError('Test did not release the worker')
            return {'title': '作品', 'quality': '720p', 'url': 'https://cdn.example/video.mp4', 'size': 4, 'stream': False}

        try:
            with patch.object(web.xpc, 'resolve_video', side_effect=resolve), patch.object(web.xpc, 'urlopen', side_effect=lambda *a, **k: Response(b'test')):
                first = self.request('/api/tasks', {'url': 'https://www.xinpianchang.com/a123'})
                self.assertEqual(first[0], 202)
                self.assertTrue(started.wait(timeout=3))
                self.assertEqual(self.request('/api/tasks', {'url': 'https://www.xinpianchang.com/a124'})[0], 202)
                self.assertEqual([t['status'] for t in self.tasks()], ['queued', 'resolving'])
                release.set()
                tasks = self.wait_for_terminal(2)
        finally:
            release.set()
        self.assertTrue(all(t['status'] == 'completed' for t in tasks))
        self.assertEqual({t['filename'] for t in tasks}, {'作品.mp4', '作品 (2).mp4'})
        for task in tasks:
            self.assertEqual(task['received'], 4)
            self.assertEqual(task['total'], 4)
            self.assertEqual((Path(self.directory.name) / task['filename']).read_bytes(), b'test')
            status, content, headers = self.request('/files/' + task['id'])
            self.assertEqual((status, content), (200, b'test'))
            self.assertIn("filename*=UTF-8''", headers['Content-Disposition'])

    def test_failure_is_displayed_and_next_task_still_downloads(self):
        with patch.object(web.xpc, 'resolve_video', side_effect=web.xpc.DownloadError('需要登录')):
            self.request('/api/tasks', {'url': 'https://www.xinpianchang.com/a123'})
            task = self.wait_for_terminal(1)[0]
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['error'], '需要登录')
        self.assertEqual(self.request('/files/' + task['id'])[0], 404)
        video = {'title': '下一条', 'quality': '720p', 'url': 'https://cdn.example/video.mp4', 'size': 4, 'stream': False}
        with patch.object(web.xpc, 'resolve_video', return_value=video), patch.object(web.xpc, 'urlopen', return_value=Response(b'test')):
            self.request('/api/tasks', {'url': 'https://www.xinpianchang.com/a124'})
            self.assertEqual(self.wait_for_terminal(2)[0]['status'], 'completed')

    def test_incomplete_video_is_not_offered_as_finished_file(self):
        video = {'title': '截断视频', 'quality': '720p', 'url': 'https://cdn.example/video.mp4', 'size': 4, 'stream': False}
        with patch.object(web.xpc, 'resolve_video', return_value=video), patch.object(web.xpc, 'urlopen', return_value=Response(b'ab')):
            self.request('/api/tasks', {'url': 'https://www.xinpianchang.com/a123'})
            task = self.wait_for_terminal(1)[0]
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(self.request('/files/' + task['id'])[0], 404)
        self.assertFalse((Path(self.directory.name) / '截断视频.mp4').exists())


if __name__ == '__main__':
    unittest.main()
