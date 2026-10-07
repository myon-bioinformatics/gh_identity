#!/usr/bin/env python3
"""gh_identity: stdlib-only GitHub operations with gh-first/urllib fallback."""
from __future__ import annotations
import argparse,json,os,re,shutil,subprocess,sys,urllib.error,urllib.parse,urllib.request
from datetime import datetime,timezone
API="https://api.github.com"; REPO=re.compile(r"^[\w.-]+/[\w.-]+$")
class Error(RuntimeError):
 def __init__(self,code,uncertain=False): super().__init__(code); self.code=code; self.uncertain=uncertain
def now(): return datetime.now(timezone.utc).isoformat()
def repo(x):
 if not isinstance(x,str) or not REPO.fullmatch(x): raise ValueError("repo must be OWNER/REPO")
 return x
def token(): return os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
def gh_available(): return shutil.which("gh") is not None
def _gh(method,path,payload=None,timeout=30,mutating=False):
 env=os.environ.copy(); env.update(GH_PROMPT_DISABLED="1",GH_PAGER="cat"); env.pop("GH_REPO",None)
 a=["gh","api","--method",method,path]+(["--input","-"] if payload is not None else [])
 try:p=subprocess.run(a,input=json.dumps(payload) if payload is not None else None,capture_output=True,text=True,encoding="utf-8",timeout=timeout,env=env)
 except FileNotFoundError as e: raise Error("gh_not_found") from e
 except subprocess.TimeoutExpired as e: raise Error("timeout",mutating) from e
 except OSError as e: raise Error("process_error",mutating) from e
 if p.returncode:
  s=(p.stderr or p.stdout).lower()
  c="authentication_required" if p.returncode==4 else "cancelled" if p.returncode==2 else "gh_failed"
  if "http 403" in s:c="permission_or_rate_limit"
  if "http 404" in s:c="not_found_or_inaccessible"
  raise Error(c,mutating and ("http 5" in s or "timed out" in s))
 if not p.stdout.strip(): return None
 try:return json.loads(p.stdout)
 except json.JSONDecodeError as e: raise Error("invalid_json",mutating) from e
def _url(method,path,payload=None,timeout=30,mutating=False):
 h={"Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28","User-Agent":"gh_identity/0.1"}
 if token():h["Authorization"]="Bearer "+token()
 data=None if payload is None else json.dumps(payload).encode()
 q=urllib.request.Request(API+"/"+path.lstrip("/"),data=data,headers=h,method=method)
 try:
  with urllib.request.urlopen(q,timeout=timeout) as r: raw=r.read()
 except urllib.error.HTTPError as e:
  raise Error({401:"authentication_required",403:"permission_or_rate_limit",404:"not_found_or_inaccessible",422:"rejected"}.get(e.code,"http_error"),mutating and e.code>=500) from e
 except (urllib.error.URLError,TimeoutError,OSError) as e: raise Error("transport_error",mutating) from e
 if not raw:return None
 try:return json.loads(raw.decode())
 except (UnicodeError,json.JSONDecodeError) as e: raise Error("invalid_json",mutating) from e
def request(method,path,payload=None,transport="auto",timeout=30,mutating=False):
 if transport not in ("auto","gh","urllib"):raise ValueError("bad transport")
 if transport=="gh" or (transport=="auto" and gh_available()):
  try:return _gh(method,path,payload,timeout,mutating)
  except Error as e:
   if transport=="gh" or e.code not in ("gh_not_found","authentication_required"):raise
 return _url(method,path,payload,timeout,mutating)
def pages(path,transport="auto",timeout=30):
 out=[]; p=1; sep="&" if "?" in path else "?"
 while True:
  x=request("GET",f"{path}{sep}per_page=100&page={p}",transport=transport,timeout=timeout)
  if not isinstance(x,list):raise Error("invalid_json")
  out+=x
  if len(x)<100:return out
  p+=1
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
 r=repo(r);xs=pages(f"repos/{r}/issues/{n}/comments",transport,timeout)
 ds=[{"id":x.get("id"),"author":(x.get("user")or{}).get("login"),"created_at":x.get("created_at"),"chars":len(x.get("body")or""),"preview":re.sub(r"\s+"," ",x.get("body")or"")[:240],"url":x.get("html_url")} for x in xs]
 if last is not None:ds=ds[-last:]
 return {"schema":"gh-identity-comments/1","repository":r,"number":n,"total":len(xs),"shown":len(ds),"complete":True,"comments":ds}
