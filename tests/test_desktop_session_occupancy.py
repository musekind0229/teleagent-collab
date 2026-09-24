"""Desktop GUI occupancy: do not steal another controller's in-flight session."""
from __future__ import annotations

import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from execution_backend.windows_supervised_v1 import WindowsSupervisedExecutionBackend
from win_collab.core import Engine, Store
from win_collab.desktop_lock import (
    controller_id_for,
    read_holder,
    reset_desktop_locks_for_tests,
)


def charter():
    return {
        'goal': 'Write result',
        'must': ['Stay in workdir'],
        'must_not': ['Read secrets'],
        'artifacts': ['a.txt'],
        'acceptance': 'a.txt exists',
        'timeout_sec': 60,
    }


class FakeClient:
    def __init__(self, base='http://127.0.0.1:4397'):
        self.base = base
        self.instance_id = base
        self.calls = []
        self.status = {}
        self.messages = {}
        self.count = 0

    def call(self, method, path, body=None, workspace=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if method == 'POST' and path == '/session':
            self.count += 1
            sid = 'ses-' + str(self.count)
            self.status[sid] = {'type': 'busy'}
            self.messages[sid] = []
            return {
                'id': sid,
                'permission': (body or {}).get('permission'),
            }
        if path.endswith('/prompt_async'):
            sid = path.split('/')[2]
            self.messages.setdefault(sid, []).append({'info': {'role': 'user'}})
            self.status[sid] = {'type': 'busy'}
            return None
        if method == 'GET' and path == '/session/status':
            return copy.deepcopy(self.status)
        if method == 'GET' and path == '/permission':
            return []
        if method == 'GET' and path == '/question':
            return []
        if method == 'GET' and path.endswith('/message'):
            return copy.deepcopy(self.messages.get(path.split('/')[2], []))
        if path.endswith('/abort'):
            self.status[path.split('/')[2]] = {'type': 'idle'}
            return True
        raise AssertionError((method, path))


def posts(client):
    return [call for call in client.calls if call[0] == 'POST' and call[1] == '/session']


def aborts(client):
    return [call for call in client.calls if 'abort' in call[1]]


class DesktopOccupancyTests(unittest.TestCase):
    def setUp(self):
        reset_desktop_locks_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            try:
                store.db.close()
            except Exception:
                pass
        reset_desktop_locks_for_tests()
        self.tmp.cleanup()

    def store(self, name):
        made = Store(self.root / name)
        self.stores.append(made)
        return made

    def test_foreign_busy_second_tick_does_not_post_or_abort(self):
        client = FakeClient()
        first = Engine(self.store('a'), client)
        second = Engine(self.store('b'), client)
        job_a = first.submit(charter())
        first.tick()
        self.assertEqual(len(posts(client)), 1)
        self.assertEqual(self.stores[0].get(job_a['id'])['state'], 'running')
        job_b = second.submit(charter())
        before = len(client.calls)
        second.tick()
        saved = self.stores[1].get(job_b['id'])
        self.assertEqual(saved['state'], 'failed')
        self.assertIn('need_human: desktop_session_busy', saved['error'])
        self.assertEqual(len(posts(client)), 1)
        self.assertEqual(aborts(client), [])
        self.assertEqual(self.stores[0].get(job_a['id'])['state'], 'running')
        # The refused controller does not touch the GUI at all.
        self.assertEqual(client.calls[before:], [])

    def test_foreign_busy_status_blocks_without_a_second_post(self):
        client = FakeClient()
        client.status['foreign-sess'] = {'type': 'busy'}
        store = self.store('only')
        engine = Engine(store, client)
        job = engine.submit(charter())
        engine.tick()
        saved = store.get(job['id'])
        self.assertEqual(saved['state'], 'failed')
        self.assertIn('desktop_session_busy', saved['error'])
        self.assertEqual(posts(client), [])
        self.assertEqual(aborts(client), [])
        self.assertEqual(client.status['foreign-sess'], {'type': 'busy'})

    def test_lock_held_by_other_controller_fails_closed(self):
        base = 'http://127.0.0.1:4397'
        holder_client = FakeClient(base)
        other_client = FakeClient(base)
        holder_store = self.store('holder')
        other_store = self.store('other')
        holder = Engine(holder_store, holder_client)
        other = Engine(other_store, other_client)
        job_h = holder.submit(charter())
        holder.tick()
        saved_h = holder_store.get(job_h['id'])
        sid = saved_h['session_id']
        holder_client.status[sid] = {'type': 'idle'}
        meta = read_holder(base)
        self.assertIsNotNone(meta)
        self.assertEqual(meta['controller_id'], controller_id_for(holder_store.home))
        self.assertEqual(meta['pid'], os.getpid())
        self.assertEqual(os.path.normcase(meta['state_dir']), os.path.normcase(str(holder_store.home)))
        job_o = other.submit(charter())
        other.tick()
        saved_o = other_store.get(job_o['id'])
        self.assertEqual(saved_o['state'], 'failed')
        self.assertIn('need_human: desktop_session_busy', saved_o['error'])
        self.assertEqual(posts(other_client), [])
        self.assertEqual(aborts(other_client), [])
        self.assertEqual(other_client.calls, [])
        self.assertEqual(holder_client.status[sid], {'type': 'idle'})
        self.assertEqual(holder_store.get(job_h['id'])['state'], 'running')

    def test_own_idle_same_controller_reclaims(self):
        client = FakeClient()
        store = self.store('same')
        engine = Engine(store, client)
        first = engine.submit(charter())
        engine.tick()
        saved = store.get(first['id'])
        self.assertEqual(saved['state'], 'running')
        client.status[saved['session_id']] = {'type': 'idle'}
        second = engine.submit(charter())
        engine.tick()
        self.assertEqual(len(posts(client)), 2)
        self.assertEqual(store.get(second['id'])['state'], 'running')
        self.assertEqual(aborts(client), [])

    def test_foreign_idle_session_does_not_block(self):
        client = FakeClient()
        client.status['leftover'] = {'type': 'idle'}
        store = self.store('idle-foreign')
        engine = Engine(store, client)
        job = engine.submit(charter())
        engine.tick()
        self.assertEqual(store.get(job['id'])['state'], 'running')
        self.assertEqual(len(posts(client)), 1)
        self.assertEqual(client.status['leftover'], {'type': 'idle'})

    def test_start_run_second_controller_need_human_and_no_wrap(self):
        client = FakeClient()
        first_backend = WindowsSupervisedExecutionBackend(
            state_dir=self.root / 'goal', client=client, stdin_wrap=False,
        )
        second_backend = WindowsSupervisedExecutionBackend(
            state_dir=self.root / 'win-collab', client=client, stdin_wrap=False,
        )
        wrap = mock.MagicMock()
        with mock.patch.dict(sys.modules, {'teleagent_adapter.windows_stdin_wrap': wrap}):
            opened = first_backend.start_run(
                title='first',
                directory=str(self.root),
                instruction='write a.txt',
                artifacts=['a.txt'],
                charter=charter(),
            )
            self.assertTrue(opened['ok'], opened)
            self.assertFalse(opened.get('need_human'))
            refused = second_backend.start_run(
                title='second',
                directory=str(self.root),
                instruction='write a.txt',
                artifacts=['a.txt'],
                charter=charter(),
            )
        self.assertFalse(refused['ok'])
        self.assertTrue(refused.get('need_human'))
        self.assertIn('desktop_session_busy', refused['error'])
        self.assertIn('desktop_session_busy', refused.get('failure_reason') or '')
        self.assertEqual(len(posts(client)), 1)
        self.assertEqual(aborts(client), [])
        self.assertFalse(first_backend.stdin_wrap)
        self.assertFalse(second_backend.stdin_wrap)
        self.assertIsNone(first_backend._wrap_handle)
        self.assertIsNone(second_backend._wrap_handle)
        wrap.ensure_stdin_wrap.assert_not_called()
        wrap.stop_stdin_wrap.assert_not_called()

    def test_start_run_own_idle_ok(self):
        client = FakeClient()
        backend = WindowsSupervisedExecutionBackend(
            state_dir=self.root / 'goal', client=client, stdin_wrap=False,
        )
        opened = backend.start_run(
            title='first',
            directory=str(self.root),
            instruction='write a.txt',
            artifacts=['a.txt'],
            charter=charter(),
        )
        self.assertTrue(opened['ok'], opened)
        client.status[opened['native_handle']] = {'type': 'idle'}
        again = backend.start_run(
            title='second',
            directory=str(self.root),
            instruction='write a.txt',
            artifacts=['a.txt'],
            charter=charter(),
        )
        self.assertTrue(again['ok'], again)
        self.assertFalse(again.get('need_human'))
        self.assertEqual(len(posts(client)), 2)


if __name__ == '__main__':
    unittest.main()
