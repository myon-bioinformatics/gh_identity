#!/usr/bin/env python3
"""gh_identity: stdlib-only GitHub operations with gh-first/urllib fallback."""
from __future__ import annotations
import argparse,json,os,re,shutil,subprocess,sys,urllib.error,urllib.parse,urllib.request
from datetime import datetime,timezone
API="https://api.github.com"; REPO=re.compile(r"^[\w.-]+/[\w.-]+$")
class Error(RuntimeError):
 def __init__(self,code,uncertain=False): super().__init__(code); self.code=code; self.uncertain=uncertain
import contextlib
import contextvars
import functools
import inspect
import math
import threading
import time


class _Budget:
 def __init__(self,max_pages=100,max_items=10000,max_bytes=10000000,timeout=30):
  for value in (max_pages,max_items,max_bytes):
   if type(value) is not int or value<1:raise ValueError("limits must be positive integers")
  if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or timeout<=0:
   raise ValueError("timeout must be positive and finite")
  self.max_pages=max_pages;self.max_items=max_items;self.max_bytes=max_bytes
  self.failure=None;self.pages=0;self.items=0;self.bytes=0;self.deadline=time.monotonic()+timeout
 def fail(self,code,uncertain=False):
  if self.failure is None:self.failure=code
  raise Error(self.failure,uncertain)
 def remaining(self):
  if self.failure is not None:raise Error(self.failure)
  left=self.deadline-time.monotonic()
  if left<=0:self.fail("operation_timeout")
  return left
 def charge(self,kind,count):
  self.remaining()
  value=getattr(self,kind)+count
  if value>getattr(self,"max_"+kind):self.fail(kind+"_limit")
  setattr(self,kind,value)

_BUDGET=contextvars.ContextVar("ghi_operation_budget",default=None)

@contextlib.contextmanager
def operation(*,max_pages=100,max_items=10000,max_bytes=10000000,timeout=30):
 """Set cumulative limits for a group of calls; nested scopes cannot reset them."""
 candidate=_Budget(max_pages,max_items,max_bytes,timeout)
 current=_BUDGET.get()
 if current is not None:
  raise ValueError("operation scopes cannot be nested")
 token=_BUDGET.set(candidate)
 try:yield
 finally:_BUDGET.reset(token)

def _bounded(fn):
 signature=inspect.signature(fn)
 @functools.wraps(fn)
 def call(*args,**kwargs):
  bound=signature.bind(*args,**kwargs)
  timeout=bound.arguments.get("timeout",bound.arguments.get("k",{}).get("timeout",30))
  if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or timeout<=0:
   raise ValueError("timeout must be positive and finite")
  if _BUDGET.get() is not None:
   _BUDGET.get().remaining()
   return fn(*args,**kwargs)
  with operation(timeout=timeout):return fn(*args,**kwargs)
 return call

def _process(argv,payload,timeout,env=None,mutating=False):
 """Bound both pipes and wall time; never use communicate/capture_output."""
 budget=_BUDGET.get();left=min(timeout,budget.remaining())
 cap=budget.max_bytes-budget.bytes
 if cap<=0:budget.fail("bytes_limit")
 try:p=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
 except FileNotFoundError as e:raise Error("process_not_found") from e
 except OSError as e:raise Error("process_error") from e
 buffers=[bytearray(),bytearray()];errors=[];lock=threading.Lock();wake=threading.Event()
 def drain(stream,index):
  try:
   while True:
    chunk=stream.read1(min(65536,cap+1))
    if not chunk:break
    with lock:
     available=cap-sum(map(len,buffers))
     buffers[index].extend(chunk[:available])
     if len(chunk)>available:
      errors.append("bytes_limit");wake.set();return
  except (OSError,ValueError):
   with lock:errors.append("process_error")
  finally:stream.close();wake.set()
 def write():
  try:
   if payload:p.stdin.write(payload)
  except (BrokenPipeError,OSError):pass
  finally:
   try:p.stdin.close()
   except OSError:pass
   wake.set()
 threads=[threading.Thread(target=drain,args=(p.stdout,0),daemon=True),
          threading.Thread(target=drain,args=(p.stderr,1),daemon=True),
          threading.Thread(target=write,daemon=True)]
 for thread in threads:thread.start()
 deadline=min(budget.deadline,time.monotonic()+left)
 failure=None
 try:
  while p.poll() is None or any(t.is_alive() for t in threads):
   if errors:failure=errors[0];break
   left=deadline-time.monotonic()
   if left<=0:failure="operation_timeout";break
   wake.wait(min(left,.02));wake.clear()
  if not failure and errors:failure=errors[0]
 finally:
  if p.poll() is None:p.kill()
  p.wait()
 if failure in ("bytes_limit","operation_timeout"):budget.fail(failure,mutating)
 if failure:raise Error(failure,mutating)
 raw,err=map(bytes,buffers)
 try:budget.charge("bytes",len(raw)+len(err))
 except Error as e:raise Error(e.code,mutating) from e
 return p.returncode,raw,err

_HTTP_CODES={401:3,403:4,404:5,422:6}
_HTTP_ERRORS={3:"authentication_required",4:"permission_or_rate_limit",5:"not_found_or_inaccessible",
              6:"rejected",7:"http_error",8:"transport_error",9:"bytes_limit",10:"server_error"}