def reviews(r,n,transport="auto",timeout=30):
 r=repo(r);xs=pages(f"repos/{r}/pulls/{n}/reviews",transport,timeout)
 ds=[{"id":x.get("id"),"author":(x.get("user")or{}).get("login"),"state":x.get("state"),"commit_id":x.get("commit_id"),"submitted_at":x.get("submitted_at"),"chars":len(x.get("body")or"")} for x in xs]
 return {"schema":"gh-identity-reviews/1","repository":r,"number":n,"count":len(ds),"complete":True,"reviews":ds}
def runs(r,limit=20,transport="auto",timeout=30):
 r=repo(r);d=request("GET",f"repos/{r}/actions/runs?per_page={min(100,max(1,limit))}",transport=transport,timeout=timeout)
 ds=[{"run_id":x.get("id"),"attempt":x.get("run_attempt"),"status":x.get("status"),"conclusion":x.get("conclusion"),"head_sha":x.get("head_sha"),"event":x.get("event"),"url":x.get("html_url")} for x in (d.get("workflow_runs")or[])[:limit]]
 return {"schema":"gh-identity-runs/1","repository":r,"runs":ds}
def variable(r,name,transport="auto",timeout=30):
 r=repo(r);d=request("GET",f"repos/{r}/actions/variables/{urllib.parse.quote(name,safe='')}",transport=transport,timeout=timeout)
 return {"schema":"gh-identity-variable/1","repository":r,"name":d.get("name"),"value":d.get("value"),"updated_at":d.get("updated_at")}
def set_variable(r,name,value,write=False,transport="auto",timeout=30):
 r=repo(r);out={"schema":"gh-identity-variable-write/1","repository":r,"name":name,"status":"planned"}
 if not write:return out
 try:
  try:request("PATCH",f"repos/{r}/actions/variables/{urllib.parse.quote(name,safe='')}",{"name":name,"value":value},transport,timeout,True)
  except Error as e:
   if e.code!="not_found_or_inaccessible":raise
   request("POST",f"repos/{r}/actions/variables",{"name":name,"value":value},transport,timeout,True)
  out["status"]="verified";out["verified"]=variable(r,name,transport,timeout)["value"]==value
 except Error as e:out.update(status="mutation_uncertain" if e.uncertain else "mutation_failed",error=e.code)
 return out
def post_comment(r,n,body,write=False,marker=None,transport="auto",timeout=30):
 r=repo(r)
 if not body.strip():raise ValueError("empty body")
 if marker:
  for x in pages(f"repos/{r}/issues/{n}/comments",transport,timeout):
   if marker in (x.get("body")or""):return {"schema":"gh-identity-comment-write/1","status":"already_exists","id":x.get("id"),"url":x.get("html_url")}
  body=marker+"\n"+body
 if not write:return {"schema":"gh-identity-comment-write/1","status":"planned","chars":len(body)}
 try:
  d=request("POST",f"repos/{r}/issues/{n}/comments",{"body":body},transport,timeout,True)
  return {"schema":"gh-identity-comment-write/1","status":"verified","id":d.get("id"),"url":d.get("html_url")}
 except Error as e:return {"schema":"gh-identity-comment-write/1","status":"mutation_uncertain" if e.uncertain else "mutation_failed","error":e.code}
def main(argv=None):
 a=argparse.ArgumentParser();a.add_argument("--transport",choices=["auto","gh","urllib"],default="auto");s=a.add_subparsers(dest="cmd",required=True)
 s.add_parser("capabilities")
 for c in ("repo","repos","pr","comments","reviews","runs","variable-get","variable-set","comment"):s.add_parser(c)
 ns,rest=a.parse_known_args(argv)
 try:
  if ns.cmd=="capabilities":o=capabilities()
  elif ns.cmd=="repo":o=repository(rest[0],transport=ns.transport)
  elif ns.cmd=="repos":o=repositories(rest[0],transport=ns.transport)
  elif ns.cmd=="pr":o=pr(rest[0],int(rest[1]),transport=ns.transport)
  elif ns.cmd=="comments":o=comments(rest[0],int(rest[1]),transport=ns.transport)
  elif ns.cmd=="reviews":o=reviews(rest[0],int(rest[1]),transport=ns.transport)
  elif ns.cmd=="runs":o=runs(rest[0],transport=ns.transport)
  elif ns.cmd=="variable-get":o=variable(rest[0],rest[1],transport=ns.transport)
  elif ns.cmd=="variable-set":o=set_variable(rest[0],rest[1],rest[2],"--write" in rest,ns.transport)
  else:o=post_comment(rest[0],int(rest[1]),rest[2],"--write" in rest,None,ns.transport)
 except (ValueError,Error,IndexError) as e:print(json.dumps({"status":"error","error":getattr(e,"code","invalid_argument")}),file=sys.stderr);return 2
 print(json.dumps(o,ensure_ascii=False));return 0
if __name__=="__main__":raise SystemExit(main())
