from contextlib import contextmanager, nullcontext
import os
from pathlib import Path
import sqlite3
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

    def test_group_replay_uses_same_member_set_and_rejects_changed_invites(self):
        group = dict(chat='room@chatroom', name='Own test group', group=True)
        spec = dict(self_member=dict(chat='self', name='Self'),
                    members=[dict(chat='a', name='A'), dict(chat='b', name='B')], member_count=3)
        with patch.object(call, 'target', return_value=(group, '/private/db')), patch.object(call, 'group_spec', return_value=spec):
            call.start('me', group['chat'], 123, 10, 'group', ['b', 'a'])
        count = len(self.actions)
        self.assertTrue(call.start('me', group['chat'], 123, 10, 'group', ['a', 'b'])['replayed'])
        with self.assertRaisesRegex(ValueError, 'CALL_REQUEST_ID_CONFLICT'):
            call.start('me', group['chat'], 123, 10, 'group', ['a'])
        self.assertEqual(len(self.actions), count)

    def test_private_call_rejects_group_members_before_navigation(self):
        with self.assertRaisesRegex(ValueError, 'MEMBERS_REQUIRE_GROUP_CHAT'):
            call.start('me', 'wxid_test', 123, 10, 'private', ['a'])
        self.assertEqual(self.actions, [])


