"""HTTP link resolution and a bounded, owner-only browser reading relay.

Shared verbatim with wechat-linux-cli. No school adapters or client login code.
Opaque mini-program tickets need an observed HTTP equivalent in private state.
"""
import base64
import hashlib
import html
import http.cookiejar
import ipaddress
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import time
import urllib.error
import urllib.parse as up
import urllib.request
from html.parser import HTMLParser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

MAX_BYTES = 8 * 1024 * 1024
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36'


def http_url(value):
    if not isinstance(value, str) or not value or len(value) > 16384 or any(ord(c) < 33 for c in value):
        raise ValueError('INVALID_WEB_URL')
    try:
        u = up.urlsplit(value)
        port = u.port
    except ValueError:
        raise ValueError('INVALID_WEB_URL') from None
    if u.scheme not in ('http', 'https') or not u.hostname or u.username is not None or u.password is not None:
        raise ValueError('HTTP_URL_REQUIRED')
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('INVALID_WEB_URL')
    return value


def origin(value):
    u = up.urlsplit(http_url(value))
    return u.scheme, u.hostname.lower(), u.port or (443 if u.scheme == 'https' else 80)


def private_read(path):
    fd = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        s = os.fstat(f.fileno())
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077 or s.st_size > MAX_BYTES:
            raise ValueError('UNSAFE_WEB_STATE')
        return json.loads(f.read(MAX_BYTES + 1))


def private_write(path, value):
    path = Path(path).expanduser().absolute()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    p = path.parent.lstat()
    if not stat.S_ISDIR(p.st_mode) or p.st_uid != os.getuid() or p.st_mode & 0o077:
        raise ValueError('UNSAFE_WEB_STATE_DIRECTORY')
    if path.exists() or path.is_symlink():
        private_read(path)
    temp = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def binding_path():
    # Both clients deliberately use the same owner configuration.
    return Path.home() / '.local/state/wechat-web/bindings.json'


def key(url):
    return hashlib.sha256(url.encode()).hexdigest()


def binding(url, file=None):
    path = Path(file) if file else binding_path()
    if not path.exists() and not path.is_symlink():
        return None
    data = private_read(path)
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('bindings'), dict):
        raise ValueError('INVALID_WEB_BINDINGS')
    item = data['bindings'].get(key(url))
    if item is not None:
        validate_binding(item)
    return item


def validate_binding(item):
    if not isinstance(item, dict) or item.get('view') not in ('json', 'text', 'html'):
        raise ValueError('INVALID_WEB_BINDING')
    targets = item.get('targets')
    if not isinstance(targets, list) or not 1 <= len(targets) <= 8:
        raise ValueError('INVALID_WEB_BINDING')
    for target in targets:
        http_url(target)
    if len(set(targets)) != len(targets):
        raise ValueError('DUPLICATE_WEB_TARGET')


def bind(url, targets, view='json', file=None):
    # Resolve syntax, but never guess a target from the opaque ticket itself.
    classify(url)
    item = {'targets': targets, 'view': view, 'configured_at': int(time.time()),
            'source_sha256': key(url), 'verification': 'caller_observed_equivalent'}
    validate_binding(item)
    path = Path(file) if file else binding_path()
    if path.exists() or path.is_symlink():
        data = private_read(path)
        if data.get('version') != 1 or not isinstance(data.get('bindings'), dict):
            raise ValueError('INVALID_WEB_BINDINGS')
    else:
        data = {'version': 1, 'bindings': {}}
    data['bindings'][key(url)] = item
    private_write(path, data)
    return {'ok': True, 'code': 'WEB_BINDING_SAVED', 'binding_file': str(path),
            'source_sha256': key(url), 'target_count': len(targets), 'view': view,
            'content_verified': False}


def classify(url):
    if not isinstance(url, str) or not url or len(url) > 16384 or any(ord(c) < 33 for c in url):
        raise ValueError('INVALID_WEB_URL')
    try:
        u = up.urlsplit(url)
        q = up.parse_qs(u.query, keep_blank_values=True, max_num_fields=80)
    except ValueError:
        raise ValueError('INVALID_WEB_URL') from None
    if u.scheme in ('http', 'https'):
        http_url(url)
        oauth = u.hostname in ('open.weixin.qq.com', 'open.work.weixin.qq.com') and 'oauth' in u.path.lower()
        short = u.hostname in ('wxmpurl.cn', 'wxaurl.cn')
        return {'kind': 'oauth' if oauth else 'mini_program_link' if short else 'http',
                'http_url': url, 'client_auth_may_be_required': oauth, 'content_verified': False}
    if u.scheme not in ('weixin', 'wxwork'):
        raise ValueError('UNSUPPORTED_WEB_SCHEME')
    if u.scheme == 'weixin' and u.netloc == 'dl' and u.path.rstrip('/') == '/business':
        return {'kind': 'mini_program_scheme', 'requires_client': True, 'content_verified': False}
    # URL-bearing webview envelopes only; OAuth callbacks are never web targets.
    if 'oauth' in (u.netloc + u.path).lower() or 'redirect_uri' in q:
        return {'kind': 'client_auth', 'requires_client': True, 'content_verified': False}
    candidates = [(k, v) for k in ('url', 'target_url', 'web_url', 'weburl') for v in q.get(k, [])]
    if len(candidates) == 1:
        field, target = candidates[0]
        http_url(target)  # parse_qs has decoded the outer envelope exactly once.
        return {'kind': 'webview_url', 'http_url': target, 'parameter': field,
                'client_auth_may_be_required': True, 'content_verified': False}
    return {'kind': 'client_action', 'requires_client': True, 'content_verified': False}


class Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, initial, credentials):
        self.initial = origin(initial)
        self.credentials = credentials

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        http_url(newurl)
        if self.credentials and origin(newurl) != self.initial:
            raise ValueError('AUTHENTICATED_CROSS_ORIGIN_REDIRECT')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, session=None):
    http_url(url)
    headers = {'User-Agent': UA, 'Accept': '*/*'}
    jar = http.cookiejar.CookieJar()
    credentials = False
    if session:
        data = private_read(session)
        if not isinstance(data, dict):
            raise ValueError('INVALID_WEB_SESSION')
        scope = data.get('origin')
        saved = data.get('headers', {})
        if not isinstance(saved, dict):
            raise ValueError('INVALID_WEB_SESSION')
        if saved and (not scope or origin(scope) != origin(url)):
            raise ValueError('WEB_SESSION_ORIGIN_REQUIRED')
        for name, value in saved.items():
            if not re.fullmatch(r'[A-Za-z0-9-]+', name) or name.lower() in ('host', 'content-length', 'connection', 'accept-encoding'):
                raise ValueError('INVALID_WEB_SESSION_HEADER')
            if not isinstance(value, str) or '\n' in value or '\r' in value:
                raise ValueError('INVALID_WEB_SESSION_HEADER')
            headers[name] = value
            credentials = True
        if data.get('user_agent'):
            if not isinstance(data['user_agent'], str) or any(ord(c) < 32 for c in data['user_agent']):
                raise ValueError('INVALID_WEB_SESSION')
            headers['User-Agent'] = data['user_agent']
        cookies = data.get('cookies', [])
        if not isinstance(cookies, list) or len(cookies) > 2000:
            raise ValueError('INVALID_WEB_SESSION')
        for c in cookies:
            if not isinstance(c, dict) or not all(isinstance(c.get(k), str) for k in ('name', 'value', 'domain')):
                raise ValueError('INVALID_WEB_SESSION_COOKIE')
            domain = c['domain'].lstrip('.').lower()
            host = up.urlsplit(url).hostname.lower()
            if host != domain and not (c['domain'].startswith('.') and host.endswith('.' + domain)):
                continue
            expires = c.get('expires')
            expires = int(expires) if isinstance(expires, (float, int)) and expires > 0 else None
            if expires is not None and expires <= time.time():
                continue
            jar.set_cookie(http.cookiejar.Cookie(0, c['name'], c['value'], None, False, c['domain'],
                c['domain'].startswith('.'), c['domain'].startswith('.'), c.get('path', '/'), True,
                bool(c.get('secure')), expires, expires is None, None, None, {}, False))
            credentials = True
    handlers = [Redirects(url, credentials), urllib.request.HTTPCookieProcessor(jar)]
    host = up.urlsplit(url).hostname
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == 'localhost'
    if loopback:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    try:
        response = opener.open(urllib.request.Request(url, headers=headers), timeout=15)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('WEB_RESPONSE_TOO_LARGE')
        charset = response.headers.get_content_charset() or 'utf-8'
        try:
            text = raw.decode(charset, errors='replace')
        except LookupError:
            raise ValueError('UNSUPPORTED_WEB_CHARSET') from None
        return {'status': response.status, 'url': response.url,
                'content_type': response.headers.get('Content-Type', ''), 'text': text, 'bytes': len(raw)}


def launcher_metadata(body):
    # Static publisher-provided launch data. Never evaluate remote JavaScript.
    result = {}
    block = body[body.rfind('window.data ='):]
    for name in ('nickname', 'user_name', 'path', 'url_scheme'):
        m = re.search(r'\b' + name + r"\s*:\s*'([^'\r\n]{0,4096})'", block)
        if m:
            result[name] = html.unescape(m.group(1))
    m = re.search(r"\bquery\s*:\s*base64Decode\('([A-Za-z0-9+/=]{0,8192})'\)", block)
    if m:
        try:
            result['query'] = base64.b64decode(m.group(1), validate=True).decode('utf-8')
        except (ValueError, UnicodeError):
            pass
    return result


