import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wechat_linux_cli import backend, cli, service


def queued_result(**worker_changes):
    return {'status': 'trial_finished', 'detached': True, 'client_running_untraced': True,
            'queued_dispatch_call': True, 'worker': {
                'worker_done': True, 'live_callbacks': 0, 'dispatch_pending': False,
                'manager_verified': True, 'request_constructed': True,
                'submission_entered': True, 'result_returned': True, 'result_success': True,
                'result_code0': 0, 'result_code1': 0, 'failure': 0, **worker_changes}}


class QueuedBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'WECHAT_LINUX_RUNTIME_DIR': self.temp.name})
        self.env.start()
        self.request = {'operation': 'send_text', 'request_id': 'queued-service-test',
                        'recipient': 'filehelper', 'text': '中文\nUnicode ✅'}

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def record(self, result=None):
        work = backend.work_for(self.request['request_id'])
        work.mkdir(parents=True)
        payload = backend.highlevel.payload_for(self.request['recipient'], self.request['text'])
        (work/'request.json').write_text(json.dumps({
            'send': True, 'recipient': 'filehelper',
            'payload_sha256': hashlib.sha256(payload).hexdigest()}))
        if result is not None:
            (work/'result.json').write_text(json.dumps(result))
        return work

    def test_completed_does_not_accept_result_errors_pending_callbacks_or_preflight(self):
        self.assertTrue(backend.completed(queued_result()))
        for changes in ({'live_callbacks': 1}, {'dispatch_pending': True},
                        {'result_returned': False}, {'result_success': False},
                        {'failure': 3}, {'result_code1': 2}, {'submission_entered': False}):
            self.assertFalse(backend.completed(queued_result(**changes)), changes)
        self.assertFalse(backend.completed({**queued_result(), 'phase': 'preflight'}))
        self.assertTrue(backend.settled(queued_result(result_success=False, failure=3)))

    def test_existing_send_replays_without_client_identity_preflight_or_privilege(self):
        work = self.record(queued_result())
        (work/'acceptance.json').write_text(json.dumps({
            'request_id': self.request['request_id'], 'local_history_integrated': True,
            'recipient_delivery_verified': True}))
        with patch.object(backend.highlevel, 'client_identity') as identity, \
             patch.object(backend.highlevel, 'trial') as trial:
            result = backend.send_text(self.request)
        self.assertTrue(result['ok'])
        self.assertTrue(result['replayed'])
        self.assertTrue(result['local_history_integrated'])
        identity.assert_not_called()
        trial.assert_not_called()
        self.assertEqual(backend.replay({**self.request, 'text': 'different'})['code'], 'REQUEST_ID_CONFLICT')

    def test_reserved_send_never_restarts_without_final_report(self):
        self.record()
        with patch.object(backend.highlevel, 'trial') as trial:
            self.assertEqual(backend.send_text(self.request)['code'], 'REQUEST_PENDING')
        trial.assert_not_called()

    def test_legacy_request_replays_before_new_recipient_scope_or_native_calls(self):
        old = backend.legacy_work(self.request['request_id'])
        old.mkdir(parents=True)
        (old/'request.json').write_text(json.dumps({
            'send': True, 'recipient': 'previously_authorized_target',
            'text_sha256': hashlib.sha256(self.request['text'].encode()).hexdigest()}))
        (old/'result.json').write_text(json.dumps({'status': 'worker_pending'}))
        with patch.object(backend.highlevel, 'client_identity') as identity:
            result = backend.send_text({**self.request, 'recipient': 'previously_authorized_target'})
        self.assertTrue(result['replayed'])
        self.assertFalse(result['ok'])
        identity.assert_not_called()
        self.assertEqual(backend.replay(self.request)['code'], 'REQUEST_ID_CONFLICT')

    def test_failed_construction_preflight_never_calls_send(self):
        proof = queued_result(submission_entered=False, failure=12)
        proof['highlevel_preflight_verified'] = False
        with patch.object(backend.highlevel, 'client_identity', return_value=(10, 99)), \
             patch.object(backend.highlevel, 'trial', return_value=proof) as trial:
            result = backend.send_text(self.request)
        self.assertFalse(result['ok'])
        self.assertEqual(result['phase'], 'preflight')
        trial.assert_called_once()
        self.assertFalse(trial.call_args.args[0])

    def test_new_send_uses_queued_construction_proof_and_records_only_database_readback(self):
        work = backend.work_for(self.request['request_id'])
        work.mkdir(parents=True)
        # Stub replay because the empty work directory is created by our fake
        # native call below; production creates it after reserving the request.
        old = {'database': 'message/message_0.db', 'local_id': 1, 'type': 1,
               'text': self.request['text'], 'server_id': '10'}
        new = {**old, 'local_id': 2, 'server_id': '11'}
        proof = {'highlevel_preflight_verified': True}
        with patch.object(backend, 'replay', return_value=None), \
             patch.object(backend.highlevel, 'client_identity', return_value=(10, 99)), \
             patch.object(backend.highlevel, 'trial', side_effect=[proof, queued_result()]) as trial, \
             patch.object(backend, 'history_snapshot', side_effect=[[old], [new, old]]):
            result = backend.send_text(self.request)
        self.assertTrue(result['ok'])
        self.assertTrue(result['local_history_integrated'])
        self.assertEqual(result['local_message_id'], 2)
        self.assertFalse(result['recipient_delivery_verified'])
        self.assertEqual(trial.call_args_list[1].kwargs['preflight_request_id'], backend.preflight_id(self.request['request_id']))
        self.assertTrue(trial.call_args_list[1].kwargs['allow_live'])
        saved = json.loads((work/'acceptance.json').read_text())
        self.assertNotIn(self.request['text'], json.dumps(saved))

    def test_missing_readback_or_multiple_new_matches_remain_unknown(self):
        row = {'database': 'message/message_0.db', 'local_id': 1,
               'type': 1, 'text': 'text', 'server_id': '10'}
        self.assertIsNone(backend.history_evidence(None, [row], 'text')['local_history_integrated'])
        self.assertIsNone(backend.history_evidence([row], [row], 'text')['local_history_integrated'])
        self.assertIsNone(backend.history_evidence([], [row, {**row, 'local_id': 2}], 'text')['local_history_integrated'])

    def test_malformed_recipient_rejected_before_any_process_observation(self):
        with patch.object(backend.highlevel, 'client_identity') as identity:
            with self.assertRaisesRegex(ValueError, 'exact native chat ID'):
                backend.send_text({**self.request, 'recipient': '../escape'})
        identity.assert_not_called()

    def test_private_group_and_clawbot_targets_reach_both_preflight_and_send(self):
        for recipient in ('wxid_fixture', 'fixture@chatroom', 'fixture@weclaw'):
            with self.subTest(recipient=recipient), \
                    patch.object(backend, 'replay', return_value=None), \
                    patch.object(backend.highlevel, 'client_identity', return_value=(10, 99)), \
                    patch.object(backend.highlevel, 'trial', side_effect=[
                        {'highlevel_preflight_verified': True}, queued_result()]) as trial, \
                    patch.object(backend, 'history_snapshot', return_value=None), \
                    patch.object(backend.highlevel.base, 'save'):
                result = backend.send_text({**self.request, 'recipient': recipient})
                self.assertTrue(result['ok'])
                self.assertEqual(trial.call_args_list[0].kwargs['recipient'], recipient)
                self.assertEqual(trial.call_args_list[1].args[3], recipient)

    def test_cli_status_reads_queued_acceptance_when_service_is_unavailable(self):
        self.record(queued_result())
        with patch.object(cli.client, 'call', side_effect=FileNotFoundError):
            result = cli.run(['send-status', '--request-id', self.request['request_id']])
        self.assertTrue(result['ok'])
        self.assertTrue(result['read_only'])

    def test_pending_construction_is_not_treated_as_no_injection(self):
        work = backend.highlevel.work_for(backend.preflight_id(self.request['request_id']), send=False)
        work.mkdir(parents=True)
        (work/'config.json').write_text('{}')
        (work/'config.json').chmod(0o600)
        self.assertFalse(service.before_injection(self.request['request_id']))
        (work/'result.json').write_text(json.dumps(queued_result(
            submission_entered=False, live_callbacks=1, dispatch_pending=True)))
        self.assertFalse(service.before_injection(self.request['request_id']))

    def test_process_identity_accepts_json_integer_and_proc_string_start_time(self):
        from wechat_linux_cli._native import native_send_candidate
        pid = os.getpid()
        start = Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()[19]
        self.assertTrue(native_send_candidate.process_running_untraced(pid, start))
        self.assertTrue(native_send_candidate.process_running_untraced(pid, int(start)))
        self.assertFalse(native_send_candidate.process_running_untraced(pid, int(start)+1))


if __name__ == '__main__':
    unittest.main()