def _http_worker():
 """Private isolated urllib worker, killed by the parent on deadline/overflow."""
 spec=json.load(sys.stdin)
 data=None if spec["payload"] is None else json.dumps(spec["payload"]).encode()
 req=urllib.request.Request(spec["url"],data=data,headers=spec["headers"],method=spec["method"])
 try:
  with urllib.request.urlopen(req,timeout=spec["timeout"]) as response:
   # At most cap+1 bytes are ever retained by this worker.
   raw=response.read(spec["cap"]+1)
   if len(raw)>spec["cap"]:return 9
   sys.stdout.buffer.write(raw)
 except urllib.error.HTTPError as e:
  e.close()
  return _HTTP_CODES.get(e.code,10 if e.code>=500 else 7)
 except (urllib.error.URLError,TimeoutError,OSError):return 8
 return 0

class Page:
 """Optional injected response carrying data and HTTP Link headers."""
 def __init__(self,data,headers=None):self.data=data;self.headers=headers or {}

def _page(path,transport,timeout,requester=None):
 budget=_BUDGET.get();budget.charge("pages",1)
 result=(requester or request)("GET",path,transport=transport,timeout=min(timeout,budget.remaining()))
 budget.remaining()
 if requester is not None:
  data=result.data if isinstance(result,Page) else result
  budget.charge("bytes",len(json.dumps(data).encode()))
 if isinstance(result,Page):
  if not isinstance(result.headers,dict):raise Error("invalid_pagination_link")
  return result.data,result.headers
 return result,None

def _next_page(path,headers,count):
 link=None if headers is None else next((v for k,v in headers.items() if isinstance(k,str) and k.lower()=="link"),"")
 if link is not None:
  if not isinstance(link,str):raise Error("invalid_pagination_link")
  candidates=re.findall(r'<([^>]+)>\s*;\s*rel=["\']?next["\']?',link)
  if len(candidates)>1:raise Error("invalid_pagination_link")
  if not candidates:return None
  target=urllib.parse.urlsplit(urllib.parse.urljoin(API+"/"+path,candidates[0]))
  before=urllib.parse.urlsplit(API+"/"+path)
  old=urllib.parse.parse_qs(before.query);new=urllib.parse.parse_qs(target.query)
  try:valid_page=new.pop("page")==[str(int(old.pop("page")[0])+1)]
  except (KeyError,ValueError,IndexError):raise Error("invalid_pagination_link")
  if target.scheme!="https" or target.netloc!="api.github.com" or target.path!=before.path or target.fragment or new!=old or not valid_page:
   raise Error("invalid_pagination_link")
  return target.path.lstrip("/")+"?"+target.query
 if count<100:return None
 parsed=urllib.parse.urlsplit(path);query=urllib.parse.parse_qs(parsed.query)
 query["page"]=[str(int(query["page"][0])+1)]
 return parsed.path+"?"+urllib.parse.urlencode(query,doseq=True)

def now(): return datetime.now(timezone.utc).isoformat()
def repo(x):
 if not isinstance(x,str) or not REPO.fullmatch(x): raise ValueError("repo must be OWNER/REPO")
 return x
def token(): return os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
def gh_available(): return shutil.which("gh") is not None
def _gh(method,path,payload=None,timeout=30,mutating=False):
 env=os.environ.copy(); env.update(GH_PROMPT_DISABLED="1",GH_PAGER="cat"); env.pop("GH_REPO",None)
 a=["gh","api","--method",method,path]+(["--input","-"] if payload is not None else [])
 try:code,raw,err=_process(a,None if payload is None else json.dumps(payload).encode(),timeout,env,mutating)
 except Error as e:
  if e.code=="process_not_found":raise Error("gh_not_found") from e
  raise
 if code:
  message=(err or raw).decode("utf-8",errors="replace").lower()
  c="authentication_required" if code==4 else "cancelled" if code==2 else "gh_failed"
  if "http 403" in message:c="permission_or_rate_limit"
  if "http 404" in message:c="not_found_or_inaccessible"
  raise Error(c,mutating and ("http 5" in message or "timed out" in message))
 if not raw.strip():return None
 try:return json.loads(raw.decode("utf-8"))
 except (UnicodeError,json.JSONDecodeError) as e:raise Error("invalid_json",mutating) from e

def _url(method,path,payload=None,timeout=30,mutating=False):
 headers={"Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28","User-Agent":"gh_identity/0.1"}
 if token():headers["Authorization"]="Bearer "+token()
 budget=_BUDGET.get()
 spec={"url":API+"/"+path.lstrip("/"),"method":method,"payload":payload,"headers":headers,
       "timeout":min(timeout,budget.remaining()),"cap":budget.max_bytes-budget.bytes}
 # Credentials go over stdin, never command-line arguments or diagnostics.
 code,raw,err=_process([sys.executable,os.path.abspath(__file__),"--_http-worker"],json.dumps(spec).encode(),timeout,mutating=mutating)
 if code==9:budget.fail("bytes_limit",mutating)
 if code:raise Error(_HTTP_ERRORS.get(code,"transport_error"),mutating and code not in (3,4,5,6))
 if not raw:return None
 try:return json.loads(raw.decode("utf-8"))
 except (UnicodeError,json.JSONDecodeError) as e:raise Error("invalid_json",mutating) from e

def request(method,path,payload=None,transport="auto",timeout=30,mutating=False):
 if transport not in ("auto","gh","urllib"):raise ValueError("bad transport")
 if transport=="gh" or (transport=="auto" and gh_available()):
  try:return _gh(method,path,payload,timeout,mutating)
  except Error as e:
   if transport=="gh" or e.code not in ("gh_not_found","authentication_required"):raise
 return _url(method,path,payload,timeout,mutating)

