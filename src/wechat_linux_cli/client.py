"""Owner-only Unix socket client; credentials never appear in command arguments."""
import json
import os
from pathlib import Path
import socket
import stat
import struct

MAX_RESPONSE = 128 * 1024


def socket_path():
    return Path('/run')/('wechat-linux-cli-' + str(os.getuid()))/'control.sock'


def call(request, path=None, timeout=180):
    path = Path(path or socket_path())
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('UNSAFE_SOCKET: expected an owner-only Unix socket')
    encoded = json.dumps(request, ensure_ascii=True, allow_nan=False).encode() + b'\n'
    if len(encoded) > 16384:
        raise ValueError('REQUEST_TOO_LARGE')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        conn.connect(str(path))
        _pid, uid, _gid = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != os.getuid():
            raise ValueError('UNEXPECTED_SERVICE_OWNER')
        conn.sendall(encoded)
        conn.shutdown(socket.SHUT_WR)
        chunks = bytearray()
        while part := conn.recv(8192):
            chunks.extend(part)
            if len(chunks) > MAX_RESPONSE:
                raise ValueError('RESPONSE_TOO_LARGE')
        return json.loads(chunks)
