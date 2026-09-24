"""Wall and step budgets apply only to sessions this controller's store owns."""
from __future__ import annotations

import copy
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from desktop_lock_isolation import install_desktop_lock_isolation
from execution_backend.windows_supervised_v1 import (
    WindowsSupervisedExecutionBackend,
    _need_human_fields,
)
from win_collab.core import (
    DEFAULT_COLLAB_MAX_STEPS,
    DEFAULT_COLLAB_MAX_WALL_S,
    COLLAB_STEPS_CEILING,
    COLLAB_WALL_S_CEILING,
    Engine,
    Store,
    assistant_step_count,
    collab_max_steps,
    collab_max_wall_s,
    confirmed_owned_session_id,
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
            return {'id': sid, 'permission': (body or {}).get('permission')}
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


def aborts(client):
    return [call for call in client.calls if 'abort' in call[1]]


class CollabSessionBudgetTests(unittest.TestCase):
    def setUp(self):
        install_desktop_lock_isolation(self)
        self._env = mock.patch.dict(os.environ, {
            'COLLAB_WIN_MAX_WALL_S': str(DEFAULT_COLLAB_MAX_WALL_S),
            'COLLAB_WIN_MAX_STEPS': str(DEFAULT_COLLAB_MAX_STEPS),
        })
        self._env.start()
        self.addCleanup(self._env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))
        self.addCleanup(self.store.db.close)
        self.client = FakeClient()
        self.engine = Engine(self.store, self.client)

    def start(self):
        job = self.engine.submit(charter())
        self.engine.tick()
        return self.store.get(job['id'])

    def save(self, job):
        with self.store.transaction():
            self.store.save(job)

    def release_scan(self, job):
        current = self.store.get(job['id'])
        current['next_scan'] = 0
        self.save(current)
        return current

    def test_limit_parsing_and_ownership_rules(self):
        self.assertEqual(assistant_step_count([
            {'info': {'role': 'user'}},
            {'info': {'role': 'assistant'}},
            {'role': 'assistant'},
            {'info': {'role': 'assistant'}},
        ]), 2)
        self.assertEqual(assistant_step_count({'info': {'role': 'assistant'}}), 0)
        with mock.patch.dict(os.environ, {
            'COLLAB_WIN_MAX_WALL_S': 'nope',
            'COLLAB_WIN_MAX_STEPS': '-3',
        }):
            self.assertEqual(collab_max_wall_s(None), DEFAULT_COLLAB_MAX_WALL_S)
            self.assertEqual(collab_max_steps(None), DEFAULT_COLLAB_MAX_STEPS)
        with mock.patch.dict(os.environ, {
            'COLLAB_WIN_MAX_WALL_S': '10',
            'COLLAB_WIN_MAX_STEPS': '7',
        }):
            self.assertEqual(collab_max_wall_s({'charter': {'max_wall_s': 'bad'}}), 10)
            self.assertEqual(collab_max_steps({'charter': {'max_steps': 3}}), 3)
        with mock.patch.dict(os.environ, {
            'COLLAB_WIN_MAX_WALL_S': str(COLLAB_WALL_S_CEILING + 5),
            'COLLAB_WIN_MAX_STEPS': str(COLLAB_STEPS_CEILING + 5),
        }):
            self.assertEqual(collab_max_wall_s(None), COLLAB_WALL_S_CEILING)
            self.assertEqual(collab_max_steps(None), COLLAB_STEPS_CEILING)
        owned = {
            'id': 'job-1',
            'session_id': 'ses-1',
            'backend': 'http://127.0.0.1:4397',
            'backend_id': 'http://127.0.0.1:4397',
        }
        rows = [dict(owned)]
        self.assertEqual(
            confirmed_owned_session_id(owned, rows, backend=owned['backend'], backend_id=owned['backend_id']),
            'ses-1',
        )
        self.assertIsNone(confirmed_owned_session_id(
            {**owned, 'session_id': 'ses-1'},
            [{**owned, 'session_id': 'other'}],
            backend=owned['backend'],
            backend_id=owned['backend_id'],
        ))
        self.assertIsNone(confirmed_owned_session_id(
            owned,
            [owned, {**owned, 'id': 'job-2'}],
            backend=owned['backend'],
            backend_id=owned['backend_id'],
        ))
        self.assertIsNone(confirmed_owned_session_id(
            {**owned, 'session_id': ''},
            rows,
            backend=owned['backend'],
            backend_id=owned['backend_id'],
        ))

    def test_owned_wall_exceed_aborts_only_that_session(self):
        victim = self.start()
        other = self.start()
        self.client.status['foreign-ses'] = {'type': 'busy'}
        self.client.messages['foreign-ses'] = [{'info': {'role': 'assistant'}} for _ in range(50)]
        current = self.store.get(victim['id'])
        current['dispatched_at'] = time.time() - 10_000
        current['deadline'] = time.time() + 3600
        self.save(current)
        with mock.patch.dict(os.environ, {'COLLAB_WIN_MAX_WALL_S': '100'}):
            self.engine.tick()
        saved = self.store.get(victim['id'])
        self.assertEqual(saved['state'], 'failed', saved)
        self.assertIn('need_human: budget_exceeded wall', saved['error'])
        match = re.search(r'wall_s=(\d+) max=(\d+)', saved['error'])
        self.assertIsNotNone(match, saved['error'])
        self.assertGreater(int(match.group(1)), int(match.group(2)))
        self.assertEqual(match.group(2), '100')
        fields = _need_human_fields(saved['error'])
        self.assertTrue(fields['need_human'])
        self.assertIn('budget_exceeded', fields['failure_reason'])
        self.assertIn('wall', fields['failure_reason'])
        self.assertIn('wall_s=', fields['failure_reason'])
        abort_paths = [call[1] for call in aborts(self.client)]
        self.assertEqual(abort_paths, [f'/session/{victim["session_id"]}/abort'])
        self.assertEqual(self.store.get(other['id'])['state'], 'running')
        self.assertEqual(self.client.status[other['session_id']]['type'], 'busy')
        self.assertEqual(self.client.status['foreign-ses']['type'], 'busy')
        backend = WindowsSupervisedExecutionBackend(state_dir=self.store.home, client=self.client)
        result = backend.collect_result(victim['id'])
        self.assertTrue(result.get('need_human'), result)
        self.assertIn('budget_exceeded', result.get('failure_reason') or '')
        self.assertIn('wall_s=', result.get('failure_reason') or '')
        event = self.store.db.execute(
            'SELECT data FROM events WHERE job_id=? AND kind=? ORDER BY seq DESC LIMIT 1',
            (victim['id'], 'failed'),
        ).fetchone()
        self.assertIn('budget_exceeded', event['data'])

    def test_owned_steps_exceed_aborts_only_that_session(self):
        victim = self.start()
        other = self.start()
        sid = victim['session_id']
        self.client.status[sid] = {'type': 'idle'}
        self.client.messages[sid].extend(
            {'info': {'role': 'assistant', 'finish': 'stop'}} for _ in range(6)
        )
        self.client.status['foreign-ses'] = {'type': 'busy'}
        self.client.messages['foreign-ses'] = [{'info': {'role': 'assistant'}} for _ in range(30)]
        self.release_scan(victim)
        self.release_scan(other)
        with mock.patch.dict(os.environ, {'COLLAB_WIN_MAX_STEPS': '5'}):
            self.engine.tick()
        saved = self.store.get(victim['id'])
        self.assertEqual(saved['state'], 'failed', saved)
        self.assertIn('need_human: budget_exceeded steps', saved['error'])
        match = re.search(r'steps=(\d+) max=(\d+)', saved['error'])
        self.assertIsNotNone(match, saved['error'])
        self.assertEqual(match.group(1), '6')
        self.assertEqual(match.group(2), '5')
        fields = _need_human_fields(saved['error'])
        self.assertTrue(fields['need_human'])
        self.assertIn('budget_exceeded', fields['failure_reason'])
        self.assertIn('steps=6', fields['failure_reason'])
        abort_paths = [call[1] for call in aborts(self.client)]
        self.assertEqual(abort_paths, [f'/session/{sid}/abort'])
        self.assertEqual(self.store.get(other['id'])['state'], 'running')
        self.assertEqual(self.client.status['foreign-ses']['type'], 'busy')
        self.assertFalse(self.store.inbox())

    def test_foreign_session_is_not_budgeted_or_aborted(self):
        job = self.start()
        self.client.status['foreign-ses'] = {'type': 'busy'}
        self.client.messages['foreign-ses'] = [{'info': {'role': 'assistant'}} for _ in range(500)]
        self.release_scan(job)
        before = list(self.client.calls)
        self.engine.tick()
        saved = self.store.get(job['id'])
        self.assertEqual(saved['state'], 'running', saved)
        self.assertFalse(saved.get('error'))
        self.assertEqual(aborts(self.client), [])
        self.assertEqual(self.client.status['foreign-ses']['type'], 'busy')
        new_calls = self.client.calls[len(before):]
        touched = [call[1] for call in new_calls]
        self.assertFalse(any('foreign-ses' in path for path in touched), touched)
        self.assertFalse(any(path.endswith('/message') for path in touched), touched)

    def test_within_budget_leaves_the_job_untouched(self):
        job = self.start()
        sid = job['session_id']
        self.assertEqual(self.store.get(job['id'])['state'], 'running')
        self.assertEqual(aborts(self.client), [])
        self.client.status[sid] = {'type': 'idle'}
        self.client.messages[sid].append({'info': {'role': 'assistant', 'finish': 'stop'}})
        self.release_scan(job)
        self.engine.tick()
        saved = self.store.get(job['id'])
        self.assertEqual(saved['state'], 'awaiting_review', saved)
        self.assertFalse(saved.get('error'))
        self.assertEqual(aborts(self.client), [])
        self.assertEqual(len(self.store.inbox()), 1)

    def test_charter_timeout_still_wins_over_wall_budget(self):
        job = self.start()
        current = self.store.get(job['id'])
        current['dispatched_at'] = time.time() - 10_000
        current['deadline'] = time.time() - 1
        self.save(current)
        with mock.patch.dict(os.environ, {'COLLAB_WIN_MAX_WALL_S': '100'}):
            self.engine.tick()
        saved = self.store.get(job['id'])
        self.assertEqual(saved['state'], 'timed_out', saved)
        self.assertNotIn('budget_exceeded', saved.get('error') or '')

    def test_unclear_ownership_does_not_abort(self):
        job = self.start()
        sid = job['session_id']
        current = self.store.get(job['id'])
        current['dispatched_at'] = time.time() - 10_000
        current['deadline'] = time.time() + 3600
        self.save(current)
        with mock.patch.object(self.engine, '_owned_sid', return_value=None):
            with mock.patch.dict(os.environ, {'COLLAB_WIN_MAX_WALL_S': '100'}):
                self.engine.tick()
        saved = self.store.get(job['id'])
        self.assertEqual(saved['state'], 'failed', saved)
        self.assertTrue(str(saved['error']).startswith('need_human:'))
        self.assertIn('budget_exceeded', saved['error'])
        self.assertIn('ownership_unclear', saved['error'])
        self.assertEqual(aborts(self.client), [])
        self.assertEqual(self.client.status[sid]['type'], 'busy')
        self.assertIsNone(saved.get('session_id'))