def pages(path,transport="auto",timeout=30,*,requester=None):
 out=[];sep="&" if "?" in path else "?";path+=sep+"per_page=100&page=1"
 while path:
  data,headers=_page(path,transport,timeout,requester)
  if not isinstance(data,list):raise Error("invalid_json")
  _BUDGET.get().charge("items",len(data));out.extend(data)
  path=_next_page(path,headers,len(data))
 return out
def capabilities():
 return {"schema":"gh-identity-capabilities/1","observed_at":now(),"python":sys.version.split()[0],"gh":gh_available(),"git":shutil.which("git") is not None,"token_present":bool(token())}
def repository(r,**k):
 r=repo(r);d=request("GET",f"repos/{r}",**k)
 return {"schema":"gh-identity-repository/1","repository":r,"id":d.get("id"),"default_branch":d.get("default_branch"),"visibility":d.get("visibility"),"archived":d.get("archived"),"url":d.get("html_url"),"observed_at":now()}
def repositories(owner,transport="auto",timeout=30):
 xs=pages(f"users/{owner}/repos?sort=full_name",transport,timeout)
 rows=[{"full_name":x.get("full_name"),"archived":x.get("archived"),"updated_at":x.get("updated_at"),"url":x.get("html_url")} for x in xs]
 return {"schema":"gh-identity-repositories/1","owner":owner,"complete":True,"count":len(rows),"repositories":rows}
def pr(r,n,**k):
 r=repo(r);d=request("GET",f"repos/{r}/pulls/{n}",**k)
 return {"schema":"gh-identity-pr/1","repository":r,"number":n,"state":d.get("state"),"draft":d.get("draft"),"merged":d.get("merged"),"mergeable":d.get("mergeable"),"head_sha":(d.get("head")or{}).get("sha"),"head_ref":(d.get("head")or{}).get("ref"),"base_sha":(d.get("base")or{}).get("sha"),"base_ref":(d.get("base")or{}).get("ref"),"url":d.get("html_url"),"observed_at":now()}
def comments(r,n,last=None,transport="auto",timeout=30):
 r=repo(r)
 if last is not None and (type(last) is not int or last<0):raise ValueError("last must be a nonnegative integer")
 xs=pages(f"repos/{r}/issues/{n}/comments",transport,timeout)
 ds=[{"id":x.get("id"),"author":(x.get("user")or{}).get("login"),"created_at":x.get("created_at"),"chars":len(x.get("body")or""),"preview":re.sub(r"\s+"," ",x.get("body")or"")[:240],"url":x.get("html_url")} for x in xs]
 if last is not None:ds=ds[-last:] if last else []
 return {"schema":"gh-identity-comments/1","repository":r,"number":n,"total":len(xs),"shown":len(ds),"complete":True,"comments":ds}
def reviews(r,n,transport="auto",timeout=30):
 r=repo(r);xs=pages(f"repos/{r}/pulls/{n}/reviews",transport,timeout)
 ds=[{"id":x.get("id"),"author":(x.get("user")or{}).get("login"),"state":x.get("state"),"commit_id":x.get("commit_id"),"submitted_at":x.get("submitted_at"),"chars":len(x.get("body")or"")} for x in xs]
 return {"schema":"gh-identity-reviews/1","repository":r,"number":n,"count":len(ds),"complete":True,"reviews":ds}

def pull_requests(r,state="open",max_items=100,max_pages=10,transport="auto",timeout=30):
 r=repo(r)
 if state not in {"open","closed","all"}:raise ValueError("invalid state")
 if not isinstance(max_items,int) or max_items<1:raise ValueError("max_items must be positive")
 if not isinstance(max_pages,int) or max_pages<1:raise ValueError("max_pages must be positive")
 rows=[];page=1;exhausted=False;pages_fetched=0
 while page<=max_pages and len(rows)<max_items:
  batch,_headers=_page(f"repos/{r}/pulls?state={state}&sort=updated&direction=desc&per_page=100&page={page}",transport=transport,timeout=timeout)
  pages_fetched += 1
  if not isinstance(batch,list):raise Error("invalid_json")
  _BUDGET.get().charge("items",len(batch))
  consumed=0
  for x in batch:
   consumed+=1
   rows.append({"number":x.get("number"),"title":x.get("title"),"state":x.get("state"),"draft":bool(x.get("draft")),
    "head_sha":(x.get("head")or{}).get("sha"),"head_ref":(x.get("head")or{}).get("ref"),
    "base_sha":(x.get("base")or{}).get("sha"),"base_ref":(x.get("base")or{}).get("ref"),
    "created_at":x.get("created_at"),"updated_at":x.get("updated_at"),"closed_at":x.get("closed_at"),"merged_at":x.get("merged_at"),"url":x.get("html_url")})
   if len(rows)>=max_items:break
  if len(batch)<100:exhausted=consumed==len(batch);break
  page+=1
 return {"schema":"gh-identity-pr-discovery/1","repository":r,"state":state,"pull_requests":rows,
  "count":len(rows),"pages_fetched":pages_fetched,"complete":exhausted,"truncated":not exhausted}

