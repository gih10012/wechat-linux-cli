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
    def test_dispatch_build_keeps_sync_and_send_disabled(self):
        from wechat_linux_cli._native import native_send_candidate as base
        with tempfile.TemporaryDirectory() as temp:
            library = base.compile_helper(
                Path(temp), source=Path(base.__file__).with_name('native_highlevel_helper.c'),
                highlevel_dispatch=True)
            helper = ctypes.CDLL(str(library))
            report = Path(temp)/'must-not-exist.json'
            for symbol, send in [('ncut_highlevel_sync', 0), ('ncut_highlevel_sync', 1),
                                 ('ncut_highlevel_enqueue', 1)]:
                call = getattr(helper, symbol)
                call.argtypes = (ctypes.c_ulong, ctypes.c_char_p, ctypes.c_size_t,
                                 ctypes.c_char_p, ctypes.c_int)
                call.restype = ctypes.c_int
                self.assertEqual(call(1, b'\x01\x00\x01\x00ab', 6,
                                      str(report).encode(), send), errno.ENOSYS)
            self.assertFalse(report.exists())
            with self.assertRaisesRegex(ValueError, 'pinned high-level helper'):
                base.compile_helper(Path(temp), highlevel_dispatch=True)
            with self.assertRaisesRegex(ValueError, 'requires queued dispatch'):
                base.compile_helper(Path(temp), highlevel_send=True)

    def test_queued_send_build_still_rejects_synchronous_entry(self):
        from wechat_linux_cli._native import native_send_candidate as base
        with tempfile.TemporaryDirectory() as temp:
            library = base.compile_helper(
                Path(temp), source=Path(base.__file__).with_name('native_highlevel_helper.c'),
                highlevel_dispatch=True, highlevel_send=True)
            call = ctypes.CDLL(str(library)).ncut_highlevel_sync
            call.argtypes = (ctypes.c_ulong, ctypes.c_char_p, ctypes.c_size_t,
                             ctypes.c_char_p, ctypes.c_int)
            call.restype = ctypes.c_int
            report = Path(temp)/'must-not-exist.json'
            for send in (0, 1):
                self.assertEqual(call(1, b'\x01\x00\x01\x00ab', 6,
                                      str(report).encode(), send), errno.ENOSYS)
            self.assertFalse(report.exists())

    def test_send_requires_completed_queued_preflight_for_same_client(self):
        request_id = 'test-queued-proof-01'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work = root/('preflight-' + hashlib.sha256(request_id.encode()).hexdigest()[:24])
            work.mkdir()
            result = {'request_id': request_id, 'highlevel_preflight_verified': True,
                      'detached': True, 'queued_dispatch_call': True,
                      'worker': {'worker_done': True, 'manager_verified': True,
                                 'request_constructed': True, 'failure': 0,
                                 'submission_entered': False, 'live_callbacks': 0,
                                 'dispatch_pending': False}}
            (work/'result.json').write_text(json.dumps(result))
            (work/'config.json').write_text(json.dumps({'pid': 10, 'start_time': '99',
                'send': False, 'launch_symbol': 'ncut_highlevel_enqueue'}))
            native_highlevel_candidate.require_preflight(root, request_id, 10, 99)
            for pid, start in [(11, 99), (10, 100)]:
                with self.assertRaisesRegex(ValueError, 'VERIFIED_PREFLIGHT_REQUIRED'):
                    native_highlevel_candidate.require_preflight(root, request_id, pid, start)
            result['worker']['submission_entered'] = True
            (work/'result.json').write_text(json.dumps(result))
            with self.assertRaisesRegex(ValueError, 'VERIFIED_PREFLIGHT_REQUIRED'):
                native_highlevel_candidate.require_preflight(root, request_id, 10, 99)

    def test_production_helper_rejects_preflight_and_send_before_target_access(self):
        with tempfile.TemporaryDirectory() as temp:
            library = Path(temp)/'helper.so'
            compiler = subprocess.run(
                ['/usr/bin/gcc', '-shared', '-fPIC', '-O2', '-std=c11', '-Wall',
                 '-Wextra', '-Werror', '-pthread',
                 str(ROOT/'src/wechat_linux_cli/_native/native_highlevel_helper.c'),
                 '-o', str(library)], capture_output=True, text=True, timeout=30)
            self.assertEqual(compiler.returncode, 0, compiler.stderr)
            helper = ctypes.CDLL(str(library))
            report = Path(temp)/'unexpected-report.json'
            for symbol in ('ncut_highlevel_sync', 'ncut_highlevel_enqueue'):
                call = getattr(helper, symbol)
                call.argtypes = (ctypes.c_ulong, ctypes.c_char_p, ctypes.c_size_t,
                                 ctypes.c_char_p, ctypes.c_int)
                call.restype = ctypes.c_int
                for send in (0, 1):
                    with self.subTest(symbol=symbol, send=send):
                        self.assertEqual(call(1, b'\x01\x00\x01\x00ab', 6,
                                              str(report).encode(), send), errno.ENOSYS)
            self.assertFalse(report.exists())

    def test_payload_is_bounded_exact_native_id_and_utf8(self):
        payload = native_highlevel_candidate.payload_for('filehelper', '中文\n✅')
        self.assertEqual(int.from_bytes(payload[:2], 'little'), len(b'filehelper'))
        self.assertEqual(int.from_bytes(payload[2:4], 'little'), len('中文\n✅'.encode()))
        self.assertEqual(payload[4:], b'filehelper' + '中文\n✅'.encode())
        self.assertIn(b'fixture-native@weclaw', native_highlevel_candidate.payload_for('fixture-native@weclaw', 'HELLO'))
        for recipient in ('wxid_other_target', 'fixture@chatroom', 'filehelper', 'fixture@weclaw'):
            self.assertIn(recipient.encode(), native_highlevel_candidate.payload_for(recipient, 'hello'))
        for recipient, text in [('display name', 'hello'), ('../escape', 'hello'), ('filehelper', ''),
                                ('filehelper', 'a' * 1025), ('filehelper', 'a\0b')]:
            with self.subTest(recipient=recipient, length=len(text)):
                with self.assertRaises(ValueError):
                    native_highlevel_candidate.payload_for(recipient, text)

    def test_native_lifecycle_preflight_send_and_manager_rejection(self):
        for kind in ('text', 'image', 'file', 'sticker'):
          with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
            binary = Path(temp)/'fixture'
            compiler = subprocess.run(['/usr/bin/gcc', '-O2', '-std=c11', '-Wall', '-Wextra',
                                       '-Werror', '-pthread',
                                       *(['-DNCUT_HIGHLEVEL_' + kind.upper() + '_REQUEST=1']
                                         if kind != 'text' else []),
                                       str(ROOT/'tests/native_highlevel_fixture.c'),
                                       '-o', str(binary)], capture_output=True, text=True,
                                      timeout=30)
            self.assertEqual(compiler.returncode, 0, compiler.stderr)
            completed = subprocess.run([str(binary)], check=True, capture_output=True,
                                       text=True, timeout=10)
            self.assertIn('highlevel fixture passed', completed.stdout)

    def test_media_gate_precedes_file_and_process_access(self):
        with patch.object(native_highlevel_candidate, 'run_desktop_preparation') as prepare:
            with self.assertRaisesRegex(ValueError, 'MEDIA_TRIAL_DISABLED'):
                native_highlevel_candidate.trial(False, '/missing.png', 'image-test-01',
                                                request_kind='image', allow_live=True)
            prepare.assert_not_called()

    def test_image_proof_cannot_use_text_preflight(self):
        request_id = 'test-image-proof-01'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work = root/('preflight-' + hashlib.sha256(request_id.encode()).hexdigest()[:24])
            work.mkdir()
            (work/'result.json').write_text(json.dumps({'request_id': request_id,
                'highlevel_preflight_verified': True, 'detached': True,
                'queued_dispatch_call': True, 'worker': {'worker_done': True,
                'manager_verified': True, 'request_constructed': True,
                'live_callbacks': 0, 'dispatch_pending': False}}))
            config = {'pid': 10, 'start_time': 99, 'send': False,
                      'launch_symbol': 'ncut_highlevel_enqueue'}
            (work/'config.json').write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, 'VERIFIED_PREFLIGHT_REQUIRED'):
                native_highlevel_candidate.require_preflight(root, request_id, 10, 99,
                    request_kind='image', media_sha256='a' * 64)
            config.update(request_kind='image', media_sha256='a' * 64)
            (work/'config.json').write_text(json.dumps(config))
            (work/'request.json').write_text(json.dumps({'recipient': 'filehelper'}))
            native_highlevel_candidate.require_preflight(root, request_id, 10, 99,
                request_kind='image', media_sha256='a' * 64, recipient='filehelper')
            with self.assertRaisesRegex(ValueError, 'VERIFIED_PREFLIGHT_REQUIRED'):
                native_highlevel_candidate.require_preflight(root, request_id, 10, 99,
                    request_kind='image', media_sha256='a' * 64, recipient='fixture@weclaw')
            with self.assertRaisesRegex(ValueError, 'VERIFIED_PREFLIGHT_REQUIRED'):
                native_highlevel_candidate.require_preflight(root, request_id, 10, 99,
                    request_kind='image', media_sha256='b' * 64)

    def test_new_preflight_is_disabled_before_any_process_preparation(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
                os.environ, {'WECHAT_LINUX_RUNTIME_DIR': temp}), patch.object(
                native_highlevel_candidate, 'run_desktop_preparation') as prepare:
            with self.assertRaisesRegex(ValueError, 'HIGHLEVEL_PREFLIGHT_DISABLED'):
                native_highlevel_candidate.trial(
                    False, 'HELLO', 'test-disabled-preflight',
                    expected_pid=10, expected_start_time=99)
            prepare.assert_not_called()
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_file_snapshot_remains_readable_after_trial_cleanup(self):
        # The uploader can open the file only after the synchronous local
        # insertion has returned and trial cleanup has run.
        for failed in (False, True):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as temp:
                source = Path(temp)/'文件验收.txt'
                source.write_bytes(b'async upload fixture')
                with patch.dict(os.environ, {'WECHAT_LINUX_RUNTIME_DIR': temp}), \
                     patch.object(native_highlevel_candidate.base, 'has_ptrace_capability', return_value=True), \
                     patch.object(native_highlevel_candidate, 'client_identity', return_value=(10, 99)), \
                     patch.object(native_highlevel_candidate.base, 'process_running_untraced', return_value=True), \
                     patch.object(native_highlevel_candidate, 'run_desktop_preparation', return_value={}), \
                     patch.object(native_highlevel_candidate.base, 'compile_helper', return_value=Path(temp)/'helper.so'), \
                     patch.object(native_highlevel_candidate.base, 'run_injection',
                                  side_effect=OSError('fixture failure') if failed else None,
                                  return_value={'status': 'trial_finished'}):
                    native_highlevel_candidate.trial(False, str(source), 'file-permission-test',
                        expected_pid=10, expected_start_time=99, allow_live=True,
                        request_kind='file', allow_media_trial=True)
                    snapshot = native_highlevel_candidate.work_for('file-permission-test', False)/'input'/source.name
                    self.assertTrue(os.access(snapshot.parent, os.X_OK))
                    self.assertEqual(snapshot.parent.stat().st_mode & 0o777, 0o700)
                    self.assertEqual(snapshot.read_bytes(), source.read_bytes())

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

    def test_saved_send_replay_needs_no_live_guard_privilege_preflight_or_process_identity(self):
        request_id = 'saved-send-replay'
        payload = native_highlevel_candidate.payload_for('filehelper', 'HELLO')
        with tempfile.TemporaryDirectory() as temp, patch.dict(
                os.environ, {'WECHAT_LINUX_RUNTIME_DIR': temp}), patch.object(
                native_highlevel_candidate, 'client_identity') as identity:
            work = native_highlevel_candidate.work_for(request_id)
            work.mkdir(parents=True)
            (work/'request.json').write_text(json.dumps({
                'payload_sha256': hashlib.sha256(payload).hexdigest(), 'send': True}))
            (work/'result.json').write_text(json.dumps({'status': 'trial_finished'}))
            self.assertTrue(native_highlevel_candidate.trial(True, 'HELLO', request_id)['replayed'])
            identity.assert_not_called()
            with self.assertRaisesRegex(ValueError, 'REQUEST_ID_CONFLICT'):
                native_highlevel_candidate.trial(True, 'CHANGED', request_id)


if __name__ == '__main__':
    unittest.main()
