"""Explicit restart ONLY of a settled host without active runs; same store."""
import json,time,hashlib
from pathlib import Path
import psutil

def validate_settled(state):
 state=Path(state);host=json.loads((state/'host.json').read_bytes());settled=json.loads((state/'settled.json').read_bytes())
 if settled.get('thread_alive')is not False:raise ValueError('host not settled')
 def gone(pid,birth):
  if type(pid)is not int or type(birth)not in(int,float):raise ValueError('unknown process identity')
  try:
   if abs(psutil.Process(pid).create_time()-birth)<.01:raise ValueError('original process still alive')
  except psutil.NoSuchProcess:pass
 gone(host.get('pid'),host.get('birth'));store=state/'.collab-durable/store.json';raw=store.read_bytes();data=json.loads(raw)
 for snapshot in data.get('goals',{}).values():
  if any(t.get('status')in ('running','awaiting_decision','review','cancel_requested')for t in snapshot.get('tasks',[])):raise ValueError('active or ambiguous task prevents host restart')
  for package in snapshot.get('resource_packages',{}).values():
   for c in package['claims']:
    if c['state']=='reserved':raise ValueError('unsettled reservation requires owner investigation')
    if c['state']=='spawned':gone(c['pid'],c['birth'])
 return host,raw

def archive_settled_host(state,*,expected_epoch,write_json):
 state=Path(state);host,raw=validate_settled(state)
 if host.get('birth')!=expected_epoch:raise ValueError('explicit restart host binding mismatch')
 folder=state/'host-history'/('host-'+str(host['pid'])+'-'+str(host['birth']))
 if folder.exists():raise ValueError('host history already archived; inspect before retry')
 folder.mkdir(parents=True)
 for name in ('host.json','settled.json','stop.json','host-release.json'):
  p=state/name
  if p.exists():p.replace(folder/name)
 write_json(folder/'RESTART.json',{'same_store':str(state/'.collab-durable/store.json'),'store_sha256':hashlib.sha256(raw).hexdigest(),'old_host':host,'new_host_defaults_to_HOLD':True,'resource_epoch_not_reset':True})
 if(state/'.collab-durable/store.json').read_bytes()!=raw:raise ValueError('store changed during explicit restart')
