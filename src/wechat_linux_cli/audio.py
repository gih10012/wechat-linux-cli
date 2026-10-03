"""Bounded playback into one existing capture stream, with exact restoration.

This is an audio transport primitive. It neither places nor answers a call.
The caller must establish connection and recipient authorization separately.
"""
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import tempfile
import time
import uuid
import wave

MAX_BYTES = 32 * 1024 * 1024
MAX_SECONDS = 300


def environment():
    env = os.environ.copy()
    env.setdefault('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')
    env['LC_ALL'] = 'C'
    return env


def pulse(*args):
    r = subprocess.run(['pactl', *map(str, args)], env=environment(),
                       capture_output=True, text=True, timeout=5)
    if r.returncode:
        raise ValueError('AUDIO_SERVER_ERROR')
    return r.stdout.strip()


def rows(kind):
    return json.loads(pulse('--format=json', 'list', kind))


def server():
    info = json.loads(pulse('--format=json', 'info'))
    if info.get('is_local') != 'yes' or not info.get('cookie'):
        raise ValueError('LOCAL_AUDIO_SERVER_REQUIRED')
    return {'cookie': info['cookie'], 'server_string': info['server_string']}


def process_start(pid):
    if pid < 1:
        raise ValueError('INVALID_AUDIO_PID')
    path = Path('/proc') / str(pid)
    if path.stat().st_uid != os.getuid():
        raise ValueError('AUDIO_PROCESS_OWNER_MISMATCH')
    fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
    if fields[0] in ('Z', 'X'):
        raise ValueError('AUDIO_PROCESS_EXITED')
    return int(fields[19])


def fingerprint(stream):
    props = stream.get('properties', {})
    return {key: stream.get(key) for key in ('index', 'client', 'driver')} | {
        'properties': {key: props.get(key) for key in (
            'application.process.id', 'application.name', 'media.name', 'object.serial')}
    }


def stream_for(pid, start, index, expected=None):
    if process_start(pid) != start:
        raise ValueError('AUDIO_PROCESS_IDENTITY_CHANGED')
    matches = [r for r in rows('source-outputs') if r['index'] == index]
    if len(matches) != 1 or matches[0].get('properties', {}).get('application.process.id') != str(pid):
        raise ValueError('EXACT_AUDIO_STREAM_NOT_FOUND')
    stream = matches[0]
    if expected is not None and fingerprint(stream) != expected:
        raise ValueError('AUDIO_STREAM_IDENTITY_CHANGED')
    return stream


def streams(pid, start):
    if process_start(pid) != start:
        raise ValueError('AUDIO_PROCESS_IDENTITY_CHANGED')
    return {'ok': True, 'pid': pid, 'start_time': start, 'read_only': True,
            'call_connection_verified': False,
            'items': [dict(fingerprint(r), source=r['source'], muted=r['mute'],
                           corked=r['corked']) for r in rows('source-outputs')
                      if r.get('properties', {}).get('application.process.id') == str(pid)]}


def wait_source(pid, start, index, expected, name):
    # PipeWire acknowledges a move before its graph metadata has settled.
    deadline = time.monotonic() + 2
    while True:
        current = stream_for(pid, start, index, expected)
        source = next((r for r in rows('sources') if r['index'] == current['source']), None)
        if source and source['name'] == name:
            return current
        if time.monotonic() >= deadline:
            raise ValueError('AUDIO_ROUTE_NOT_VERIFIED')
        time.sleep(.02)


def wav_snapshot(file):
    fd = os.open(Path(file).expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= MAX_BYTES:
            raise ValueError('AUDIO_FILE_SIZE_OR_TYPE')
        data = handle.read(MAX_BYTES + 1)
        after = os.fstat(handle.fileno())
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError('AUDIO_FILE_CHANGED')
    try:
        with wave.open(io.BytesIO(data), 'rb') as audio:
            rate, frames, channels, width = (audio.getframerate(), audio.getnframes(),
                                             audio.getnchannels(), audio.getsampwidth())
            if (audio.getcomptype() != 'NONE' or channels not in (1, 2)
                    or width not in (1, 2, 3, 4) or not 8000 <= rate <= 96000
                    or not 0 < frames / rate <= MAX_SECONDS):
                raise ValueError('AUDIO_PCM_WAV_REQUIRED')
            if len(audio.readframes(frames)) != frames * channels * width:
                raise ValueError('AUDIO_WAV_TRUNCATED')
    except (wave.Error, EOFError) as exc:
        raise ValueError('AUDIO_PCM_WAV_REQUIRED') from exc
    return data, frames / rate


def root():
    path = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'wechat-audio'
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('UNSAFE_AUDIO_STATE')
    path.chmod(0o700)
    return path


def record_path(request_id):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,96}', request_id) or request_id in ('.', '..'):
        raise ValueError('INVALID_AUDIO_REQUEST_ID')
    return root() / (request_id + '.json')