def run_history(r,max_items=100,max_pages=10,head_sha=None,branch=None,event=None,transport="auto",timeout=30):
 r=repo(r)
 if not isinstance(max_items,int) or max_items<1:raise ValueError("max_items must be positive")
 if not isinstance(max_pages,int) or max_pages<1:raise ValueError("max_pages must be positive")
 rows=[];page=1;exhausted=False;pages_fetched=0
 while page<=max_pages and len(rows)<max_items:
  batch,_headers=_page(f"repos/{r}/actions/runs?per_page=100&page={page}",transport=transport,timeout=timeout)
  pages_fetched += 1
  if not isinstance(batch,dict) or not isinstance(batch.get("workflow_runs"),list):raise Error("invalid_json")
  raw=batch["workflow_runs"]
  _BUDGET.get().charge("items",len(raw))
  consumed=0
  for x in raw:
   consumed+=1
   row={"run_id":x.get("id"),"attempt":x.get("run_attempt"),"workflow_id":x.get("workflow_id"),"name":x.get("name"),
    "status":x.get("status"),"conclusion":x.get("conclusion"),"head_sha":x.get("head_sha"),"head_branch":x.get("head_branch"),
    "event":x.get("event"),"created_at":x.get("created_at"),"updated_at":x.get("updated_at"),"run_started_at":x.get("run_started_at"),"url":x.get("html_url")}
   if head_sha is not None and row["head_sha"]!=head_sha:continue
   if branch is not None and row["head_branch"]!=branch:continue
   if event is not None and row["event"]!=event:continue
   rows.append(row)
   if len(rows)>=max_items:break
  if len(raw)<100:exhausted=consumed==len(raw);break
  page+=1
 return {"schema":"gh-identity-run-discovery/1","repository":r,"runs":rows,"count":len(rows),
  "pages_fetched":pages_fetched,"complete":exhausted,"truncated":not exhausted,
  "filters":{"head_sha":head_sha,"branch":branch,"event":event}}

def runs(r,limit=20,transport="auto",timeout=30):
 r=repo(r);d=request("GET",f"repos/{r}/actions/runs?per_page={min(100,max(1,limit))}",transport=transport,timeout=timeout)
 ds=[{"run_id":x.get("id"),"attempt":x.get("run_attempt"),"status":x.get("status"),"conclusion":x.get("conclusion"),"head_sha":x.get("head_sha"),"event":x.get("event"),"url":x.get("html_url")} for x in (d.get("workflow_runs")or[])[:limit]]
 return {"schema":"gh-identity-runs/1","repository":r,"runs":ds}
def variable(r,name,transport="auto",timeout=30):
 r=repo(r);d=request("GET",f"repos/{r}/actions/variables/{urllib.parse.quote(name,safe='')}",transport=transport,timeout=timeout)
 return {"schema":"gh-identity-variable/1","repository":r,"name":d.get("name"),"value":d.get("value"),"updated_at":d.get("updated_at")}
def set_variable(r,name,value,write=False,transport="auto",timeout=30):
 r=repo(r);out={"schema":"gh-identity-variable-write/1","repository":r,"name":name,"status":"planned","mutation_status":"not_attempted","verification_status":"not_attempted"}
 if not write:return out
 try:
  try:request("PATCH",f"repos/{r}/actions/variables/{urllib.parse.quote(name,safe='')}",{"name":name,"value":value},transport,timeout,True)
  except Error as e:
   if e.code!="not_found_or_inaccessible":raise
   request("POST",f"repos/{r}/actions/variables",{"name":name,"value":value},transport,timeout,True)
 except Error as e:
  out.update(status="mutation_uncertain" if e.uncertain else "mutation_failed",mutation_status="uncertain" if e.uncertain else "failed",error=e.code)
  return out
 out["mutation_status"]="succeeded"
 try:
  got=variable(r,name,transport,timeout)
 except Error as e:
  out.update(status="verification_failed",verification_status="failed",verification_error=e.code)
  return out
 out["verified"]=got.get("value")==value
 out["verification_status"]="verified" if out["verified"] else "mismatch"
 out["status"]="verified" if out["verified"] else "verification_mismatch"
 return out

def post_comment(r,n,body,write=False,marker=None,transport="auto",timeout=30,sanitize_mentions=False):
 r=repo(r)
 if not body.strip():raise ValueError("empty body")
 if re.search(r"(^|[^A-Za-z0-9_])@[A-Za-z0-9_-]+",body):
  if sanitize_mentions:body=re.sub(r"(^|[^A-Za-z0-9_])@(?=[A-Za-z0-9_-]+)",lambda m:m.group(1)+"＠",body)
  else:raise ValueError("comment contains an active mention")
 if write and not marker:raise ValueError("marker is required for comment writes")
 if marker:
  if not re.fullmatch(r"<!-- gh-identity:[A-Za-z0-9_.:-]+ -->",marker):raise ValueError("invalid marker")
  for x in pages(f"repos/{r}/issues/{n}/comments",transport,timeout):
   if marker in (x.get("body")or""):return {"schema":"gh-identity-comment-write/1","status":"already_exists","id":x.get("id"),"url":x.get("html_url"),"marker":marker}
  body=marker+"\n"+body
 if not write:return {"schema":"gh-identity-comment-write/1","status":"planned","chars":len(body),"marker":marker,"body":body}
 out={"schema":"gh-identity-comment-write/1","marker":marker,"mutation_status":"not_attempted","verification_status":"not_attempted"}
 try:
  d=request("POST",f"repos/{r}/issues/{n}/comments",{"body":body},transport,timeout,True)
 except Error as e:
  out.update(status="mutation_uncertain" if e.uncertain else "mutation_failed",mutation_status="uncertain" if e.uncertain else "failed",error=e.code)
  return out
 # A successful transport is not evidence of a valid comment identity.
 cid=d.get("id") if isinstance(d,dict) else None
 if type(cid) is not int or cid<1:
  out.update(status="mutation_uncertain",mutation_status="uncertain",error="invalid_comment_response")
  return out
 out.update(id=cid,mutation_status="succeeded")
 try:
  got=request("GET",f"repos/{r}/issues/comments/{cid}",transport=transport,timeout=timeout)
 except Error as e:
  out.update(status="verification_failed",verification_status="failed",verification_error=e.code)
  return out
 expected_issue=f"{API}/repos/{r}/issues/{n}"
 expected_url=f"https://github.com/{r}/issues/{n}#issuecomment-{cid}"
 # PR conversation comments may use /pull/ in their human-facing URL.
 urls={expected_url,f"https://github.com/{r}/pull/{n}#issuecomment-{cid}"}
 verified=(isinstance(got,dict) and type(got.get("id")) is int and got.get("id")==cid
           and got.get("issue_url")==expected_issue and got.get("body")==body
           and got.get("html_url") in urls)
 out.update(status="verified" if verified else "verification_mismatch",
            verification_status="verified" if verified else "mismatch",verified=verified)
 if verified:out["url"]=got["html_url"]
 return out

