"""Persist exact adapter request/return before planner summary loses context.
Only authorized synthetic engineering context belongs in this host state.
"""
import json,os,uuid,hashlib,time
from pathlib import Path
class EvidenceAdapter:
 def __init__(self,adapter,root):self.adapter=adapter;self.root=Path(root);self.name=adapter.name
 def decide(self,request,**kwargs):
  folder=self.root/('exchange-'+uuid.uuid4().hex);folder.mkdir(parents=True,exist_ok=False)
  def put(name,value):
   raw=json.dumps(value,ensure_ascii=False,indent=2,default=str).encode('utf-8');p=folder/name;p.write_bytes(raw);return hashlib.sha256(raw).hexdigest()
  put('REQUEST.json',{'request':request,'schema':kwargs.get('schema'),'timeout_sec':kwargs.get('timeout_sec'),'cwd':kwargs.get('cwd')})
  try:
   raw,parsed=self.adapter.decide(request,**kwargs);put('RETURN.json',{'raw':raw,'parsed':parsed,'evidence_note':'Adapter raw_evidence contains original stream provenance; raw is decoded summary, not reconstructed stdout bytes'});return raw,parsed
  except Exception as exc:
   put('EXCEPTION.json',{'type':type(exc).__name__,'message':str(exc)});raise
