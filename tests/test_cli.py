import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wechat_linux_cli import cli


class CliTests(unittest.TestCase):
    def test_cli_routes_read_arguments_without_shell_interpolation(self):
        with patch.object(cli.native_messages, 'main', return_value={'ok': True}) as native:
            self.assertEqual(cli.run(['messages', '--chat', 'A $(literal)', '--before', '200', '--since', '100']), {'ok': True})
        self.assertEqual(native.call_args.args[0], ['messages', '--account', 'me', '--chat', 'A $(literal)', '--limit', '20', '--max-chars', '1000', '--before', '200', '--since', '100'])

    def test_errors_have_json_and_nonzero_exit(self):
        with patch.object(cli.native_messages, 'main', side_effect=ValueError('NATIVE_KEYS_REQUIRED: missing')), patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(['status']), 1)
        self.assertEqual(json.loads(output.getvalue())['code'], 'NATIVE_KEYS_REQUIRED')

    def test_missing_chat_does_not_read_any_account_data(self):
        with patch.object(cli.native_messages, 'main') as native, patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(['messages']), 1)
        native.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['code'], 'USAGE_ERROR')

    def test_send_status_recovers_preinjection_failure_after_service_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)/'.local/state/wechat-linux-cli/service/operation.json'
            state.parent.mkdir(parents=True)
            state.write_text(json.dumps({'last_request_id': 'failed-once', 'pending': None,
                                         'last_result': {'ok': False,
                                                         'code': 'NATIVE_OPERATION_FAILED'}}))
            state.chmod(0o600)
            with patch.object(cli.client, 'call', side_effect=FileNotFoundError), \
                 patch.object(Path, 'home', return_value=Path(directory)):
                result = cli.run(['send-status', '--request-id', 'failed-once'])
            self.assertFalse(result['ok'])
            self.assertTrue(result['read_only'])
            self.assertEqual(result['code'], 'NATIVE_OPERATION_FAILED')


if __name__ == '__main__':
    unittest.main()
