"""One operation in an isolated process; the service never injects in its own thread."""
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import time

from ._native import native_send_candidate as native
from ._native import native_highlevel_candidate as highlevel
from ._native import native_messages
from . import media


def preflight_id(request_id):
    return 'cli-preflight-' + hashlib.sha256(request_id.encode()).hexdigest()[:32]


def work_for(request_id):
    return highlevel.work_for(request_id)


def legacy_work(request_id):
    return native.runtime_root(Path.home())/native.trial_name(request_id)


def settled(result):
    """All injected callbacks finished; this does not claim message delivery."""
    worker = result.get('worker', {})
    if result.get('queued_dispatch_call'):
        return (result.get('status') == 'trial_finished'
                and result.get('detached') is True
                and result.get('client_running_untraced') is True
                and worker.get('worker_done') is True
                and worker.get('live_callbacks') == 0
                and worker.get('dispatch_pending') is False)
    return completed(result)


def completed(result):
    worker = result.get('worker', {})
    if result.get('queued_dispatch_call'):
        return (settled(result) and result.get('phase') != 'preflight'
                and all(worker.get(k) is True for k in
                        ('manager_verified', 'request_constructed', 'submission_entered',
                         'result_returned', 'result_success'))
                and all(worker.get(k) == 0 for k in ('failure', 'result_code0', 'result_code1')))
    return (result.get('status') == 'trial_finished'
            and result.get('detached') is True
            and result.get('client_running_untraced') is True
            and all(worker.get(k) is True for k in
                    ('worker_done', 'native_roundtrip_verified', 'submission_entered'))
            and worker.get('callback_count') == 1 and worker.get('callback_destroyed') == 1
            and all(worker.get(k) == 0 for k in
                    ('live_callbacks', 'error_type', 'error_code', 'failure')))


def inspect_trial(request_id):
    if work_for(request_id).exists() and legacy_work(request_id).exists():
        return {'ok': False, 'code': 'REQUEST_ID_AMBIGUOUS', 'read_only': True,
                'automatic_retry_allowed': False}
    if work_for(request_id).exists():
        result = highlevel.inspect_trial(request_id)
    elif legacy_work(request_id).exists():
        return native.inspect_trial(request_id)
    else:
        result = highlevel.inspect_trial(preflight_id(request_id), send=False)
        result.update(phase='preflight', preflight_request_id=preflight_id(request_id),
                      request_id=request_id, code='BACKEND_ENDED_BEFORE_SEND')
    result['ok'] = completed(result)
    return result


def replay(request):
    """Return any existing request, including old network-only sends."""
    request_id = request['request_id']
    current, old = work_for(request_id), legacy_work(request_id)
    if current.exists() and old.exists():
        return {'ok': False, 'code': 'REQUEST_ID_AMBIGUOUS', 'automatic_retry_allowed': False}
    if not (current.exists() or old.exists()):
        return None
    work = current if current.exists() else old
    recorded = json.loads((work/'request.json').read_text())
    kind = 'image' if request['operation'] == 'send_image' else 'text'
    if recorded.get('request_kind', 'text') != kind:
        matches = False
    elif kind == 'image':
        _data, _suffix, sha256 = media.read_image(request['file'])
        matches = (work == current and recorded.get('payload_sha256') ==
                   media.image_fingerprint(request['recipient'], sha256))
    elif work == current:
        fingerprint = hashlib.sha256(highlevel.payload_for(request['recipient'], request['text'])).hexdigest()
        matches = recorded.get('payload_sha256') == fingerprint
    else:
        matches = recorded.get('text_sha256') == hashlib.sha256(request['text'].encode()).hexdigest()
    if (not matches or recorded.get('recipient') != request['recipient']
            or recorded.get('send') is not True):
        return {'ok': False, 'code': 'REQUEST_ID_CONFLICT', 'automatic_retry_allowed': False}
    try:
        result = inspect_trial(request_id)
    except (OSError, ValueError):
        return {'ok': False, 'code': 'REQUEST_PENDING', 'automatic_retry_allowed': False}
    return {**result, 'ok': completed(result), 'replayed': True}


def history_snapshot(recipient):
    try:
        return native_messages.main(['messages', '--account', 'me', '--chat', recipient,
                                     '--limit', '50', '--max-chars', '3000'])['items']
    except (OSError, ValueError, sqlite3.DatabaseError):
        return None


def history_evidence(before, after, text):
    if before is None or after is None:
        return {'local_history_integrated': None}
    ids = {(r['database'], r['local_id']) for r in before}
    matches = [r for r in after if (r['database'], r['local_id']) not in ids
               and r.get('type') == 1 and r.get('text') == text and not r.get('truncated')]
    proof = {'local_history_integrated': True if len(matches) == 1 else None,
             'local_history_exact_matches': len(matches),
             'local_history_source': 'local_client_database'}
    if len(matches) == 1:
        proof.update(local_message_id=matches[0]['local_id'],
                     server_message_id=matches[0]['server_id'])
    return proof