def resolve(url, file=None, probe=False, session=None):
    info = classify(url)
    item = binding(url, file)
    result = {'ok': not info.get('requires_client', False), 'source_kind': info['kind'],
              'content_verified': False, 'client_auth_may_be_required': info.get('client_auth_may_be_required', False)}
    if item:
        result.update(ok=True, code='OBSERVED_HTTP_BINDING', targets=item['targets'], view=item['view'],
                      verification=item['verification'])
    elif info.get('http_url'):
        result.update(code='HTTP_TARGET', targets=[info['http_url']], view='html')
    else:
        result.update(code='CLIENT_REQUIRED', requires_client=True,
                      next='Use the current client to observe an HTTP equivalent, then bind it; otherwise use computer-use.')
    if probe and result.get('targets'):
        observations = []
        for target in result['targets']:
            r = fetch(target, session)
            metadata = launcher_metadata(r['text'])
            observations.append({k: r[k] for k in ('status', 'url', 'content_type', 'bytes')} | {'launch_data': metadata})
            if metadata.get('url_scheme') or '/deeplink/noaccess' in up.urlsplit(r['url']).path:
                result.update(ok=False, code='MINI_PROGRAM_LAUNCH_ONLY', requires_client=True)
            elif not 200 <= r['status'] < 300:
                result.update(ok=False, code='HTTP_TARGET_UNAVAILABLE')
        result['http_observations'] = observations
    return result


def open_browser(url, browser='default'):
    http_url(url)
    executable = None
    if browser in ('chrome', 'edge'):
        choices = ('google-chrome-stable', 'google-chrome', 'chromium') if browser == 'chrome' else ('microsoft-edge-stable', 'microsoft-edge')
        executable = next((shutil.which(c) for c in choices if shutil.which(c)), None)
        if executable:
            command = [executable, '--new-window', url]
        else:
            executable = shutil.which('gtk-launch')
            desktop = 'com.google.Chrome' if browser == 'chrome' else 'com.microsoft.Edge'
            if not executable or not any((Path(p) / 'applications' / (desktop + '.desktop')).exists()
                   for p in (str(Path.home() / '.local/share'), '/usr/share', '/var/lib/flatpak/exports/share')):
                raise ValueError('BROWSER_NOT_INSTALLED')
            command = [executable, desktop, url]
    else:
        executable = shutil.which('xdg-open')
        if not executable:
            raise ValueError('BROWSER_NOT_INSTALLED')
        command = [executable, url]
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return {'ok': True, 'code': 'BROWSER_OPEN_REQUESTED', 'browser': browser,
            'url': url, 'content_verified': False}


class TextPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip += 1
        if tag in ('p', 'div', 'br', 'section', 'h1', 'h2', 'li'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, text):
        if not self.skip:
            self.parts.append(text)


def render_json(value, depth=0):
    if depth > 80:
        raise ValueError('WEB_JSON_TOO_DEEP')
    if isinstance(value, dict):
        return '<dl>' + ''.join('<dt>' + html.escape(str(k)) + '</dt><dd>' + render_json(v, depth + 1) + '</dd>'
                               for k, v in value.items()) + '</dl>'
    if isinstance(value, list):
        return '<ol>' + ''.join('<li>' + render_json(v, depth + 1) + '</li>' for v in value) + '</ol>'
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return '<p>' + html.escape(text) + '</p>'


def page(targets, view, session=None):
    sections = []
    errors = False
    for target in targets:
        r = fetch(target, session)
        errors |= not 200 <= r['status'] < 300
        if view == 'json':
            try:
                content = render_json(json.loads(r['text']))
            except (json.JSONDecodeError, RecursionError):
                raise ValueError('WEB_JSON_BODY_REQUIRED') from None
        else:
            parser = TextPage()
            parser.feed(r['text'])
            content = '<pre>' + html.escape(''.join(parser.parts) if view == 'html' else r['text']) + '</pre>'
        sections.append('<section><h2>' + html.escape(up.urlsplit(r['url']).hostname) +
                        '</h2><small>HTTP ' + str(r['status']) + '</small>' + content + '</section>')
    output = '''<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>微信 / 企微浏览器读取</title>
<style>body{font:16px/1.6 system-ui,sans-serif;max-width:1000px;margin:24px auto;padding:0 20px;color:#222;background:#f6f7f9}
section{background:white;padding:20px;border-radius:12px;margin:20px 0;overflow-wrap:anywhere}h1{font-size:24px}
dl{margin:0}dt{font-size:13px;color:#667085;margin-top:8px}dd{margin-left:16px}p,pre{margin:4px 0;white-space:pre-wrap}
ol{padding-left:24px}li{padding:12px 0;border-bottom:1px solid #eee}small{color:#667085}</style>
<h1>微信 / 企微浏览器读取</h1><p>只读页面 · ''' + html.escape(time.strftime('%Y-%m-%d %H:%M:%S')) + \
        ' · 刷新会重新读取当前来源</p>' + ''.join(sections) + '</html>'
    return output.encode(), errors


