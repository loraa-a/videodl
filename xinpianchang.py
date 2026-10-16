"""Download public Xinpianchang works using only the Python standard library."""

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / 'videodl_outputs' / 'XinpianchangVideoClient'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36',
    'Referer': 'https://www.xinpianchang.com/',
}


class DownloadError(Exception):
    """An error suitable for displaying to the person downloading a video."""


def article_id(text):
    text = text.strip()
    if text.startswith(('www.xinpianchang.com/', 'xinpianchang.com/')):
        text = 'https://' + text
    for candidate in re.findall(r'https?://[^\s<>"\']+', text):
        parsed = urlsplit(candidate.rstrip('。，,；;）)'))
        match = re.fullmatch(r'/a(\d+)/?', parsed.path)
        if parsed.hostname in ('www.xinpianchang.com', 'xinpianchang.com') and match:
            return match.group(1)
    raise DownloadError('请粘贴新片场作品链接，例如 https://www.xinpianchang.com/a13019115')


def request_json(url):
    with urlopen(Request(url, headers=HEADERS), timeout=30) as response:
        result = json.load(response)
    if not isinstance(result, dict) or not isinstance(result.get('data'), dict):
        raise DownloadError('网站没有返回作品信息，请在浏览器中确认作品可以正常播放。')
    if result.get('status') not in (None, 0):
        raise DownloadError(str(result.get('message') or '网站暂时无法提供这条作品。'))
    return result['data']


def select_resource(resources):
    streams = resources.get('progressive') or []
    available = [s for s in streams if (s.get('url') or s.get('backupUrl') or '').startswith('https://')]
    if available:
        stream = max(available, key=lambda s: (
            (s.get('height') or s.get('codedHeight') or 0) * (s.get('width') or s.get('codedWidth') or 0),
            s.get('filesize') or 0,
        ))
        return {
            'url': stream.get('url') or stream['backupUrl'],
            'quality': stream.get('quality') or stream.get('profile') or '可用最高画质',
            'size': stream.get('filesize') or 0,
            'stream': False,
        }
    for kind in ('hls', 'dash'):
        stream = resources.get(kind) or {}
        if isinstance(stream, dict) and (stream.get('url') or '').startswith('https://'):
            return {'url': stream['url'], 'quality': kind.upper(), 'size': 0, 'stream': True}
    raise DownloadError('网站没有提供可下载的播放地址，请在浏览器中确认作品的播放权限。')


def resolve_video(text):
    identifier = article_id(text)
    article = request_json(f'https://app.xinpianchang.com/article/{identifier}')
    video = article.get('video') or {}
    if not video.get('vid') or not video.get('appKey'):
        raise DownloadError('这条作品没有可用的视频信息，可能已删除或需要登录。')
    query = urlencode({'appKey': video['appKey'], 'extend': 'userInfo,userStatus'})
    media = request_json(f'https://mod-api.xinpianchang.com/mod/api/v2/media/{video["vid"]}?{query}')
    selected = select_resource(media.get('resource') or {})
    selected['title'] = media.get('title') or article.get('title') or f'新片场_{identifier}'
    return selected


def safe_title(title):
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', title).strip(' .')[:100].rstrip(' .') or '新片场视频'
    if re.fullmatch(r'(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', title):
        title = '_' + title
    return title


def output_path(directory, title):
    directory.mkdir(parents=True, exist_ok=True)
    stem = safe_title(title)
    target = directory / f'{stem}.mp4'
    number = 2
    while target.exists() or target.with_suffix('.mp4.part').exists():
        target = directory / f'{stem} ({number}).mp4'
        number += 1
    return target


def find_ffmpeg():
    candidates = [ROOT / 'ffmpeg' / 'bin' / 'ffmpeg.exe', ROOT / 'ffmpeg.exe', ROOT / 'tools' / 'ffmpeg.exe']
    return next((str(p) for p in candidates if p.is_file()), None) or shutil.which('ffmpeg')


def save_video(video, directory):
    destination = output_path(directory, video['title'])
    partial = destination.with_suffix('.mp4.part')
    if video['stream']:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise DownloadError('这条视频需要 FFmpeg 合并。请将 ffmpeg.exe 放到项目目录，或将其 bin 目录加入 PATH。')
        headers = ''.join(f'{key}: {value}\r\n' for key, value in HEADERS.items())
        command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n', '-headers', headers,
                   '-i', video['url'], '-c', 'copy', '-f', 'mp4', str(partial)]
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if result.returncode:
            raise DownloadError('视频合并失败，请稍后重试。' + ('\n' + result.stderr.strip()[-800:] if result.stderr else ''))
    else:
        request = Request(video['url'], headers={**HEADERS, 'Accept-Encoding': 'identity', 'Range': 'bytes=0-'})
        with urlopen(request, timeout=30) as response:
            content_type = response.headers.get('Content-Type', '').lower()
            if any(kind in content_type for kind in ('text/html', 'application/json')):
                raise DownloadError('视频服务器返回了错误页面，请稍后重试。')
            total = int(response.headers.get('Content-Length') or video['size'] or 0)
            received, last_update = 0, 0
            with partial.open('xb') as file:
                while chunk := response.read(256 * 1024):
                    file.write(chunk)
                    received += len(chunk)
                    if time.monotonic() - last_update >= 0.25:
                        progress = f'{received / total:.0%}' if total else f'{received / 1048576:.1f} MB'
                        print(f'\r正在下载：{progress}    ', end='', flush=True)
                        last_update = time.monotonic()
            print()
            if not received or (total and received != total):
                raise DownloadError('下载未完成，请重试；临时文件保留为 .part，不会当作完整视频。')
    if not partial.exists() or not partial.stat().st_size:
        raise DownloadError('没有下载到有效的视频文件，请稍后重试。')
    partial.rename(destination)
    return destination


def download(text, directory):
    print('正在获取视频信息……', flush=True)
    video = resolve_video(text)
    print(f'作品：{video["title"]}\n画质：{video["quality"]}')
    destination = save_video(video, directory)
    print(f'下载完成：{destination}')
    return destination


def run_download(text, directory):
    try:
        download(text, directory)
        return True
    except HTTPError as error:
        print(f'下载失败：网站返回 HTTP {error.code}，请确认作品可正常播放，或稍后重试。')
    except URLError:
        print('下载失败：无法连接视频服务器，请检查网络后重试。')
    except (DownloadError, OSError, ValueError) as error:
        print(f'下载失败：{error}')
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description='新片场视频下载：粘贴作品链接即可，无需安装依赖。')
    parser.add_argument('url', nargs='?', help='新片场作品链接；不填写则进入交互模式')
    parser.add_argument('-o', '--output', type=Path, default=DEFAULT_OUTPUT, help='视频保存目录')
    args = parser.parse_args(argv)
    try:
        if args.url:
            return 0 if run_download(args.url, args.output.resolve()) else 1
        print('新片场视频下载\n粘贴作品链接后按回车。输入 q 或直接按回车退出。')
        print(f'保存目录：{args.output.resolve()}\n')
        while True:
            text = input('作品链接：').strip()
            if not text or text.lower() in ('q', 'quit', 'exit'):
                return 0
            run_download(text, args.output.resolve())
            print()
    except (KeyboardInterrupt, EOFError):
        print('\n已退出。')
        return 0


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='replace')
    sys.exit(main())
