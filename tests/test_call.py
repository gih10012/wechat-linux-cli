from contextlib import contextmanager, nullcontext
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wechat_linux_cli import call
from wechat_linux_cli._call_ui import Ui

REAL_UI = call.ui


class CallTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        env = patch.dict(os.environ, XDG_STATE_HOME=temp.name)
        env.start()
        self.addCleanup(env.stop)
        self.handle = dict(pid=123, start_time=10, bus=':1.test', path='/call/1', window_id=42)
        self.live = dict(ok=True, active=None, incoming=[], call_connection_verified=False)
        self.actions = []
        for module, name, effect in (
            (call, 'target', lambda *args: (dict(chat='wxid_test', name='Test', alias='', external=False), '/private/db')),
            (call, 'desktop_session', lambda: nullcontext()),
            (call, 'ui', self.ui),
            (call, 'process_start', lambda pid: 10),
            (call, 'load_keys', lambda account: dict(database_root='/private/db')),
        ):
            mock = patch.object(module, name, side_effect=effect)
            mock.start()
            self.addCleanup(mock.stop)

    def ui(self, request):
        self.actions.append(request['operation'])
        if request['operation'] == 'inspect':
            return self.live
        if request['operation'] in ('start', 'answer'):
            return dict(ok=True, active=dict(handle=self.handle, state='ringing'),
                        call_connection_verified=False, dial_entered=True)
        return dict(ok=True, status='ended', hangup_observed=True)

    def start(self, request_id='trial', chat='wxid_test'):
        return call.start('me', chat, 123, 10, request_id)

    def test_same_id_replays_after_end_without_another_invitation(self):
        self.assertEqual(self.start()['status'], 'active')
        call.hangup('trial')
        count = len(self.actions)
        self.assertTrue(self.start()['replayed'])
        self.assertEqual(len(self.actions), count)
        self.assertEqual(self.actions.count('start'), 1)
        self.assertEqual(call.record_path('trial').stat().st_mode & 0o777, 0o600)

    def test_changed_target_conflicts_without_navigation(self):
        self.start()
        count = len(self.actions)
        with self.assertRaisesRegex(ValueError, 'CALL_REQUEST_ID_CONFLICT'):
            self.start(chat='wxid_other')
        self.assertEqual(len(self.actions), count)

    def test_unknown_invitation_blocks_new_id(self):
        with patch.object(call, 'ui', side_effect=[self.live, dict(ok=False, dial_entered=True)]):
            self.assertEqual(self.start()['status'], 'dial_unknown')
        with self.assertRaisesRegex(ValueError, 'PREVIOUS_CALL_RESULT_UNRESOLVED'):
            self.start('new')
        self.assertNotIn('start', self.actions)

    def test_changed_window_is_never_hung_up(self):
        self.start()
        self.live = dict(ok=True, active=dict(handle=dict(self.handle, path='/call/2')))
        with self.assertRaisesRegex(ValueError, 'CALL_HANDLE_CHANGED'):
            call.hangup('trial')
        self.assertNotIn('hangup', self.actions)

    def test_display_restore_failure_preserves_observed_handle(self):
        @contextmanager
        def failed_restore():
            yield
            raise ValueError('DISPLAY_RESTORE_FAILED')
        with patch.object(call, 'desktop_session', side_effect=failed_restore):
            result = self.start()
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'active')
        self.assertEqual(result['handle'], self.handle)
        self.assertEqual(call.read_record(call.record_path('trial'))['handle'], self.handle)

    def test_display_entry_failure_does_not_dial_or_mark_unknown(self):
        with patch.object(call, 'desktop_session', side_effect=ValueError('DISPLAY_ENTRY_FAILED')):
            result = self.start()
        self.assertEqual(result['status'], 'failed_no_call')
        self.assertNotIn('start', self.actions)

    def test_unrelated_older_client_is_not_assumed_to_have_ended(self):
        self.start()
        with patch.object(call, 'inspect', side_effect=[self.live, dict(ok=False, code='CLIENT_UNREACHABLE')]):
            with self.assertRaisesRegex(ValueError, 'PREVIOUS_CALL_RESULT_UNRESOLVED'):
                self.start('new')
        self.assertEqual(call.read_record(call.record_path('trial'))['status'], 'active')

    def test_symlink_request_record_is_refused(self):
        source = self.directory / 'unrelated.json'
        source.write_text('{}')
        call.record_path('trial').symlink_to(source)
        with self.assertRaises(OSError):
            self.start()
        self.assertEqual(self.actions, [])

    def test_play_refuses_waiting_call_without_touching_audio(self):
        self.start()
        self.live = dict(ok=True, active=dict(handle=self.handle, state='ringing'), call_connection_verified=False)
        with patch('wechat_linux_cli.audio.locked') as audio:
            result = call.play('trial', '/file.wav', 'audio')
            self.assertEqual(result['code'], 'CALL_NOT_CONNECTED')
            audio.assert_not_called()

    def test_play_refuses_replaced_call_without_touching_audio(self):
        self.start()
        self.live = dict(ok=True, active=dict(handle=dict(self.handle, path='/call/new'), state='connected'),
                         call_connection_verified=True)
        with patch('wechat_linux_cli.audio.locked') as audio:
            with self.assertRaisesRegex(ValueError, 'CALL_HANDLE_NO_LONGER_ACTIVE'):
                call.play('trial', '/file.wav', 'audio')
            audio.assert_not_called()

    def test_play_requires_unique_client_capture_stream(self):
        self.start()
        self.live = dict(ok=True, active=dict(handle=self.handle, state='connected'), call_connection_verified=True)
        with patch('wechat_linux_cli.audio.streams', return_value=dict(items=[dict(index=1), dict(index=2)])):
            with self.assertRaisesRegex(ValueError, 'CALL_CAPTURE_STREAM_NOT_UNIQUE'):
                call.play('trial', '/file.wav', 'audio')

    def test_connected_play_uses_original_call_process_and_audio_id(self):
        self.start()
        self.live = dict(ok=True, active=dict(handle=self.handle, state='connected'), call_connection_verified=True)
        with patch('wechat_linux_cli.audio.streams', return_value=dict(items=[dict(index=1)])), \
                patch('wechat_linux_cli.audio.locked', return_value=dict(ok=True, replayed=True)) as audio:
            result = call.play('trial', '/file.wav', 'audio-id')
        self.assertEqual(audio.call_args.args[1:], ('/file.wav', 123, 10, 1, 'audio-id'))
        self.assertTrue(result['call_connection_verified_before_playback'])
        self.assertTrue(result['replayed'])

    def test_dead_process_status_keeps_historical_request_readable(self):
        self.start()
        with patch.object(call, 'process_start', side_effect=FileNotFoundError):
            result = call.status('trial')
        self.assertEqual(result['request_id'], 'trial')
        self.assertTrue(result['live']['client_process_ended'])
        self.assertFalse(result['current_call_matches'])

    def test_confirming_unknown_ended_never_redials_or_claims_outcome(self):
        with patch.object(call, 'ui', side_effect=[self.live, dict(ok=False, dial_entered=True)]):
            self.start()
        self.assertTrue(call.resolve_ended('trial')['resolution_only'])
        saved = call.read_record(call.record_path('trial'))
        self.assertEqual(saved['invitation_outcome'], 'unknown')
        self.assertEqual(saved['previous_status'], 'dial_unknown')
        self.assertTrue(self.start()['replayed'])
        self.assertNotIn('start', self.actions)

    def test_confirm_ended_refuses_any_active_call(self):
        self.start()
        self.live = dict(ok=True, active=dict(handle=self.handle))
        with self.assertRaisesRegex(ValueError, 'CALL_NOT_CONFIRMED_ENDED'):
            call.resolve_ended('trial')

    def test_answer_replays_without_accepting_a_second_invitation(self):
        token = 'ab' * 32
        self.live = dict(ok=True, active=None, incoming=[dict(invitation_token=token, caption='Test')])
        result = call.answer('me', 123, 10, token, 'answer')
        self.assertEqual(result['status'], 'active')
        self.assertFalse(result['caller_identity_verified'])
        self.assertFalse(result['invitation_performed'])
        self.assertTrue(call.answer('me', 123, 10, token, 'answer')['replayed'])
        self.assertEqual(self.actions.count('answer'), 1)

    def test_old_invitation_token_is_refused_without_accept_input(self):
        self.live = dict(ok=True, active=None, incoming=[dict(invitation_token='cd' * 32)])
        with self.assertRaisesRegex(ValueError, 'EXACT_INCOMING_INVITATION_NOT_FOUND'):
            call.answer('me', 123, 10, 'ab' * 32, 'answer')
        self.assertNotIn('answer', self.actions)

    def test_lost_accept_result_blocks_another_id(self):
        token = 'ab' * 32
        incoming = dict(ok=True, active=None, incoming=[dict(invitation_token=token)])
        with patch.object(call, 'ui', side_effect=[incoming, dict(ok=False, accept_entered=True)]):
            self.assertEqual(call.answer('me', 123, 10, token, 'answer')['status'], 'accept_unknown')
        with self.assertRaisesRegex(ValueError, 'PREVIOUS_CALL_RESULT_UNRESOLVED'):
            self.start('new')

    def test_accept_transport_timeout_is_unknown(self):
        import subprocess
        with patch.object(call.subprocess, 'run', side_effect=subprocess.TimeoutExpired('helper', 20)):
            result = REAL_UI(dict(operation='answer', pid=123, start_time=10))
        self.assertTrue(result['accept_entered'])
        self.assertFalse(result['dial_entered'])


