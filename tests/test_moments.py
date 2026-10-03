import sqlite3
import unittest
from unittest.mock import patch

from wechat_linux_cli import cli
from wechat_linux_cli._native import native_moments as moments


def xml(identifier, user, text='完整正文\n✅'):
    return (f'<SnsDataItem><TimelineObject><id>{identifier}</id><username>{user}</username>'
            f'<createTime>1700000000</createTime><contentDesc>{text}</contentDesc>'
            '<ContentObject><type>1</type><mediaList><media><id>9007199254740999</id>'
            '<url key="private-key" token="private-token">https://example.invalid/image</url>'
            '<size width="100" height="200"/></media></mediaList></ContentObject>'
            '<location city="测试"/></TimelineObject><LocalExtraInfo><nickname>显示名</nickname>'
            '<like_user_list><user_comment><username>friend</username><nickname>好友</nickname>'
            '</user_comment></like_user_list><comment_user_list>'
            '<user_comment><comment_64id>9007199254740998</comment_64id><content>评论一</content>'
            '</user_comment><user_comment><content>回复二</content><ref_username>friend</ref_username>'
            '</user_comment></comment_user_list><unknown flag="kept">保留字段</unknown>'
            '</LocalExtraInfo></SnsDataItem>').encode()


class MomentsTests(unittest.TestCase):
    def setUp(self):
        self.names = {'a': 'Alice', 'b': 'Bob', 'c': 'Same', 'd': 'Same'}
        self.ids = [(1 << 63) + 2, (1 << 63) + 1, (1 << 63) - 1]
        self.rows = [(identifier - (1 << 64) if identifier >= 1 << 63 else identifier,
                      'a' if i != 1 else 'b', xml(identifier, 'a' if i != 1 else 'b'))
                     for i, identifier in enumerate(self.ids)]
        self.context = [patch.object(moments, 'load_keys', return_value={}),
                        patch.object(moments, 'contact_names', return_value=(self.names, {})),
                        patch.object(moments, 'open_database', side_effect=self.database)]
        for context in self.context:
            context.start(); self.addCleanup(context.stop)

    def database(self, keys, relative):
        conn = sqlite3.connect(':memory:'); conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE SnsTimeLine(tid INTEGER,user_name TEXT,content TEXT)')
        conn.executemany('INSERT INTO SnsTimeLine VALUES(?,?,?)', self.rows)
        conn.execute('PRAGMA query_only=ON')
        return conn, {'database': relative}

    def test_unsigned_ids_paginate_without_loss_or_overlap(self):
        seen, cursor = [], None
        for _ in range(3):
            page = moments.read(limit=1, cursor=cursor)
            seen += [item['id'] for item in page['items']]; cursor = page['next_cursor']
        self.assertEqual(seen, [str(identifier) for identifier in self.ids])
        self.assertIsNone(cursor)
        self.assertTrue(page['cached_history_exhausted'])
        self.assertFalse(page['server_history_complete'])
        self.assertFalse(page['marks_read'])

    def test_all_pages_and_person_query_preserve_full_content(self):
        self.rows[0] = (*self.rows[0][:2], xml(self.ids[0], 'a', '长正文' * 10000))
        page = moments.read(user='Alice', limit=1, all_pages=True, include_xml=True)
        self.assertEqual([item['user_id'] for item in page['items']], ['a', 'a'])
        item = page['items'][0]
        self.assertEqual(item['text'], '长正文' * 10000)
        self.assertEqual(item['media'][0]['url']['attributes']['key'], 'private-key')
        self.assertEqual(item['comments'][0]['comment_64id'], '9007199254740998')
        self.assertEqual(len(item['comments']), 2)
        self.assertEqual(item['likes'][0]['nickname'], '好友')
        self.assertEqual(item['details']['LocalExtraInfo']['unknown']['attributes']['flag'], 'kept')
        self.assertIn('<SnsDataItem>', item['xml'])
        self.assertIsNone(page['next_cursor'])

    def test_scope_bound_cursor_and_ambiguous_person_never_select_first(self):
        cursor = moments.read(limit=1)['next_cursor']
        for user in ['a', 'Bob']:
            with self.assertRaisesRegex(ValueError, 'MOMENTS_CURSOR_INVALID'):
                moments.read(user=user, cursor=cursor)
        with self.assertRaisesRegex(ValueError, 'MOMENTS_USER_NOT_UNIQUE'):
            moments.read(user='Same')
        self.assertEqual(moments.read(user='c')['items'], [])
        for cursor in ['not-base64', 'e30', 'A' * 161]:
            with self.assertRaisesRegex(ValueError, 'MOMENTS_CURSOR_INVALID'):
                moments.read(cursor=cursor)

    def test_bad_row_remains_visible_and_does_not_destroy_pagination(self):
        self.rows[0] = (*self.rows[0][:2], b'<broken>')
        page = moments.read(limit=1)
        self.assertEqual(page['items'][0]['id'], str(self.ids[0]))
        self.assertFalse(page['items'][0]['content_complete'])
        self.assertIsNotNone(page['next_cursor'])
        self.rows[0] = (*self.rows[0][:2], xml(self.ids[0], 'wrong-person'))
        self.assertIn('MOMENTS_IDENTITY_MISMATCH', moments.read(limit=1)['items'][0]['parse_error'])

    def test_cli_reads_without_contacting_privileged_control_service(self):
        with patch.object(cli.client, 'call') as service:
            page = cli.run(['moments', '--user', 'Alice', '--all', '--include-xml'])
        self.assertEqual(len(page['items']), 2)
        service.assert_not_called()
        for limit in [0, 101]:
            with self.assertRaisesRegex(ValueError, 'MOMENTS_LIMIT_INVALID'):
                moments.read(limit=limit)


if __name__ == '__main__':
    unittest.main()
