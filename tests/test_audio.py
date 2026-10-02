import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from wechat_linux_cli import audio


class Player:
    returncode = None
    terminated = False

    def __init__(self, *args, **kwargs):
        self.remaining = 10

    def poll(self):
        if self.remaining:
            self.remaining -= 1
            return None
        self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, **kwargs):
        return self.returncode


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.file = self.directory / 'notification.wav'
        with wave.open(str(self.file), 'wb') as wav:
            wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            wav.writeframes(b'\x00\x01' * 1600)
        self.streams = [dict(index=42, client=3, driver='PipeWire', source=10,
                            mute=False, corked=False, properties={
                                'application.process.id': '123', 'application.name': 'test',
                                'media.name': 'call', 'object.serial': '99'})]
        self.sources = [dict(index=10, name='original')]
        self.sinks = []
        self.actions = []
        self.player = None
        self.move_then_fail = False
        for item in [patch.dict(os.environ, {'XDG_STATE_HOME': self.temp.name}),
                     patch.object(audio, 'process_start', return_value=456),
                     patch.object(audio, 'pulse', side_effect=self.pulse),
                     patch.object(audio.subprocess, 'Popen', side_effect=self.popen),
                     patch.object(audio.time, 'sleep')]:
            item.start()
            self.addCleanup(item.stop)

    def popen(self, *args, **kwargs):
        self.player = Player()
        return self.player

    def pulse(self, *args):
        if args == ('--format=json', 'info'):
            return json.dumps(dict(is_local='yes', cookie='same-server', server_string='local'))
        if args[:2] == ('--format=json', 'list'):
            values = {'source-outputs': self.streams, 'sources': self.sources, 'sinks': self.sinks}
            return json.dumps(values[args[2]])
        self.actions.append(args)
        if args[0] == 'load-module':
            name = args[2].split('=', 1)[1]
            self.sinks.append(dict(index=20, name=name, owner_module=77))
            self.sources.append(dict(index=21, name=name + '.monitor'))
            return '77'
        if args[0] == 'move-source-output':
            source = next(r for r in self.sources if r['name'] == args[2])
            self.streams[0]['source'] = source['index']
            if self.move_then_fail and args[2] != 'original':
                raise ValueError('SIMULATED_LOST_ACK_AFTER_MOVE')
            return ''
        if args[0] == 'unload-module':
            self.sinks = []
            self.sources = self.sources[:1]
            return ''
        self.fail(args)

    def play(self, request_id='test-one'):
        return audio.locked(audio._play, self.file, 123, 456, 42, request_id)

    def test_exact_process_and_stream_guard_prevents_other_app_routing(self):
        self.streams[0]['properties']['application.process.id'] = '124'
        with self.assertRaisesRegex(ValueError, 'EXACT_AUDIO_STREAM_NOT_FOUND'):
            self.play()
        self.assertEqual(self.actions, [])

    def test_mute_or_waiting_stream_never_plays(self):
        self.streams[0]['corked'] = True
        with self.assertRaisesRegex(ValueError, 'NOT_ACTIVE'):
            self.play()
        self.assertEqual(self.actions, [])
        self.assertIsNone(self.player)

    def test_finished_play_restores_route_and_id_replay_never_repeats_audio(self):
        result = self.play()
        self.assertTrue(result['ok'])
        self.assertFalse(result['remote_delivery_verified'])
        self.assertFalse(result['call_connection_verified'])
        self.assertEqual(self.streams[0]['source'], 10)
        self.assertEqual(self.sinks, [])
        before = list(self.actions)
        replay = self.play()
        self.assertTrue(replay['replayed'])
        self.assertFalse(replay['playback_performed_this_invocation'])
        self.assertEqual(self.actions, before)
        self.assertEqual(audio.record_path('test-one').stat().st_mode & 0o777, 0o600)

    def test_same_id_with_changed_content_fails_without_audio(self):
        self.play()
        content = self.file.read_bytes()
        self.file.write_bytes(content[:44] + content[44:].replace(b'\x00\x01', b'\x00\x02'))
        before = list(self.actions)
        with self.assertRaisesRegex(ValueError, 'REQUEST_ID_CONFLICT'):
            self.play()
        self.assertEqual(self.actions, before)

    def test_lost_move_ack_is_restored_without_playback(self):
        self.move_then_fail = True
        result = self.play()
        self.assertFalse(result['ok'])
        self.assertTrue(result['cleanup']['ok'])
        self.assertFalse(result['playback_started'])
        self.assertEqual(self.streams[0]['source'], 10)
        self.assertEqual(self.sinks, [])
        self.assertIsNone(self.player)

    def test_disconnected_or_reused_stream_stops_player_without_touching_replacement(self):
        original_rows = audio.rows
        reads = 0
        def rows(kind):
            nonlocal reads
            if kind == 'source-outputs':
                reads += 1
                if reads == 4:
                    self.streams[0]['properties']['object.serial'] = 'new-stream'
            return original_rows(kind)
        with patch.object(audio, 'rows', side_effect=rows):
            result = self.play()
        self.assertFalse(result['ok'])
        self.assertTrue(self.player.terminated)
        self.assertFalse(any(a[0] == 'move-source-output' and a[2] == 'original' for a in self.actions))
        self.assertEqual(self.sinks, [])

    def test_move_ack_metadata_delay_is_polled_before_play_and_restore(self):
        original_rows = audio.rows
        reads = 0
        def rows(kind):
            nonlocal reads
            result = original_rows(kind)
            if kind == 'source-outputs':
                reads += 1
                if reads == 3:
                    result[0]['source'] = 10
            return result
        with patch.object(audio, 'rows', side_effect=rows):
            result = self.play()
        self.assertTrue(result['ok'])
        self.assertEqual(self.streams[0]['source'], 10)

    def test_unknown_journal_blocks_new_audio_and_recovery_never_replays(self):
        result = self.play()
        result['status'] = 'playing'
        audio.write_record(audio.record_path('test-one'), result)
        with self.assertRaisesRegex(ValueError, 'UNRESOLVED'):
            self.play('different-id')
        before = list(self.actions)
        recovered = audio.locked(audio.recover, 'test-one')
        self.assertEqual(recovered['status'], 'recovered_no_replay')
        self.assertEqual(self.actions, before)

    def test_truncated_and_symlink_wav_rejected_before_routing(self):
        self.file.write_bytes(self.file.read_bytes()[:-2])
        with self.assertRaisesRegex(ValueError, 'TRUNCATED'):
            self.play()
        link = self.directory / 'link.wav'
        link.symlink_to(self.file)
        with self.assertRaises(OSError):
            audio.wav_snapshot(link)
        self.assertEqual(self.actions, [])

    def test_process_reuse_and_private_record_symlink_rejected(self):
        with patch.object(audio, 'process_start', return_value=457):
            with self.assertRaisesRegex(ValueError, 'IDENTITY_CHANGED'):
                self.play()
        self.play()
        result = audio.record_path('test-one')
        link = audio.record_path('linked')
        link.symlink_to(result)
        with self.assertRaises(OSError):
            audio.read_record(link)


if __name__ == '__main__':
    unittest.main()
