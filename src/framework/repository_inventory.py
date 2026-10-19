"""Bounded metadata-only repository inventory; never follow reparse/state trees."""
from pathlib import Path
import argparse,json,os,stat,subprocess
PRUNE={'.git','states','state','raw','logs','workspaces','node_modules','.venv','venv','__pycache__','.collab-durable','.collab-run-data','cache','.agy-runs','.grok','.env','credentials','accounts','account-pool'}
MARKERS=('src/framework/app_service.py','src/execution_backend/antigravity_cli_v1.py','bin/hermes-collab-request.py')
def source_tree(root):
 """Same broad source-copy definition as the recorded historical baseline.
 Only local HEAD path metadata; never Git config, object contents or network.
 """
 if not(root/'src/framework').is_dir():return False
 env=dict(os.environ);env.update(GIT_OPTIONAL_LOCKS='0',GIT_TERMINAL_PROMPT='0')
 try:
  p=subprocess.run(['git','-C',str(root),'-c','core.fsmonitor=false','ls-tree','-r','--name-only','HEAD','--','src/framework/'],capture_output=True,timeout=15,env=env)
 except(OSError,subprocess.TimeoutExpired):return False
 return p.returncode==0 and bool(p.stdout.strip())

def inventory(root, *, max_directories=100000):
 root=Path(root).absolute();rows=[];excluded=[];errors=[];seen=0;stack=[root]
 def reparse(p):
  s=p.lstat();return stat.S_ISLNK(s.st_mode)or bool(getattr(s,'st_file_attributes',0)&0x400)
 if reparse(root):raise ValueError('inventory root reparse refused')
 while stack:
  p=stack.pop();seen+=1
  if seen>max_directories:errors.append({'path':str(p),'reason':'directory bound exceeded'});break
  try:
   if reparse(p):excluded.append({'path':str(p),'reason':'reparse'});continue
   name=p.name.lower()
   if p!=root and (name in PRUNE or 'backup'in name or name.startswith('baseline')):
    excluded.append({'path':str(p),'reason':'protected/state/cache/backup or baseline scope exclusion'});continue
   marker=p/'.git'
   if marker.exists():
    if reparse(marker):excluded.append({'path':str(p),'reason':'git marker reparse'});continue
    if marker.is_file():kind='worktree_git_file'
    elif marker.is_dir():kind='complete_collab_copy'if source_tree(p)else'other_git_directory'
    else:kind='unknown_git_marker'
    rows.append({'path':str(p),'kind':kind,'current_layout_marker_match':all((p/m).is_file()for m in MARKERS),'source_copy_definition':'readable HEAD framework tree plus checked-out framework directory; not full fsck'})
   elif(p/'HEAD').is_file()and(p/'objects').is_dir()and(p/'refs').is_dir():
    rows.append({'path':str(p),'kind':'bare_objects_archive'});continue
   with os.scandir(p)as entries:
    for entry in entries:
     if entry.name=='.git':continue
     if entry.is_symlink():excluded.append({'path':entry.path,'reason':'symlink'});continue
     if entry.is_dir(follow_symlinks=False):stack.append(Path(entry.path))
  except OSError as exc:errors.append({'path':str(p),'reason':type(exc).__name__})
 counts={k:sum(r['kind']==k for r in rows)for k in ('complete_collab_copy','worktree_git_file','other_git_directory','bare_objects_archive','unknown_git_marker')}
 counts['current_layout_marker_matches']=sum(bool(r.get('current_layout_marker_match'))for r in rows)
 return {'root':str(root),'scope':'metadata only; HEAD path metadata for source classification; project-records bound; excludes state/raw/workspaces/cache/backup/baseline/private trees and reparse paths; NOT interchangeable with all .git candidate counts','markers':list(MARKERS),'counts':counts,'scan_complete_within_scope':not errors,'directories_visited':seen,'repositories':sorted(rows,key=lambda r:r['path']),'excluded':excluded,'errors':errors}
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',required=True);p.add_argument('--output',required=True);a=p.parse_args()
 # A diagnostic output only. No repository mutation, clone, git credential or
 # live model process. Caller must explicitly supply a bounded engineering root.
 result=inventory(a.root);Path(a.output).write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps({'counts':result['counts'],'complete_within_scope':result['scan_complete_within_scope'],'output':a.output}));return 0 if result['scan_complete_within_scope']else 2
if __name__=='__main__':raise SystemExit(main())
