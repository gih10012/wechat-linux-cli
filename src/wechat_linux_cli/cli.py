"""Public JSON command interface."""
import argparse
import json
import sys

from . import __version__
from . import client
from ._native import native_messages


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError('USAGE_ERROR: ' + message)


def parser():
    command = Parser(description='Read and control the owner\'s local Linux WeChat client as JSON.')
    command.add_argument('--version', action='version', version=__version__)
    operations = command.add_subparsers(dest='operation', required=True)
    status = operations.add_parser('status', help='Check local database readability')
    status.add_argument('--account', default='me')
    conversations = operations.add_parser('conversations', help='List recent local conversations')
    conversations.add_argument('--account', default='me')
    conversations.add_argument('--query', default='')
    conversations.add_argument('--unread', action='store_true')
    conversations.add_argument('--limit', type=int, default=10)
    messages = operations.add_parser('messages', help='Read one local conversation')
    messages.add_argument('--account', default='me')
    messages.add_argument('--chat', required=True, help='Exact chat ID or unique display name')
    messages.add_argument('--limit', type=int, default=20)
    messages.add_argument('--before', type=int, help='Exclusive Unix timestamp')
    messages.add_argument('--since', type=int, help='Inclusive Unix timestamp')
    messages.add_argument('--max-chars', type=int, default=1000)
    operations.add_parser('service-status', help='Check the installed local control service')
    operations.add_parser('inspect-pending', help='Inspect an unfinished operation without sending again')
    capture = operations.add_parser('capture-keys', help='Read keys once from the owner\'s running client through the service')
    capture.add_argument('--account', default='me')
    capture.add_argument('--seconds', type=int, default=15)
    send = operations.add_parser('send-text', help='Send via the local service; local outgoing history integration is pending')
    send.add_argument('--recipient', required=True, help='Exact native chat ID; use conversations to resolve the intended target')
    send.add_argument('--text', required=True)
    send.add_argument('--request-id', required=True, help='Unique ID for this operation; reuse it for the same operation only')
    status_send = operations.add_parser('send-status', help='Read the recorded outcome of a prior send')
    status_send.add_argument('--request-id', required=True)
    return command


def run(argv=None):
    args = parser().parse_args(argv)
    if args.operation == 'service-status':
        return client.call({'operation': 'health'})
    if args.operation == 'inspect-pending':
        return client.call({'operation': 'inspect_pending'})
    if args.operation == 'capture-keys':
        return client.call({'operation': 'capture_keys', 'account': args.account, 'seconds': args.seconds})
    if args.operation == 'send-text':
        return client.call({'operation': 'send_text', 'recipient': args.recipient,
                            'text': args.text, 'request_id': args.request_id})
    if args.operation == 'send-status':
        from pathlib import Path
        from . import service
        from ._native.native_send_candidate import inspect_trial, make_payload
        make_payload(0, 'validate identifier', args.request_id)
        try:
            return client.call({'operation': 'send_status', 'request_id': args.request_id})
        except (OSError, ValueError):
            # A stopped service still leaves its last definitive pre-injection
            # result in owner-only state, even when no native trial exists.
            state_path = Path.home()/'.local/state/wechat-linux-cli/service/operation.json'
            if state_path.exists():
                state = service.read_private(state_path)
                if (state.get('last_request_id') == args.request_id
                        and isinstance(state.get('last_result'), dict)):
                    return {'read_only': True, **state['last_result']}
            return {'ok': True, 'read_only': True, **inspect_trial(args.request_id)}
    native_args = [args.operation, '--account', args.account]
    if args.operation == 'conversations':
        native_args += ['--query', args.query, '--limit', str(args.limit)]
        if args.unread:
            native_args.append('--unread')
    elif args.operation == 'messages':
        native_args += ['--chat', args.chat, '--limit', str(args.limit), '--max-chars', str(args.max_chars)]
        if args.before is not None:
            native_args += ['--before', str(args.before)]
        if args.since is not None:
            native_args += ['--since', str(args.since)]
    return native_messages.main(native_args)


def main(argv=None):
    try:
        result = run(argv)
    except ValueError as error:
        message = str(error)
        prefix, separator, _ = message.partition(':')
        code = prefix if separator and prefix.isupper() and ' ' not in prefix else 'NATIVE_READ_UNAVAILABLE'
        result = {'ok': False, 'code': code, 'message': message}
    except OSError as error:
        result = {'ok': False, 'code': 'LOCAL_IO_ERROR', 'message': type(error).__name__}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
