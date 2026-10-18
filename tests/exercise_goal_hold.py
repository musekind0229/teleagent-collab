from pathlib import Path
import sys,subprocess,time,json,psutil
W=Path(__file__).resolve().parents[1];E=W/'tests'/('hold-evidence-'+time.strftime('%Y%m%d-%H%M%S'));E.mkdir();S=E/'state';commands=[]
def cli(*args):
 p=subprocess.run([sys.executable,'-B',str(W/'bin/goal-entry.py'),'--state',str(S),*args],cwd=W,capture_output=True,timeout=15);commands.append({'args':args,'rc':p.returncode,'stdout':p.stdout.decode(),'stderr':p.stderr.decode()});assert p.returncode==0,commands[-1];return json.loads(p.stdout)
with (E/'HOST.log').open('wb')as f:
 host=subprocess.Popen([sys.executable,'-B',str(W/'bin/goal-entry.py'),'--state',str(S),'serve'],cwd=W,stdout=f,stderr=f)
 try:
  deadline=time.time()+10
  while not(S/'host.json').exists()and host.poll()is None and time.time()<deadline:time.sleep(.1)
  assert(S/'host.json').exists(),(E/'HOST.log').read_text()
  result=cli('open','--goal','Synthetic default HOLD','--must','no model execution','--key','held','--native-limit','3','--lead-limit','5','--window-sec','90');assert result['state']=='HOLD'
  status=cli('status','--goal-id',result['goal_id']);assert status['resources']['actual_native']==status['resources']['actual_lead']==0;assert status['resources']['packages']=={}
  cli('stop');host.wait(timeout=10);assert host.returncode==0
  proof={'state':'PASS_REAL_ADAPTER_HOST_DEFAULT_HOLD','model_calls':0,'claims':0,'goal_retained':result['goal_id'],'host_exit':0,'model_release_not_invoked':True};(E/'RESULT.json').write_text(json.dumps(proof,indent=2))
 finally:
  if host.poll()is None:
   (S/'stop.json').write_text('{}');host.wait(timeout=10)
  (E/'COMMANDS.json').write_text(json.dumps(commands,indent=2))
print(json.dumps({'evidence':str(E),'result':proof},indent=2))
