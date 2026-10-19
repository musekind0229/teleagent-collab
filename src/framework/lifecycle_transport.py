"""Explicit operator-approved local Hermes transports; route unknown fails closed.
No route/credential discovery. Merely importing/constructing never sends.
"""
from pathlib import Path
import json,subprocess,tempfile
class HermesLifecycleSender:
 def __init__(self,route_file):self.route_file=Path(route_file)
 def __call__(self,channel,payload):
  if not self.route_file.is_file():return {'status':'route_unbound'}
  rule=json.loads(self.route_file.read_bytes())
  if rule.get('approved')is not True:return {'status':'route_unapproved'}
  binary=Path(rule.get('hermes_executable',''))
  if not binary.is_absolute()or not binary.is_file():return {'status':'route_invalid_binary'}
  route=rule.get(channel)or{}
  if channel=='qq':
   target=route.get('target');profile=route.get('profile')
   if profile!='default':return {'status':'route_profile_not_approved'}
   if not isinstance(target,str)or not target.startswith('qqbot:')or target=='qqbot:':return {'status':'route_unbound'}
   argv=[str(binary),'-p',profile,'send','--to',target,'--json','--file','-'];text=payload
  else:
   profile=route.get('profile')
   if profile not in('project-manager','default'):return {'status':'route_unbound'}
   text=json.dumps(payload,ensure_ascii=False)
   # Existing canonical Bot Chat CLI gate handles busy ownership; no locks or
   # leases are deleted and no independently named conversation is introduced.
   argv=[str(binary),'-p',profile,'chat','--in','~','-c','Bot Chat','--create-if-missing','-Q','--query-file','-']
   # Installed chat query-file stdin support is not assumed. A unique local
   # engineering-only pointer file is passed, removed in finally.
  if channel!='qq':
   with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',suffix='.txt',dir=self.route_file.parent,delete=False)as f:f.write(text);path=Path(f.name)
   argv[-1]=str(path)
  else:path=None
  try:
   result=subprocess.run(argv,input=text if channel=='qq'else None,capture_output=True,text=True,timeout=60)
  except subprocess.TimeoutExpired:return {'status':'unknown_delivery'}
  except OSError:return {'status':'transport_unavailable'}
  finally:
   if path is not None:path.unlink(missing_ok=True)
  try:obj=json.loads(result.stdout)
  except ValueError:obj={}
  if channel=='qq':
   if result.returncode==0 and obj.get('success')is True and not obj.get('skipped'):
    return {'status':'sent','delivery_id':obj.get('message_id')or obj.get('id'),'provider_success':True}
   return {'status':'unknown_delivery'if result.returncode==0 else'provider_rejected'}
  if result.returncode!=0:
   # Do not retain Bot reply/raw/private content in engineering packets.
   return {'status':'target_busy'if 'target_busy'in result.stdout+result.stderr else'bot_rejected'}
  return {'status':'settled','delivery_id':obj.get('delivery_id'),'explicit_receipt_required':True}
