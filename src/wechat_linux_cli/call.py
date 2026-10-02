"""Replay-safe control of normal Qt private-call UI on niri."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time

from .audio import process_start, read_record, write_record
from ._native.native_db import load_keys, open_database


def root():
    path = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'wechat-calls'
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('UNSAFE_CALL_STATE')
    path.chmod(0o700)
    return path


def record_path(request_id):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,96}', request_id) or request_id in ('.', '..'):
        raise ValueError('INVALID_CALL_REQUEST_ID')
    return root() / (request_id + '.json')


@contextmanager
def lock():
    fd = os.open(root() / '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('UNSAFE_CALL_LOCK')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError:
        raise ValueError('CALL_OPERATION_BUSY') from None
    finally:
        os.close(fd)


def target(account, chat):
    keys = load_keys(account)
    conn, _ = open_database(keys, 'contact/contact.db')
    try:
        rows = [dict(r) for r in conn.execute('SELECT username, alias, remark, nick_name FROM contact')]
    finally:
        conn.close()
    exact = [r for r in rows if r['username'] == chat]
    if len(exact) != 1:
        raise ValueError('EXACT_CALL_CONTACT_ID_REQUIRED')
    row = exact[0]
    group = chat.endswith('@chatroom')
    name = row['remark'] or row['nick_name'] or chat
    external = chat.endswith('@openim')
    same = [r for r in rows if (r['remark'] or r['nick_name'] or r['username']) == name
            and r['username'].endswith('@openim') == external and r['username'].endswith('@chatroom') == group]
    if len(same) != 1:
        raise ValueError('AMBIGUOUS_CALL_CONTACT_LABEL: assign a unique remark or use computer-use')
    if not name or len(name) > 256 or any(c in name for c in '\n\r\0'):
        raise ValueError('UNSUPPORTED_CALL_CONTACT_LABEL')
    return dict(chat=chat, name=name, alias=row['alias'] or '', external=external, group=group), str(Path(keys['database_root']).resolve())


def ui(request):
    if process_start(request['pid']) != request['start_time']:
        raise ValueError('CALL_PROCESS_CHANGED')
    try:
        result = subprocess.run(['/usr/bin/python3', '-I', str(Path(__file__).with_name('_call_ui.py'))],
                                input=json.dumps(request, ensure_ascii=False), capture_output=True,
                                text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return dict(ok=False, code='CALL_UI_TIMEOUT_RESULT_UNKNOWN', dial_entered=request['operation'] == 'start',
                    accept_entered=request['operation'] == 'answer',
                    automatic_retry_allowed=False)
    if len(result.stdout) > 65536:
        raise ValueError('CALL_UI_RESPONSE_TOO_LARGE')
    try:
        value = json.loads(result.stdout)
    except ValueError:
        return dict(ok=False, code='CALL_UI_RESPONSE_UNKNOWN', dial_entered=request['operation'] == 'start',
                    accept_entered=request['operation'] == 'answer',
                    automatic_retry_allowed=False)
    if not isinstance(value, dict):
        raise ValueError('INVALID_CALL_UI_RESPONSE')
    return value


@contextmanager
def desktop_session():
    configured = os.environ.get('WECHAT_DESKTOP_SESSION_HELPER')
    helper = Path(configured) if configured else Path.home() / '.local/share/niri-computer-use/bin/niri-desktop-session.py'
    if not helper.is_file():
        raise ValueError('DESKTOP_SESSION_HELPER_REQUIRED: configure WECHAT_DESKTOP_SESSION_HELPER')

    def run(operation):
        r = subprocess.run(['/usr/bin/python3', str(helper), operation, '--owner-pid', str(os.getpid())],
                           capture_output=True, text=True, timeout=8)
        value = json.loads(r.stdout)
        if r.returncode or not value.get('ok'):
            raise ValueError('DESKTOP_SESSION_' + operation.upper() + '_FAILED')
        return value

    if run('status').get('session'):
        raise ValueError('DESKTOP_SESSION_ALREADY_OWNED')
    entered = False
    try:
        run('begin')
        entered = True
        run('wake')
        yield
    finally:
        if entered:
            run('end')


def inspect(pid, start):
    return ui(dict(operation='inspect', pid=pid, start_time=start))


def observe(record):
    intent = record['intent']
    try:
        alive = process_start(intent['pid']) == intent['start_time']
    except FileNotFoundError:
        alive = False
    if not alive:
        return dict(ok=True, active=None, incoming=[], read_only=True,
                    client_process_ended=True, call_connection_verified=False)
    return inspect(intent['pid'], intent['start_time'])


def open_chat(account, chat, pid, process_time):
    resolved, database_root = target(account, chat)
    with desktop_session():
        return ui(dict(operation='open', pid=pid, start_time=process_time,
                       target=resolved, database_root=database_root))


def start(account, chat, pid, process_time, request_id):
    path = record_path(request_id)
    intent = dict(account=account, chat=chat, pid=pid, start_time=process_time)
    if path.exists() or path.is_symlink():
        saved = read_record(path)
        if saved['intent'] != intent:
            raise ValueError('CALL_REQUEST_ID_CONFLICT')
        return dict(saved, replayed=True)
    resolved, database_root = target(account, chat)
    if resolved.get('group'):
        raise ValueError('GROUP_CALL_MEMBER_SELECTION_NOT_IMPLEMENTED')
    live = inspect(pid, process_time)
    if not live.get('ok'):
        return live
    if live.get('active') or live.get('incoming'):
        raise ValueError('CALL_ALREADY_ACTIVE_OR_INCOMING')
    for old_path in root().glob('*.json'):
        old = read_record(old_path)
        if old['status'] in ('prepared', 'dial_unknown', 'accept_unknown'):
            raise ValueError('PREVIOUS_CALL_RESULT_UNRESOLVED:' + old['request_id'])
        if old['status'] == 'active':
            old_live = observe(old)
            if not old_live.get('ok'):
                raise ValueError('PREVIOUS_CALL_RESULT_UNRESOLVED:' + old['request_id'])
            if old_live.get('active') and old_live['active']['handle'] == old.get('handle'):
                raise ValueError('PREVIOUS_CALL_STILL_ACTIVE:' + old['request_id'])
            old.update(status='ended', ended_observed=True)
            write_record(old_path, old)
    record = dict(intent=intent, request_id=request_id, target=resolved, database_root=database_root,
                  ok=False, status='prepared', automatic_retry_allowed=False, created_at=time.time(),
                  transport='qt_atspi_niri', native_call_api_used=False,
                  call_connection_verified=False, remote_delivery_verified=False)
    write_record(path, record)
    ui_entered = False
    try:
        with desktop_session():
            ui_entered = True
            result = ui(dict(operation='start', **intent, target=resolved, database_root=database_root))
            if result.get('ok') and result.get('active'):
                record.update(ok=True, status='active', handle=result['active']['handle'],
                              invitation_observed=True, call_connection_verified=result['call_connection_verified'],
                              last_observation=result)
            else:
                record.update(status='dial_unknown' if result.get('dial_entered') else 'failed_no_call',
                              code=result.get('code', 'CALL_RESULT_UNKNOWN'), last_observation=result)
            # Keep the handle even if restoring display power fails afterwards.
            write_record(path, record)
    except Exception as error:
        if record['status'] == 'prepared':
            record.update(status='dial_unknown' if ui_entered else 'failed_no_call')
        record.update(ok=False, code=str(error))
    write_record(path, record)
    return record


def status(request_id):
    record = read_record(record_path(request_id))
    try:
        live = observe(record)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        live = dict(ok=False, code=str(error) if isinstance(error, ValueError) else type(error).__name__)
    matches = bool(live.get('active') and live['active']['handle'] == record.get('handle'))
    return dict(record, read_only=True, live=live, current_call_matches=matches,
                current_state=live['active']['state'] if matches else None,
                call_connection_verified=bool(matches and live.get('call_connection_verified')))


def answer(account, pid, process_time, invitation_token, request_id):
    if not re.fullmatch(r'[0-9a-f]{64}', invitation_token):
        raise ValueError('INVALID_INCOMING_INVITATION_TOKEN')
    path = record_path(request_id)
    intent = dict(account=account, pid=pid, start_time=process_time, invitation_token=invitation_token)
    if path.exists() or path.is_symlink():
        record = read_record(path)
        if record['intent'] != intent:
            raise ValueError('CALL_REQUEST_ID_CONFLICT')
        return dict(record, replayed=True)
    keys = load_keys(account)
    database_root = str(Path(keys['database_root']).resolve())
    live = inspect(pid, process_time)
    invitations = [v for v in live.get('incoming', []) if v['invitation_token'] == invitation_token]
    if not live.get('ok') or live.get('active') or len(invitations) != 1:
        raise ValueError('EXACT_INCOMING_INVITATION_NOT_FOUND')
    for old_path in root().glob('*.json'):
        old = read_record(old_path)
        if old['status'] in ('prepared', 'dial_unknown', 'accept_unknown'):
            raise ValueError('PREVIOUS_CALL_RESULT_UNRESOLVED:' + old['request_id'])
    record = dict(intent=intent, request_id=request_id, database_root=database_root,
                  invitation=invitations[0], ok=False, status='prepared', created_at=time.time(),
                  transport='qt_atspi_niri', native_call_api_used=False,
                  caller_identity_verified=False, participants_verified=False,
                  invitation_performed=False, automatic_retry_allowed=False,
                  call_connection_verified=False, remote_delivery_verified=False)
    write_record(path, record)
    entered = False
    try:
        with desktop_session():
            entered = True
            result = ui(dict(operation='answer', **intent, database_root=database_root))
            if result.get('ok') and result.get('active'):
                record.update(ok=True, status='active', handle=result['active']['handle'],
                              accepted_invitation=True, last_observation=result,
                              call_connection_verified=result['call_connection_verified'])
            else:
                record.update(status='accept_unknown' if result.get('accept_entered') else 'failed_no_accept',
                              code=result.get('code', 'CALL_ACCEPT_RESULT_UNKNOWN'), last_observation=result)
            write_record(path, record)
    except Exception as error:
        if record['status'] == 'prepared':
            record['status'] = 'accept_unknown' if entered else 'failed_no_accept'
        record.update(ok=False, code=str(error))
    write_record(path, record)
    return record


def hangup(request_id):
    path = record_path(request_id)
    record = read_record(path)
    if record['status'] == 'ended':
        return dict(record, read_only=True, replayed=True)
    if record['status'] != 'active' or not record.get('handle'):
        raise ValueError('CALL_HANDLE_REQUIRED: inspect the unknown result before further action')
    request = dict(operation='hangup', pid=record['intent']['pid'], start_time=record['intent']['start_time'],
                   handle=record['handle'], database_root=record['database_root'])
    live = observe(record)
    if not live.get('ok'):
        return live
    if live.get('active') and live['active']['handle'] != record['handle']:
        raise ValueError('CALL_HANDLE_CHANGED')
    if not live.get('active'):
        result = dict(ok=True, status='already_ended', read_only=True)
    else:
        with desktop_session():
            result = ui(request)
    if result.get('ok'):
        record.update(status='ended', ended_observed=True, hangup_result=result)
        write_record(path, record)
    return dict(result, request_id=request_id, automatic_retry_allowed=False)


def resolve_ended(request_id):
    path = record_path(request_id)
    record = read_record(path)
    live = observe(record)
    if not live.get('ok') or live.get('active') or live.get('incoming'):
        raise ValueError('CALL_NOT_CONFIRMED_ENDED')
    record.update(previous_status=record['status'], status='ended', ended_confirmed=True,
                  invitation_outcome='unknown' if not record.get('invitation_observed') else 'observed')
    write_record(path, record)
    return dict(ok=True, request_id=request_id, resolution_only=True,
                invitation_performed=False, automatic_retry_allowed=False)


def play(request_id, file, audio_request_id, wait_seconds=0, source_output=None):
    if not 0 <= wait_seconds <= 60:
        raise ValueError('CALL_WAIT_RANGE: 0 to 60 seconds')
    deadline = time.monotonic() + wait_seconds
    while True:
        observed = status(request_id)
        if not observed.get('current_call_matches'):
            raise ValueError('CALL_HANDLE_NO_LONGER_ACTIVE')
        if observed['call_connection_verified']:
            break
        if time.monotonic() >= deadline:
            return dict(ok=False, code='CALL_NOT_CONNECTED', audio_playback_entered=False,
                        automatic_retry_allowed=False)
        time.sleep(.25)
    from . import audio
    intent = observed['intent']
    streams = audio.streams(intent['pid'], intent['start_time'])['items']
    if source_output is not None:
        streams = [s for s in streams if s['index'] == source_output]
    if len(streams) != 1:
        raise ValueError('CALL_CAPTURE_STREAM_NOT_UNIQUE: select --source-output from audio streams')
    result = audio.locked(audio._play, file, intent['pid'], intent['start_time'], streams[0]['index'], audio_request_id)
    return dict(result, call_request_id=request_id, call_handle=observed['handle'],
                call_connection_verified_before_playback=True)


def add_parser(operations):
    parser = operations.add_parser('call', help='Control normal Qt private-call UI on niri; requires desktop accessibility')
    actions = parser.add_subparsers(dest='call_action', required=True)
    for action in ('inspect', 'open', 'start'):
        command = actions.add_parser(action)
        command.add_argument('--pid', type=int, required=True)
        command.add_argument('--start-time', type=int, required=True)
        if action in ('open', 'start'):
            command.add_argument('--account', default='me')
            command.add_argument('--chat', required=True, help='Exact contact ID; ambiguous display labels are refused')
        if action == 'start':
            command.add_argument('--request-id', required=True)
    command = actions.add_parser('answer', help='Accept one exact observed GUI invitation; caller authorization is separate')
    command.add_argument('--account', default='me')
    command.add_argument('--pid', type=int, required=True)
    command.add_argument('--start-time', type=int, required=True)
    command.add_argument('--invitation-token', required=True, help='Token from current call inspect; a caption is not a native caller ID')
    command.add_argument('--request-id', required=True)
    for action in ('status', 'hangup'):
        command = actions.add_parser(action)
        command.add_argument('--request-id', required=True)
    command = actions.add_parser('resolve', help='Confirm an uncertain call has ended; never redials')
    command.add_argument('--request-id', required=True)
    command.add_argument('--ended', action='store_true', required=True,
                         help='Operator has checked the uncertain invitation and confirms the call ended')
    command = actions.add_parser('play', help='Play only after the recorded call is connected')
    command.add_argument('--request-id', required=True, help='Existing call request ID')
    command.add_argument('--file', required=True, help='PCM WAV supported by audio play')
    command.add_argument('--audio-request-id', required=True, help='Separate playback ID; repeating it never replays sound')
    command.add_argument('--wait-seconds', type=int, default=0, help='Wait up to 60 seconds for connection')
    command.add_argument('--source-output', type=int, help='Required if the client owns multiple capture streams')


def run(args):
    try:
        if args.call_action == 'inspect':
            return inspect(args.pid, args.start_time)
        if args.call_action == 'status':
            return status(args.request_id)
        if args.call_action == 'play':
            return play(args.request_id, args.file, args.audio_request_id, args.wait_seconds, args.source_output)
        with lock():
            if args.call_action == 'answer':
                return answer(args.account, args.pid, args.start_time, args.invitation_token, args.request_id)
            if args.call_action == 'resolve':
                return resolve_ended(args.request_id)
            if args.call_action == 'open':
                return open_chat(args.account, args.chat, args.pid, args.start_time)
            if args.call_action == 'start':
                return start(args.account, args.chat, args.pid, args.start_time, args.request_id)
            return hangup(args.request_id)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        code = str(error) if isinstance(error, ValueError) else type(error).__name__
        return dict(ok=False, code=code, automatic_retry_allowed=False)
