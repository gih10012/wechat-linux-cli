import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from wechat_linux_cli import backend, cards, cli
from wechat_linux_cli._native import native_messages as messages, native_send_candidate as native
from test_backend import queued_result

ARTICLE = b'<msg><appmsg><title>Fixture</title><type>5</type><url>https://example.org/article</url></appmsg></msg>'
MINI = b'<msg><appmsg><title>Mini</title><type>33</type><weappinfo><appid>fixture</appid><username>fixture</username></weappinfo></appmsg></msg>'


class CardTests(unittest.TestCase):
    def test_xml_boundary_utf8_and_required_semantics(self):
        self.assertEqual(cards.parse_xml(ARTICLE)['app_type'], 5)
        self.assertEqual(cards.parse_xml(MINI)['app_type'], 33)
        for value in (b'', b'a' * 65537, b'\xff', ARTICLE + b'\0', b'<msg>',
                      b'<!DOCTYPE msg [<!ENTITY a "b">]>' + ARTICLE,
                      ARTICLE.replace(b'<type>5', b'<type>6'),
                      ARTICLE.replace(b'Fixture', b''), MINI.replace(b'<appid>fixture</appid>', b'')):
            with self.subTest(value=value[:40]), self.assertRaises(ValueError):
                cards.parse_xml(value)

    def test_special_xml_files_rejected_without_blocking_or_client_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fifo = root/'fifo'; os.mkfifo(fifo)
            oversized = root/'large.xml'; oversized.write_bytes(b'x' * 65537)
            with patch.object(backend.highlevel, 'client_identity') as client:
                for path in (fifo, oversized):
                    with self.assertRaises(ValueError):
                        backend.send_xml({'operation': 'send_xml', 'recipient': 'fixture@chatroom',
                                          'file': str(path), 'request_id': 'xml-invalid-01'})
            client.assert_not_called()

    def test_forward_source_is_exact_and_ambiguous_shards_rejected(self):
        table = 'Msg_' + hashlib.md5(b'fixture@chatroom').hexdigest()
        def database(keys, relative):
            conn = sqlite3.connect(':memory:'); conn.row_factory = sqlite3.Row
            conn.executescript('CREATE TABLE Name2Id(user_name); INSERT INTO Name2Id VALUES("sender");'
                              'CREATE TABLE "' + table + '"(local_id,server_id,local_type,message_content,compress_content,real_sender_id);')
            conn.execute('INSERT INTO "' + table + '" VALUES(?,?,?,?,?,?)',
                         (2, 9007199254740997, 49, 'sender:\n' + ARTICLE.decode(), '', 1))
            return conn, {}
        keys = {'files': {'message/message_0.db': 'fake', 'message/message_1.db': 'fake'}}
        with patch.object(messages, 'load_keys', return_value=keys), \
                patch.object(messages, 'contact_names', return_value=({}, {})), \
                patch.object(messages, 'resolve_chat', return_value='fixture@chatroom'), \
                patch.object(messages, 'open_database', side_effect=database):
            with self.assertRaisesRegex(ValueError, 'MESSAGE_NOT_UNIQUE'):
                messages.forward_source('me', 'fixture@chatroom', 2)
            result = messages.forward_source('me', 'fixture@chatroom', 2, 'message/message_0.db')
            self.assertEqual(result['xml'], ARTICLE.decode())
            self.assertEqual(result['source_identity']['server_id'], '9007199254740997')
            self.assertFalse(result['marks_read'])
            for local in (1, 0, True):
                with self.assertRaises(ValueError):
                    messages.forward_source('me', 'fixture@chatroom', local, 'message/message_0.db')

    def test_forward_identity_and_custom_xml_cannot_share_request_id(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'WECHAT_LINUX_RUNTIME_DIR': directory}):
            root = Path(directory); xml = root/'input.xml'; xml.write_bytes(ARTICLE)
            source = {'chat_id': 'fixture@chatroom', 'database': 'message/message_0.db', 'local_id': 2, 'server_id': '9'}
            request = {'operation': 'send_xml', 'recipient': 'filehelper', 'request_id': 'card-replay-01',
                       'file': str(xml), 'source_identity': source}
            work = backend.work_for(request['request_id']); work.mkdir(parents=True)
            (work/'request.json').write_text(json.dumps({'send': True, 'request_kind': 'xml',
                'recipient': 'filehelper', 'payload_sha256': cards.fingerprint('filehelper', hashlib.sha256(ARTICLE).hexdigest(), source)}))
            (work/'result.json').write_text(json.dumps(queued_result()))
            with patch.object(backend.highlevel, 'client_identity') as identity, patch.object(backend.highlevel, 'trial') as trial:
                self.assertTrue(backend.send_xml(request)['replayed'])
                for changed in (None, {**source, 'local_id': 3}):
                    self.assertEqual(backend.send_xml({**request, 'source_identity': changed})['code'], 'REQUEST_ID_CONFLICT')
                xml.write_bytes(ARTICLE.replace(b'Fixture', b'Changed'))
                self.assertEqual(backend.send_xml(request)['code'], 'REQUEST_ID_CONFLICT')
            identity.assert_not_called(); trial.assert_not_called()

    def test_xml_snapshot_is_identical_for_preflight_and_send(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'WECHAT_LINUX_RUNTIME_DIR': directory}):
            xml = Path(directory)/'custom.xml'; xml.write_bytes(ARTICLE)
            seen = []
            def trial(send, path, request_id, recipient, **options):
                seen.append((Path(path).read_bytes(), options))
                if not send:
                    xml.write_bytes(MINI)
                    return {'highlevel_preflight_verified': True}
                return queued_result()
            with patch.object(backend.highlevel, 'client_identity', return_value=(10, 99)), \
                    patch.object(backend.highlevel, 'trial', side_effect=trial), \
                    patch.object(backend, 'history_snapshot', return_value=None), patch.object(backend.highlevel.base, 'save'):
                result = backend.send_xml({'operation': 'send_xml', 'file': str(xml), 'request_id': 'card-snapshot-01', 'recipient': 'filehelper'})
            self.assertTrue(result['ok']); self.assertFalse(result['recipient_delivery_verified'])
            self.assertEqual([v[0] for v in seen], [ARTICLE, ARTICLE])
            self.assertTrue(all(v[1]['request_kind'] == 'xml' for v in seen))

    def test_cli_routes_card_paths_and_source_without_shell(self):
        with patch.object(cli.client, 'call', return_value={'ok': True}) as call:
            cli.run(['forward', '--chat', 'fixture@chatroom', '--local-id', '4', '--recipient', 'filehelper', '--request-id', 'forward-cli-01'])
            self.assertEqual(call.call_args.args[0]['local_id'], 4)
            self.assertIsNone(call.call_args.args[0]['database'])
            cli.run(['send-xml', '--recipient', 'filehelper', '--file', 'custom.xml', '--request-id', 'xml-cli-01'])
            self.assertTrue(Path(call.call_args.args[0]['file']).is_absolute())

    def test_xml_build_requires_exclusive_queued_dispatch_and_keeps_sync_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for options in ({'highlevel_xml': True}, {'highlevel_xml': True, 'highlevel_dispatch': True, 'highlevel_image': True}):
                with self.assertRaises(ValueError):
                    native.compile_helper(root, source=Path(native.__file__).with_name('native_highlevel_helper.c'), **options)
            native.compile_helper(root, source=Path(native.__file__).with_name('native_highlevel_helper.c'), highlevel_dispatch=True, highlevel_xml=True)

    def test_xml_native_ownership_transfer_and_rejections(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'fixture'
            subprocess.run(['/usr/bin/gcc', '-O2', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-pthread', str(Path(__file__).with_name('native_xml_fixture.c')),
                            '-o', str(output)], check=True, capture_output=True, timeout=30)
            result = subprocess.run([str(output)], check=True, capture_output=True, text=True, timeout=10)
            self.assertIn('ownership', result.stdout)


if __name__ == '__main__':
    unittest.main()