def resolve_ref(r,ref,transport="auto",timeout=30):
 r=repo(r);d=request("GET",f"repos/{r}/commits/{urllib.parse.quote(ref,safe='')}",transport=transport,timeout=timeout)
 sha=d.get("sha") if isinstance(d,dict) else None
 if not isinstance(sha,str) or not re.fullmatch(r"[0-9a-fA-F]{40}",sha):raise Error("invalid_commit")
 return {"schema":"gh-identity-ref/1","repository":r,"ref":ref,"sha":sha.lower(),"observed_at":now()}
def source_identity(r,ref,path,transport="auto",timeout=30):
 r=repo(r)
 if not isinstance(path,str) or not path or path.startswith("/") or "\\" in path or any(p in ("",".","..") for p in path.split("/")):
  raise ValueError("invalid source path")
 resolved=resolve_ref(r,ref,transport,timeout)
 encoded="/".join(urllib.parse.quote(p,safe="") for p in path.split("/"))
 d=request("GET",f"repos/{r}/contents/{encoded}?ref={resolved['sha']}",transport=transport,timeout=timeout)
 if not isinstance(d,dict) or d.get("type")!="file" or d.get("submodule_git_url"):raise Error("source_not_regular_file")
 tree=request("GET",f"repos/{r}/git/trees/{resolved['sha']}?recursive=1",transport=transport,timeout=timeout)
 entries=tree.get("tree") if isinstance(tree,dict) else None
 if not isinstance(entries,list):raise Error("invalid_json")
 matches=[x for x in entries if isinstance(x,dict) and x.get("path")==path]
 if len(matches)!=1 or matches[0].get("type")!="blob" or matches[0].get("mode") not in ("100644","100755"):
  raise Error("source_not_regular_file")
 if matches[0].get("sha")!=d.get("sha"):raise Error("source_identity_mismatch")
 blob=d.get("sha");size=d.get("size")
 if not isinstance(blob,str) or not re.fullmatch(r"[0-9a-fA-F]{40}",blob):raise Error("invalid_blob")
 if not isinstance(size,int) or size<0:raise Error("invalid_source_size")
 return {"schema":"gh-identity-source/1","repository":r,"ref":ref,"commit_sha":resolved["sha"],"path":path,"blob_sha":blob.lower(),"size":size,"type":"file","observed_at":now()}
def _min_checks(value):
 if not isinstance(value,int) or isinstance(value,bool) or value<1:raise ValueError("min_checks must be at least 1")
 return value

def summarize_checks(rows,expected_count,min_checks=1):
 min_checks=_min_checks(min_checks)
 if not isinstance(rows,list):raise ValueError("check rows must be a list")
 normalized=[{"id":x.get("id"),"name":x.get("name"),"status":x.get("status"),"conclusion":x.get("conclusion"),"url":x.get("html_url") or x.get("url"),"annotations_count":x.get("annotations_count",(x.get("output")or{}).get("annotations_count",0))} for x in rows]
 complete=isinstance(expected_count,int) and not isinstance(expected_count,bool) and expected_count==len(normalized)
 bad={"failure","cancelled","timed_out","action_required","startup_failure","stale"}
 if not complete:state="incomplete"
 elif len(normalized)<min_checks:state="pending"
 elif any(x["status"]!="completed" for x in normalized):state="pending"
 elif any(x["conclusion"] in bad for x in normalized):state="failed"
 elif not any(x["conclusion"]=="success" for x in normalized):state="failed"
 else:state="green"
 return {"state":state,"complete":complete,"expected_count":expected_count,"count":len(normalized),"min_checks":min_checks,"checks":normalized}

def checks_for_sha(r,sha,min_checks=1,transport="auto",timeout=30,*,requester=None):
 min_checks=_min_checks(min_checks);r=repo(r);rows=[];expected=None
 path=f"repos/{r}/commits/{sha}/check-runs?per_page=100&page=1"
 while path:
  d,headers=_page(path,transport,timeout,requester)
  if not isinstance(d,dict) or not isinstance(d.get("check_runs"),list):raise Error("invalid_json")
  total=d.get("total_count")
  if type(total) is not int or total<0:raise Error("pagination_incomplete")
  if expected is None:expected=total
  elif total!=expected:raise Error("pagination_incomplete")
  batch=d["check_runs"]
  _BUDGET.get().charge("items",len(batch));rows.extend(batch)
  if len(rows)>expected:raise Error("pagination_incomplete")
  path=_next_page(path,headers,len(batch))
 summary=summarize_checks(rows,expected,min_checks)
 return {"schema":"gh-identity-checks/1","repository":r,"sha":sha,**summary,"observed_at":now()}

