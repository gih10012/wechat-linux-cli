"""Read complete locally loaded Moments with stable, scope-bound pagination."""
import base64
import binascii
import hashlib
import json
import sqlite3
import xml.etree.ElementTree as ET

from .native_db import load_keys, open_database
from .native_messages import contact_names, iso

MASK = (1 << 64) - 1


def xml_value(element):
    """Preserve repeated nodes, unknown fields and media reference attributes."""
    if element is None:
        return None
    children = list(element)
    if not children and not element.attrib:
        return element.text or ''
    result = {'attributes': dict(element.attrib)} if element.attrib else {}
    if element.text and element.text.strip():
        result['text'] = element.text
    for child in children:
        value = xml_value(child)
        if child.tag not in result:
            result[child.tag] = value
        elif not isinstance(result[child.tag], list):
            result[child.tag] = [result[child.tag], value]
        else:
            result[child.tag].append(value)
    return result


def number(parent, name, default=0):
    value = parent.findtext(name) if parent is not None else None
    return int(value) if value else default


def parse_moment(row, names, include_xml=False):
    data = row['content']
    data = data.encode() if isinstance(data, str) else bytes(data or b'')
    if len(data) > 4 * 1024 * 1024 or b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('MOMENTS_XML_INVALID: unsupported or oversized XML')
    root = ET.fromstring(data)
    timeline = root if root.tag == 'TimelineObject' else root.find('TimelineObject')
    if timeline is None:
        raise ValueError('MOMENTS_XML_INVALID: missing TimelineObject')
    identifier = str(row['tid'] & MASK)
    if timeline.findtext('id') != identifier or timeline.findtext('username') != row['user_name']:
        raise ValueError('MOMENTS_IDENTITY_MISMATCH: row and XML differ')
    extra = root.find('LocalExtraInfo')
    content = timeline.find('ContentObject')
    timestamp = number(timeline, 'createTime')
    actor = row['user_name']
    result = {'id': identifier, 'user_id': actor,
              'author': names.get(actor) or (extra.findtext('nickname') if extra is not None else None) or actor,
              'timestamp': timestamp, 'time': iso(timestamp),
              'text': timeline.findtext('contentDesc') or '',
              'content_type': number(content, 'type'),
              'media': [xml_value(e) for e in timeline.findall('ContentObject/mediaList/media')],
              'location': xml_value(timeline.find('location')),
              'likes': [xml_value(e) for e in root.findall('LocalExtraInfo/like_user_list/user_comment')],
              'comments': [xml_value(e) for e in root.findall('LocalExtraInfo/comment_user_list/user_comment')],
              'details': xml_value(root), 'content_complete': True,
              'media_bytes_included': False, 'interaction_coverage': 'loaded client-visible likes and comments'}
    if include_xml:
        result['xml'] = data.decode('utf-8')
    return result


def scope_for(account, user):
    return hashlib.sha256(json.dumps([account, user], ensure_ascii=False).encode()).hexdigest()[:24]


def encode_cursor(identifier, scope):
    return base64.urlsafe_b64encode(json.dumps([1, scope, identifier], separators=(',', ':')).encode()).decode().rstrip('=')


def decode_cursor(cursor, scope):
    if not isinstance(cursor, str) or len(cursor) > 160:
        raise ValueError('MOMENTS_CURSOR_INVALID: invalid cursor')
    try:
        value = json.loads(base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True))
        version, stored_scope, identifier = value
        if version != 1 or stored_scope != scope or type(identifier) is not int or not 0 <= identifier <= MASK:
            raise ValueError()
    except (ValueError, TypeError, binascii.Error):
        raise ValueError('MOMENTS_CURSOR_INVALID: use a cursor from this account and user query') from None
    return identifier


def resolve_user(query, names, usernames):
    if query is None:
        return None
    if not query or len(query.encode()) > 1024 or '\x00' in query:
        raise ValueError('MOMENTS_USER_INVALID: choose an exact user ID or unique full name')
    if query in names or query in usernames:
        return query
    candidates = [user for user, name in names.items() if name == query]
    if len(candidates) != 1:
        raise ValueError('MOMENTS_USER_NOT_UNIQUE: choose an exact user ID or unique full name')
    return candidates[0]


def read(account='me', user=None, limit=20, cursor=None, all_pages=False, include_xml=False):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('MOMENTS_LIMIT_INVALID: page size must be 1..100')
    keys = load_keys(account)
    names, contact_snapshot = contact_names(keys)
    conn, snapshot = open_database(keys, 'sns/sns.db')
    try:
        usernames = {row[0] for row in conn.execute('SELECT DISTINCT user_name FROM SnsTimeLine')}
        user_id = resolve_user(user, names, usernames)
        scope = scope_for(account, user_id)
        where, parameters = [], []
        if user_id is not None:
            where.append('user_name=?'); parameters.append(user_id)
        total = conn.execute('SELECT count(*) FROM SnsTimeLine' + (' WHERE '+ ' AND '.join(where) if where else ''), parameters).fetchone()[0]
        if cursor is not None:
            identifier = decode_cursor(cursor, scope)
            sign = int(identifier >= 1 << 63)
            signed = identifier - (1 << 64) if sign else identifier
            where.append('((tid<0)<? OR ((tid<0)=? AND tid<?))')
            parameters += [sign, sign, signed]
        sql = ('SELECT tid,user_name,CAST(content AS BLOB) AS content FROM SnsTimeLine' +
               (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY (tid<0) DESC,tid DESC')
        if not all_pages:
            sql += ' LIMIT ?'; parameters.append(limit + 1)
        rows = list(conn.execute(sql, parameters))
        has_more = not all_pages and len(rows) > limit
        selected = rows[:limit] if has_more else rows
        items = []
        for row in selected:
            try:
                items.append(parse_moment(row, names, include_xml))
            except (ValueError, ET.ParseError, UnicodeError) as error:
                # A bad entry remains visible; it never silently disappears.
                items.append({'id': str(row['tid'] & MASK), 'user_id': row['user_name'],
                              'content_complete': False, 'parse_error': str(error)})
        return {'ok': True, 'items': items, 'user_id': user_id, 'cached_count': total,
                'next_cursor': encode_cursor(selected[-1]['tid'] & MASK, scope) if has_more else None,
                'cached_history_exhausted': not has_more,
                'server_history_complete': False, 'server_sync_verified': False,
                'coverage': 'complete fields of locally loaded Moments; older server pages require client loading',
                'source': 'local_client_database', 'order': 'newest_id_first',
                'read_only': True, 'marks_read': False,
                'snapshots': [snapshot, contact_snapshot]}
    except sqlite3.OperationalError:
        raise ValueError('MOMENTS_SCHEMA_UNSUPPORTED: no supported native Moments tables') from None
    finally:
        conn.close()
