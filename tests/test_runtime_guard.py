"""v2 runtime gate: synthetic Git, no models/remote/credentials."""
import json,os,sys,tempfile,unittest,subprocess
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'src')]
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']);self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)/'runtime';self.root.mkdir();self.registry=Path(self.tmp.name)/'registry.json'
  self.sha='a'*40;self.branch='integration/core-convergence-20261018';self.url='https://github.com/musekind0229/teleagent-collab.git'
  g=patch('framework.runtime_guard.github_head',return_value=self.sha);g.start();self.real_github_head=g.temp_original;self.addCleanup(g.stop)
  self.registry.write_text(json.dumps({'runtime_root':str(self.root),'branch':self.branch,'github_url':self.url}))
  for target in ('subprocess.Popen','socket.socket.connect'):
   p=patch(target,side_effect=AssertionError('SIDE EFFECT FORBIDDEN'));p.start();self.addCleanup(p.stop)
 def run_git(self,args,**kwargs):
  self.assertFalse(kwargs.get('shell'));self.assertIn('timeout',kwargs)
  if 'ls-remote'in args:return subprocess.CompletedProcess(args,0,self.sha+'\trefs/heads/'+self.branch+'\n','')
  if '--show-toplevel'in args:return subprocess.CompletedProcess(args,0,str(self.root)+'\n','')
  if '--show-current'in args:return subprocess.CompletedProcess(args,0,self.branch+'\n','')
  if 'status'in args:return subprocess.CompletedProcess(args,0,'','')
  return subprocess.CompletedProcess(args,0,self.sha+'\n','')
 def check(self):
  from framework.runtime_guard import verify_runtime
  return verify_runtime(self.root,registration_path=self.registry)
 def test_exact_registered_clean_published_branch_accepts(self):
  with patch('subprocess.run',side_effect=self.run_git):r=self.check()
  self.assertEqual(r['head'],self.sha);self.assertEqual(r['runtime_root'],str(self.root.resolve()));self.assertTrue(r['github_verified'])
 def test_missing_registry_wrong_root_branch_dirty_and_unknown_remote_refuse(self):
  from framework.runtime_guard import RuntimeRejected,verify_runtime
  with self.assertRaises(RuntimeRejected):verify_runtime(self.root,registration_path=Path(self.tmp.name)/'missing')
  other=Path(self.tmp.name)/'other';other.mkdir()
  with self.assertRaises(RuntimeRejected):verify_runtime(other,registration_path=self.registry)
  for mode in ('branch','dirty','unpublished','offline'):
   def fail(args,**kw):
    if mode=='branch'and '--show-current'in args:return subprocess.CompletedProcess(args,0,'other\n','')
    if mode=='dirty'and 'status'in args:return subprocess.CompletedProcess(args,0,' M src/x.py\n','')
    if 'ls-remote'in args:
     if mode=='offline':raise subprocess.TimeoutExpired(args,1)
     if mode=='unpublished':return subprocess.CompletedProcess(args,0,'','')
    return self.run_git(args,**kw)
   def remote():
    if mode=='offline':raise RuntimeRejected('GitHub network unknown')
    return 'b'*40 if mode=='unpublished' else self.sha
   with self.subTest(mode=mode),patch('subprocess.run',side_effect=fail),patch('framework.runtime_guard.github_head',side_effect=remote),self.assertRaises(RuntimeRejected):self.check()
 def test_environment_cannot_release_or_relocate_registration(self):
  from framework.runtime_guard import registration_path
  before=registration_path()
  with patch.dict(os.environ,{'COLLAB_RUNTIME_BYPASS':'1','COLLAB_RUNTIME_ROOT':str(self.root),'COLLAB_RUNTIME_REGISTRY':str(self.registry),'LOCALAPPDATA':str(self.root)}):self.assertEqual(registration_path(),before)
 def test_service_checks_before_any_state_write(self):
  with patch('subprocess.run',return_value=subprocess.CompletedProcess([],0,'synthetic\n','')):
   import framework.goal_service as m
  from framework.runtime_guard import RuntimeRejected
  dest=Path(self.tmp.name)/'never-created'
  with patch('framework.runtime_guard.verify_runtime',side_effect=RuntimeRejected('unpublished'))as gate,self.assertRaises(RuntimeRejected):m.GoalService(dest)
  self.assertEqual(gate.call_count,1);self.assertFalse(dest.exists())
 def test_inventory_distinguishes_worktree_bare_fixture_and_full_source(self):
  from framework.repository_inventory import inventory,MARKERS
  for name,kind in [('full','dir'),('fixture','dir'),('worktree','file')]:
   p=Path(self.tmp.name)/name;p.mkdir()
   if kind=='dir':(p/'.git').mkdir()
   else:(p/'.git').write_text('gitdir: not-read')
   if name=='full':
    for m in MARKERS:(p/m).parent.mkdir(parents=True,exist_ok=True);(p/m).write_text('synthetic')
  bare=Path(self.tmp.name)/'bare';bare.mkdir();(bare/'HEAD').write_text('not-read');(bare/'objects').mkdir();(bare/'refs').mkdir()
  protected=Path(self.tmp.name)/'states/fixture/.git';protected.mkdir(parents=True)
  r=inventory(Path(self.tmp.name));self.assertEqual(r['counts']['complete_collab_copy'],1);self.assertEqual(r['counts']['worktree_git_file'],1);self.assertEqual(r['counts']['other_git_directory'],1);self.assertEqual(r['counts']['bare_objects_archive'],1);self.assertTrue(r['scan_complete_within_scope'])
 def test_all_existing_cli_mains_have_gate_and_goal_serve_checks_before_mkdir(self):
  import ast
  for path in (ROOT/'bin').glob('*.py'):
   tree=ast.parse(path.read_text(encoding='utf-8'));m=next((n for n in tree.body if isinstance(n,ast.FunctionDef)and n.name=='main'),None)
   if path.name=='goal-entry.py':
    s=path.read_text(encoding='utf-8');self.assertLess(s.index('runtime_verification=verify_runtime(ROOT)'),s.index('state.mkdir'))
   else:
    self.assertIsNotNone(m);self.assertIsInstance(m.body[0],ast.ImportFrom);self.assertEqual(m.body[0].module,'framework.runtime_guard');self.assertEqual(m.body[1].value.func.id,'verify_runtime')

 def test_fixed_github_https_ref_parser_no_auth_redirect_or_unknown_fallback(self):
  from unittest.mock import MagicMock
  from framework.runtime_guard import RuntimeRejected
  response=MagicMock();response.status=200;response.read.return_value=json.dumps({'ref':'refs/heads/'+self.branch,'object':{'type':'commit','sha':self.sha}}).encode()
  conn=MagicMock();conn.getresponse.return_value=response
  with patch('http.client.HTTPSConnection',return_value=conn)as cls:
   self.assertEqual(self.real_github_head(),self.sha);cls.assert_called_once_with('api.github.com',timeout=15)
  self.assertNotIn('Authorization',conn.request.call_args.kwargs['headers']);self.assertEqual(conn.request.call_args.args[0],'GET');conn.close.assert_called()
  for status in (301,404,403,429,500):
   response.status=status
   with self.subTest(status=status),patch('http.client.HTTPSConnection',return_value=conn),self.assertRaises(RuntimeRejected):self.real_github_head()

if __name__=='__main__':unittest.main(verbosity=2)
