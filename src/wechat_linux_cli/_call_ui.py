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
            if role == 'frame':
                frame = node.path
            state = node.get_state_set()
            component = node.get_component_iface()
            bounds = component.get_extents(self.spi.CoordType.SCREEN) if component else None
            rows.append(dict(node=node, path=node.path, bus=node.app.bus_name, parent=parent,
                             frame=frame, name=node.get_name() or '', role=role,
                             showing=state.contains(self.spi.StateType.SHOWING),
                             focused=state.contains(self.spi.StateType.FOCUSED),
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

    def button(self, window, name, frame, expected_path=None, accepting=False):
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
                  and r['showing'] and y <= r['bounds'][1] < y + h}
        target = self.request['target']
        external = any(s.startswith('@') for s in labels)
        return target['name'] in labels and external == target['external']

    def inspect(self):
        rows = self.tree()
        frames = [r for r in rows if r['role'] == 'frame' and r['name'] == '语音聊天' and r['showing']]
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
        windows = [w for w in self.windows() if w['title'] == '语音聊天']
        if len(windows) != 1:
            raise ValueError('CALL_WINDOW_NOT_UNIQUE')
        children = [r for r in rows if r['frame'] == frame['path'] and r['showing']]
        timers = [r['name'] for r in children if r['role'] == 'label'
                  and re.fullmatch(r'\d{2,3}:[0-5]\d', r['name'])]
        buttons = [r['name'] for r in children if r['role'] == 'button']
        labels = [r['name'] for r in children if r['role'] == 'label']
        connected = len(timers) == 1 and '挂断' in buttons
        state = 'connected' if connected else ('ringing' if '取消' in buttons else 'connecting')
        handle = dict(pid=self.pid, start_time=self.request['start_time'],
                      bus=frame['bus'], path=frame['path'], window_id=windows[0]['id'])
        result.update(active=dict(handle=handle, state=state, timer=timers[0] if timers else None,
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
            if not node.get_component_iface().grab_focus():
                raise ValueError('CALL_SEARCH_FOCUS_FAILED')
            editable = node.get_editable_text_iface()
            if editable is None or not editable.set_text_contents('') or not editable.set_text_contents(query):
                raise ValueError('CALL_SEARCH_EDIT_FAILED')
            if self.spi.Text.get_text(node, 0, -1) != query:
                raise ValueError('CALL_SEARCH_TEXT_NOT_VERIFIED')
            time.sleep(.15)
            rows = self.tree()
            section = '群聊' if target.get('group') else '联系人'
            lists = [r for r in rows if r['role'] == 'list' and
                     any(s['parent'] == r['path'] and s['name'] == section and s['showing'] for s in rows)]
            if len(lists) != 1:
                raise ValueError('CALL_LOCAL_SEARCH_RESULTS_REQUIRED')
            items = [r for r in rows if r['parent'] == lists[0]['path'] and r['role'] == 'list item' and r['showing']]
            start = next(i for i, r in enumerate(items) if r['name'] == section) + 1
            end = next((i for i in range(start, len(items)) if items[i]['name'] in
                        ('联系人', '群聊', '聊天记录', '聊天文件', '更多')), len(items))
            positions = [i - start for i in range(start, end) if items[i]['name'] == target['name']]
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
        window = self.window('语音聊天')
        name = '挂断' if '挂断' in live['active']['controls'] else '取消'
        self.button(window, name, expected['path'])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = self.inspect()
            if not current['active'] or current['active']['handle'] != expected:
                return dict(ok=True, status='ended', hangup_observed=True, read_only=False)
            time.sleep(.1)
        raise ValueError('CALL_HANGUP_RESULT_UNKNOWN')

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
