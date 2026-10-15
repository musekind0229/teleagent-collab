"""Caller-owned pre-review command evidence, never worker-supplied commands.

cwd containment and a process-tree deadline are not a filesystem sandbox.
Evidence is kept outside the worker workspace under the coordinator review root.
"""
from __future__ import annotations
import hashlib,json,math,os,signal,subprocess,tempfile,threading,time
from pathlib import Path,PureWindowsPath
from collections.abc import Mapping

def validate_command(value):
    if not isinstance(value,Mapping):raise ValueError('acceptance.command must be an object')
    if set(value)-{'argv','cwd','timeout_sec','max_output_bytes'}:raise ValueError('unknown acceptance.command field')
    argv=value.get('argv')
    if not isinstance(argv,list) or not 1<=len(argv)<=64 or any(not isinstance(a,str) or not a.strip() or '\x00' in a for a in argv):raise ValueError('acceptance.command.argv must be nonempty strings')
    if sum(len(a.encode('utf-8')) for a in argv)>32768:raise ValueError('acceptance.command.argv too large')
    cwd=value.get('cwd','.')
    if not isinstance(cwd,str) or not cwd or '\x00' in cwd or PureWindowsPath(cwd).drive or PureWindowsPath(cwd).root or Path(cwd).is_absolute() or '..' in cwd.replace('\\','/').split('/'):raise ValueError('acceptance.command.cwd must stay inside workspace')
    timeout=value.get('timeout_sec',10)
    if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or not .05<=timeout<=120:raise ValueError('acceptance.command.timeout_sec must be finite, 0.05..120')
    limit=value.get('max_output_bytes',65536)
    if isinstance(limit,bool) or not isinstance(limit,int) or not 16<=limit<=1048576:raise ValueError('acceptance.command.max_output_bytes must be 16..1048576')
    return {'argv':list(argv),'cwd':cwd,'timeout_sec':float(timeout),'max_output_bytes':limit}

def _reparse(path):
    return path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction())

def _cwd(workspace,relative):
    raw=Path(workspace)
    if _reparse(raw):raise ValueError('workspace is a reparse point')
    base=raw.resolve(strict=True);target=base
    for part in relative.replace('\\','/').split('/'):
        if part in ('','.'):continue
        target=target/part
        if _reparse(target):raise ValueError('cwd contains a reparse point')
    target=target.resolve(strict=True)
    if not target.is_relative_to(base) or not target.is_dir():raise ValueError('cwd escapes workspace or is not a directory')
    return target

