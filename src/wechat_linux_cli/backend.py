"""One operation in an isolated process; the service never injects in its own thread."""
import json
import sys

from ._native import native_send_candidate as native


def completed(result):
    worker = result.get('worker', {})
    return (result.get('status') == 'trial_finished'
            and result.get('detached') is True
            and result.get('client_running_untraced') is True
            and all(worker.get(k) is True for k in
                    ('worker_done', 'native_roundtrip_verified', 'submission_entered'))
            and worker.get('callback_count') == 1 and worker.get('callback_destroyed') == 1
            and all(worker.get(k) == 0 for k in
                    ('live_callbacks', 'error_type', 'error_code', 'failure')))


def main():
    try:
        request = json.load(sys.stdin)
        result = native.trial(True, request['text'], request['request_id'], request['recipient'])
        result['ok'] = completed(result)
        result['local_history_integrated'] = False
    except (ValueError, OSError) as error:
        result = {'ok': False, 'code': 'NATIVE_OPERATION_FAILED',
                  'error_type': type(error).__name__, 'automatic_retry_allowed': False}
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
