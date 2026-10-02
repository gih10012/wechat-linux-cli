import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from wechat_linux_cli import web


class Source(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', 'http://localhost:' + str(self.server.server_port) + '/json')
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps({'content': '<script>alert(1)</script>\n中文 ✅',
                                    'auth': self.headers.get('Sid'),
                                    'cookie': self.headers.get('Cookie')}, ensure_ascii=False).encode())


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = HTTPServer(('127.0.0.1', 0), Source)
        cls.thread = threading.Thread(target=cls.source.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.source.server_port}/json'

    @classmethod
    def tearDownClass(cls):
        cls.source.shutdown()
        cls.source.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.file = self.directory / 'bindings.json'

    def test_webview_decodes_only_outer_url_preserving_signed_query(self):
        url = 'https://example.org/a?x=a%2Fb&ticket=a%2Bb#section'
        envelope = 'wxwork://webview?url=' + web.up.quote(url, safe='')
        self.assertEqual(web.resolve(envelope, self.file)['targets'], [url])
        self.assertFalse(web.resolve(envelope, self.file)['content_verified'])

    def test_opaque_tickets_and_oauth_callbacks_are_not_guessed(self):
        self.assertEqual(web.resolve('weixin://dl/business/?t=test', self.file)['code'], 'CLIENT_REQUIRED')
        oauth = 'wxwork://oauth?redirect_uri=https%3A%2F%2Fexample.org&url=https%3A%2F%2Fother.org'
        self.assertFalse(web.resolve(oauth, self.file)['ok'])
        self.assertNotIn('targets', web.resolve(oauth, self.file))
        self.assertFalse(web.resolve('wxwork://jump?businessid=abc', self.file)['ok'])

    def test_url_rejects_credentials_and_non_http_payloads(self):
        for url in ['https://user:pass@example.org', 'file:///etc/passwd', 'https://example.org/\r\nX:1',
                    'weixin://web?url=javascript%3Aalert(1)', 'wxwork://web?url=https%3A%2F%2Fa&url=https%3A%2F%2Fb']:
            try:
                result = web.resolve(url, self.file)
            except ValueError:
                continue
            self.assertFalse(result['ok'], url)

    def test_binding_is_exact_private_and_not_a_general_ticket_decoder(self):
        url = 'weixin://dl/business/?t=one'
        web.bind(url, [self.url], file=self.file)
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)
        result = web.resolve(url, self.file, True)
        self.assertTrue(result['ok'])
        self.assertFalse(result['content_verified'])
        self.assertFalse(web.resolve(url.replace('one', 'two'), self.file)['ok'])
        self.file.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'UNSAFE_WEB_STATE'):
            web.resolve(url, self.file)

    def test_symlink_session_is_rejected(self):
        session = self.directory / 'session.json'
        web.private_write(session, {})
        symlink = self.directory / 'link.json'
        symlink.symlink_to(session)
        with self.assertRaises(OSError):
            web.fetch(self.url, symlink)

    def test_credentials_require_exact_origin_and_cannot_cross_redirect(self):
        session = self.directory / 'session.json'
        web.private_write(session, {'origin': self.url, 'headers': {'Sid': 'synthetic-secret'},
            'cookies': [{'name': 'mine', 'value': 'synthetic-cookie', 'domain': '127.0.0.1', 'path': '/'}]})
        result = json.loads(web.fetch(self.url, session)['text'])
        self.assertEqual(result['auth'], 'synthetic-secret')
        self.assertIn('mine=synthetic-cookie', result['cookie'])
        with self.assertRaisesRegex(ValueError, 'AUTHENTICATED_CROSS_ORIGIN_REDIRECT'):
            web.fetch(self.url.replace('/json', '/redirect'), session)
        with self.assertRaisesRegex(ValueError, 'WEB_SESSION_ORIGIN_REQUIRED'):
            web.fetch(self.url.replace('127.0.0.1', 'localhost'), session)

    def test_static_launch_data_is_not_web_content_or_executed_js(self):
        text = "window.data = {nickname:'Example',user_name:'gh_example',path:'pages/x',query:base64Decode('aWQ9MQ=='),url_scheme:'weixin://dl/business/?t=x'}; throw Error('must not execute');"
        data = web.launcher_metadata(text)
        self.assertEqual(data['query'], 'id=1')
        self.assertEqual(data['path'], 'pages/x')

    def test_reading_relay_escapes_content_rejects_writes_and_closes(self):
        source = 'weixin://dl/business/?t=readonly'
        web.bind(source, [self.url], file=self.file)
        ready = self.directory / 'ready.json'
        values = []
        thread = threading.Thread(target=lambda: values.append(web.relay(source, self.file, seconds=2, ready_file=ready)))
        thread.start()
        for _ in range(50):
            if ready.exists():
                break
            time.sleep(.02)
        local = web.private_read(ready)['url']
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(local, timeout=3) as response:
            body = response.read().decode()
            self.assertIn('中文 ✅', body)
            self.assertIn('&lt;script&gt;', body)
            self.assertNotIn('<script>', body)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
        for request, code in [(urllib.request.Request(local, method='POST', data=b'{}'), 501),
                              (urllib.request.Request(local, headers={'Host': 'example.org'}), 404),
                              (urllib.request.Request(local, headers={'Sec-Fetch-Site': 'cross-site'}), 403)]:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3)
            self.assertEqual(error.exception.code, code)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(values[0]['code'], 'WEB_RELAY_CLOSED')
        with self.assertRaises(urllib.error.URLError):
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(local, timeout=1)


if __name__ == '__main__':
    unittest.main()
