"""Serial owner-scoped Unix service, intended for systemd CAP_SYS_PTRACE execution."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import re

from . import __version__
from ._native import native_send_candidate as native
from .backend import completed
from .client import socket_path


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Expected an owner-only state directory')


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Expected an owner-only state file')
        return json.load(stream)


def save(path, value):
    fd, filename = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    temp = Path(filename)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=True, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def process_start(pid):
    try:
        return Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def trial_work(request_id):
    return native.runtime_root(Path.home()) / native.trial_name(request_id)


def before_injection(request_id):
    """Only the absence of the saved GDB config proves no inferior call began.

    run_injection writes config.json before launching GDB. All later stages,
    including a crashed caller with an unrecorded debugger PID, leave it here.
    """
    work = trial_work(request_id)
    return not any((work/name).exists() for name in
                   ('config.json', 'debugger-process.json', 'injection.json',
                    'worker.json', 'worker.json.arm'))


def terminal_preflight(result, request_id):
    if not isinstance(result, dict) or not before_injection(request_id):
        return False
    return (result.get('status') == 'local_failure'
            and result.get('stage') in ('prepare_executable', 'compile_helper')
            or result.get('code') == 'NATIVE_OPERATION_FAILED')


class Runner:
    def __init__(self, directory, timeout=150):
        private_dir(directory)
        self.directory, self.timeout = directory, timeout
        self.state_path = directory/'operation.json'
        self.state = read_private(self.state_path) if self.state_path.exists() else {}
        self.process = None

    def __call__(self, request):
        if self.state.get('pending'):
            return {'ok': False, 'code': 'PENDING_OPERATION', 'pending': self.state['pending'],
                    'automatic_retry_allowed': False}
        if not native.has_ptrace_capability() and os.geteuid() != 0:
            return {'ok': False, 'code': 'PRIVILEGE_REQUIRED', 'automatic_retry_allowed': False}
        fd, filename = tempfile.mkstemp(prefix='result-', suffix='.json', dir=self.directory)
        result_path = Path(filename)
        with os.fdopen(fd, 'wb') as output:
            env = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG', 'WECHAT_LINUX_RUNTIME_DIR')
                   if k in os.environ}
            self.process = subprocess.Popen(
                [sys.executable, '-I', '-m', 'wechat_linux_cli.backend'],
                stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL,
                env=env, start_new_session=True)
            pending = {'pid': self.process.pid, 'start_time': process_start(self.process.pid),
                       'request_id': request['request_id'], 'result_file': str(result_path)}
            self.state = {'pending': pending}
            # The child cannot act before this record: it still waits on stdin.
            save(self.state_path, self.state)
            try:
                self.process.communicate(json.dumps(request).encode(), timeout=self.timeout)
            except subprocess.TimeoutExpired:
                return {'ok': False, 'code': 'NATIVE_OPERATION_PENDING', 'pending': pending,
                        'automatic_retry_allowed': False}
        try:
            result = read_private(result_path)
        except (ValueError, OSError):
            if before_injection(request['request_id']):
                result = {'ok': False, 'code': 'BACKEND_ENDED_BEFORE_INJECTION',
                          'automatic_retry_allowed': False}
                self.state = {'last_request_id': request['request_id'], 'last_result': result,
                              'pending': None}
                save(self.state_path, self.state)
                result_path.unlink(missing_ok=True)
                return result
            return {'ok': False, 'code': 'NATIVE_RESULT_UNREADABLE', 'pending': pending,
                    'automatic_retry_allowed': False}
        if not isinstance(result, dict):
            return {'ok': False, 'code': 'NATIVE_RESULT_UNREADABLE', 'pending': pending,
                    'automatic_retry_allowed': False}
        if completed(result) or terminal_preflight(result, request['request_id']):
            self.state = {'last_request_id': request['request_id'], 'last_result': result,
                          'pending': None}
            save(self.state_path, self.state)
            result_path.unlink()
        return result

    def inspect_pending(self):
        pending = self.state.get('pending')
        if not pending:
            return {'ok': True, 'pending': None}
        if pending.get('start_time') is not None and process_start(pending['pid']) == pending['start_time']:
            return {'ok': False, 'code': 'NATIVE_OPERATION_PENDING', 'pending': pending}
        request_id = pending['request_id']
        try:
            result = read_private(Path(pending['result_file']))
        except (OSError, ValueError, KeyError):
            result = None
        if result is None and before_injection(request_id):
            result = {'ok': False, 'code': 'BACKEND_ENDED_BEFORE_INJECTION',
                      'automatic_retry_allowed': False}
        if terminal_preflight(result, request_id):
            self.state = {'last_request_id': request_id, 'last_result': result, 'pending': None}
            save(self.state_path, self.state)
            Path(pending['result_file']).unlink(missing_ok=True)
            return {'ok': True, 'pending': None, 'result': result, 'read_only': True}
        if (isinstance(result, dict)
                and result.get('code') == 'BACKEND_ENDED_BEFORE_INJECTION'
                and before_injection(request_id)):
            self.state = {'last_request_id': request_id, 'last_result': result, 'pending': None}
            save(self.state_path, self.state)
            Path(pending['result_file']).unlink(missing_ok=True)
            return {'ok': True, 'pending': None, 'result': result, 'read_only': True}
        try:
            native_result = native.inspect_trial(request_id)
            config = read_private(Path(native_result['result_path']).parent/'config.json')
            # A recorded old client flag cannot prove the client is untraced now.
            native_result['client_running_untraced'] = native.process_running_untraced(
                native_result.get('inferior_pid', 0), config['start_time'])
            if (native_result.get('status') in ('worker_pending', 'callback_pending')
                    and native_result.get('worker', {}).get('worker_done')):
                native_result['status'] = 'trial_finished'
            done = completed(native_result)
        except (OSError, ValueError, KeyError, TypeError):
            native_result, done = None, False
        if not done:
            return {'ok': False, 'code': 'PENDING_REQUIRES_INSPECTION', 'pending': pending,
                    'result': native_result, 'automatic_retry_allowed': False}
        self.state = {'last_request_id': request_id, 'last_result': native_result,
                      'pending': None}
        save(self.state_path, self.state)
        return {'ok': True, 'pending': None, 'result': native_result, 'read_only': True}

    def capture_keys(self, request):
        if self.state.get('pending'):
            return {'ok': False, 'code': 'PENDING_OPERATION', 'pending': self.state['pending']}
        if not native.has_ptrace_capability():
            return {'ok': False, 'code': 'PRIVILEGE_REQUIRED'}
        # This child only opens the owner's process memory read-only. Killing a
        # timed-out scan cannot interrupt an inferior call or leave it traced.
        command = [sys.executable, '-I', '-m', 'wechat_linux_cli._native.native_keys',
                   'capture', '--account', request['account'], '--seconds', str(request['seconds'])]
        try:
            result = subprocess.run(command, capture_output=True, timeout=request['seconds'] + 15,
                                    env={k: os.environ[k] for k in ('PATH', 'HOME', 'LANG', 'WECHAT_LINUX_STATE_DIR')
                                         if k in os.environ})
        except subprocess.TimeoutExpired:
            return {'ok': False, 'code': 'KEY_CAPTURE_TIMEOUT'}
        try:
            return json.loads(result.stdout)
        except ValueError:
            return {'ok': False, 'code': 'KEY_CAPTURE_FAILED'}


class Service:
    def __init__(self, path, runner):
        self.path, self.runner = Path(path), runner
        self.stopping = False
        private_dir(self.path.parent)
        self.lock = os.open(str(self.path) + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.listener = None
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.exists():
                info = self.path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise ValueError('Refusing to replace non-socket state')
                self.path.unlink()
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener.bind(str(self.path))
            self.path.chmod(0o600)
            self.listener.listen(4)
            self.listener.settimeout(.5)
        except BaseException:
            if self.listener is not None:
                self.listener.close()
            os.close(self.lock)
            raise

    def dispatch(self, request):
        if not isinstance(request, dict):
            raise ValueError('Expected a JSON object')
        operation = request.get('operation')
        if operation == 'health' and set(request) == {'operation'}:
            return {'ok': True, 'version': __version__, 'pid': os.getpid(), 'uid': os.getuid(),
                    'ptrace_capability': native.has_ptrace_capability(),
                    'stopping': self.stopping,
                    'pending': getattr(self.runner, 'state', {}).get('pending')}
        if operation == 'inspect_pending' and set(request) == {'operation'}:
            return self.runner.inspect_pending()
        if operation == 'capture_keys' and set(request) == {'operation', 'account', 'seconds'}:
            if self.stopping:
                return {'ok': False, 'code': 'SERVICE_STOPPING'}
            if (not isinstance(request['account'], str)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', request['account'])
                    or type(request['seconds']) is not int or not 1 <= request['seconds'] <= 45):
                raise ValueError('Invalid capture scope')
            return self.runner.capture_keys(request)
        if operation == 'send_status' and set(request) == {'operation', 'request_id'}:
            native.make_payload(0, 'validate identifier', request['request_id'])
            state = getattr(self.runner, 'state', {})
            if (state.get('last_request_id') == request['request_id']
                    and state.get('last_result')):
                return {'ok': True, 'read_only': True, **state['last_result']}
            result = native.inspect_trial(request['request_id'])
            return {'ok': True, **result}
        if operation != 'send_text' or set(request) != {'operation', 'text', 'request_id', 'recipient'}:
            raise ValueError('Unsupported operation or parameters')
        # Protocol validation occurs before starting any backend process.
        native.make_payload(int(time.time()), request['text'], request['request_id'], request['recipient'])
        if request['request_id'] == native.REQUEST_ID:
            raise ValueError('The legacy fixed acceptance ID cannot be submitted')
        work = trial_work(request['request_id'])
        if work.exists():
            recorded = read_private(work/'request.json')
            fingerprint = hashlib.sha256(request['text'].encode()).hexdigest()
            if (recorded.get('text_sha256') != fingerprint
                    or recorded.get('recipient') != request['recipient']
                    or recorded.get('send') is not True):
                return {'ok': False, 'code': 'REQUEST_ID_CONFLICT',
                        'automatic_retry_allowed': False}
            try:
                result = native.inspect_trial(request['request_id'])
            except (OSError, ValueError):
                return {'ok': False, 'code': 'REQUEST_PENDING',
                        'automatic_retry_allowed': False}
            return {**result, 'ok': completed(result), 'replayed': True,
                    'local_history_integrated': False}
        if self.stopping:
            return {'ok': False, 'code': 'SERVICE_STOPPING'}
        return self.runner(request)

    def handle(self, conn):
        _pid, uid, _gid = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != os.getuid():
            return
        conn.settimeout(5)
        data = bytearray()
        try:
            while b'\n' not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > 16384:
                    raise ValueError('Request too large')
            result = self.dispatch(json.loads(data))
        except (ValueError, OSError, TypeError, KeyError):
            result = {'ok': False, 'code': 'INVALID_REQUEST_OR_LOCAL_STATE',
                      'automatic_retry_allowed': False}
        try:
            conn.sendall(json.dumps(result, ensure_ascii=True, allow_nan=False).encode() + b'\n')
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def run(self):
        try:
            while True:
                if self.stopping:
                    if not getattr(self.runner, 'state', {}).get('pending'):
                        break
                    # Keep the owner-only socket available for read-only status
                    # while a detached backend finishes. An uncertain result
                    # must remain pending instead of being killed at shutdown.
                    try:
                        self.runner.inspect_pending()
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
                    if not self.runner.state.get('pending'):
                        break
                try:
                    conn, _ = self.listener.accept()
                except socket.timeout:
                    continue
                with conn:
                    self.handle(conn)
        finally:
            self.listener.close()
            self.path.unlink(missing_ok=True)
            os.close(self.lock)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', type=Path, default=socket_path())
    parser.add_argument('--state', type=Path, default=Path.home()/'.local/state/wechat-linux-cli/service')
    args = parser.parse_args(argv)
    if os.getuid() == 0:
        raise ValueError('Run as the desktop user with CAP_SYS_PTRACE; a root service is unnecessary')
    os.umask(0o077)
    service = Service(args.socket, Runner(args.state))
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: setattr(service, 'stopping', True))
    service.run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
