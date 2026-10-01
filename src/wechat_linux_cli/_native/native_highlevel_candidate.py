#!/usr/bin/env python3
"""Pinned-client queued message construction and submission.

Direct experimental calls remain disabled; the service enables the reviewed
queued route explicitly. The synchronous C entry stays compile-disabled.
"""
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
from .. import media

LIVE_HIGHLEVEL_PREFLIGHT_ENABLED = False
LIVE_HIGHLEVEL_SEND_ENABLED = False


def payload_for(recipient, text):
    if not isinstance(recipient, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,128}', recipient):
        raise ValueError('Recipient must be an exact native chat ID: 1..128 ASCII letters/digits/_.@-')
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


def work_for(request_id, send=True):
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{3,79}', request_id):
        raise ValueError('request-id must be 4..80 ASCII letters/digits/dot/underscore/hyphen')
    uid = int(os.environ.get('SUDO_UID', '0')) if os.geteuid() == 0 else os.getuid()
    return runtime_root(pwd.getpwuid(uid).pw_dir)/(
        ('send-' if send else 'preflight-') + hashlib.sha256(request_id.encode()).hexdigest()[:24])


def inspect_trial(request_id, send=True):
    """Read reports and independent acceptance without touching the client."""
    work = work_for(request_id, send)
    result = json.loads((work/'result.json').read_text())
    if (work/'worker.json').exists():
        result['worker'] = json.loads((work/'worker.json').read_text())
    if (work/'acceptance.json').exists():
        proof = json.loads((work/'acceptance.json').read_text())
        if proof.get('request_id') == request_id:
            for key in ('local_history_integrated', 'local_history_exact_matches',
                        'local_history_type_matches', 'local_history_source',
                        'local_message_id', 'server_message_id', 'recipient_delivery_verified',
                        'linux_ui_verified', 'replay_verified'):
                if key in proof:
                    result[key] = proof[key]
    worker = result.get('worker', {})
    if (result.get('status') in ('worker_pending', 'callback_pending')
            and worker.get('worker_done') and worker.get('live_callbacks') == 0
            and worker.get('dispatch_pending') is False):
        result['status'] = 'trial_finished'
    if send:
        result['native_submission_entered'] = bool(worker.get('submission_entered'))
        result['local_insert_result_success'] = (
            bool(worker.get('result_success')) if worker.get('result_returned') else None)
    return {'result_path': str(work/'result.json'), 'read_only': True, **result}


def client_identity():
    uid = int(os.environ.get('SUDO_UID', '0')) if os.geteuid() == 0 else os.getuid()
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
    start = int((target/'stat').read_text().rsplit(')', 1)[1].split()[19])
    if not base.process_running_untraced(int(target.name), start):
        raise ValueError('Client is already traced, stopped or exiting')
    return int(target.name), start


def require_preflight(root, request_id, pid, start_time, *, request_kind='text', media_sha256=None,
                      recipient=None):
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{3,79}', request_id):
        raise ValueError('VERIFIED_PREFLIGHT_REQUIRED: a preflight request ID is required')
    work = root/('preflight-' + hashlib.sha256(request_id.encode()).hexdigest()[:24])
    result = json.loads((work/'result.json').read_text())
    config = json.loads((work/'config.json').read_text())
    media_target_matches = (request_kind == 'text' or recipient is None or
                           json.loads((work/'request.json').read_text()).get('recipient') == recipient)
    worker = result.get('worker', {})
    if not (result.get('request_id') == request_id and result.get('highlevel_preflight_verified')
            and result.get('detached') and result.get('queued_dispatch_call')
            and config.get('pid') == pid and str(config.get('start_time')) == str(start_time)
            and config.get('send') is False and config.get('launch_symbol') == 'ncut_highlevel_enqueue'
            and config.get('request_kind', 'text') == request_kind
            and (request_kind == 'text' or config.get('media_sha256') == media_sha256)
            and media_target_matches
            and worker.get('worker_done') and worker.get('manager_verified')
            and worker.get('request_constructed') and not worker.get('failure')
            and not worker.get('submission_entered') and not worker.get('live_callbacks')
            and not worker.get('dispatch_pending')):
        raise ValueError('VERIFIED_PREFLIGHT_REQUIRED: queued construction proof does not match this client')


