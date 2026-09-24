import ctypes
import errno
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_linux_cli._native import native_highlevel_candidate


ROOT = Path(__file__).resolve().parents[1]


class HighLevelCandidateTests(unittest.TestCase):
    def test_production_helper_rejects_send_before_target_access(self):
        with tempfile.TemporaryDirectory() as temp:
            library = Path(temp)/'helper.so'
            compiler = subprocess.run(
                ['/usr/bin/gcc', '-shared', '-fPIC', '-O2', '-std=c11', '-Wall',
                 '-Wextra', '-Werror', '-pthread',
                 str(ROOT/'src/wechat_linux_cli/_native/native_highlevel_helper.c'),
                 '-o', str(library)], capture_output=True, text=True, timeout=30)
            self.assertEqual(compiler.returncode, 0, compiler.stderr)
            call = ctypes.CDLL(str(library)).ncut_highlevel_sync
            call.argtypes = (ctypes.c_ulong, ctypes.c_char_p, ctypes.c_size_t,
                             ctypes.c_char_p, ctypes.c_int)
            call.restype = ctypes.c_int
            report = Path(temp)/'unexpected-report.json'
            self.assertEqual(call(1, b'\x01\x00\x01\x00ab', 6,
                                  str(report).encode(), 1), errno.ENOSYS)
            self.assertFalse(report.exists())

    def test_payload_is_bounded_exact_native_id_and_utf8(self):
        payload = native_highlevel_candidate.payload_for('filehelper', '中文\n✅')
        self.assertEqual(int.from_bytes(payload[:2], 'little'), len(b'filehelper'))
        self.assertEqual(int.from_bytes(payload[2:4], 'little'), len('中文\n✅'.encode()))
        self.assertEqual(payload[4:], b'filehelper' + '中文\n✅'.encode())
        for recipient, text in [('other', 'hello'), ('filehelper', ''),
                                ('filehelper', 'a' * 1025), ('filehelper', 'a\0b')]:
            with self.subTest(recipient=recipient, length=len(text)):
                with self.assertRaises(ValueError):
                    native_highlevel_candidate.payload_for(recipient, text)

    def test_native_lifecycle_preflight_send_and_manager_rejection(self):
        with tempfile.TemporaryDirectory() as temp:
            binary = Path(temp)/'fixture'
            compiler = subprocess.run(['/usr/bin/gcc', '-O2', '-std=c11', '-Wall', '-Wextra',
                                       '-Werror', '-pthread',
                                       str(ROOT/'tests/native_highlevel_fixture.c'),
                                       '-o', str(binary)], capture_output=True, text=True,
                                      timeout=30)
            self.assertEqual(compiler.returncode, 0, compiler.stderr)
            completed = subprocess.run([str(binary)], check=True, capture_output=True,
                                       text=True, timeout=10)
            self.assertIn('highlevel fixture passed', completed.stdout)

    def test_same_preflight_id_replays_record_without_a_new_native_call(self):
        request_id = 'test-replay-highlevel-1'
        payload = native_highlevel_candidate.payload_for('filehelper', 'HELLO')
        with tempfile.TemporaryDirectory() as temp, patch.dict(
                os.environ, {'WECHAT_LINUX_RUNTIME_DIR': temp}):
            work = (Path(temp)/'native-highlevel'/
                    ('preflight-' + hashlib.sha256(request_id.encode()).hexdigest()[:24]))
            work.mkdir(parents=True)
            (work/'request.json').write_text(json.dumps({
                'payload_sha256': hashlib.sha256(payload).hexdigest(), 'send': False}))
            (work/'result.json').write_text(json.dumps({'status': 'trial_finished'}))
            proof = {'event_tid': 12, 'expected_pid': 10, 'expected_start_time': 99}
            replay = native_highlevel_candidate.trial(False, 'HELLO', request_id, **proof)
            self.assertTrue(replay['replayed'])
            self.assertEqual(replay['status'], 'trial_finished')
            self.assertEqual(replay['result_path'], str(work/'result.json'))
            with self.assertRaisesRegex(ValueError, 'REQUEST_ID_CONFLICT'):
                native_highlevel_candidate.trial(False, 'CHANGED', request_id, **proof)
            with self.assertRaisesRegex(ValueError, 'HIGHLEVEL_SEND_NOT_READY'):
                native_highlevel_candidate.trial(True, 'HELLO', request_id, **proof)


if __name__ == '__main__':
    unittest.main()
