import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wechat_linux_cli import backend, media
from test_backend import queued_result


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'WECHAT_LINUX_RUNTIME_DIR': str(self.root/'state')})
        self.env.start()
        self.image = self.root/'image.png'
        self.image.write_bytes(b'\x89PNG\r\n\x1a\nfixture image')
        self.request = {'operation': 'send_image', 'request_id': 'image-fixture-01',
                        'recipient': 'fixture@chatroom', 'file': str(self.image)}

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_special_files_wrong_formats_and_oversize_rejected_before_client_access(self):
        fifo = self.root/'fifo'
        os.mkfifo(fifo)
        bad = self.root/'bad.png'
        bad.write_text('not image bytes')
        large = self.root/'large.png'
        with large.open('wb') as stream:
            stream.truncate(media.MAX_IMAGE_BYTES + 1)
        with patch.object(backend.highlevel, 'client_identity') as identity:
            for path in (fifo, bad, large, Path('relative.png')):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    backend.send_image({**self.request, 'file': str(path)})
        identity.assert_not_called()

    def test_replay_uses_content_not_filename_and_never_calls_native(self):
        work = backend.work_for(self.request['request_id'])
        work.mkdir(parents=True)
        sha = media.read_image(str(self.image))[2]
        (work/'request.json').write_text(json.dumps({
            'send': True, 'recipient': self.request['recipient'], 'request_kind': 'image',
            'payload_sha256': media.image_fingerprint(self.request['recipient'], sha)}))
        (work/'result.json').write_text(json.dumps(queued_result()))
        renamed = self.root/'same.jpg'
        renamed.write_bytes(self.image.read_bytes())
        with patch.object(backend.highlevel, 'client_identity') as identity, \
                patch.object(backend.highlevel, 'trial') as trial:
            result = backend.send_image({**self.request, 'file': str(renamed)})
            self.assertTrue(result['ok'])
            self.assertTrue(result['replayed'])
            renamed.write_bytes(renamed.read_bytes() + b'changed')
            self.assertEqual(backend.send_image({**self.request, 'file': str(renamed)})['code'],
                             'REQUEST_ID_CONFLICT')
            self.assertEqual(backend.replay({**self.request, 'operation': 'send_text', 'text': 'image'})['code'],
                             'REQUEST_ID_CONFLICT')
        identity.assert_not_called()
        trial.assert_not_called()

    def test_snapshot_remains_identical_between_preflight_and_send(self):
        original = self.image.read_bytes()
        seen = []
        def trial(send, path, request_id, recipient, **options):
            seen.append((send, Path(path).read_bytes(), options))
            self.assertEqual(recipient, self.request['recipient'])
            if not send:
                self.image.write_bytes(b'\x89PNG\r\n\x1a\nchanged original')
                return {'highlevel_preflight_verified': True}
            return queued_result()
        with patch.object(backend.highlevel, 'client_identity', return_value=(10, 99)), \
                patch.object(backend.highlevel, 'trial', side_effect=trial), \
                patch.object(backend, 'history_snapshot', return_value=None), \
                patch.object(backend.highlevel.base, 'save'):
            result = backend.send_image(self.request)
        self.assertTrue(result['ok'])
        self.assertEqual([s[1] for s in seen], [original, original])
        self.assertTrue(all(s[2]['request_kind'] == 'image' for s in seen))
        self.assertTrue(all(s[2]['allow_media_trial'] for s in seen))
        self.assertIsNone(result['local_history_integrated'])
        self.assertFalse(result['recipient_delivery_verified'])
        with self.assertRaisesRegex(ValueError, 'REQUEST_ID_CONFLICT'):
            backend.stage_image(self.request)

    def test_failed_image_construction_never_submits(self):
        with patch.object(backend.highlevel, 'client_identity', return_value=(10, 99)), \
                patch.object(backend.highlevel, 'trial', return_value={
                    'highlevel_preflight_verified': False}) as trial:
            result = backend.send_image(self.request)
        self.assertEqual(result['code'], 'NATIVE_PREFLIGHT_FAILED')
        trial.assert_called_once()
        self.assertFalse(trial.call_args.args[0])

    def test_new_image_row_is_candidate_and_does_not_prove_content_or_delivery(self):
        row = {'database': 'message_0.db', 'local_id': 10, 'type': 3, 'server_id': '42'}
        result = backend.image_history_evidence([], [row])
        self.assertIsNone(result['local_history_integrated'])
        self.assertEqual(result['local_history_type_matches'], 1)
        self.assertEqual(result['server_message_id'], '42')

    def test_file_snapshot_preserves_filename_including_reserved_state_names(self):
        original = self.root/'request.json'
        original.write_bytes(b'file payload without an image header')
        request = {**self.request, 'operation': 'send_file', 'file': str(original)}
        path = Path(backend.stage_media(request, 'file'))
        self.assertEqual(path.name, original.name)
        self.assertEqual(path.read_bytes(), original.read_bytes())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads((path.parent.parent/'request.json').read_text())['request_kind'], 'file')
        renamed = self.root/'renamed.json'
        renamed.write_bytes(original.read_bytes())
        with self.assertRaisesRegex(ValueError, 'REQUEST_ID_CONFLICT'):
            backend.stage_media({**request, 'file': str(renamed)}, 'file')

    def test_file_replay_binds_bytes_and_filename_without_native_calls(self):
        request = {**self.request, 'operation': 'send_file'}
        work = backend.work_for(request['request_id'])
        work.mkdir(parents=True)
        _data, name, sha = media.read_file(request['file'])
        (work/'request.json').write_text(json.dumps({'send': True,
            'recipient': request['recipient'], 'request_kind': 'file',
            'payload_sha256': media.file_fingerprint(request['recipient'], name, sha)}))
        (work/'result.json').write_text(json.dumps(queued_result()))
        with patch.object(backend.highlevel, 'client_identity') as identity, \
                patch.object(backend.highlevel, 'trial') as trial:
            self.assertTrue(backend.send_file(request)['replayed'])
            self.assertEqual(backend.send_image(self.request)['code'], 'REQUEST_ID_CONFLICT')
            changed = self.root/'renamed.png'
            changed.write_bytes(self.image.read_bytes())
            self.assertEqual(backend.send_file({**request, 'file': str(changed)})['code'],
                             'REQUEST_ID_CONFLICT')
        identity.assert_not_called()
        trial.assert_not_called()

    def test_file_preflight_and_send_share_snapshot_name_bytes_and_kind(self):
        request = {**self.request, 'operation': 'send_file'}
        original = self.image.read_bytes()
        seen = []
        def trial(send, path, request_id, recipient, **options):
            seen.append((send, Path(path).name, Path(path).read_bytes(), options))
            if not send:
                self.image.write_bytes(b'changed original')
                return {'highlevel_preflight_verified': True}
            return queued_result()
        with patch.object(backend.highlevel, 'client_identity', return_value=(10, 99)), \
                patch.object(backend.highlevel, 'trial', side_effect=trial), \
                patch.object(backend, 'history_snapshot', return_value=None), \
                patch.object(backend.highlevel.base, 'save'):
            result = backend.send_file(request)
        self.assertTrue(result['ok'])
        self.assertEqual([(s[1], s[2]) for s in seen], [(self.image.name, original)] * 2)
        self.assertTrue(all(s[3]['request_kind'] == 'file' for s in seen))
        self.assertFalse(result['recipient_delivery_verified'])


if __name__ == '__main__':
    unittest.main()
