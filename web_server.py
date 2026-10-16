"""Local download page. Run with Python; no third-party packages required."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import shutil
import sys
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
import webbrowser

import xinpianchang as xpc


PAGE = Path(__file__).resolve().parent / 'web' / 'index.html'


class DownloadService:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.token = secrets.token_urlsafe(32)
        self.jobs = {}
        self.lock = threading.Lock()
        # Serial downloads also avoid collisions when two works share a title.
        self.executor = ThreadPoolExecutor(max_workers=1)

    def update(self, identifier, **values):
        with self.lock:
            self.jobs[identifier].update(values)

    def snapshot(self):
        with self.lock:
            return [dict(job) for job in reversed(list(self.jobs.values()))]

    def submit(self, url):
        xpc.article_id(url)
        with self.lock:
            if sum(job['status'] in ('queued', 'resolving', 'downloading') for job in self.jobs.values()) >= 20:
                raise xpc.DownloadError('等待下载的任务较多，请完成后再添加。')
            identifier = secrets.token_hex(12)
            self.jobs[identifier] = {
                'id': identifier, 'url': url, 'status': 'queued', 'title': '等待获取作品信息',
                'quality': '', 'received': 0, 'total': 0, 'error': '', 'filename': '',
            }
        self.executor.submit(self.run, identifier, url)
        return identifier

    def run(self, identifier, url):
        try:
            self.update(identifier, status='resolving')
            video = xpc.resolve_video(url)
            self.update(identifier, status='downloading', title=video['title'], quality=video['quality'], total=video['size'])
            destination = xpc.save_video(
                video, self.directory,
                on_progress=lambda received, total: self.update(identifier, received=received, total=total),
            )
            self.update(identifier, status='completed', filename=destination.name, received=destination.stat().st_size)
        except HTTPError as error:
            self.update(identifier, status='failed', error=f'网站返回 HTTP {error.code}，请确认作品可正常播放，或稍后重试。')
        except URLError:
            self.update(identifier, status='failed', error='无法连接视频服务器，请检查网络后重试。')
        except (xpc.DownloadError, OSError, ValueError) as error:
            self.update(identifier, status='failed', error=str(error))
        except Exception:
            self.update(identifier, status='failed', error='下载时出现异常，请重试或在服务窗口查看详情。')
            import traceback
            traceback.print_exc()


class DownloadServer(ThreadingHTTPServer):
    def __init__(self, address, service):
        self.service = service
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Polling does not need to fill the service window.
        if args and str(args[1]) != '200':
            super().log_message(format, *args)

    def send_data(self, status, body, content_type='application/json; charset=utf-8'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, data):
        self.send_data(status, json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def do_GET(self):
        path = urlsplit(self.path).path
        service = self.server.service
        if path == '/':
            page = PAGE.read_text(encoding='utf-8').replace('__API_TOKEN__', service.token)
            self.send_data(200, page.encode('utf-8'), 'text/html; charset=utf-8')
        elif path == '/api/tasks':
            self.send_json(200, {'tasks': service.snapshot(), 'output': str(service.directory)})
        elif path.startswith('/files/'):
            identifier = path.removeprefix('/files/')
            with service.lock:
                job = dict(service.jobs.get(identifier, {}))
            if job.get('status') != 'completed':
                self.send_json(404, {'error': '文件不存在或尚未下载完成。'})
                return
            try:
                file = (service.directory / job['filename']).open('rb')
            except OSError:
                self.send_json(404, {'error': '文件已被移动或删除。'})
                return
            with file:
                self.send_response(200)
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Content-Length', str(Path(file.name).stat().st_size))
                self.send_header('Content-Disposition', f"attachment; filename*=UTF-8''{quote(job['filename'])}")
                self.end_headers()
                try:
                    shutil.copyfileobj(file, self.wfile)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
        else:
            self.send_json(404, {'error': '页面不存在。'})

    def do_POST(self):
        if urlsplit(self.path).path != '/api/tasks':
            self.send_json(404, {'error': '接口不存在。'})
            return
        if self.headers.get('X-Download-Token') != self.server.service.token:
            self.send_json(403, {'error': '请刷新页面后重试。'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8192:
                raise ValueError
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or not isinstance(payload.get('url'), str):
                raise ValueError
            identifier = self.server.service.submit(payload['url'].strip())
        except xpc.DownloadError as error:
            self.send_json(400, {'error': str(error)})
            return
        except (ValueError, UnicodeDecodeError):
            self.send_json(400, {'error': '请输入有效的新片场作品链接。'})
            return
        self.send_json(202, {'id': identifier})


def main(argv=None):
    parser = argparse.ArgumentParser(description='启动本地视频下载网页，无需安装依赖。')
    parser.add_argument('--port', type=int, default=8765, help='本地端口，默认 8765')
    parser.add_argument('-o', '--output', type=Path, default=xpc.DEFAULT_OUTPUT, help='视频保存目录')
    parser.add_argument('--no-browser', action='store_true', help='只启动服务，不自动打开网页')
    args = parser.parse_args(argv)
    service = DownloadService(args.output)
    try:
        server = DownloadServer(('127.0.0.1', args.port), service)
    except OSError as error:
        print(f'启动失败：{error}\n如果服务已经启动，请直接打开 http://127.0.0.1:{args.port}/；也可用 --port 更换端口。')
        service.executor.shutdown(wait=False)
        return 1
    address = f'http://127.0.0.1:{server.server_port}/'
    print(f'视频下载服务已启动：{address}\n保存目录：{service.directory}\n请保持此窗口打开，按 Ctrl+C 停止服务。', flush=True)
    if not args.no_browser:
        try:
            webbrowser.open(address)
        except webbrowser.Error:
            print('请在浏览器中打开上方地址。')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n正在停止服务，已开始的下载完成后退出……', flush=True)
    finally:
        server.server_close()
        service.executor.shutdown(wait=True, cancel_futures=True)
    return 0


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='replace')
    sys.exit(main())