class SearchNavigationTests(unittest.TestCase):
    def test_network_result_with_same_name_cannot_replace_local_group_result(self):
        names = ['搜索网络结果', 'Exact', '联系人', 'Other', '群聊', 'Exact', '聊天记录', 'Exact']
        rows = [dict(parent='/results', role='list item', showing=True, name=name) for name in names]
        self.assertEqual(Ui.search_positions(rows, dict(path='/results'), '群聊', 'Exact'), [2])
        self.assertEqual(Ui.search_positions(rows, dict(path='/results'), '联系人', 'Exact'), [])

    def test_search_requires_one_requested_section_and_visible_local_matches(self):
        rows = [dict(parent='/results', role='list item', showing=True, name='Exact')]
        self.assertEqual(Ui.search_positions(rows, dict(path='/results'), '群聊', 'Exact'), [])
        rows.insert(0, dict(parent='/results', role='list item', showing=True, name='群聊'))
        rows.append(dict(parent='/results', role='list item', showing=False, name='Exact'))
        self.assertEqual(Ui.search_positions(rows, dict(path='/results'), '群聊', 'Exact'), [0])
        rows.insert(0, dict(parent='/results', role='list item', showing=True, name='群聊'))
        self.assertEqual(Ui.search_positions(rows, dict(path='/results'), '群聊', 'Exact'), [])

    def test_search_with_lost_entry_focus_never_types(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        ui = Ui.__new__(Ui)
        ui.spi = SimpleNamespace(StateType=SimpleNamespace(FOCUSED='focused'))
        ui.shell = Mock(); ui.focused = Mock()
        node = Mock()
        node.get_component_iface.return_value.grab_focus.return_value = True
        node.get_state_set.return_value.contains.return_value = False
        with self.assertRaisesRegex(ValueError, 'CALL_SEARCH_FOCUS_CHANGED'):
            ui.search_text(dict(id=42), node, 'Exact')
        ui.shell.assert_not_called()

    def test_search_waits_for_async_keyboard_text_before_navigation(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        ui = Ui.__new__(Ui)
        read = Mock(side_effect=['partial', 'Exact'])
        ui.spi = SimpleNamespace(StateType=SimpleNamespace(FOCUSED='focused'), Text=SimpleNamespace(get_text=read))
        ui.shell = Mock(); ui.focused = Mock()
        node = Mock()
        node.get_component_iface.return_value.grab_focus.return_value = True
        node.get_state_set.return_value.contains.return_value = True
        with patch('wechat_linux_cli._call_ui.time.sleep'):
            ui.search_text(dict(id=42), node, 'Exact')
        self.assertEqual(read.call_count, 2)
        ui.shell.assert_called_once()


class GroupMembershipTests(unittest.TestCase):
    def specification(self, members, duplicate_label=False):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript('CREATE TABLE contact(username TEXT, remark TEXT, nick_name TEXT, alias TEXT);'
                           'CREATE TABLE name2id(username TEXT); CREATE TABLE chatroom_member(room_id INT, member_id INT);')
        conn.executemany('INSERT INTO name2id VALUES(?)', [('room@chatroom',), ('wxid_self',), ('a',), ('b',)])
        conn.executemany('INSERT INTO chatroom_member VALUES(1,?)', [(2,), (3,), (4,)])
        conn.executemany('INSERT INTO contact VALUES(?,?,?,?)',
                         [('wxid_self', '', 'Self', ''), ('a', '', 'Same' if duplicate_label else 'A', 'alias-a'),
                          ('b', '', 'Same' if duplicate_label else 'B', 'alias-b')])
        with patch.object(call, 'load_keys', return_value=dict(database_root='/cache/wxid_self_123/db_storage')), \
                patch.object(call, 'open_database', return_value=(conn, None)):
            return call.group_spec('me', 'room@chatroom', members)

    def test_only_exact_cached_other_members_can_be_invited(self):
        self.assertEqual(self.specification(['b'])['members'], [dict(chat='b', name='B', alias='alias-b')])
        for members in (['unknown'], ['wxid_self']):
            with self.assertRaisesRegex(ValueError, 'EXACT_OTHER_GROUP_MEMBER_IDS_REQUIRED'):
                self.specification(members)

    def test_same_name_members_are_refused_before_gui_selection(self):
        with self.assertRaisesRegex(ValueError, 'AMBIGUOUS_GROUP_MEMBER_LABEL'):
            self.specification(['a'], duplicate_label=True)

    def test_group_requires_explicit_nonduplicate_members(self):
        for members in (None, [], ['a', 'a'], list('123456789')):
            with self.assertRaisesRegex(ValueError, 'GROUP_REQUIRES_ONE_TO_EIGHT_DISTINCT_MEMBER_IDS'):
                self.specification(members)


class GroupSelectorTests(unittest.TestCase):
    def group(self, selection_failure=False):
        ui = Ui.__new__(Ui)
        ui.request = dict(group_spec=dict(members=[dict(chat='b', name='B')]))
        ui.dial_entered = False
        popup = dict(id=43)
        visible = [True]
        actions = []
        ui.open_target = lambda: dict(id=42)
        ui.main_frame = lambda rows: '/main'
        ui.tree = lambda: [dict(path='/selector', name='微信选择成员', role='filler', enabled=True,
                               showing=visible[0])]
        ui.window = lambda name: popup
        ui.windows = lambda: [popup] if visible[0] else []
        ui.focus = lambda window: None

        def button(window, name, frame, **kwargs):
            actions.append(name)
            if name == '取消':
                visible[0] = False

        def selection(window, frame):
            if selection_failure:
                raise ValueError('GROUP_MEMBER_FOCUS_NOT_VERIFIED')

        ui.button = button
        ui.select_group_members = selection
        return ui, actions

    def test_prepare_cancels_and_observes_original_selector_closed(self):
        ui, actions = self.group()
        result = ui.group(False)
        self.assertTrue(result['selector_closed_verified'])
        self.assertFalse(result['invitation_performed'])
        self.assertEqual(actions, ['语音通话', '取消'])

    def test_selection_error_cancels_without_submitting_invitation(self):
        ui, actions = self.group(selection_failure=True)
        with self.assertRaisesRegex(ValueError, 'GROUP_MEMBER_FOCUS_NOT_VERIFIED'):
            ui.group(True)
        self.assertFalse(ui.dial_entered)
        self.assertEqual(actions, ['语音通话', '取消'])

    def selector(self, wrong_focus=False, extra_checked=False):
        from types import SimpleNamespace
        ui = Ui.__new__(Ui)
        ui.request = dict(group_spec=dict(self_member=dict(chat='self', name='Self'), members=[dict(chat='b', name='B')]))
        rows = [dict(path='/members', role='list', name='请勾选需要添加的联系人', frame='/selector',
                     showing=True, enabled=True, node=SimpleNamespace(get_component_iface=lambda: SimpleNamespace(grab_focus=lambda: True)))]
        for name in ['Self', 'A', 'B']:
            rows.append(dict(path='/' + name, parent='/members', role='check box', name=name, frame='/selector',
                             showing=True, enabled=True, focused=False, checked=name == 'Self'))
        rows.append(dict(path='/complete', role='button', name='完成', frame='/selector', showing=True, enabled=True))
        actions = []

        def keys(window, *pressed):
            actions.append(pressed)
            if pressed[0] == 'Home':
                selected = rows[1 + pressed.count('Down')]
                if wrong_focus:
                    selected = rows[2]
                for r in rows[1:4]:
                    r['focused'] = r is selected
            elif pressed == ('space',):
                next(r for r in rows if r.get('focused'))['checked'] = True
                if extra_checked:
                    rows[2]['checked'] = True

        ui.keys = keys
        ui.tree = lambda: rows
        ui.group_member_list = lambda frame: (rows, rows[0], rows[1:4])
        ui.selection_count = lambda rows, frame: sum(r.get('checked', False) for r in rows)
        ui.target_matches = lambda rows: True
        return ui, actions

    def test_keyboard_selection_checks_only_requested_member(self):
        ui, actions = self.selector()
        ui.select_group_members(dict(id=42), '/selector')
        self.assertEqual(actions, [('Home', 'Down', 'Down'), ('space',)])
        self.assertEqual([r['name'] for r in ui.tree() if r.get('checked')], ['Self', 'B'])

    def test_wrong_focused_row_is_never_toggled(self):
        ui, actions = self.selector(wrong_focus=True)
        with self.assertRaisesRegex(ValueError, 'GROUP_MEMBER_FOCUS_NOT_VERIFIED'):
            ui.select_group_members(dict(id=42), '/selector')
        self.assertNotIn(('space',), actions)

    def test_additional_unrequested_selection_is_rejected_before_submit(self):
        ui, _ = self.selector(extra_checked=True)
        with self.assertRaisesRegex(ValueError, 'GROUP_MEMBER_CHECK_NOT_VERIFIED'):
            ui.select_group_members(dict(id=42), '/selector')


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
