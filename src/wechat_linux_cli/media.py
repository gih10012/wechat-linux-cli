"""Bounded owner-readable image inputs and content-based request identity."""
import hashlib
import os
from pathlib import Path
import stat

MAX_IMAGE_BYTES = 10 * 1024 * 1024


def image_path(value):
    if (not isinstance(value, str) or not value or '\0' in value
            or len(value.encode()) > 1024 or not Path(value).is_absolute()):
        raise ValueError('IMAGE_PATH_INVALID: use an absolute path of at most 1024 UTF-8 bytes')
    return Path(value)


def read_image(value):
    path = image_path(value)
    # NONBLOCK lets us reject a FIFO without waiting for its writer. Inspect
    # the opened descriptor rather than a separate, racy path stat.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= MAX_IMAGE_BYTES:
            raise ValueError('IMAGE_FILE_INVALID: expected a regular file of 1 byte..10 MiB')
        data = stream.read(MAX_IMAGE_BYTES + 1)
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError('IMAGE_FILE_INVALID: image changed size beyond the input bound')
    suffix = ('.png' if data.startswith(b'\x89PNG\r\n\x1a\n') else
              '.jpg' if data.startswith(b'\xff\xd8\xff') else None)
    if suffix is None:
        raise ValueError('IMAGE_FORMAT_UNSUPPORTED: only PNG and JPEG bytes are accepted')
    return data, suffix, hashlib.sha256(data).hexdigest()


def image_fingerprint(recipient, sha256):
    # Moving or renaming an identical image does not create a different send.
    return hashlib.sha256(b'image\0' + recipient.encode() + b'\0'
                          + bytes.fromhex(sha256)).hexdigest()
