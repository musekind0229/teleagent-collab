"""Self-contained lossless review content interning; no tool/file references.

The document is traversable JSON. Ref values reference bytes IN this packet,
not files a tool-free lead would be unable to read. Gates run before packing.
"""
import base64,copy,hashlib,json

def pack_material(value):
 table={}
 def visit(v,key=''):
  if isinstance(v,dict):return {k:visit(x,k)for k,x in v.items()}
  if isinstance(v,list):return [visit(x,key)for x in v]
  if isinstance(v,str)and len(v)>=128 and key in ('code','diff','before_hex','after_hex','worker_self_report'):
   encoding='hex'if key.endswith('_hex')else'utf8'
   try:raw=bytes.fromhex(v)if encoding=='hex'else v.encode('utf-8')
   except ValueError:return v
   digest=hashlib.sha256(raw).hexdigest()
   if digest not in table:
    try:table[digest]={'utf8':raw.decode('utf-8')}
    except UnicodeDecodeError:table[digest]={'base64':base64.b64encode(raw).decode('ascii')}
   return {'$review_content':digest,'encoding':encoding}
  return copy.deepcopy(v)
 doc=visit(value)
 packet={'encoding':'collab-review-content-v1','content_table':table,'document':doc,'reading_rule':'All content is inline in content_table. Replace each $review_content object with that content (utf8 as source text, hex as exact bytes). Each digest is SHA256. Source, complete before/after delta, scope, rules and failure remain intact. This is not permission to fetch files or omit review.'}
 if unpack_material(packet)!=value:raise ValueError('review content encoding is not lossless')
 # Do not grow a small packet just to use the codec.
 return packet if len(json.dumps(packet,ensure_ascii=False))<len(json.dumps(value,ensure_ascii=False))else copy.deepcopy(value)

def unpack_material(packet):
 if packet.get('encoding')!='collab-review-content-v1':return copy.deepcopy(packet)
 table=packet['content_table']
 def visit(v):
  if isinstance(v,dict):
   if set(v)=={'$review_content','encoding'}:
    digest=v['$review_content'];row=table[digest];raw=row['utf8'].encode('utf-8')if 'utf8'in row else base64.b64decode(row['base64'],validate=True)
    if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('review content hash mismatch')
    if v['encoding']=='hex':return raw.hex()
    if v['encoding']=='utf8':return raw.decode('utf-8')
    raise ValueError('unknown review content encoding')
   return {k:visit(x)for k,x in v.items()}
  if isinstance(v,list):return [visit(x)for x in v]
  return v
 return visit(packet['document'])
