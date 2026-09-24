#!/usr/bin/env python3
"""Pinned-client high-level message construction trial; not a supported CLI route yet."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import time

from . import native_send_candidate as base
from .native_send_probe import desktop_identity, run_desktop_preparation


def payload_for(recipient, text):
    if (not isinstance(recipient, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,128}', recipient)
            or recipient != 'filehelper'):
        raise ValueError('Only the exact filehelper native chat ID is enabled for this trial')
    if not isinstance(text, str) or not text or '\0' in text:
        raise ValueError('Text must be nonempty and contain no NUL')
    recipient_bytes, text_bytes = recipient.encode(), text.encode()
    if not 1 <= len(text_bytes) <= 1024:
        raise ValueError('Text must be 1..1024 UTF-8 bytes')
    return (len(recipient_bytes).to_bytes(2, 'little') + len(text_bytes).to_bytes(2, 'little')
            + recipient_bytes + text_bytes)


def runtime_root(home):
    configured = os.environ.get('WECHAT_LINUX_RUNTIME_DIR')
    return ((Path(configured).expanduser() if configured else
             Path(home)/'.local/state/wechat-linux-cli')/'native-highlevel')


def trial(send, text, request_id, recipient='filehelper', *, event_tid=None,
          expected_pid=None, expected_start_time=None):
    if send:
        raise ValueError('HIGHLEVEL_SEND_NOT_READY: event-thread call path is not verified')
    if not all(isinstance(value, int) and value > 0
               for value in (event_tid, expected_pid, expected_start_time)):
        raise ValueError('Observed event TID, client PID and start time are required')
    payload = payload_for(recipient, text)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{3,79}', request_id):
        raise ValueError('request-id must be 4..80 ASCII letters/digits/dot/underscore/hyphen')
    uid = int(os.environ.get('SUDO_UID', '0')) if os.geteuid() == 0 else os.getuid()
    owner = pwd.getpwuid(uid)
    work_root = runtime_root(owner.pw_dir)
    work_name = ('send-' if send else 'preflight-') + hashlib.sha256(request_id.encode()).hexdigest()[:24]
    work = work_root/work_name
    fingerprint = hashlib.sha256(payload).hexdigest()
    if work.exists():
        previous = json.loads((work/'request.json').read_text())
        if previous.get('payload_sha256') != fingerprint or previous.get('send') != send:
            raise ValueError('REQUEST_ID_CONFLICT: same ID has different content or operation')
        if (work/'result.json').exists():
            return {'result_path': str(work/'result.json'), 'replayed': True,
                    **json.loads((work/'result.json').read_text())}
        raise ValueError('REQUEST_PENDING: inspect the existing request before retrying')
    if uid == 0 or (os.geteuid() != 0 and not base.has_ptrace_capability()):
        raise ValueError('PRIVILEGE_REQUIRED: owner-scoped ptrace capability is required')
    targets = []
    for target in Path('/proc').iterdir():
        if not target.name.isdigit():
            continue
        try:
            if target.stat().st_uid == uid and Path(os.readlink(target/'exe')).name == 'wechat':
                targets.append(target)
        except OSError:
            pass
    if len(targets) != 1:
        raise ValueError('Exactly one desktop WeChat process is required')
    target = targets[0]
    pid = int(target.name)
    start = (target/'stat').read_text().rsplit(')', 1)[1].split()[19]
    if pid != expected_pid or start != str(expected_start_time):
        raise ValueError('Client process identity changed since the read-only observation')
    if not Path('/proc', str(pid), 'task', str(event_tid)).exists():
        raise ValueError('Observed event thread no longer exists')
    if not base.process_running_untraced(pid, start):
        raise ValueError('Client is already traced, stopped or exiting')
    with desktop_identity(uid, owner.pw_gid):
        work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        work_root.chmod(0o700)
        work.mkdir(mode=0o700)
        base.save(work/'request.json', {'request_id': request_id, 'recipient': recipient,
                                        'send': send, 'payload_sha256': fingerprint})
    config = None
    stage = 'prepare_executable'
    try:
        prepared = run_desktop_preparation(target, work, uid, owner.pw_gid)
        stage = 'compile_helper'
        helper = base.compile_helper(work, uid, owner.pw_gid,
                                     source=Path(__file__).with_name('native_highlevel_helper.c'))
        config = {**prepared, 'pid': pid, 'start_time': start, 'uid': uid, 'gid': owner.pw_gid,
                  'binary_copy': str(work/'wechat.elf'), 'helper': str(helper),
                  'injection_result': str(work/'injection.json'),
                  'worker_result': str(work/'worker.json'), 'payload_hex': payload.hex(),
                  'send': send, 'launch_symbol': 'ncut_highlevel_sync',
                  'sync_call': True, 'event_tid': event_tid}
        stage = 'run_injection'
        result = base.run_injection(config, work)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        result = {'status': 'local_failure', 'stage': stage,
                  'error': str(error)[:500] if isinstance(error, ValueError) else type(error).__name__,
                  'automatic_retry_allowed': False}
    finally:
        debugger_live = False
        try:
            record = json.loads((work/'debugger-process.json').read_text())
            debugger_live = Path('/proc', str(record['pid'])).exists()
        except (OSError, ValueError, KeyError):
            pass
        if not debugger_live:
            (work/'wechat.elf').unlink(missing_ok=True)
            if config is not None:
                config.pop('payload_hex', None)
                base.save(work/'config.json', config)
        for artifact in work.iterdir():
            try:
                artifact.chmod(0o600)
                os.chown(artifact, uid, owner.pw_gid)
            except FileNotFoundError:
                pass
    result['client_running_untraced'] = base.process_running_untraced(pid, start)
    result['request_id'] = request_id
    worker = result.get('worker', {})
    result['highlevel_preflight_verified'] = bool(
        result.get('status') == 'trial_finished' and result.get('detached')
        and result['client_running_untraced'] and worker.get('worker_done')
        and not worker.get('failure')
        and worker.get('manager_verified') and worker.get('request_constructed'))
    result['native_submission_entered'] = bool(send and worker.get('submission_entered'))
    result['local_insert_result_success'] = (bool(worker.get('result_success'))
                                             if send and worker.get('result_returned') else None)
    result['message_send_performed'] = False if not send else None
    result['local_history_integrated'] = None
    result['automatic_retry_allowed'] = False
    result['completed_at'] = time.time()
    base.save(work/'result.json', result)
    return {'result_path': str(work/'result.json'), **result}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['preflight', 'send'])
    parser.add_argument('--request-id', required=True)
    parser.add_argument('--text', required=True)
    parser.add_argument('--recipient', default='filehelper')
    parser.add_argument('--event-tid', type=int, required=True)
    parser.add_argument('--expected-pid', type=int, required=True)
    parser.add_argument('--expected-start-time', type=int, required=True)
    args = parser.parse_args(argv)
    try:
        result = trial(args.operation == 'send', args.text, args.request_id, args.recipient,
                       event_tid=args.event_tid, expected_pid=args.expected_pid,
                       expected_start_time=args.expected_start_time)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError) as error:
        print(json.dumps({'ok': False, 'error': str(error)[:500]}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.exit(main())
