import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from wechat_linux_cli import client, service


class UnixServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'run/control.sock'
        self.calls = []
        def sender(request):
            self.calls.append(dict(request))
            return {'ok': True, 'request_id': request['request_id']}
        self.server = service.Service(self.path, sender)
        self.thread = threading.Thread(target=self.server.run)
        self.thread.start()

    def tearDown(self):
        self.server.stopping = True
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())
        self.assertFalse(self.path.exists())
        self.temp.cleanup()

    def test_actual_socket_health_and_unicode_request_preserve_values(self):
        health = client.call({'operation': 'health'}, self.path)
        self.assertTrue(health['ok'])
        self.assertEqual(health['uid'], os.getuid())
        request = {'operation': 'send_text', 'request_id': 'socket-test-0001',
                   'recipient': 'filehelper', 'text': '你好\nCLI ✅'}
        self.assertTrue(client.call(request, self.path)['ok'])
        self.assertEqual(self.calls, [request])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_invalid_request_cannot_reach_sender(self):
        cases = [None, {'operation': 'shell', 'command': 'echo no'},
                 {'operation': 'capture_keys', 'account': '../escape', 'seconds': 15},
                 {'operation': 'capture_keys', 'account': 'me', 'seconds': 46},
                 {'operation': 'send_text', 'request_id': 'abc', 'text': 'hello', 'recipient': 'filehelper'},
                 {'operation': 'send_text', 'request_id': 'valid-id', 'text': '', 'recipient': 'filehelper'},
                 {'operation': 'send_text', 'request_id': 'valid-id', 'text': 'hello', 'recipient': '../escape'},
                 {'operation': 'send_text', 'request_id': service.native.REQUEST_ID,
                  'text': 'changed legacy message', 'recipient': 'filehelper'}]
        for request in cases:
            with self.subTest(request=request):
                self.assertFalse(client.call(request, self.path)['ok'])
        self.assertEqual(self.calls, [])

    def test_conflicting_listener_is_rejected_without_unlinking_live_socket(self):
        with self.assertRaises(BlockingIOError):
            service.Service(self.path, lambda _: None)
        self.assertTrue(client.call({'operation': 'health'}, self.path)['ok'])

    def test_wrong_peer_uid_is_rejected_before_read(self):
        conn = Mock()
        conn.getsockopt.return_value = service.struct.pack('3i', 42, os.getuid()+1, os.getgid())
        self.server.handle(conn)
        conn.recv.assert_not_called()
        conn.sendall.assert_not_called()


class PendingOperationTests(unittest.TestCase):
    def test_preinjection_failure_clears_pending_but_keeps_result_for_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = service.Runner(root)
            request = {'operation': 'send_text', 'request_id': 'preflight-request',
                       'recipient': 'filehelper', 'text': 'private body'}
            with patch.object(service.native, 'has_ptrace_capability', return_value=True), \
                 patch.object(service.subprocess, 'Popen') as spawn, \
                 patch.object(service, 'trial_work', return_value=root/'trial'):
                child = spawn.return_value
                child.pid = 999999
                def finish(_payload, timeout):
                    output = spawn.call_args.kwargs['stdout']
                    output.write(json.dumps({'ok': False, 'code': 'NATIVE_OPERATION_FAILED',
                                             'automatic_retry_allowed': False}).encode())
                    output.flush()
                    return b'', b''
                child.communicate.side_effect = finish
                result = runner(request)
                self.assertEqual(result['code'], 'NATIVE_OPERATION_FAILED')
                self.assertIsNone(runner.state['pending'])
                self.assertEqual(runner.state['last_request_id'], request['request_id'])
                self.assertEqual(service.Runner(root).state['last_result'], result)

    def test_injection_config_keeps_pending_even_on_backend_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trial = root/'trial'
            trial.mkdir()
            (trial/'config.json').write_text('{}')
            runner = service.Runner(root)
            request = {'operation': 'send_text', 'request_id': 'uncertain-request',
                       'recipient': 'filehelper', 'text': 'private body'}
            with patch.object(service.native, 'has_ptrace_capability', return_value=True), \
                 patch.object(service.subprocess, 'Popen') as spawn, \
                 patch.object(service, 'trial_work', return_value=trial):
                child = spawn.return_value
                child.pid = 999999
                def finish(_payload, timeout):
                    output = spawn.call_args.kwargs['stdout']
                    output.write(b'{"ok": false, "code": "NATIVE_OPERATION_FAILED"}')
                    output.flush()
                    return b'', b''
                child.communicate.side_effect = finish
                self.assertEqual(runner(request)['code'], 'NATIVE_OPERATION_FAILED')
                self.assertEqual(runner.state['pending']['request_id'], request['request_id'])

    def test_timeout_persists_process_and_blocks_new_instance_without_killing_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = service.Runner(root, timeout=1)
            request = {'operation': 'send_text', 'request_id': 'pending-request',
                       'recipient': 'filehelper', 'text': 'private body'}
            with patch.object(service.native, 'has_ptrace_capability', return_value=True), \
                 patch.object(service.subprocess, 'Popen') as spawn:
                child = spawn.return_value
                child.pid = 999999
                child.communicate.side_effect = subprocess.TimeoutExpired('backend', 1)
                result = runner(request)
                self.assertEqual(result['code'], 'NATIVE_OPERATION_PENDING')
                self.assertEqual(runner.state['pending']['request_id'], 'pending-request')
                child.kill.assert_not_called()
                child.terminate.assert_not_called()
                self.assertNotIn('private body', repr(spawn.call_args))
                self.assertNotIn('private body', runner.state_path.read_text())
                second = service.Runner(root)
                self.assertEqual(second({**request, 'request_id': 'different-request'})['code'], 'PENDING_OPERATION')
                spawn.assert_called_once()

    def test_without_capability_returns_before_creating_backend(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(service.native, 'has_ptrace_capability', return_value=False), \
             patch.object(service.os, 'geteuid', return_value=1000), \
             patch.object(service.subprocess, 'Popen') as spawn:
            runner = service.Runner(Path(directory))
            self.assertEqual(runner({})['code'], 'PRIVILEGE_REQUIRED')
            spawn.assert_not_called()

    def test_capture_is_owner_scoped_and_requires_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = service.Runner(Path(directory))
            request = {'operation': 'capture_keys', 'account': 'me', 'seconds': 15}
            with patch.object(service.native, 'has_ptrace_capability', return_value=False), \
                 patch.object(service.subprocess, 'run') as scan:
                self.assertEqual(runner.capture_keys(request)['code'], 'PRIVILEGE_REQUIRED')
                scan.assert_not_called()
            with patch.object(service.native, 'has_ptrace_capability', return_value=True), \
                 patch.object(service.subprocess, 'run') as scan:
                scan.return_value.stdout = b'{"ok":true,"verified_key_count":2}'
                self.assertTrue(runner.capture_keys(request)['ok'])
                argv = scan.call_args.args[0]
                self.assertEqual(argv[1:4], ['-I', '-m', 'wechat_linux_cli._native.native_keys'])
                self.assertNotIn('--pid', argv)
                self.assertEqual(scan.call_args.kwargs['timeout'], 30)


if __name__ == '__main__':
    unittest.main()