def trial(send, text, request_id, recipient='filehelper', *, event_tid=None,
          expected_pid=None, expected_start_time=None, preflight_request_id=None,
          allow_live=False, request_kind='text', allow_media_trial=False):
    if request_kind not in ('text', 'image'):
        raise ValueError('Unsupported queued request kind')
    if request_kind != 'text' and not allow_media_trial:
        raise ValueError('MEDIA_TRIAL_DISABLED: image requests require a reviewed one-shot trial')
    payload = payload_for(recipient, text)
    work = work_for(request_id, send)
    uid = int(os.environ.get('SUDO_UID', '0')) if os.geteuid() == 0 else os.getuid()
    owner = pwd.getpwuid(uid)
    work_root = runtime_root(owner.pw_dir)
    fingerprint = hashlib.sha256(payload).hexdigest()
    media_bytes = None
    media_sha256 = None
    media_suffix = None
    if request_kind == 'image':
        # Read as the desktop owner, even for a root-run trial. Only regular,
        # bounded PNG/JPEG inputs enter the experiment; snapshot before attach.
        with desktop_identity(uid, owner.pw_gid):
            media_bytes, media_suffix, media_sha256 = media.read_image(text)
        fingerprint = media.image_fingerprint(recipient, media_sha256)
    if work.exists():
        previous = json.loads((work/'request.json').read_text())
        if previous.get('payload_sha256') != fingerprint or previous.get('send') != send:
            raise ValueError('REQUEST_ID_CONFLICT: same ID has different content or operation')
        if (work/'result.json').exists():
            return {**inspect_trial(request_id, send), 'replayed': True}
        raise ValueError('REQUEST_PENDING: inspect the existing request before retrying')
    if send and not (LIVE_HIGHLEVEL_SEND_ENABLED or allow_live):
        raise ValueError('HIGHLEVEL_SEND_NOT_READY: live queued send acceptance is not enabled')
    if not (LIVE_HIGHLEVEL_PREFLIGHT_ENABLED or allow_live):
        raise ValueError(
            'HIGHLEVEL_PREFLIGHT_DISABLED: direct trials are disabled; use the installed service')
    if not all(type(value) is int and value > 0
               for value in (expected_pid, expected_start_time)):
        raise ValueError('Observed client PID and start time are required')
    if send:
        require_preflight(work_root, preflight_request_id, expected_pid, expected_start_time,
                          request_kind=request_kind, media_sha256=media_sha256, recipient=recipient)
    if uid == 0 or (os.geteuid() != 0 and not base.has_ptrace_capability()):
        raise ValueError('PRIVILEGE_REQUIRED: owner-scoped ptrace capability is required')
    pid, start = client_identity()
    target = Path('/proc', str(pid))
    if pid != expected_pid or start != expected_start_time:
        raise ValueError('Client process identity changed since the read-only observation')
    if not base.process_running_untraced(pid, start):
        raise ValueError('Client is already traced, stopped or exiting')
    with desktop_identity(uid, owner.pw_gid):
        work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        work_root.chmod(0o700)
        work.mkdir(mode=0o700)
        base.save(work/'request.json', {'request_id': request_id, 'recipient': recipient,
                                        'send': send, 'payload_sha256': fingerprint,
                                        'request_kind': request_kind, 'media_sha256': media_sha256})
        if media_bytes is not None:
            snapshot_path = work/('input' + media_suffix)
            with snapshot_path.open('xb') as file:
                file.write(media_bytes)
            snapshot_path.chmod(0o600)
            payload = payload_for(recipient, str(snapshot_path))
    config = None
    stage = 'prepare_executable'
    try:
        prepared = run_desktop_preparation(target, work, uid, owner.pw_gid)
        stage = 'compile_helper'
        helper = base.compile_helper(work, uid, owner.pw_gid,
                                     source=Path(__file__).with_name('native_highlevel_helper.c'),
                                     highlevel_dispatch=True, highlevel_send=send,
                                     highlevel_image=request_kind == 'image')
        config = {**prepared, 'pid': pid, 'start_time': start, 'uid': uid, 'gid': owner.pw_gid,
                  'binary_copy': str(work/'wechat.elf'), 'helper': str(helper),
                  'injection_result': str(work/'injection.json'),
                  'worker_result': str(work/'worker.json'), 'payload_hex': payload.hex(),
                  'send': send, 'launch_symbol': 'ncut_highlevel_enqueue',
                  'sync_call': False, 'dispatch_call': True, 'highlevel_send_trial': send,
                  'preflight_request_id': preflight_request_id,
                  'request_kind': request_kind, 'media_sha256': media_sha256}
        stage = 'run_injection'
        result = base.run_injection(config, work)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        result = {'status': 'local_failure', 'stage': stage,
                  'error': str(error)[:500] if isinstance(error, ValueError) else type(error).__name__,
                  'automatic_retry_allowed': False}
    except KeyboardInterrupt:
        result = {'status': 'operator_interrupt', 'stage': stage,
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
    result['request_kind'] = request_kind
    worker = result.get('worker', {})
    result['highlevel_preflight_verified'] = bool(
        result.get('status') == 'trial_finished' and result.get('detached')
        and result['client_running_untraced'] and worker.get('worker_done')
        and result.get('queued_dispatch_call') and not result.get('armed')
        and not worker.get('live_callbacks') and not worker.get('dispatch_pending')
        and not worker.get('failure')
        and not worker.get('submission_entered')
        and worker.get('manager_verified') and worker.get('request_constructed'))
    result['helper_unload_policy'] = 'small_module_remains_until_client_exit_for_callback_safety'
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
    parser.add_argument('--event-tid', type=int, help='Legacy option; queued preflight does not select this thread')
    parser.add_argument('--expected-pid', type=int, required=True)
    parser.add_argument('--expected-start-time', type=int, required=True)
    parser.add_argument('--preflight-request-id')
    args = parser.parse_args(argv)
    try:
        result = trial(args.operation == 'send', args.text, args.request_id, args.recipient,
                       event_tid=args.event_tid, expected_pid=args.expected_pid,
                       expected_start_time=args.expected_start_time,
                       preflight_request_id=args.preflight_request_id)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError) as error:
        print(json.dumps({'ok': False, 'error': str(error)[:500]}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.exit(main())