def send_text(request):
    previous = replay(request)
    if previous is not None:
        return previous
    # Validate syntax here. Authorization belongs to the calling skill/owner,
    # and is independent of the targets covered by runtime acceptance.
    highlevel.payload_for(request['recipient'], request['text'])
    pid, start = highlevel.client_identity()
    proof_id = preflight_id(request['request_id'])
    proof = highlevel.trial(False, 'HELLO', proof_id, expected_pid=pid,
                           recipient=request['recipient'], expected_start_time=start, allow_live=True)
    if not proof.get('highlevel_preflight_verified'):
        return {**proof, 'ok': False, 'phase': 'preflight',
                'preflight_request_id': proof_id, 'request_id': request['request_id'],
                'code': 'NATIVE_PREFLIGHT_FAILED', 'automatic_retry_allowed': False}
    before = history_snapshot(request['recipient'])
    result = highlevel.trial(True, request['text'], request['request_id'], request['recipient'],
                            expected_pid=pid, expected_start_time=start,
                            preflight_request_id=proof_id, allow_live=True)
    result['ok'] = completed(result)
    if result['ok']:
        evidence = {'local_history_integrated': None}
        for attempt in range(3):
            evidence = history_evidence(before, history_snapshot(request['recipient']), request['text'])
            if evidence.get('local_history_integrated') or before is None:
                break
            if attempt < 2:
                time.sleep(.5)
        result.update(evidence)
        # No recipient/UI inference from the native return or database snapshot.
        result['recipient_delivery_verified'] = False
        highlevel.base.save(work_for(request['request_id'])/'acceptance.json',
                            {'request_id': request['request_id'], **evidence,
                             'recipient_delivery_verified': False, 'verified_at': time.time()})
    return result


def stage_image(request):
    """Reserve a content identity and snapshot once for construction and send."""
    from .service import private_dir, read_private, save
    data, suffix, sha256 = media.read_image(request['file'])
    root = highlevel.runtime_root(Path.home())
    private_dir(root)
    private_dir(root/'media-inputs')
    directory = root/'media-inputs'/hashlib.sha256(request['request_id'].encode()).hexdigest()[:24]
    private_dir(directory)
    manifest = directory/'request.json'
    expected = {'request_id': request['request_id'], 'request_kind': 'image',
                'recipient': request['recipient'], 'media_sha256': sha256, 'suffix': suffix}
    path = directory/('input' + suffix)
    if manifest.exists():
        if read_private(manifest) != expected:
            raise ValueError('REQUEST_ID_CONFLICT: image request was already reserved')
        # An interrupted snapshot is an inspection case, never a fresh send.
        if path.is_symlink() or media.read_image(str(path))[2] != sha256:
            raise ValueError('REQUEST_PENDING: reserved image snapshot is unavailable or changed')
    else:
        save(manifest, expected)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    return str(path)


def image_history_evidence(before, after):
    if before is None or after is None:
        return {'local_history_integrated': None}
    ids = {(r['database'], r['local_id']) for r in before}
    matches = [r for r in after if (r['database'], r['local_id']) not in ids and r.get('type') == 3]
    # Type and timing cannot identify image bytes. Leave integration unproved
    # and expose candidates for independent content/UI acceptance instead.
    proof = {'local_history_integrated': None, 'local_history_type_matches': len(matches),
             'local_history_source': 'local_client_database'}
    if len(matches) == 1:
        proof.update(local_message_id=matches[0]['local_id'], server_message_id=matches[0]['server_id'])
    return proof


def send_image(request):
    previous = replay(request)
    if previous is not None:
        return previous
    highlevel.payload_for(request['recipient'], request['file'])
    path = stage_image(request)
    pid, start = highlevel.client_identity()
    proof_id = preflight_id(request['request_id'])
    options = {'expected_pid': pid, 'expected_start_time': start, 'allow_live': True,
               'request_kind': 'image', 'allow_media_trial': True}
    proof = highlevel.trial(False, path, proof_id, request['recipient'], **options)
    if not proof.get('highlevel_preflight_verified'):
        return {**proof, 'ok': False, 'phase': 'preflight',
                'preflight_request_id': proof_id, 'request_id': request['request_id'],
                'code': 'NATIVE_PREFLIGHT_FAILED', 'automatic_retry_allowed': False}
    before = history_snapshot(request['recipient'])
    result = highlevel.trial(True, path, request['request_id'], request['recipient'],
                            preflight_request_id=proof_id, **options)
    result['ok'] = completed(result)
    if result['ok']:
        evidence = image_history_evidence(before, history_snapshot(request['recipient']))
        result.update(evidence, recipient_delivery_verified=False)
        highlevel.base.save(work_for(request['request_id'])/'acceptance.json',
                            {'request_id': request['request_id'], **evidence,
                             'recipient_delivery_verified': False, 'verified_at': time.time()})
    return result


def main():
    try:
        request = json.load(sys.stdin)
        if request['operation'] == 'send_text':
            result = send_text(request)
        elif request['operation'] == 'send_image':
            result = send_image(request)
        else:
            raise ValueError('Unsupported operation')
    except (ValueError, OSError) as error:
        prefix = str(error).partition(':')[0]
        result = {'ok': False, 'code': prefix if prefix in ('REQUEST_ID_CONFLICT', 'REQUEST_PENDING')
                  else 'NATIVE_OPERATION_FAILED',
                  'error_type': type(error).__name__, 'automatic_retry_allowed': False}
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
