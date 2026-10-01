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
    moments = operations.add_parser('moments', help='Read full locally loaded Moments, optionally for one person')
    moments.add_argument('--account', default='me')
    moments.add_argument('--user', help='Exact native user ID or unique full contact name')
    moments.add_argument('--limit', type=int, default=20)
    moments.add_argument('--cursor', help='next_cursor from the previous page of the same query')
    moments.add_argument('--all', dest='all_pages', action='store_true', help='Read all currently loaded pages')
    moments.add_argument('--include-xml', action='store_true')
    operations.add_parser('service-status', help='Check the installed local control service')
    operations.add_parser('inspect-pending', help='Inspect an unfinished operation without sending again')
    capture = operations.add_parser('capture-keys', help='Read keys once from the owner\'s running client through the service')
    capture.add_argument('--account', default='me')
    capture.add_argument('--seconds', type=int, default=15)
    send = operations.add_parser('send-text', help='Send through the client message-creation pipeline')
    send.add_argument('--recipient', required=True, help='Exact native chat ID; resolve it with conversations')
    send.add_argument('--text', required=True)
    send.add_argument('--request-id', required=True, help='Unique ID for this operation; reuse it for the same operation only')
    image = operations.add_parser('send-image', help='Send a PNG/JPEG through the client image pipeline')
    image.add_argument('--recipient', required=True, help='Exact native chat ID')
    image.add_argument('--file', required=True, help='Owner-readable PNG/JPEG, at most 10 MiB')
    image.add_argument('--request-id', required=True)
    sticker = operations.add_parser('send-sticker', help='Send a GIF/PNG/JPEG as a native type 47 sticker')
    sticker.add_argument('--recipient', required=True, help='Exact native chat ID')
    sticker.add_argument('--file', required=True, help='Owner-readable GIF/PNG/JPEG, at most 10 MiB')
    sticker.add_argument('--request-id', required=True)
    file = operations.add_parser('send-file', help='Send a regular file through the client attachment pipeline')
    file.add_argument('--recipient', required=True, help='Exact native chat ID')
    file.add_argument('--file', required=True, help='Owner-readable regular file, at most 10 MiB')
    file.add_argument('--request-id', required=True)
    xml = operations.add_parser('send-xml', help='Construct an article or mini-program card from app-message XML')
    xml.add_argument('--recipient', required=True, help='Exact native chat ID')
    xml.add_argument('--file', required=True, help='UTF-8 msg/appmsg XML file, at most 64 KiB')
    xml.add_argument('--request-id', required=True)
    forward = operations.add_parser('forward', help='Forward a precise local article or mini-program card')
    forward.add_argument('--account', default='me')
    forward.add_argument('--chat', required=True, help='Exact native source chat ID')
    forward.add_argument('--local-id', type=int, required=True)
    forward.add_argument('--database', help='Message shard from messages output, if needed to resolve the ID')
    forward.add_argument('--recipient', required=True, help='Exact native destination chat ID')
    forward.add_argument('--request-id', required=True)
    raw = operations.add_parser('message-xml', help='Read the XML of one precise local article or mini-program card')
    raw.add_argument('--account', default='me')
    raw.add_argument('--chat', required=True)
    raw.add_argument('--local-id', type=int, required=True)
    raw.add_argument('--database')
    status_send = operations.add_parser('send-status', help='Read the recorded outcome of a prior send')
    status_send.add_argument('--request-id', required=True)
    return command


def run(argv=None):
    args = parser().parse_args(argv)
    if args.operation == 'moments':
        from ._native import native_moments
        return native_moments.read(args.account, args.user, args.limit, args.cursor, args.all_pages, args.include_xml)
    if args.operation == 'service-status':
        return client.call({'operation': 'health'})
    if args.operation == 'inspect-pending':
        return client.call({'operation': 'inspect_pending'})
    if args.operation == 'capture-keys':
        return client.call({'operation': 'capture_keys', 'account': args.account, 'seconds': args.seconds})
    if args.operation == 'send-text':
        return client.call({'operation': 'send_text', 'recipient': args.recipient,
                            'text': args.text, 'request_id': args.request_id})
    if args.operation in ('send-image', 'send-file', 'send-xml', 'send-sticker'):
        from pathlib import Path
        return client.call({'operation': args.operation.replace('-', '_'), 'recipient': args.recipient,
                            'file': str(Path(args.file).expanduser().absolute()),
                            'request_id': args.request_id})
    if args.operation == 'message-xml':
        return native_messages.forward_source(args.account, args.chat, args.local_id, args.database)
    if args.operation == 'forward':
        return client.call({'operation': 'forward', 'account': args.account, 'chat': args.chat,
                            'local_id': args.local_id, 'database': args.database,
                            'recipient': args.recipient, 'request_id': args.request_id})
    if args.operation == 'send-status':
        from pathlib import Path
        from . import service
        from .backend import inspect_trial, completed
        from ._native.native_send_candidate import make_payload
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
            result = inspect_trial(args.request_id)
            return {**result, 'ok': completed(result), 'read_only': True}
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