class QtStateTests(unittest.TestCase):
    def test_incoming_window_uses_frame_size_instead_of_same_title_main_window(self):
        ui = Ui.__new__(Ui)
        rows = [dict(path='/popup', role='frame', name='微信', bounds=(0, 0, 300, 106))]
        popup = dict(id=43, title='微信', layout=dict(window_size=[300, 106]))
        ui.windows = lambda: [dict(id=42, title='微信', layout=dict(window_size=[838, 1017])), popup]
        self.assertEqual(ui.invitation_window(rows, dict(frame='/popup'))['id'], 43)
        ui.windows = lambda: [popup, dict(popup, id=44)]
        with self.assertRaisesRegex(ValueError, 'INCOMING_WINDOW_NOT_UNIQUE'):
            ui.invitation_window(rows, dict(frame='/popup'))

    def test_replaced_accept_control_receives_no_keyboard_input(self):
        ui = Ui.__new__(Ui)
        ui.accept_entered = False
        ui.focus = lambda window: None
        ui.tree = lambda: [dict(showing=True, enabled=True, role='button', name='接听',
                               frame='/popup', path='/accept/new')]
        actions = []
        ui.keys = lambda *args: actions.append(args)
        with self.assertRaisesRegex(ValueError, 'CALL_CONTROL_OBJECT_CHANGED'):
            ui.button(dict(id=43), '接听', '/popup', '/accept/old', accepting=True)
        self.assertFalse(ui.accept_entered)
        self.assertEqual(actions, [])

    def state(self, labels, buttons, other_labels=()):
        ui = Ui.__new__(Ui)
        ui.pid = 123
        ui.request = dict(start_time=10)
        rows = [dict(role='frame', name='语音聊天', showing=True, path='/call/1', frame='/call/1', bus=':1.test')]
        rows += [dict(role=role, name=name, frame='/call/1', showing=True)
                 for role, names in [('label', labels), ('button', buttons)] for name in names]
        rows += [dict(role='label', name=name, frame='/main', showing=True) for name in other_labels]
        ui.tree = lambda: rows
        ui.windows = lambda: [dict(title='语音聊天', id=42)]
        return ui.inspect()

    def test_ringing_does_not_prove_connection(self):
        result = self.state(['Test', '等待对方接受邀请..'], ['取消', '麦克风已开'])
        self.assertEqual(result['active']['state'], 'ringing')
        self.assertFalse(result['call_connection_verified'])

    def test_timer_in_another_window_does_not_prove_connection(self):
        result = self.state(['Test'], ['挂断'], ['00:42'])
        self.assertFalse(result['call_connection_verified'])

    def test_call_timer_and_hangup_button_prove_gui_connected_state(self):
        result = self.state(['Test', '00:42'], ['挂断'])
        self.assertTrue(result['call_connection_verified'])
        self.assertEqual(result['active']['handle']['path'], '/call/1')

    def test_incoming_token_ignores_animation_but_changes_with_widget(self):
        ui = Ui.__new__(Ui)
        ui.pid = 123
        ui.request = dict(start_time=10)
        ui.invitation_window = lambda rows, caption: dict(id=42)
        caption = dict(role='label', name='Test邀请你语音通话.', showing=True, frame='/incoming/1',
                       path='/caption/1', bus=':1.test')
        accept = dict(role='button', name='接听', showing=True, enabled=True,
                      frame='/incoming/1', path='/accept/1')
        ui.tree = lambda: [caption, accept]
        first = ui.inspect()['incoming'][0]
        caption['name'] = 'Test邀请你语音通话...'
        self.assertEqual(first['invitation_token'], ui.inspect()['incoming'][0]['invitation_token'])
        caption['path'] = '/caption/2'
        self.assertNotEqual(first['invitation_token'], ui.inspect()['incoming'][0]['invitation_token'])
        self.assertFalse(first['caller_identity_verified'])

    def test_changed_invitation_between_focus_and_accept_refuses_button(self):
        ui = Ui.__new__(Ui)
        ui.request = dict(invitation_token='original')
        invitation = dict(invitation_token='original', frame='/incoming/1', window_id=42)
        states = iter([dict(active=None, incoming=[invitation]), dict(active=None, incoming=[])])
        ui.inspect = lambda: next(states)
        ui.windows = lambda: [dict(id=42)]
        ui.focus = lambda window: None
        actions = []
        ui.button = lambda *args: actions.append(args)
        with self.assertRaisesRegex(ValueError, 'INCOMING_INVITATION_CHANGED'):
            ui.answer()
        self.assertEqual(actions, [])


if __name__ == '__main__':
    unittest.main()
