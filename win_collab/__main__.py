from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .client import Client
from .core import DEFAULT_HOME, Engine, Store


def main():
    parser = argparse.ArgumentParser(description='TeleAgent worker with a persistent, pluggable lead inbox')
    parser.add_argument('--home', type=Path, default=DEFAULT_HOME)
    subs = parser.add_subparsers(dest='command', required=True)
    subs.add_parser('doctor')
    s = subs.add_parser('submit'); s.add_argument('charter', type=Path)
    subs.add_parser('status')
    subs.add_parser('inbox')
    s = subs.add_parser('tick'); s.add_argument('--seconds', type=int, default=0)
    s.add_argument('--parallel', type=int, default=3)
    s = subs.add_parser('decide'); s.add_argument('decision', type=Path)
    s = subs.add_parser('cancel'); s.add_argument('job_id')
    s = subs.add_parser('report'); s.add_argument('job_id')
    s = subs.add_parser('lead', help='Run a separately selected lead command for one request')
    s.add_argument('request_id'); s.add_argument('--backend',choices=['codex','json-command'],required=True)
    s.add_argument('--exe',required=True,help='Absolute path to the selected lead executable')
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        if args.command == 'doctor':
            client = Client()
            health = client.call('GET', '/global/health')
            config = client.call('GET', '/config')
            output = {'endpoint': client.base, 'health': health,
                      'agent_names': list((config.get('agent') or {}).keys()),
                      'global_permission': config.get('permission'),
                      'auth': 'connected (values omitted)'}
        else:
            store = Store(args.home)
            if args.command == 'inbox':
                output = store.inbox()
            elif args.command == 'status':
                output = Engine(store, None).summary()
            elif args.command == 'submit':
                output = Engine(store, None).submit(json.loads(args.charter.read_text(encoding='utf-8-sig')))
            elif args.command == 'report':
                output = {'job':store.get(args.job_id), 'events':[
                    dict(x) for x in store.db.execute('SELECT time,kind,data FROM events WHERE job_id=? ORDER BY seq', (args.job_id,))]}
            else:
                engine = Engine(store, Client(), max_parallel=getattr(args, 'parallel', 3))
                if args.command == 'decide':
                    output = engine.decide(json.loads(args.decision.read_text(encoding='utf-8-sig')))
                elif args.command == 'lead':
                    from .lead import ask
                    pending=next((p for p in store.inbox() if p['request_id']==args.request_id),None)
                    if pending is None:
                        raise ValueError('Unknown pending request')
                    exe=Path(args.exe)
                    if not exe.is_absolute() or not exe.is_file():
                        raise ValueError('Lead executable must be an existing absolute file path')
                    # Count invocations across failed attempts, separately from request count.
                    with store.transaction():
                        job=store.get(pending['job_id'])
                        count=job.get('lead_invocations',0)
                        if count>=job['charter'].get('max_lead_requests',12):
                            raise ValueError('Lead invocation budget exhausted')
                        job['lead_invocations']=count+1
                        store.save(job)
                        store.event(job,'lead_invoked',{'backend':args.backend,'request_id':args.request_id})
                    reply=ask(pending,backend=args.backend,executable=str(exe),control_dir=store.home/'lead')
                    output=engine.decide(reply)
                elif args.command == 'cancel':
                    output = engine.cancel(args.job_id)
                else:
                    if not 0 <= args.seconds <= 60:
                        raise ValueError('Use --seconds 0..60; repeat tick to continue existing sessions')
                    end = time.monotonic() + args.seconds
                    while True:
                        output = engine.tick()
                        if store.inbox() or time.monotonic() >= end or all(j['state'] in ('passed','failed','cancelled','timed_out') for j in output):
                            break
                        time.sleep(1)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, RuntimeError, OSError) as error:
        print(json.dumps({'error':str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