def observe_pr(r,n,min_checks=1,transport="auto",timeout=30):
 before=pr(r,n,transport=transport,timeout=timeout)
 ch=checks_for_sha(r,before["head_sha"],min_checks,transport,timeout)
 cs=comments(r,n,transport=transport,timeout=timeout)
 rs=reviews(r,n,transport=transport,timeout=timeout)
 after=pr(r,n,transport=transport,timeout=timeout)
 return {"schema":"gh-identity-pr-observation/1","repository":repo(r),"number":n,"head_sha":before["head_sha"],"base_sha":before["base_sha"],
  "state":before["state"],"draft":before["draft"],"mergeable":before["mergeable"],"checks":ch,"conversation":cs,"reviews":rs,
  "stale":before["head_sha"]!=after["head_sha"],"head_sha_after":after["head_sha"],"observed_at":now()}
def workflow(r,w,transport="auto",timeout=30):
 r=repo(r);d=request("GET",f"repos/{r}/actions/workflows/{urllib.parse.quote(str(w),safe='')}",transport=transport,timeout=timeout)
 return {"schema":"gh-identity-workflow/1","repository":r,"id":d.get("id"),"name":d.get("name"),"path":d.get("path"),"state":d.get("state"),"url":d.get("html_url"),"observed_at":now()}

def run(r, run_id, attempt=None, transport="auto", timeout=30):
 """Read an exact Actions run and optionally one exact rerun attempt."""
 r=repo(r)
 def positive(x, label):
  if isinstance(x,bool) or not str(x).isdigit() or int(x)<1:raise ValueError("invalid " + label + " identifier")
  return int(x)
 run_id=positive(run_id, "run")
 if attempt is not None:attempt=positive(attempt, "attempt")
 path=f"repos/{r}/actions/runs/{run_id}"
 if attempt is not None:path+=f"/attempts/{attempt}"
 d=request("GET",path,transport=transport,timeout=timeout)
 if not isinstance(d,dict) or d.get("id")!=run_id:raise Error("invalid_json")
 actual=d.get("run_attempt")
 if attempt is not None and actual!=attempt:raise Error("attempt_mismatch")
 return {"schema":"gh-identity-run/1","repository":r,
  "workflow_id":d.get("workflow_id"),"run_id":d["id"],"attempt":actual,
  "head_sha":d.get("head_sha"),"event":d.get("event"),
  "status":d.get("status"),"conclusion":d.get("conclusion"),
  "created_at":d.get("created_at"),"url":d.get("html_url"),"observed_at":now()}


def jobs(r, run_id, attempt=None, transport="auto", timeout=30):
 """Read all jobs and their steps for one Actions run or exact attempt."""
 r=repo(r)
 def ident(value):
  if isinstance(value,bool) or not str(value).isdigit() or int(value)<1:
   raise ValueError("invalid job lookup identifier")
  return int(value)
 run_id=ident(run_id)
 if attempt is not None:attempt=ident(attempt)
 base=f"repos/{r}/actions/runs/{run_id}"
 if attempt is not None:base+=f"/attempts/{attempt}"
 rows=[];page=1;total=None
 while True:
  data,_headers=_page(f"{base}/jobs?per_page=100&page={page}",transport=transport,timeout=timeout)
  if not isinstance(data,dict) or not isinstance(data.get("jobs"),list):raise Error("invalid_json")
  if total is None:total=data.get("total_count")
  batch=data["jobs"];_BUDGET.get().charge("items",len(batch));rows.extend(batch)
  if len(batch)<100:break
  page+=1
  if page>100:raise Error("pagination_incomplete")
 if type(total) is not int or total<0 or len(rows)!=total:raise Error("pagination_incomplete")
 out=[];seen=set()
 for x in rows:
  if not isinstance(x,dict):raise Error("invalid_json")
  jid=x.get("id");actual_run=x.get("run_id");actual_attempt=x.get("run_attempt")
  if type(jid) is not int or jid<1:raise Error("invalid_job_identity")
  if jid in seen:raise Error("ambiguous_job_identity")
  seen.add(jid)
  if type(actual_run) is not int or actual_run!=run_id:raise Error("job_identity_mismatch")
  if type(actual_attempt) is not int or actual_attempt<1:raise Error("invalid_attempt_identity")
  if attempt is not None and actual_attempt!=attempt:raise Error("attempt_mismatch")
  steps=x.get("steps",[])
  if not isinstance(steps,list) or any(not isinstance(step,dict) for step in steps):raise Error("invalid_json")
  out.append({"job_id":x.get("id"),"run_id":x.get("run_id"),"attempt":x.get("run_attempt"),
   "name":x.get("name"),"status":x.get("status"),"conclusion":x.get("conclusion"),
   "started_at":x.get("started_at"),"completed_at":x.get("completed_at"),
   "url":x.get("html_url"),"steps":[{"number":step.get("number"),"name":step.get("name"),
   "status":step.get("status"),"conclusion":step.get("conclusion")} for step in (x.get("steps") or [])]})
 return {"schema":"gh-identity-jobs/1","repository":r,"run_id":run_id,"attempt":attempt,
  "complete":True,"count":len(out),"jobs":out,"observed_at":now()}