def read_record(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('UNSAFE_AUDIO_RECORD')
        return json.load(handle)


def write_record(path, record):
    fd, name = tempfile.mkstemp(prefix='.audio-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(record, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def cleanup(record):
    """Restore only our original stream, and remove only our own null sink."""
    errors = []
    try:
        if server() != record['server']:
            return {'ok': False, 'errors': ['AUDIO_SERVER_CHANGED'], 'route_restored': False}
        if record.get('module_load_entered'):
            matching = [r for r in rows('sinks') if r.get('name') == record['sink_name']
                        and (record.get('module') is None or r.get('owner_module') == record['module'])]
            if len(matching) == 1:
                try:
                    current = stream_for(record['pid'], record['start_time'], record['stream_index'],
                                         record['stream_fingerprint'])
                except (OSError, ValueError):
                    current = None
                if current:
                    source = next((r for r in rows('sources')
                                   if r.get('index') == current['source']), None)
                    # Leave a subsequent manual reroute alone.
                    if source and source['name'] == record['sink_name'] + '.monitor':
                        originals = [r for r in rows('sources') if r.get('name') == record['original_source']]
                        if len(originals) != 1:
                            raise ValueError('ORIGINAL_AUDIO_SOURCE_DISAPPEARED')
                        pulse('move-source-output', current['index'], record['original_source'])
                        wait_source(record['pid'], record['start_time'], current['index'],
                                    record['stream_fingerprint'], record['original_source'])
                pulse('unload-module', matching[0]['owner_module'])
                if any(r.get('name') == record['sink_name'] for r in rows('sinks')):
                    raise ValueError('AUDIO_MODULE_CLEANUP_NOT_VERIFIED')
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        errors.append(str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
    return {'ok': not errors, 'errors': errors, 'route_restored': not errors}


def stop_player(player):
    if player is not None and player.poll() is None:
        player.terminate()
        try:
            player.wait(timeout=2)
        except subprocess.TimeoutExpired:
            player.kill()
            player.wait(timeout=2)


def _play(file, pid, start, stream_index, request_id):
    data, duration = wav_snapshot(file)
    path = record_path(request_id)
    intent = {'pid': pid, 'start_time': start, 'stream_index': stream_index,
              'audio_sha256': hashlib.sha256(data).hexdigest()}
    if path.exists() or path.is_symlink():
        previous = read_record(path)
        if previous['intent'] != intent:
            raise ValueError('AUDIO_REQUEST_ID_CONFLICT')
        return dict(previous, replayed=True, playback_performed_this_invocation=False)
    for old in root().glob('*.json'):
        previous = read_record(old)
        if previous['status'] in ('prepared', 'routed', 'playing', 'cleanup_required'):
            raise ValueError('PREVIOUS_AUDIO_OPERATION_UNRESOLVED:' + previous['request_id'])
    stream = stream_for(pid, start, stream_index)
    if stream.get('corked') or stream.get('mute'):
        raise ValueError('AUDIO_STREAM_NOT_ACTIVE_OR_MUTED')
    original = [r for r in rows('sources') if r['index'] == stream['source']]
    if len(original) != 1:
        raise ValueError('ORIGINAL_AUDIO_SOURCE_NOT_FOUND')
    record = dict(intent, intent=intent, request_id=request_id, ok=False, status='prepared',
                  server=server(), original_source=original[0]['name'],
                  stream_fingerprint=fingerprint(stream), sink_name='wechat_audio_' + uuid.uuid4().hex,
                  module=None, duration_seconds=duration, playback_started=False,
                  remote_delivery_verified=False, call_connection_verified=False,
                  worker_pid=os.getpid(), worker_start_time=process_start(os.getpid()))
    write_record(path, record)
    player = None
    try:
        record['module_load_entered'] = True
        write_record(path, record)
        record['module'] = int(pulse('load-module', 'module-null-sink',
                                     'sink_name=' + record['sink_name'], 'rate=48000', 'channels=1'))
        write_record(path, record)
        stream_for(pid, start, stream_index, record['stream_fingerprint'])
        pulse('move-source-output', stream_index, record['sink_name'] + '.monitor')
        wait_source(pid, start, stream_index, record['stream_fingerprint'], record['sink_name'] + '.monitor')
        record['status'] = 'routed'
        write_record(path, record)
        with tempfile.NamedTemporaryFile(suffix='.wav', dir=root()) as snapshot:
            snapshot.write(data)
            snapshot.flush()
            # Persist intent before the first sample could be transmitted.
            record.update(status='playing', playback_started=True)
            write_record(path, record)
            with tempfile.TemporaryFile(dir=root()) as errors:
                player = subprocess.Popen(['paplay', '--device=' + record['sink_name'],
                                           '--latency-msec=20', snapshot.name], env=environment(),
                                          stdout=subprocess.DEVNULL, stderr=errors)
                deadline = time.monotonic() + duration + 5
                while player.poll() is None:
                    current = stream_for(pid, start, stream_index, record['stream_fingerprint'])
                    monitor = next((r for r in rows('sources') if r['index'] == current['source']), None)
                    if (server() != record['server'] or not monitor
                            or monitor['name'] != record['sink_name'] + '.monitor'
                            or current.get('mute') or current.get('corked')):
                        raise ValueError('AUDIO_CALL_STREAM_ENDED_OR_CHANGED')
                    if time.monotonic() > deadline:
                        raise ValueError('AUDIO_PLAYBACK_TIMEOUT')
                    time.sleep(.1)
                if player.returncode:
                    raise ValueError('AUDIO_PLAYER_FAILED')
        record.update(ok=True, status='playback_finished')
    except (OSError, ValueError, subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        record.update(ok=False, status='playback_interrupted',
                      code=str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
    finally:
        stop_player(player)
        record['cleanup'] = cleanup(record)
        if not record['cleanup']['ok']:
            record.update(ok=False, status='cleanup_required')
        write_record(path, record)
    return record


def locked(fn, *args):
    fd = os.open(root() / '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('AUDIO_OPERATION_IN_PROGRESS') from exc
        old = signal.getsignal(signal.SIGTERM)
        def interrupted(signum, frame):
            raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM, interrupted)
        try:
            return fn(*args)
        finally:
            signal.signal(signal.SIGTERM, old)


def recover(request_id):
    path = record_path(request_id)
    record = read_record(path)
    if record['status'] not in ('prepared', 'routed', 'playing', 'cleanup_required'):
        return dict(record, read_only=True)
    result = cleanup(record)
    record.update(ok=False, status='recovered_no_replay' if result['ok'] else 'cleanup_required',
                  cleanup=result)
    write_record(path, record)
    return record


def add_parser(sub):
    command = sub.add_parser('audio', help='Route specified audio into one existing capture stream; does not call')
    operations = command.add_subparsers(dest='audio_operation', required=True)
    for operation in ('streams', 'play'):
        args = operations.add_parser(operation)
        args.add_argument('--pid', type=int, required=True)
        args.add_argument('--start-time', type=int, required=True)
        if operation == 'play':
            args.add_argument('--source-output', type=int, required=True)
            args.add_argument('--file', type=Path, required=True, help='PCM WAV, up to 5 minutes and 32 MiB')
            args.add_argument('--request-id', required=True)
    for operation in ('status', 'recover'):
        args = operations.add_parser(operation)
        args.add_argument('--request-id', required=True)


def run(args):
    try:
        if args.audio_operation == 'streams':
            return streams(args.pid, args.start_time)
        if args.audio_operation == 'play':
            return locked(_play, args.file, args.pid, args.start_time, args.source_output, args.request_id)
        if args.audio_operation == 'recover':
            result = locked(recover, args.request_id)
            return dict(result, ok=result.get('cleanup', {}).get('ok', False), recovery_only=True)
        return dict(read_record(record_path(args.request_id)), read_only=True)
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        code = str(exc) if isinstance(exc, ValueError) else (
            'AUDIO_SERVER_TIMEOUT' if isinstance(exc, subprocess.TimeoutExpired) else 'LOCAL_AUDIO_IO_ERROR')
        return {'ok': False, 'code': code, 'automatic_retry_allowed': False}