def _spawn(argv,cwd,env):
    """Windows starts suspended, joins owned Job, then resumes its sole thread."""
    job=None;p=None
    try:
        if os.name=='nt':
            import win32api,win32job,win32process,psutil
            job=win32job.CreateJobObject(None,'')
            info=win32job.QueryInformationJobObject(job,win32job.JobObjectExtendedLimitInformation)
            info['BasicLimitInformation']['LimitFlags']|=win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(job,win32job.JobObjectExtendedLimitInformation,info)
            p=subprocess.Popen(argv,cwd=cwd,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=win32process.CREATE_SUSPENDED)
            win32job.AssignProcessToJobObject(job,int(p._handle))
            threads=psutil.Process(p.pid).threads()
            if len(threads)!=1:raise RuntimeError('unexpected suspended process thread count')
            handle=win32api.OpenThread(0x0002,False,threads[0].id)
            try:
                if win32process.ResumeThread(handle)!=1:raise RuntimeError('unexpected suspend count')
            finally:win32api.CloseHandle(handle)
        else:p=subprocess.Popen(argv,cwd=cwd,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        return p,job
    except BaseException:
        if p is not None:
            p.kill();p.wait(timeout=3)
        if job is not None:
            import win32api
            win32api.CloseHandle(job)
        raise

def _stop_tree(p,job):
    if os.name=='nt':
        import win32job
        win32job.TerminateJobObject(job,1)
    else:
        try:os.killpg(p.pid,signal.SIGKILL)
        except ProcessLookupError:pass

def execute_command(command,*,workspace,evidence_root,binding,remaining_wall_sec=None):
    root=Path(evidence_root)
    if _reparse(root):raise ValueError('evidence root is a reparse point')
    root.mkdir(parents=True,exist_ok=True)
    dest=Path(tempfile.mkdtemp(prefix='command-',dir=root))
    result={'source':'caller_contract','binding':dict(binding),'configured':True,'ok':False,'returncode':None,'timed_out':False,'capture_complete':False,'started_at':time.time(),'stdout':'','stderr':'','error':None}
    p=None;job=None;readers=[];buffers={'stdout':bytearray(),'stderr':bytearray()};totals={'stdout':0,'stderr':0};errors=[]
    try:
        spec=validate_command(command);cwd=_cwd(workspace,spec['cwd']);result.update(spec,cwd=str(cwd))
        result['requested_timeout_sec']=spec['timeout_sec']
        if remaining_wall_sec is not None:
            spec['timeout_sec']=max(0.0,min(spec['timeout_sec'],remaining_wall_sec))
            result['timeout_sec']=spec['timeout_sec']
            if spec['timeout_sec']<=0:
                result['timed_out']=True
                raise TimeoutError('task wall budget exhausted before caller command')
        # Do not hand caller-controlled test code the service token/provider keys.
        allowed={'PATH','SystemRoot','WINDIR','COMSPEC','PATHEXT','SYSTEMDRIVE','LANG','LC_ALL','GIT_CONFIG_GLOBAL','GIT_CONFIG_SYSTEM','GIT_CONFIG_NOSYSTEM','XDG_CONFIG_HOME'}
        env={k:v for k,v in os.environ.items() if k in allowed}
        tmp=dest/'tmp';tmp.mkdir()
        env.update(TMP=str(tmp),TEMP=str(tmp),TMPDIR=str(tmp),PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1')
        p,job=_spawn(spec['argv'],str(cwd),env);result['pid']=p.pid;result['process_tree_containment']='windows_job' if job is not None else 'posix_session'
        def collect(name,stream):
            try:
                while True:
                    chunk=stream.read(8192)
                    if not chunk:break
                    totals[name]+=len(chunk);remaining=spec['max_output_bytes']-len(buffers[name])
                    if remaining>0:buffers[name].extend(chunk[:remaining])
            except Exception as exc:errors.append(type(exc).__name__+': '+str(exc))
        for name,stream in (('stdout',p.stdout),('stderr',p.stderr)):
            t=threading.Thread(target=collect,args=(name,stream),daemon=True);t.start();readers.append(t)
        try:p.wait(timeout=spec['timeout_sec'])
        except subprocess.TimeoutExpired:result['timed_out']=True;_stop_tree(p,job);p.wait(timeout=3)
        result['returncode']=p.returncode
        # Acceptance commands may not leave background descendants holding pipes.
        _stop_tree(p,job)
        for t in readers:t.join(timeout=3)
        result['capture_complete']=not errors and all(not t.is_alive() for t in readers)
        if errors:result['error']='; '.join(errors)
        result['ok']=p.returncode==0 and not result['timed_out'] and result['capture_complete']
    except Exception as exc:result['error']=type(exc).__name__+': '+str(exc)
    finally:
        if p is not None:
            try:
                _stop_tree(p,job)
                if p.poll() is None:p.wait(timeout=3)
            except Exception as exc:result.update(ok=False,error='process cleanup failed: '+str(exc))
        if p is not None and all(not t.is_alive() for t in readers):
            for stream in (p.stdout,p.stderr):
                if stream is not None:stream.close()
        if job is not None:
            import win32api
            win32api.CloseHandle(job)
        for name in ('stdout','stderr'):
            data=bytes(buffers[name]);path=dest/(name+'.bin');path.write_bytes(data)
            result.update({name:data.decode('utf-8',errors='replace'),name+'_path':str(path),name+'_sha256':hashlib.sha256(data).hexdigest(),name+'_total_bytes':totals[name],name+'_truncated':totals[name]>len(data)})
        result['finished_at']=time.time();result['receipt_path']=str(dest/'receipt.json')
        (dest/'receipt.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result