def _positive_identifier(value,label):
 if isinstance(value,bool) or not isinstance(value,(int,str)) or not re.fullmatch(r"[0-9]+",str(value)) or int(value)<1:
  raise ValueError("invalid "+label+" identifier")
 return int(value)

def step_url(r,run_id,job_id,step_number,line=None):
 """Build a UI permalink from explicit identities; performs no observation."""
 r=repo(r)
 run_id=_positive_identifier(run_id,"run")
 job_id=_positive_identifier(job_id,"job")
 step_number=_positive_identifier(step_number,"step")
 if line is not None:line=_positive_identifier(line,"line")
 encoded="/".join(urllib.parse.quote(part,safe="") for part in r.split("/"))
 anchor=f"#step:{step_number}"+(f":{line}" if line is not None else "")
 return f"https://github.com/{encoded}/actions/runs/{run_id}/job/{job_id}{anchor}"

def step_url_from_jobs(observation,job_id,step_number,line=None):
 """Return a link only for a unique step in a complete jobs() observation."""
 job_id=_positive_identifier(job_id,"job")
 step_number=_positive_identifier(step_number,"step")
 if line is not None:line=_positive_identifier(line,"line")
 if not isinstance(observation,dict) or observation.get("schema")!="gh-identity-jobs/1" or observation.get("complete") is not True:
  raise Error("incomplete_job_observation")
 r=repo(observation.get("repository"))
 run_id=_positive_identifier(observation.get("run_id"),"run")
 attempt=observation.get("attempt")
 if attempt is not None:attempt=_positive_identifier(attempt,"attempt")
 rows=observation.get("jobs")
 if not isinstance(rows,list):raise Error("invalid_json")
 if type(observation.get("count")) is not int or observation["count"]!=len(rows):raise Error("incomplete_job_observation")
 if any(not isinstance(row,dict) for row in rows):raise Error("invalid_json")
 matches=[row for row in rows if row.get("job_id")==job_id and type(row.get("job_id")) is int]
 if not matches:raise Error("step_not_found")
 if len(matches)!=1:raise Error("ambiguous_job_identity")
 job=matches[0]
 if type(job.get("run_id")) is not int or job.get("run_id")!=run_id:raise Error("job_identity_mismatch")
 if attempt is not None and (type(job.get("attempt")) is not int or job.get("attempt")!=attempt):raise Error("attempt_mismatch")
 steps=job.get("steps")
 if not isinstance(steps,list) or any(not isinstance(step,dict) for step in steps):raise Error("invalid_json")
 matches=[step for step in steps if step.get("number")==step_number and type(step.get("number")) is int]
 if not matches:raise Error("step_not_found")
 if len(matches)!=1:raise Error("ambiguous_step_identity")
 return step_url(r,run_id,job_id,step_number,line)

def gh_help(*parts,timeout=15):
 if not gh_available():raise Error("gh_not_found")
 env=os.environ.copy();env.update(GH_PROMPT_DISABLED="1",GH_PAGER="cat");env.pop("GH_REPO",None)
 try:p=subprocess.run(["gh","help",*parts],capture_output=True,text=True,encoding="utf-8",timeout=timeout,env=env)
 except OSError as e:raise Error("process_error") from e
 if p.returncode:raise Error("gh_failed")
 return p.stdout

def local_identity(*,cwd=None,identity=None,env=None):
 env=os.environ if env is None else env
 if identity is not None:
  sha=identity.get("sha")
  if sha is not None and not re.fullmatch(r"[0-9a-fA-F]{40}",str(sha)):raise ValueError("invalid local sha")
  return {"schema":"gh-identity-local/1","sha":str(sha).lower() if sha else None,"ref":identity.get("ref"),"dirty":identity.get("dirty"),"source":identity.get("source","provided"),"observed_at":now()}
 sha=env.get("GITHUB_SHA")
 ref=env.get("GITHUB_HEAD_REF") or env.get("GITHUB_REF_NAME")
 if sha and re.fullmatch(r"[0-9a-fA-F]{40}",sha):
  return {"schema":"gh-identity-local/1","sha":sha.lower(),"ref":ref,"dirty":None,"source":"github-env","observed_at":now()}
 if shutil.which("git") is None:
  return {"schema":"gh-identity-local/1","sha":None,"ref":None,"dirty":None,"source":"unavailable","observed_at":now()}
 root=cwd or os.getcwd()
 def git(*args):
  p=subprocess.run(["git",*args],cwd=root,capture_output=True,text=True,encoding="utf-8",check=False)
  if p.returncode:raise Error("git_failed")
  return p.stdout.strip()
 sha=git("rev-parse","HEAD")
 if not re.fullmatch(r"[0-9a-fA-F]{40}",sha):raise Error("invalid_commit")
 ref=git("branch","--show-current") or None
 dirty=bool(git("status","--porcelain"))
 return {"schema":"gh-identity-local/1","sha":sha.lower(),"ref":ref,"dirty":dirty,"source":"git","observed_at":now()}

def compare_sha(local,remote_sha):
 lsha=local.get("sha") if isinstance(local,dict) else None
 lsha=str(lsha).lower() if lsha else None
 rsha=str(remote_sha).lower() if remote_sha else None
 return {"schema":"gh-identity-comparison/1","local_sha":lsha,"remote_sha":rsha,"comparable":bool(lsha and rsha),"same":(lsha==rsha) if lsha and rsha else None,"observed_at":now()}

