"""Bounded app-message XML and identities for native card construction."""
import hashlib
import json
import os
import stat
import xml.etree.ElementTree as ET

from .media import image_path

MAX_XML_BYTES = 65536
APP_TYPES = {5, 33, 36}


def parse_xml(data):
    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_XML_BYTES or b'\0' in data:
        raise ValueError('XML_INVALID: expected 1..65536 UTF-8 bytes without NUL')
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('XML_INVALID: DTD and entity declarations are unsupported')
    try:
        document = ET.fromstring(data.decode('utf-8'))
        app = document.find('appmsg') if document.tag == 'msg' else None
        kind = int(app.findtext('type', '0')) if app is not None else 0
    except (UnicodeError, ET.ParseError, ValueError):
        raise ValueError('XML_INVALID: expected a msg/appmsg document with an integer type') from None
    if kind not in APP_TYPES:
        raise ValueError('XML_TYPE_UNSUPPORTED: article (5) and mini-program (33/36) app messages supported')
    title = app.findtext('title') or ''
    if not title or len(title.encode()) > 8192:
        raise ValueError('XML_INVALID: a title of 1..8192 UTF-8 bytes is required')
    if kind in (33, 36):
        mini = app.find('weappinfo')
        if mini is None or not mini.findtext('appid') or not mini.findtext('username'):
            raise ValueError('XML_INVALID: mini-program appid and username are required')
    return {'app_type': kind, 'title': title, 'url': app.findtext('url') or ''}


def read_xml(value):
    path = image_path(value)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= MAX_XML_BYTES:
            raise ValueError('XML_INVALID: expected a regular file of 1..65536 bytes')
        data = stream.read(MAX_XML_BYTES + 1)
    metadata = parse_xml(data)
    return data, metadata, hashlib.sha256(data).hexdigest()


def fingerprint(recipient, sha256, source_identity=None):
    source = json.dumps(source_identity, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(b'xml\0' + recipient.encode() + b'\0' + bytes.fromhex(sha256)
                          + b'\0' + source.encode()).hexdigest()