def relay(url, file=None, view=None, session=None, seconds=300, browser=None, ready_file=None):
    if not 1 <= seconds <= 3600:
        raise ValueError('WEB_RELAY_SECONDS_OUT_OF_RANGE')
    result = resolve(url, file)
    if not result['ok']:
        return result
    if result['source_kind'] == 'mini_program_link' and result['code'] != 'OBSERVED_HTTP_BINDING':
        return {**result, 'ok': False, 'code': 'CLIENT_REQUIRED', 'requires_client': True}
    targets = result['targets']
    view = view or result['view']
    # Verify the initial GET before announcing a reachable relay.
    initial, failed = page(targets, view, session)
    if failed:
        return {'ok': False, 'code': 'HTTP_TARGET_UNAVAILABLE', 'content_verified': False}
    token = secrets.token_urlsafe(32)
    route = '/' + token + '/'
    deadline = time.monotonic() + seconds
    state = {'requests': 0}

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(3)

        def log_message(self, *args):
            pass  # Neither private URLs nor browser requests go to logs.

        def do_GET(self):
            if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}' or self.path != route:
                self.send_error(404)
                return
            fetch_site = self.headers.get('Sec-Fetch-Site', '')
            if fetch_site not in ('', 'none', 'same-origin'):
                self.send_error(403)
                return
            try:
                body, error = (initial, False) if state['requests'] == 0 else page(targets, view, session)
                state['requests'] += 1
                self.send_response(502 if error else 200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
                self.end_headers()
                self.wfile.write(body)
            except (OSError, ValueError):
                self.send_error(502, 'Source unavailable')

    with HTTPServer(('127.0.0.1', 0), Handler) as server:
        server.timeout = .5
        local_url = f'http://127.0.0.1:{server.server_port}{route}'
        ready = {'ok': True, 'code': 'WEB_RELAY_READY', 'url': local_url,
                 'seconds': seconds, 'view': view, 'source_kind': result['source_kind'],
                 'target_count': len(targets), 'initial_http_success': True,
                 'business_success_verified': False, 'read_only': True, 'autostart': False}
        if ready_file:
            private_write(ready_file, ready)
        print(json.dumps(ready, ensure_ascii=False), flush=True)
        if browser:
            open_browser(local_url, browser)
        try:
            while time.monotonic() < deadline:
                server.handle_request()
        except KeyboardInterrupt:
            pass
    return {'ok': True, 'code': 'WEB_RELAY_CLOSED', 'requests': state['requests'], 'autostart': False}


def add_parser(sub):
    web = sub.add_parser('web', help='Resolve webview links; bounded readonly HTTP browser relay')
    actions = web.add_subparsers(dest='web_command', required=True)
    for name in ('resolve', 'open', 'bind', 'relay'):
        p = actions.add_parser(name)
        p.add_argument('--url', required=True)
        p.add_argument('--bindings', type=Path)
        if name == 'bind':
            p.add_argument('--target', action='append', required=True)
            p.add_argument('--view', choices=('json', 'text', 'html'), default='json')
        if name in ('resolve', 'relay'):
            p.add_argument('--session', type=Path)
        if name == 'resolve':
            p.add_argument('--probe', action='store_true', help='GET HTTP targets; does not run JavaScript or prove business success')
        if name in ('open', 'relay'):
            p.add_argument('--browser', choices=('default', 'chrome', 'edge'), default='default' if name == 'open' else None)
        if name == 'relay':
            p.add_argument('--view', choices=('json', 'text', 'html'))
            p.add_argument('--seconds', type=int, default=300)
            p.add_argument('--ready-file', type=Path)


def run(args):
    if args.web_command == 'bind':
        return bind(args.url, args.target, args.view, args.bindings)
    if args.web_command == 'resolve':
        return resolve(args.url, args.bindings, args.probe, args.session)
    if args.web_command == 'relay':
        return relay(args.url, args.bindings, args.view, args.session, args.seconds, args.browser, args.ready_file)
    result = resolve(args.url, args.bindings)
    if not result['ok']:
        return result
    if result['view'] != 'html' or len(result['targets']) != 1:
        return {**result, 'ok': False, 'code': 'USE_WEB_RELAY'}
    if result['source_kind'] == 'mini_program_link' and result['code'] != 'OBSERVED_HTTP_BINDING':
        return {**result, 'ok': False, 'code': 'CLIENT_REQUIRED', 'requires_client': True}
    return open_browser(result['targets'][0], args.browser)