def _main(argv=None):
 argv=list(sys.argv[1:] if argv is None else argv)
 transport="auto"
 if "--transport" in argv:
  i=argv.index("--transport")
  if i+1>=len(argv): print(json.dumps({"status":"error","error":"invalid_argument"}),file=sys.stderr);return 2
  transport=argv[i+1]
  if transport not in ("auto","gh","urllib"): print(json.dumps({"status":"error","error":"invalid_argument"}),file=sys.stderr);return 2
  del argv[i:i+2]
 a=argparse.ArgumentParser(epilog="Global options: --transport auto|gh|urllib, --max-pages N (100), --max-items N (10000), --max-bytes N (10000000), --timeout SECONDS (30). Limits are cumulative per operation.");s=a.add_subparsers(dest="cmd",required=True)
 s.add_parser("capabilities")
 x=s.add_parser("repo");x.add_argument("repo")
 x=s.add_parser("repos");x.add_argument("owner")
 x=s.add_parser("pr");x.add_argument("repo");x.add_argument("number",type=int)
 x=s.add_parser("comments");x.add_argument("repo");x.add_argument("number",type=int)
 x=s.add_parser("reviews");x.add_argument("repo");x.add_argument("number",type=int)
 x=s.add_parser("runs");x.add_argument("repo")
 x=s.add_parser("workflow");x.add_argument("repo");x.add_argument("workflow_id")
 x=s.add_parser("run");x.add_argument("repo");x.add_argument("run_id",type=int);x.add_argument("--attempt",type=int)
 x=s.add_parser("jobs");x.add_argument("repo");x.add_argument("run_id",type=int);x.add_argument("--attempt",type=int)
 x=s.add_parser("step-url");x.add_argument("repo");x.add_argument("run_id",type=int);x.add_argument("job_id",type=int);x.add_argument("step_number",type=int);x.add_argument("--line",type=int)
 x=s.add_parser("variable-get");x.add_argument("repo");x.add_argument("name")
 x=s.add_parser("variable-set");x.add_argument("repo");x.add_argument("name");x.add_argument("value");x.add_argument("--write",action="store_true")
 x=s.add_parser("comment");x.add_argument("repo");x.add_argument("number",type=int);x.add_argument("body");x.add_argument("--operation-key");x.add_argument("--sanitize-mentions",action="store_true");x.add_argument("--write",action="store_true")
 try:ns=a.parse_args(argv)
 except SystemExit as e:return int(e.code)
 try:
  if ns.cmd=="capabilities":o=capabilities()
  elif ns.cmd=="repo":o=repository(ns.repo,transport=transport)
  elif ns.cmd=="repos":o=repositories(ns.owner,transport=transport)
  elif ns.cmd=="pr":o=pr(ns.repo,ns.number,transport=transport)
  elif ns.cmd=="comments":o=comments(ns.repo,ns.number,transport=transport)
  elif ns.cmd=="reviews":o=reviews(ns.repo,ns.number,transport=transport)
  elif ns.cmd=="runs":o=runs(ns.repo,transport=transport)
  elif ns.cmd=="workflow":o=workflow(ns.repo,ns.workflow_id,transport=transport)
  elif ns.cmd=="run":o=run(ns.repo,ns.run_id,attempt=ns.attempt,transport=transport)
  elif ns.cmd=="jobs":o=jobs(ns.repo,ns.run_id,attempt=ns.attempt,transport=transport)
  elif ns.cmd=="step-url":o={"schema":"gh-identity-step-url/1","url":step_url(ns.repo,ns.run_id,ns.job_id,ns.step_number,ns.line)}
  elif ns.cmd=="variable-get":o=variable(ns.repo,ns.name,transport=transport)
  elif ns.cmd=="variable-set":o=set_variable(ns.repo,ns.name,ns.value,ns.write,transport)
  else:
   marker=f"<!-- gh-identity:{ns.operation_key} -->" if ns.operation_key else None
   o=post_comment(ns.repo,ns.number,ns.body,ns.write,marker,transport,sanitize_mentions=ns.sanitize_mentions)
 except (ValueError,Error) as e:print(json.dumps({"status":"error","error":getattr(e,"code","invalid_argument")}),file=sys.stderr);return 2
 print(json.dumps(o,ensure_ascii=False))
 if ns.cmd in ("comment","variable-set"):
  return 0 if o.get("status") in ("planned","already_exists","verified") else 1
 return 0
def main(argv=None):
 args=list(sys.argv[1:] if argv is None else argv);limits={}
 try:
  for flag,key,convert in (("--max-pages","max_pages",int),("--max-items","max_items",int),
                           ("--max-bytes","max_bytes",int),("--timeout","timeout",float)):
   if flag in args:
    index=args.index(flag)
    if index+1>=len(args):raise ValueError("missing limit")
    limits[key]=convert(args[index+1]);del args[index:index+2]
  with operation(**limits):return _main(args)
 except (ValueError,Error) as e:
  print(json.dumps({"status":"error","error":getattr(e,"code","invalid_argument")}),file=sys.stderr)
  return 2

# One outer deadline/budget is shared across nested calls and gh fallback.
for _name in ("_gh","_url","request","pages","repository","repositories","pr","comments","reviews",
              "pull_requests","run_history","runs","variable","set_variable","post_comment",
              "resolve_ref","source_identity","checks_for_sha","observe_pr","workflow","run","jobs"):
 globals()[_name]=_bounded(globals()[_name])
if __name__=="__main__":
 raise SystemExit(_http_worker() if sys.argv[1:]==["--_http-worker"] else main())
