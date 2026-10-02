"""System-Python AT-SPI helper for normal Qt call controls on niri.

Invoked with a JSON request on stdin. No screen coordinates or private APIs.
The parent journals before dial; an error after Return must never be retried.
"""
import json
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import time


class Ui:
    def __init__(self, request):
        self.request = request
        self.pid = request['pid']
        self.dial_entered = False
        self.accept_entered = False
        process = self.identity()
        for item in (process / 'environ').read_bytes().split(b'\0'):
            name, _, value = item.partition(b'=')
            if name in (b'DISPLAY', b'WAYLAND_DISPLAY', b'XDG_RUNTIME_DIR', b'DBUS_SESSION_BUS_ADDRESS'):
                os.environ[name.decode()] = value.decode()
        import gi
        gi.require_version('Atspi', '2.0')
        from gi.repository import Atspi
        self.spi = Atspi
        Atspi.set_timeout(600, 600)

    def identity(self):
        path = Path('/proc') / str(self.pid)
        fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
        if path.stat().st_uid != os.getuid() or fields[0] in ('Z', 'X') or int(fields[19]) != self.request['start_time']:
            raise ValueError('CALL_PROCESS_CHANGED')
        if Path(os.readlink(path / 'exe')).name != 'wechat':
            raise ValueError('CALL_CLIENT_EXECUTABLE_MISMATCH')
        if self.request.get('database_root'):
            root = Path(self.request['database_root']).resolve()
            expected = {root / 'contact/contact.db', root / 'session/session.db'}
            opened = set()
            for fd in (path / 'fd').iterdir():
                try:
                    opened.add(fd.resolve(strict=True))
                except OSError:
                    continue
            if not expected.issubset(opened):
                raise ValueError('CALL_ACCOUNT_DATABASE_NOT_OPEN')
        return path

    def shell(self, *args):
        return subprocess.run(args, env=os.environ, capture_output=True, text=True,
                              check=True, timeout=5).stdout

    def windows(self):
        return [w for w in json.loads(self.shell('niri', 'msg', '--json', 'windows'))
                if w.get('pid') == self.pid and w.get('app_id') == 'wechat']

    def window(self, title):
        items = [w for w in self.windows() if w['title'] == title]
        if len(items) != 1:
            raise ValueError('CALL_WINDOW_NOT_UNIQUE')
        return items[0]

    def invitation_window(self, rows, caption):
        frame = next(r for r in rows if r['path'] == caption['frame'] and r['role'] == 'frame')
        width, height = frame['bounds'][2:]
        items = [w for w in self.windows() if w['title'] == frame['name']
                 and w.get('layout', {}).get('window_size') == [width, height]]
        if width <= 0 or height <= 0 or len(items) != 1:
            raise ValueError('INCOMING_WINDOW_NOT_UNIQUE')
        return items[0]

    def focused(self, window):
        self.identity()
        current = json.loads(self.shell('niri', 'msg', '--json', 'focused-window'))
        if not current or current['id'] != window['id'] or current['pid'] != self.pid:
            raise ValueError('CALL_WINDOW_FOCUS_CHANGED')

    def focus(self, window):
        self.shell('niri', 'msg', 'action', 'focus-window', '--id', str(window['id']))
        self.focused(window)

    def keys(self, window, *keys):
        self.focused(window)
        args = ['wtype']
        for key in keys:
            args += ['-k', key]
        self.shell(*args)

    def search_text(self, window, node, text):
        # Qt EditableText can change the displayed property without firing the
        # normal search signal. Use keyboard events in the verified entry.
        if not node.get_component_iface().grab_focus():
            raise ValueError('CALL_SEARCH_FOCUS_FAILED')
        node.clear_cache()
        if not node.get_state_set().contains(self.spi.StateType.FOCUSED):
            raise ValueError('CALL_SEARCH_FOCUS_CHANGED')
        self.focused(window)
        self.shell('wtype', '-M', 'ctrl', '-k', 'a', '-m', 'ctrl', '-k', 'BackSpace', '--', text)
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            node.clear_cache()
            if self.spi.Text.get_text(node, 0, -1) == text:
                return
            time.sleep(.02)
        raise ValueError('CALL_SEARCH_TEXT_NOT_VERIFIED')

    def tree(self):
        self.identity()
        desktop = self.spi.get_desktop(0)
        apps = [desktop.get_child_at_index(i) for i in range(desktop.get_child_count())]
        apps = [a for a in apps if a.get_process_id() == self.pid]
        if len(apps) != 1 or apps[0].get_toolkit_name() != 'Qt':
            raise ValueError('CALL_ACCESSIBILITY_APPLICATION_NOT_UNIQUE')
        rows = []
        deadline = time.monotonic() + 8

        def visit(node, parent=None, frame=None, depth=0):
            if depth > 60 or len(rows) >= 2000 or time.monotonic() > deadline:
                raise ValueError('CALL_ACCESSIBILITY_TREE_LIMIT')
            node.clear_cache()
            role = node.get_role_name()
            if role == 'frame' or (depth == 1 and role == 'filler'):
                frame = node.path
            state = node.get_state_set()
            component = node.get_component_iface()
            bounds = component.get_extents(self.spi.CoordType.SCREEN) if component else None
            rows.append(dict(node=node, path=node.path, bus=node.app.bus_name, parent=parent,
                             frame=frame, name=node.get_name() or '', role=role,
                             showing=state.contains(self.spi.StateType.SHOWING),
                             focused=state.contains(self.spi.StateType.FOCUSED),
                             checked=state.contains(self.spi.StateType.CHECKED),
                             enabled=state.contains(self.spi.StateType.ENABLED),
                             bounds=(bounds.x, bounds.y, bounds.width, bounds.height) if bounds else (0, 0, 0, 0)))
            for index in range(node.get_child_count()):
                visit(node.get_child_at_index(index), node.path, frame, depth + 1)

        visit(apps[0])
        return rows

    def one(self, rows, name, role, frame=None):
        matches = [r for r in rows if r['showing'] and r['name'] == name and r['role'] == role
                   and (frame is None or r['frame'] == frame)]
        if len(matches) != 1 or not matches[0]['enabled']:
            raise ValueError('CALL_CONTROL_NOT_UNIQUE_OR_DISABLED')
        return matches[0]

    def button(self, window, name, frame, expected_path=None, accepting=False, dialing=False):
        self.focus(window)
        row = self.one(self.tree(), name, 'button', frame)
        if expected_path is not None and row['path'] != expected_path:
            raise ValueError('CALL_CONTROL_OBJECT_CHANGED')
        node = row['node']
        if not node.get_component_iface().grab_focus():
            raise ValueError('CALL_ELEMENT_FOCUS_FAILED')
        node.clear_cache()
        if not node.get_state_set().contains(self.spi.StateType.FOCUSED):
            raise ValueError('CALL_ELEMENT_FOCUS_CHANGED')
        if accepting:
            self.accept_entered = True
        if dialing:
            self.dial_entered = True
        self.keys(window, 'space')

    def main_frame(self, rows):
        return self.one(rows, '微信', 'frame')['path']

    def target_matches(self, rows):
        frame = self.main_frame(rows)
        buttons = [r for r in rows if r['frame'] == frame and r['role'] == 'button'
                   and r['showing'] and r['name'] == '语音通话']
        if len(buttons) != 1:
            return False
        _, y, _, h = buttons[0]['bounds']
        labels = {r['name'] for r in rows if r['frame'] == frame and r['role'] == 'label'
                  and r['showing'] and y - 20 <= r['bounds'][1] < y + h}
        target = self.request['target']
        if target.get('group'):
            headings = [s for s in labels if re.fullmatch(re.escape(target['name']) + r'\(\d+\)', s)]
            return target['name'] in labels and len(headings) == 1 and (
                'member_count' not in target or headings[0] == f"{target['name']}({target['member_count']})")
        external = any(s.startswith('@') for s in labels)
        return target['name'] in labels and external == target['external']

    def inspect(self):
        rows = self.tree()
        frames = [r for r in rows if r['role'] == 'frame'
                  and r['name'] in ('语音聊天', '语音通话') and r['showing']]
        incoming = []
        captions = [r for r in rows if r['showing'] and r['role'] == 'label'
                    and re.fullmatch(r'.+邀请你语音通话[.。…]*', r['name'])]
        for caption in captions:
            accept = self.one(rows, '接听', 'button', caption['frame'])
            descriptor = dict(pid=self.pid, start_time=self.request['start_time'],
                              bus=caption['bus'], frame=caption['frame'],
                              caption_path=caption['path'], accept_path=accept['path'],
                              caption=caption['name'].rstrip('.。…'),
                              window_id=self.invitation_window(rows, caption)['id'])
            token = hashlib.sha256(json.dumps(descriptor, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
            incoming.append(dict(descriptor, invitation_token=token, caller_identity_verified=False,
                                 participants_verified=False))
        result = dict(ok=True, transport='qt_atspi_niri', read_only=True,
                      incoming=incoming, active=None, call_connection_verified=False)
        if not frames:
            return result
        if len(frames) != 1:
            raise ValueError('MULTIPLE_CALL_FRAMES')
        frame = frames[0]
        windows = [w for w in self.windows() if w['title'] == frame['name']]
        if len(windows) != 1:
            raise ValueError('CALL_WINDOW_NOT_UNIQUE')
        children = [r for r in rows if r['frame'] == frame['path'] and r['showing']]
        timers = [r['name'] for r in children if r['role'] == 'label'
                  and re.fullmatch(r'\d{2,3}:[0-5]\d', r['name'])]
        buttons = [r['name'] for r in children if r['role'] == 'button']
        labels = [r['name'] for r in children if r['role'] == 'label']
        group = frame['name'] == '语音通话'
        connected = not group and len(timers) == 1 and '挂断' in buttons
        state = ('group_connection_unverified' if group else
                 ('connected' if connected else ('ringing' if '取消' in buttons else 'connecting')))
        handle = dict(pid=self.pid, start_time=self.request['start_time'],
                      bus=frame['bus'], path=frame['path'], window_id=windows[0]['id'])
        result.update(active=dict(handle=handle, kind='group' if group else 'private',
                                  state=state, timer=timers[0] if timers else None,
                                  labels=labels[:16], controls=buttons),
                      call_connection_verified=connected)
        return result

    def open_target(self):
        window = self.window('微信')
        self.focus(window)
        if self.target_matches(self.tree()):
            return window
        target = self.request['target']
        query = target.get('alias') or target['name']
        # Inspect local search results before activating any row. Header
        # verification occurs again after navigation and before the call.
        candidates = None
        for ordinal in range(4):
            rows = self.tree()
            search = self.one(rows, '搜索', 'text', self.main_frame(rows))
            node = search['node']
            self.search_text(window, node, query)
            section = '群聊' if target.get('group') else '联系人'
            deadline = time.monotonic() + 1.5
            lists = []
            while time.monotonic() < deadline:
                rows = self.tree()
                if self.spi.Text.get_text(node, 0, -1) != query:
                    raise ValueError('CALL_SEARCH_TEXT_CHANGED')
                lists = [r for r in rows if r['role'] == 'list' and
                         any(s['parent'] == r['path'] and s['name'] == section and s['showing'] for s in rows)]
                if len(lists) == 1 and self.search_positions(rows, lists[0], section, target['name']):
                    break
                time.sleep(.05)
            if len(lists) != 1:
                raise ValueError('CALL_LOCAL_SEARCH_RESULTS_REQUIRED')
            positions = self.search_positions(rows, lists[0], section, target['name'])
            if candidates is None:
                candidates = positions
            if positions != candidates or len(candidates) > 4 or ordinal >= len(candidates):
                raise ValueError('CALL_SEARCH_TARGET_NOT_VERIFIED')
            self.keys(window, *(['Down'] * candidates[ordinal]), 'Return')
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                if self.target_matches(self.tree()):
                    return window
                time.sleep(.05)
        raise ValueError('CALL_SEARCH_TARGET_NOT_VERIFIED')

    @staticmethod
    def search_positions(rows, listing, section, target_name):
        items = [r for r in rows if r['parent'] == listing['path'] and r['role'] == 'list item' and r['showing']]
        headings = ('搜索网络结果', '联系人', '群聊', '聊天记录', '聊天文件', '更多')
        starts = [i for i, r in enumerate(items) if r['name'] == section]
        if len(starts) != 1:
            return []
        start = starts[0] + 1
        end = next((i for i in range(start, len(items)) if items[i]['name'] in headings), len(items))
        # Keyboard navigation starts with the first selectable result, including
        # the network query above the requested contact/group section.
        return [sum(r['name'] not in headings for r in items[:i])
                for i in range(start, end) if items[i]['name'] == target_name]

    def open(self):
        live = self.inspect()
        if live['active'] or live['incoming']:
            raise ValueError('CALL_ALREADY_ACTIVE_OR_INCOMING')
        self.open_target()
        return dict(ok=True, contact=self.request['target']['chat'],
                    selection_match='unique_local_contact_label_and_external_namespace',
                    gui_exact_id_exposed=False, invitation_performed=False)

    def start(self):
        live = self.inspect()
        if live['active'] or live['incoming']:
            raise ValueError('CALL_ALREADY_ACTIVE_OR_INCOMING')
        if self.request['target'].get('group'):
            return self.group(True)
        window = self.open_target()
        rows = self.tree()
        if not self.target_matches(rows):
            raise ValueError('CALL_SELECTED_TARGET_CHANGED')
        self.button(window, '语音通话', self.main_frame(rows))
        self.keys(window, 'Down')
        rows = self.tree()
        item = self.one(rows, '语音通话', 'menu item')
        if not item['focused'] or not self.target_matches(rows):
            raise ValueError('VOICE_MENU_TARGET_NOT_VERIFIED')
        self.dial_entered = True
        self.keys(window, 'Return')
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            live = self.inspect()
            if live['active']:
                live.update(read_only=False, dial_entered=True, invitation_observed=True)
                return live
            time.sleep(.1)
        raise ValueError('CALL_INVITATION_RESULT_UNKNOWN')

    def hangup(self):
        live = self.inspect()
        expected = self.request['handle']
        if not live['active']:
            return dict(ok=True, status='already_ended', read_only=True)
        if live['active']['handle'] != expected:
            raise ValueError('CALL_HANDLE_CHANGED')
        window = self.window('语音通话' if live['active'].get('kind') == 'group' else '语音聊天')
        name = '挂断' if '挂断' in live['active']['controls'] else '取消'
        self.button(window, name, expected['path'])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = self.inspect()
            if not current['active'] or current['active']['handle'] != expected:
                return dict(ok=True, status='ended', hangup_observed=True, read_only=False)
            time.sleep(.1)
        raise ValueError('CALL_HANGUP_RESULT_UNKNOWN')

    def selector(self, rows):
        return self.one(rows, '微信选择成员', 'filler')['path']

    def selection_count(self, rows, frame):
        counts = {int(m.group(1)) for r in rows if r['frame'] == frame and r['showing']
                  and r['role'] == 'label' and (m := re.fullmatch(r'已选择(\d+)个联系人', r['name']))}
        if len(counts) != 1:
            raise ValueError('GROUP_SELECTION_COUNT_UNAVAILABLE')
        return counts.pop()

    def group_member_list(self, frame):
        rows = self.tree()
        if self.selector(rows) != frame:
            raise ValueError('GROUP_SELECTOR_CHANGED')
        search = self.one(rows, '搜索', 'text', frame)['node']
        # Qt's filtered results are unnamed list items. Only the unfiltered
        # checkboxes expose a name, focus and checked state we can verify.
        if self.spi.Text.get_text(search, 0, -1):
            self.search_text(self.window('微信选择成员'), search, '')
        if self.spi.Text.get_text(search, 0, -1) != '':
            raise ValueError('GROUP_MEMBER_SEARCH_CLEAR_NOT_VERIFIED')
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            rows = self.tree()
            if self.selector(rows) != frame:
                raise ValueError('GROUP_SELECTOR_CHANGED')
            listings = [r for r in rows if r['frame'] == frame and r['showing']
                        and r['role'] == 'list' and r['name'] == '请勾选需要添加的联系人']
            if len(listings) == 1:
                listing = self.one(rows, '请勾选需要添加的联系人', 'list', frame)
                children = [r for r in rows if r['parent'] == listing['path']]
                if children and all(r['role'] == 'check box' and r['name'] for r in children):
                    return rows, listing, children
            time.sleep(.05)
        raise ValueError('GROUP_NAMED_MEMBER_LIST_UNAVAILABLE')

    def select_group_members(self, window, frame):
        spec = self.request['group_spec']
        rows, listing, children = self.group_member_list(frame)
        initial = [r for r in children if r['checked']]
        if self.selection_count(rows, frame) != 1 or len(initial) != 1 or initial[0]['name'] != spec['self_member']['name']:
            raise ValueError('GROUP_DEFAULT_SELF_SELECTION_NOT_VERIFIED')
        original = [(r['path'], r['name']) for r in children]
        wanted = [spec['self_member'], *spec['members']]
        for member in wanted:
            if sum(r['name'] == member['name'] for r in children) != 1:
                raise ValueError('GROUP_MEMBER_NOT_UNIQUE_OR_UNAVAILABLE')
        for count, member in enumerate(spec['members'], 2):
            rows, current_list, children = self.group_member_list(frame)
            if current_list['path'] != listing['path'] or [(r['path'], r['name']) for r in children] != original:
                raise ValueError('GROUP_MEMBER_LIST_CHANGED')
            ordinal, selected = next((i, r) for i, r in enumerate(children) if r['name'] == member['name'])
            if selected['checked']:
                raise ValueError('UNEXPECTED_PRESELECTED_GROUP_MEMBER')
            if not current_list['node'].get_component_iface().grab_focus():
                raise ValueError('GROUP_MEMBER_LIST_FOCUS_FAILED')
            self.keys(window, 'Home', *(['Down'] * ordinal))
            focused = self.one(self.tree(), member['name'], 'check box', frame)
            if not focused['focused'] or focused['path'] != selected['path']:
                raise ValueError('GROUP_MEMBER_FOCUS_NOT_VERIFIED')
            self.keys(window, 'space')
            rows = self.tree()
            if not self.one(rows, member['name'], 'check box', frame)['checked'] or self.selection_count(rows, frame) != count:
                raise ValueError('GROUP_MEMBER_CHECK_NOT_VERIFIED')
        rows, current_list, children = self.group_member_list(frame)
        checked = {r['path'] for r in children if r['checked']}
        expected = {path for path, name in original if name in {m['name'] for m in wanted}}
        if current_list['path'] != listing['path'] or [(r['path'], r['name']) for r in children] != original or checked != expected:
            raise ValueError('GROUP_SELECTED_MEMBER_CHANGED')
        if self.selection_count(rows, frame) != len(wanted):
            raise ValueError('GROUP_SELECTION_COUNT_CHANGED')
        self.one(rows, '完成', 'button', frame)
        if not self.target_matches(rows):
            raise ValueError('GROUP_CHAT_HEADER_CHANGED')

    def group(self, submit):
        window = self.open_target()
        self.button(window, '语音通话', self.main_frame(self.tree()))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            rows = self.tree()
            if any(r['showing'] and r['name'] == '微信选择成员' for r in rows):
                break
            time.sleep(.05)
        frame = self.selector(rows)
        window = self.window('微信选择成员')
        result = None
        try:
            self.focus(window)
            self.select_group_members(window, frame)
            if not submit:
                result = dict(ok=True, member_selection_verified=True, invitation_performed=False,
                              gui_exact_id_exposed=False, selected_member_ids=[v['chat'] for v in self.request['group_spec']['members']])
                return result
            self.button(window, '完成', frame, dialing=True)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                live = self.inspect()
                if live['active'] and live['active']['kind'] == 'group':
                    return dict(live, read_only=False, dial_entered=True, invitation_observed=True,
                                member_selection_verified=True, gui_exact_id_exposed=False,
                                selected_member_ids=[v['chat'] for v in self.request['group_spec']['members']])
                time.sleep(.1)
            raise ValueError('GROUP_CALL_INVITATION_RESULT_UNKNOWN')
        finally:
            if not self.dial_entered and any(r['path'] == frame and r['showing'] for r in self.tree()):
                self.button(window, '取消', frame)
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline:
                    if (not any(r['path'] == frame and r['showing'] for r in self.tree())
                            and not any(w['id'] == window['id'] for w in self.windows())):
                        if result is not None:
                            result['selector_closed_verified'] = True
                        break
                    time.sleep(.05)
                else:
                    raise ValueError('GROUP_SELECTOR_CANCEL_NOT_OBSERVED')

    def group_prepare(self):
        live = self.inspect()
        if live['active'] or live['incoming']:
            raise ValueError('CALL_ALREADY_ACTIVE_OR_INCOMING')
        return self.group(False)

    def answer(self):
        live = self.inspect()
        matches = [v for v in live['incoming'] if v['invitation_token'] == self.request['invitation_token']]
        if live['active'] or len(matches) != 1:
            raise ValueError('EXACT_INCOMING_INVITATION_NOT_FOUND')
        invitation = matches[0]
        windows = [w for w in self.windows() if w['id'] == invitation['window_id']]
        if len(windows) != 1:
            raise ValueError('INCOMING_WINDOW_CHANGED')
        window = windows[0]
        self.focus(window)
        current = self.inspect()
        if not any(v == invitation for v in current['incoming']):
            raise ValueError('INCOMING_INVITATION_CHANGED')
        self.button(window, '接听', invitation['frame'], invitation['accept_path'], accepting=True)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = self.inspect()
            if current['active']:
                return dict(current, read_only=False, accept_entered=True,
                            accepted_invitation=invitation, invitation_performed=False)
            time.sleep(.1)
        raise ValueError('CALL_ACCEPT_RESULT_UNKNOWN')


def main():
    ui = None
    try:
        request = json.loads(sys.stdin.read(16384))
        ui = Ui(request)
        result = getattr(ui, request['operation'])()
    except Exception as error:
        code = str(error) if isinstance(error, ValueError) else type(error).__name__
        result = dict(ok=False, code=code, dial_entered=bool(ui and ui.dial_entered),
                      accept_entered=bool(ui and ui.accept_entered),
                      message=str(error)[:256], automatic_retry_allowed=False)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
