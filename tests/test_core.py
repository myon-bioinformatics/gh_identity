"""🧱 Core: CLI, transports, operation limits, identity API, ignore-filter benchmark. Stdlib only: CI also runs this file under `python -S -m unittest`.

Sections (each was one file; `-k` selects inside them): gh_identity, capabilities_versions, cli_exception_merge, operation_limits, benchmark_validation, library_budget_and_git_runner, identity_api.
"""

from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from unittest.mock import patch
import contextlib
import io
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import unittest
import urllib.parse

try:
    import pytest
except ImportError:  # `python -S` (no site-packages): only the unittest.TestCase classes below run.
    class _NoPytest:  # pytest-style tests stay defined but are never collected by unittest
        def __getattr__(self, name): return self
        def __call__(self, *args, **kwargs):
            return args[0] if len(args) == 1 and not kwargs and callable(args[0]) else self
    pytest = _NoPytest()

import gh_identity as g
from vendor.cli_args import run_cli


# ──── gh_identity ─────────────────────────────────────────────────

class T(unittest.TestCase):
 def test_repo_validation(self):
  self.assertEqual(g.repo("o/r"),"o/r")
  self.assertEqual(g.repo("日本/リポ"),"日本/リポ")
  self.assertEqual(g.repo("acme/.github"),"acme/.github")
  with self.assertRaises(ValueError): g.repo("bad")
  for bad in ("../secret", "./secret", "owner/..", "owner/.", "owner/."):
   with self.assertRaises(ValueError): g.repo(bad)
  self.assertTrue(g._github_full_name_ok("acme/.github"))
  self.assertTrue(g._github_full_name_ok("owner/.hidden"))
  self.assertEqual(g._pin_repo("acme/.github"), "acme/.github")
  self.assertEqual(g._pin_repo("https://github.com/acme/widgets.git"), "acme/widgets")
  self.assertEqual(g._pin_repo("o/repo.git"), "o/repo")
  self.assertFalse(g._github_full_name_ok("o/repo.git"))
  for bad in ("../secret", "owner/..", "owner/.", "./secret", "o/a\\b", "a/b/c", None,
              "https://evil.example/acme/widgets", "/tmp/remotes/acme/widgets"):
   with self.assertRaises(ValueError): g._pin_repo(bad)
  with self.assertRaises(ValueError): g._selected_repos([" ../secret "])
 def test_capabilities_no_secret(self):
  with mock.patch.dict(g.os.environ,{"GH_TOKEN":"secret"},clear=False):
   x=g.capabilities(); self.assertTrue(x["token_present"]); self.assertNotIn("secret",json.dumps(x))
 def test_auto_falls_back_without_gh(self):
  with mock.patch.object(g,"gh_available",return_value=False), mock.patch.object(g,"_url",return_value={"ok":1}) as u:
   self.assertEqual(g.request("GET","x"),{"ok":1}); u.assert_called_once()
 def test_gh_preferred(self):
  with mock.patch.object(g,"gh_available",return_value=True), mock.patch.object(g,"_gh",return_value={"ok":1}) as h:
   self.assertEqual(g.request("GET","x"),{"ok":1}); h.assert_called_once()
 def test_comments_digest(self):
  rows=[{"id":1,"body":"a"*300,"user":{"login":"u"},"created_at":"t","html_url":"x"}]
  with mock.patch.object(g,"pages",return_value=rows):
   x=g.comments("o/r",1); self.assertEqual(x["comments"][0]["chars"],300); self.assertEqual(len(x["comments"][0]["preview"]),240)
 def test_comment_marker_idempotent(self):
  with mock.patch.object(g,"pages",return_value=[{"id":7,"body":"<!-- gh-identity:k -->","html_url":"u"}]), mock.patch.object(g,"request") as q:
   x=g.post_comment("o/r",1,"body",write=True,marker="<!-- gh-identity:k -->"); self.assertEqual(x["status"],"already_exists"); q.assert_not_called()
 def test_variable_dry_run(self):
  x=g.set_variable("o/r","A","B"); self.assertEqual(x["status"],"planned")
 def test_uncertain_comment(self):
  with mock.patch.object(g,"pages",return_value=[]), mock.patch.object(g,"request",side_effect=g.Error("timeout",True)):
   x=g.post_comment("o/r",1,"x",write=True,marker="<!-- gh-identity:k -->"); self.assertEqual(x["status"],"mutation_uncertain")
 def test_pr_identity(self):
  raw={"state":"open","draft":False,"merged":False,"mergeable":True,"head":{"sha":"h","ref":"f"},"base":{"sha":"b","ref":"main"},"html_url":"u"}
  with mock.patch.object(g,"request",return_value=raw):
   x=g.pr("o/r",2); self.assertEqual((x["head_sha"],x["base_sha"]),("h","b"))

 def test_zero_checks_not_green(self):
  with mock.patch.object(g,"request",return_value={"total_count":0,"check_runs":[]}):
   self.assertEqual(g.checks_for_sha("o/r","a"*40)["state"],"pending")
 def test_summarize_checks_is_pure_and_normalizes_evidence(self):
  rows=[{"id":1,"name":"unit","status":"completed","conclusion":"success",
         "html_url":"u","output":{"annotations_count":3}}]
  original=json.loads(json.dumps(rows))
  x=g.summarize_checks(rows,1)
  self.assertEqual(x["state"],"green"); self.assertTrue(x["complete"])
  self.assertEqual(x["checks"][0]["url"],"u")
  self.assertEqual(x["checks"][0]["annotations_count"],3)
  self.assertEqual(rows,original)

 def test_summarize_checks_explicit_incomplete_wins(self):
  rows=[{"id":1,"name":"unit","status":"completed","conclusion":"success"}]
  x=g.summarize_checks(rows,2)
  self.assertEqual(x["state"],"incomplete"); self.assertFalse(x["complete"])

 def test_checks_for_sha_delegates_classification_to_summary(self):
  row={"id":1,"name":"unit","status":"completed","conclusion":"success"}
  sentinel={"state":"green","complete":True,"expected_count":1,"count":1,"min_checks":1,"checks":[{"sentinel":True}]}
  with mock.patch.object(g,"request",return_value={"total_count":1,"check_runs":[row]}), \
       mock.patch.object(g,"summarize_checks",return_value=sentinel) as summary:
   x=g.checks_for_sha("o/r","a"*40)
  summary.assert_called_once_with([row],1,1)
  self.assertEqual(x["checks"],[{"sentinel":True}])

 def test_checks_invalid_minimum_fails_before_transport(self):
  with mock.patch.object(g,"request") as request:
   with self.assertRaises(ValueError): g.checks_for_sha("o/r","a"*40,min_checks=0)
  request.assert_not_called()

 def test_checks_reject_nonpositive_minimum(self):
  for value in (0,-1,False):
   with self.subTest(value=value), self.assertRaises(ValueError):
    g.checks_for_sha("o/r","a"*40,min_checks=value)

 def test_checks_fewer_than_minimum_pending(self):
  ok={"id":1,"name":"ok","status":"completed","conclusion":"success","output":{"annotations_count":2}}
  with mock.patch.object(g,"request",return_value={"total_count":1,"check_runs":[ok]}):
   x=g.checks_for_sha("o/r","a"*40,min_checks=2)
  self.assertEqual(x["state"],"pending"); self.assertEqual(x["checks"][0]["annotations_count"],2)

 def test_checks_in_progress_pending(self):
  row={"id":1,"name":"unit","status":"in_progress","conclusion":None}
  with mock.patch.object(g,"request",return_value={"total_count":1,"check_runs":[row]}):
   self.assertEqual(g.checks_for_sha("o/r","a"*40)["state"],"pending")

 def test_checks_all_skipped_or_neutral_not_green(self):
  rows=[{"id":1,"name":"skip","status":"completed","conclusion":"skipped"},
        {"id":2,"name":"neutral","status":"completed","conclusion":"neutral"}]
  with mock.patch.object(g,"request",return_value={"total_count":2,"check_runs":rows}):
   self.assertEqual(g.checks_for_sha("o/r","a"*40)["state"],"failed")

 def test_checks_success_plus_skipped_is_green(self):
  rows=[{"id":1,"name":"unit","status":"completed","conclusion":"success"},
        {"id":2,"name":"optional","status":"completed","conclusion":"skipped"}]
  with mock.patch.object(g,"request",return_value={"total_count":2,"check_runs":rows}):
   self.assertEqual(g.checks_for_sha("o/r","a"*40)["state"],"green")

 def test_observe_pr_stale(self):
  p1={"schema":"x","repository":"o/r","number":1,"state":"open","draft":False,"mergeable":True,"head_sha":"a"*40,"base_sha":"b"*40}
  p2=dict(p1,head_sha="c"*40)
  with mock.patch.object(g,"pr",side_effect=[p1,p2]), mock.patch.object(g,"checks_for_sha",return_value={"state":"green"}), mock.patch.object(g,"comments",return_value={}), mock.patch.object(g,"reviews",return_value={}):
   self.assertTrue(g.observe_pr("o/r",1)["stale"])
 def test_resolve_ref(self):
  with mock.patch.object(g,"request",return_value={"sha":"A"*40}):
   self.assertEqual(g.resolve_ref("o/r","main")["sha"],"a"*40)
 def test_transport_after_subcommand(self):
  with mock.patch.object(g,"repository",return_value={"ok":1}) as fn, mock.patch("builtins.print"):
   self.assertEqual(g.main(["repo","o/r","--transport","urllib"]),0)
   self.assertEqual(fn.call_args.kwargs["transport"],"urllib")
 def test_transport_before_subcommand(self):
  with mock.patch.object(g,"repository",return_value={"ok":1}) as fn, mock.patch("builtins.print"):
   self.assertEqual(g.main(["--transport","gh","repo","o/r"]),0)
   self.assertEqual(fn.call_args.kwargs["transport"],"gh")
 def test_unknown_flag_rejected(self):
  with mock.patch("sys.stderr"):
   self.assertEqual(g.main(["repo","o/r","--wat"]),2)

 def test_verification_failure_preserves_successful_write(self):
  with mock.patch.object(g,"request",return_value=None), mock.patch.object(g,"variable",side_effect=g.Error("timeout")):
   x=g.set_variable("o/r","A","B",write=True)
  self.assertEqual(x["mutation_status"],"succeeded")
  self.assertEqual(x["status"],"verification_failed")

 def test_checks_second_page_failure(self):
  ok={"id":1,"name":"ok","status":"completed","conclusion":"success","html_url":"u"}
  bad={"id":2,"name":"bad","status":"completed","conclusion":"failure","html_url":"u"}
  first={"total_count":101,"check_runs":[ok]*100}; second={"total_count":101,"check_runs":[bad]}
  with mock.patch.object(g,"request",side_effect=[first,second]):
   x=g.checks_for_sha("o/r","a"*40)
  self.assertTrue(x["complete"]); self.assertEqual(x["count"],101); self.assertEqual(x["state"],"failed")
 def test_checks_incomplete_never_green(self):
  ok={"id":1,"name":"ok","status":"completed","conclusion":"success","html_url":"u"}
  with mock.patch.object(g,"request",return_value={"total_count":2,"check_runs":[ok]}):
   self.assertEqual(g.checks_for_sha("o/r","a"*40)["state"],"incomplete")
 def test_comment_write_requires_marker(self):
  with self.assertRaises(ValueError): g.post_comment("o/r",1,"hello",write=True)
 def test_comment_rejects_active_mention(self):
  with self.assertRaises(ValueError): g.post_comment("o/r",1,"hello @bot",marker="<!-- gh-identity:k -->")
 def test_comment_can_sanitize_mention(self):
  with mock.patch.object(g,"pages",return_value=[]):
   x=g.post_comment("o/r",1,"hello @bot",marker="<!-- gh-identity:k -->",sanitize_mentions=True)
  self.assertIn("＠bot",x["body"])

 def test_comment_email_like_text_is_not_mention(self):
  x=g.post_comment("o/r",1,"mail x@y.example")
  self.assertEqual(x["status"],"planned")

 def test_local_identity_prefers_provided_contract(self):
  x=g.local_identity(identity={"sha":"A"*40,"ref":"main","dirty":False,"source":"metadata"})
  self.assertEqual(x["sha"],"a"*40); self.assertEqual(x["source"],"metadata")
 def test_local_identity_uses_github_environment(self):
  x=g.local_identity(env={"GITHUB_SHA":"B"*40,"GITHUB_HEAD_REF":"feature"})
  self.assertEqual((x["sha"],x["ref"],x["source"]),("b"*40,"feature","github-env"))
 def test_local_identity_unavailable_without_git(self):
  with mock.patch.object(g.shutil,"which",return_value=None):
   x=g.local_identity(env={})
  self.assertIsNone(x["sha"]); self.assertEqual(x["source"],"unavailable")
 def test_compare_sha(self):
  x=g.compare_sha({"sha":"C"*40},"c"*40)
  self.assertTrue(x["comparable"]); self.assertTrue(x["same"])
 def test_compare_sha_unknown_is_not_false(self):
  x=g.compare_sha({"sha":None},"d"*40)
  self.assertFalse(x["comparable"]); self.assertIsNone(x["same"])

 def test_source_identity_resolves_commit_then_regular_file(self):
  calls=[]
  def req(method,path,**kwargs):
   calls.append(path)
   if "/commits/" in path:return {"sha":"A"*40}
   if "/git/trees/" in path:return {"tree":[{"path":"dir/a b.py","type":"blob","mode":"100644","sha":"B"*40}]}
   return {"type":"file","sha":"B"*40,"size":123}
  with mock.patch.object(g,"request",side_effect=req):
   x=g.source_identity("o/r","main","dir/a b.py")
  self.assertEqual(x["commit_sha"],"a"*40); self.assertEqual(x["blob_sha"],"b"*40)
  self.assertEqual(x["size"],123); self.assertEqual(x["path"],"dir/a b.py")
  self.assertIn("contents/dir/a%20b.py?ref="+"a"*40,calls[1])
  self.assertIn("git/trees/"+"a"*40+"?recursive=1",calls[2])
 def test_source_identity_rejects_non_regular_source(self):
  def req(method,path,**kwargs):
   return {"sha":"a"*40} if "/commits/" in path else {"type":"symlink","sha":"b"*40,"size":1}
  with mock.patch.object(g,"request",side_effect=req):
   with self.assertRaises(g.Error) as cm:g.source_identity("o/r","main","link")
  self.assertEqual(cm.exception.code,"source_not_regular_file")
 def test_source_identity_rejects_unsafe_path_before_network(self):
  with mock.patch.object(g,"request") as req:
   with self.assertRaises(ValueError):g.source_identity("o/r","main","../secret")
  req.assert_not_called()
 def test_source_identity_rejects_bad_blob_identity(self):
  def req(method,path,**kwargs):
   if "/commits/" in path:return {"sha":"a"*40}
   if "/git/trees/" in path:return {"tree":[{"path":"a.py","type":"blob","mode":"100644","sha":"short"}]}
   return {"type":"file","sha":"short","size":1}
  with mock.patch.object(g,"request",side_effect=req):
   with self.assertRaises(g.Error) as cm:g.source_identity("o/r","main","a.py")
  self.assertEqual(cm.exception.code,"invalid_blob")

 def test_source_identity_rejects_symlink_to_file_contents_shape(self):
  def req(method,path,**kwargs):
   if "/commits/" in path:return {"sha":"a"*40}
   if "/git/trees/" in path:return {"tree":[{"path":"link","type":"blob","mode":"120000","sha":"b"*40}]}
   # GitHub Contents API may expose the target file shape for an in-repo symlink.
   return {"type":"file","sha":"b"*40,"size":1}
  with mock.patch.object(g,"request",side_effect=req):
   with self.assertRaises(g.Error) as cm:g.source_identity("o/r","main","link")
  self.assertEqual(cm.exception.code,"source_not_regular_file")

 def test_source_identity_rejects_submodule_even_if_contents_says_file(self):
  def req(method,path,**kwargs):
   if "/commits/" in path:return {"sha":"a"*40}
   return {"type":"file","sha":"b"*40,"size":1,"submodule_git_url":"https://example.invalid/x"}
  with mock.patch.object(g,"request",side_effect=req):
   with self.assertRaises(g.Error) as cm:g.source_identity("o/r","main","vendor")
  self.assertEqual(cm.exception.code,"source_not_regular_file")

 def test_source_identity_accepts_executable_regular_file(self):
  def req(method,path,**kwargs):
   if "/commits/" in path:return {"sha":"a"*40}
   if "/git/trees/" in path:return {"tree":[{"path":"tool","type":"blob","mode":"100755","sha":"b"*40}]}
   return {"type":"file","sha":"b"*40,"size":7}
  with mock.patch.object(g,"request",side_effect=req):
   self.assertEqual(g.source_identity("o/r","main","tool")["blob_sha"],"b"*40)

 def test_source_identity_rejects_tree_contents_sha_mismatch(self):
  def req(method,path,**kwargs):
   if "/commits/" in path:return {"sha":"a"*40}
   if "/git/trees/" in path:return {"tree":[{"path":"a.py","type":"blob","mode":"100644","sha":"c"*40}]}
   return {"type":"file","sha":"b"*40,"size":1}
  with mock.patch.object(g,"request",side_effect=req):
   with self.assertRaises(g.Error) as cm:g.source_identity("o/r","main","a.py")
  self.assertEqual(cm.exception.code,"source_identity_mismatch")


 def test_pull_request_discovery_walks_older_pages_and_is_bounded(self):
  calls=[]
  def fake(method,path,*args,**kwargs):
   calls.append(path)
   page=int(path.rsplit("page=",1)[1])
   if page==1:return [{"number":n,"title":str(n),"state":"closed","head":{"sha":"a"*40,"ref":"f"},"base":{"sha":"b"*40,"ref":"main"}} for n in range(200,100,-1)]
   return [{"number":100,"title":"100","state":"closed","head":{"sha":"c"*40,"ref":"old"},"base":{"sha":"b"*40,"ref":"main"}}]
  with mock.patch.object(g,"request",side_effect=fake):
   out=g.pull_requests("o/r",state="all",max_items=101,max_pages=3)
  self.assertEqual(out["pull_requests"][-1]["number"],100)
  self.assertEqual(out["pages_fetched"],2);self.assertTrue(out["complete"])
  self.assertEqual(len(calls),2)

 def test_pull_request_discovery_reports_truncation(self):
  batch=[{"number":n,"head":{},"base":{}} for n in range(100)]
  with mock.patch.object(g,"request",return_value=batch):
   out=g.pull_requests("o/r",max_items=50,max_pages=1)
  self.assertEqual(out["count"],50);self.assertFalse(out["complete"]);self.assertTrue(out["truncated"])
  self.assertEqual(out["pages_fetched"],1)

 def test_run_history_finds_older_sha_across_pages(self):
  target="d"*40
  page1=[{"id":n,"head_sha":"a"*40,"head_branch":"main","event":"push"} for n in range(100)]
  page2=[{"id":999,"run_attempt":2,"workflow_id":7,"name":"CI","head_sha":target,"head_branch":"feature","event":"pull_request","status":"completed","conclusion":"success"}]
  def fake(method,path,*args,**kwargs):
   params=urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
   return {"workflow_runs":page1 if params.get("page") == ["1"] else page2}
  with mock.patch.object(g,"request",side_effect=fake):
   out=g.run_history("o/r",head_sha=target,max_items=10,max_pages=3)
  self.assertEqual([x["run_id"] for x in out["runs"]],[999])
  self.assertEqual(out["pages_fetched"],2);self.assertTrue(out["complete"])

 def test_run_history_bound_is_explicit_not_latest_equals_relevant(self):
  batch={"workflow_runs":[{"id":n,"head_sha":"a"*40,"head_branch":"main","event":"push"} for n in range(100)]}
  with mock.patch.object(g,"request",return_value=batch):
   out=g.run_history("o/r",max_items=5,max_pages=1)
  self.assertEqual(out["count"],5);self.assertTrue(out["truncated"]);self.assertFalse(out["complete"])
  self.assertEqual(out["pages_fetched"],1)


class WorkflowRunIdentityTests(unittest.TestCase):
 def test_exact_run_attempt(self):
  payload={"id":123,"workflow_id":456,"run_attempt":2,"head_sha":"a"*40,
           "event":"workflow_dispatch","status":"completed","conclusion":"success",
           "created_at":"2026-10-08T00:00:00Z","html_url":"https://github.com/o/r/actions/runs/123"}
  with mock.patch.object(g,"request",return_value=payload) as req:
   result=g.run("o/r",123,attempt=2)
   self.assertEqual((result["workflow_id"],result["run_id"],result["attempt"]),(456,123,2))
   req.assert_called_once_with("GET","repos/o/r/actions/runs/123/attempts/2",transport="auto",timeout=30)
 def test_attempt_mismatch_is_rejected(self):
  with mock.patch.object(g,"request",return_value={"id":123,"run_attempt":3}):
   with self.assertRaises(g.Error):g.run("o/r",123,attempt=2)
 def test_invalid_ids_rejected_before_network(self):
  with mock.patch.object(g,"request") as req:
   for value in (0,-1,True,"x"):
    with self.assertRaises(ValueError):g.run("o/r",value)
   req.assert_not_called()
 def test_invalid_attempt_has_specific_error(self):
  with self.assertRaisesRegex(ValueError, "invalid attempt identifier"):
   g.run("o/r", 123, attempt=0)
 def test_cli_workflow_and_run(self):
  with mock.patch.object(g,"workflow",return_value={"id":42}) as wf:
   with mock.patch("builtins.print") as out:
    self.assertEqual(g.main(["workflow","o/r","42"]),0)
   wf.assert_called_once()
  with mock.patch.object(g,"run",return_value={"run_id":123}) as rn:
   with mock.patch("builtins.print"):
    self.assertEqual(g.main(["run","o/r","123","--attempt","2"]),0)
   self.assertEqual(rn.call_args.kwargs["attempt"],2)


class JobsTests(unittest.TestCase):
 def test_exact_attempt_and_steps(self):
  data={"total_count":1,"jobs":[{"id":91,"run_id":20,"run_attempt":2,"name":"pytest",
   "status":"completed","conclusion":"success","steps":[{"number":1,"name":"Run tests",
   "status":"completed","conclusion":"success"}]}]}
  with mock.patch.object(g,"request",return_value=data) as req:
   result=g.jobs("o/r",20,attempt=2)
   self.assertEqual(result["jobs"][0]["job_id"],91)
   self.assertEqual(result["jobs"][0]["steps"][0]["number"],1)
   self.assertEqual(req.call_args.args,("GET","repos/o/r/actions/runs/20/attempts/2/jobs?per_page=100&page=1"))
   self.assertEqual(req.call_args.kwargs["transport"],"auto")
   self.assertTrue(0 < req.call_args.kwargs["timeout"] <= 30)
 def test_incomplete_response_fails(self):
  with mock.patch.object(g,"request",return_value={"total_count":2,"jobs":[]}):
   with self.assertRaises(g.Error):g.jobs("o/r",20)
 def test_invalid_job_lookup_rejected(self):
  with mock.patch.object(g,"request") as req:
   with self.assertRaises(ValueError):g.jobs("o/r",0)
   req.assert_not_called()


class DiscoveryBoundaryTests(unittest.TestCase):
 def test_page_and_item_bounds(self):
  cases=[
   # batch sizes, max_items, max_pages, returned count, fetched pages, complete
   ([100],200,1,100,1,False),
   ([100,100],300,2,200,2,False),
   ([100],50,3,50,1,False),
   ([100],100,3,100,1,False),
   ([100,100],101,3,101,2,False),
   ([100,0],101,3,100,2,True),
   ([99],100,3,99,1,True),
   ([0],100,3,0,1,True),
   ([99],50,3,50,1,False),
   ([50],50,3,50,1,True),
  ]
  for discover in (g.pull_requests,g.run_history):
   for sizes,items,pages,count,fetched,complete in cases:
    with self.subTest(api=discover.__name__,sizes=sizes,items=items,pages=pages):
     payloads=[]
     for size in sizes:
      rows=[{"number":n,"id":n} for n in range(size)]
      payloads.append(rows if discover is g.pull_requests else {"workflow_runs":rows})
     with mock.patch.object(g,"request",side_effect=payloads) as request:
      result=discover("o/r",max_items=items,max_pages=pages,transport="urllib",timeout=7)
     self.assertEqual(result["count"],count)
     self.assertEqual(result["pages_fetched"],fetched)
     self.assertEqual(request.call_count,fetched)
     self.assertEqual(result["complete"],complete)
     self.assertEqual(result["truncated"],not complete)
     for page,call in enumerate(request.call_args_list,1):
      params=urllib.parse.parse_qs(urllib.parse.urlsplit(call.args[1]).query)
      self.assertEqual(params["page"],[str(page)])
      self.assertEqual(params["per_page"],["100"])
      self.assertEqual(call.kwargs["transport"],"urllib")
      self.assertTrue(0 < call.kwargs["timeout"] <= 7)

 def test_filter_mismatch_counts_actual_requests(self):
  row={"id":1,"head_sha":"a"*40,"head_branch":"main","event":"push"}
  filters=({"head_sha":"b"*40},{"branch":"feature"},{"event":"pull_request"})
  for filter_ in filters:
   for sizes,pages,fetched,complete in (([100],1,1,False),([100,100],2,2,False),([100,0],3,2,True),([99],1,1,True)):
    with self.subTest(filter=filter_,sizes=sizes):
     with mock.patch.object(g,"request",side_effect=[{"workflow_runs":[row]*n} for n in sizes]) as request:
      result=g.run_history("o/r",max_items=1,max_pages=pages,**filter_)
     self.assertEqual(result["runs"],[])
     self.assertEqual(result["pages_fetched"],fetched)
     self.assertEqual(request.call_count,fetched)
     self.assertEqual(result["complete"],complete)
     self.assertEqual(result["truncated"],not complete)


class StepPermalinkTests(unittest.TestCase):
 def observation(self,attempt=2):
  return {"schema":"gh-identity-jobs/1","repository":"o/r","run_id":20,"attempt":attempt,
   "complete":True,"count":1,"jobs":[{"job_id":91,"run_id":20,"attempt":2,
   "steps":[{"number":7,"name":"Tests","status":"completed","conclusion":"failure"}]}]}

 def test_pure_link_with_optional_line(self):
  with mock.patch.object(g,"request") as request:
   self.assertEqual(g.step_url("o/r",20,91,7,1),"https://github.com/o/r/actions/runs/20/job/91#step:7:1")
   self.assertEqual(g.step_url("o/r","20","91","7"),"https://github.com/o/r/actions/runs/20/job/91#step:7")
  request.assert_not_called()

 def test_repository_segments_are_encoded(self):
  self.assertEqual(g.step_url("日本/リポ",20,91,7),"https://github.com/%E6%97%A5%E6%9C%AC/%E3%83%AA%E3%83%9D/actions/runs/20/job/91#step:7")
  for repository in ("o/r?x", "o/r#x", "o/r/extra", "o r/r"):
   with self.subTest(repo=repository),self.assertRaises(ValueError):g.step_url(repository,20,91,7)

 def test_invalid_ids_fail_without_network(self):
  for field in range(4):
   for value in (0,-1,True,False,None,1.5,"1.5","1/2","１","+1"," 1",[]):
    args=[20,91,7,1];args[field]=value
    if field==3 and value is None:continue
    with self.subTest(field=field,value=value),mock.patch.object(g,"request") as request:
     with self.assertRaises(ValueError):g.step_url("o/r",*args)
     request.assert_not_called()

 def test_observed_step_number_not_list_position(self):
  self.assertEqual(g.step_url_from_jobs(self.observation(),91,7,1),g.step_url("o/r",20,91,7,1))
  with self.assertRaises(g.Error) as cm:g.step_url_from_jobs(self.observation(),91,1)
  self.assertEqual(cm.exception.code,"step_not_found")

 def test_exact_rerun_job_is_required(self):
  obs=self.observation();obs["jobs"][0]["attempt"]=1
  with self.assertRaises(g.Error) as cm:g.step_url_from_jobs(obs,91,7)
  self.assertEqual(cm.exception.code,"attempt_mismatch")
  obs=self.observation();obs["jobs"][0]["run_id"]=21
  with self.assertRaises(g.Error) as cm:g.step_url_from_jobs(obs,91,7)
  self.assertEqual(cm.exception.code,"job_identity_mismatch")

 def test_absent_ambiguous_or_incomplete_data_has_no_link(self):
  cases=[]
  obs=self.observation();obs["count"]=2;cases.append((obs,"incomplete_job_observation"))
  obs=self.observation();obs["jobs"][0]["steps"][0]["number"]=7.0;cases.append((obs,"step_not_found"))
  obs=self.observation();obs["complete"]=False;cases.append((obs,"incomplete_job_observation"))
  obs=self.observation();obs["jobs"]=[];obs["count"]=0;cases.append((obs,"step_not_found"))
  obs=self.observation();obs["jobs"][0]["steps"]=[];cases.append((obs,"step_not_found"))
  obs=self.observation();obs["jobs"].append(dict(obs["jobs"][0]));obs["count"]=2;cases.append((obs,"ambiguous_job_identity"))
  obs=self.observation();obs["jobs"][0]["steps"]*=2;cases.append((obs,"ambiguous_step_identity"))
  obs=self.observation();obs["jobs"][0].pop("run_id");cases.append((obs,"job_identity_mismatch"))
  obs=self.observation();obs["jobs"][0].pop("steps");cases.append((obs,"invalid_json"))
  for obs,error in cases:
   with self.subTest(error=error),mock.patch.object(g,"step_url") as builder:
    with self.assertRaises(g.Error) as cm:g.step_url_from_jobs(obs,91,7)
    self.assertEqual(cm.exception.code,error);builder.assert_not_called()

 def test_jobs_observation_connects_to_permalink(self):
  data={"total_count":1,"jobs":[{"id":91,"run_id":20,"run_attempt":2,
   "steps":[{"number":7,"name":"Tests","status":"completed","conclusion":"failure"}]}]}
  with mock.patch.object(g,"request",return_value=data):obs=g.jobs("o/r",20,attempt=2)
  self.assertEqual(g.step_url_from_jobs(obs,91,7,1),g.step_url("o/r",20,91,7,1))

 def test_cli_is_offline_json(self):
  with mock.patch.object(g,"request") as request,mock.patch("builtins.print") as out:
   self.assertEqual(g.main(["step-url","o/r","20","91","7","--line","1"]),0)
  request.assert_not_called()
  self.assertEqual(json.loads(out.call_args.args[0]),{"schema":"gh-identity-step-url/1","url":g.step_url("o/r",20,91,7,1)})
  with mock.patch("sys.stderr"):
   self.assertEqual(g.main(["step-url","o/r","20","91","0"]),2)


class HabitsAndConsolidationTests(unittest.TestCase):
 def test_pr_files_filter_and_bounded_patch(self):
  batch=[
   {"filename":"src/a.py","status":"modified","additions":3,"deletions":1,"changes":4,"sha":"a"*40,"patch":"@@ -1 +1,3 @@\n-old\n+new1\n+new2\n+new3"},
   {"filename":"docs/readme.md","status":"added","additions":2,"deletions":0,"changes":2,"sha":"b"*40,"patch":"@@ -0,0 +1,2 @@\n+hi"}
  ]
  with mock.patch.object(g,"request",return_value=batch):
   out=g.pr_files("o/r",12,path="*.py",include_patch=True,patch_bytes=20)
  self.assertEqual(out["schema"],"gh-identity-pr-files/1")
  self.assertTrue(out["complete"])
  self.assertEqual(out["count"],1)
  self.assertEqual(out["files"][0]["filename"],"src/a.py")
  self.assertTrue(out["files"][0]["patch_truncated"])
  self.assertLessEqual(len(out["files"][0]["patch"].encode("utf-8")),20)
  rendered=g.render_pr_files(out)
  self.assertIn("o/r#12 files=1 filter=*.py",rendered)
  self.assertIn("... (patch truncated)",rendered)

 def test_pr_files_cli_and_duplicate_filename_rejected(self):
  batch=[{"filename":"a.py","status":"modified","additions":1,"deletions":0,"changes":1,"sha":"a"*40}]
  with mock.patch.object(g,"request",return_value=batch),mock.patch("builtins.print") as out:
   self.assertEqual(g.main(["pr-files","o/r","5","--text"]),0)
  self.assertIn("a.py",out.call_args.args[0])
  with mock.patch.object(g,"request",return_value=batch*2):
   with self.assertRaises(g.Error) as cm:g.pr_files("o/r",5)
  self.assertEqual(cm.exception.code,"invalid_pr_files_response")

 def test_remote_file_resolves_ref_and_verifies_blob(self):
  content=b"hello world\n"
  blob_sha=g.git_blob_sha(content)
  def req(method,path,**kw):
   if "/commits/" in path:return {"sha":"c"*40}
   if "/contents/" in path:return {"type":"file","sha":blob_sha,"size":len(content)}
   if "/git/blobs/" in path:return {"encoding":"base64","content":g.base64.b64encode(content).decode("ascii")}
   raise AssertionError(path)
  with mock.patch.object(g,"request",side_effect=req):
   out=g.remote_file("o/r","dir/hello.txt",ref="main",transport="gh")
  self.assertEqual(out["schema"],"gh-identity-file/1")
  self.assertEqual(out["commit"],"c"*40)
  self.assertEqual(out["blob_sha"],blob_sha)
  self.assertFalse(out["binary"])
  self.assertEqual(out["text"],"hello world\n")
  self.assertEqual(g.render_remote_file(out),"hello world\n")
  with mock.patch.object(g,"request",side_effect=req),mock.patch("builtins.print") as printed:
   self.assertEqual(g.main(["file","o/r","dir/hello.txt","--ref","main","--transport","gh","--text"]),0)
  self.assertEqual(printed.call_args.args[0],"hello world\n")

 def test_compare_refs_and_fork_diff_delegation(self):
  payload={
   "status":"ahead","ahead_by":2,"behind_by":1,"total_commits":2,
   "base_commit":{"sha":"a"*40},"merge_base_commit":{"sha":"b"*40},
   "commits":[{"sha":"c"*40,"commit":{"message":"feat: first\n\nbody","committer":{"date":"2026-10-01T00:00:00Z"}},"author":{"login":"alice"}}],
   "files":[{"filename":"gh_identity.py","status":"modified","additions":10,"deletions":2,"changes":12,"sha":"d"*40}],
   "html_url":"https://github.com/o/r/compare/main...feat"
  }
  with mock.patch.object(g,"request",return_value=payload):
   out=g.compare_refs("o/r","main","feat")
   fork=g._repo_fork_diff("me/r",{"fork":True,"default_branch":"feat","parent":{"full_name":"o/r","default_branch":"main"}})
  self.assertEqual(out["schema"],"gh-identity-compare/1")
  self.assertEqual((out["ahead_by"],out["behind_by"],out["status"]),(2,1,"ahead"))
  self.assertEqual(out["commits"][0]["title"],"feat: first")
  self.assertEqual(out["files"][0]["filename"],"gh_identity.py")
  self.assertIn("commit cccccccc feat: first",g.render_compare(out))
  self.assertEqual((fork["ahead_by"],fork["behind_by"],fork["status"]),(2,1,"ahead"))

 def test_pr_checks_steps_enriches_failed_steps_and_annotations(self):
  def req(method,path,**kw):
   if path=="repos/o/r/pulls/3":return {"head":{"sha":"a"*40},"base":{"sha":"b"*40}}
   if "check-runs?per_page=100" in path:
    return {"total_count":1,"check_runs":[{"id":55,"name":"unit","status":"completed","conclusion":"failure"}]}
   if path=="repos/o/r/actions/jobs/55":
    return {"steps":[{"name":"Checkout","conclusion":"success"},{"name":"pytest","conclusion":"failure"}]}
   if path=="repos/o/r/check-runs/55/annotations":
    return [{"annotation_level":"failure","path":"tests/test_x.py","start_line":42,"message":"assert 1 == 2"}]
   raise AssertionError(path)
  with mock.patch.object(g,"request",side_effect=req):
   out=g.pr_checks(["o/r#3"],steps=True)
  self.assertEqual(out["state"],"failed")
  att=out["rows"][0]["attention"][0]
  self.assertEqual(att["failed_steps"],["pytest"])
  self.assertEqual(att["annotations"][0]["path"],"tests/test_x.py")
  text=g.render_pr_checks(out)
  self.assertIn("  step: pytest",text)
  self.assertIn("  failure: tests/test_x.py:42 assert 1 == 2",text)

 def test_search_code_kind_and_render(self):
  payload={
   "total_count":1,"incomplete_results":False,
   "items":[{"name":"gh_identity.py","path":"src/gh_identity.py","sha":"A"*40,
             "html_url":"https://github.com/o/r/blob/main/src/gh_identity.py",
             "repository":{"full_name":"o/r"}}]
  }
  with mock.patch.object(g,"request",return_value=payload) as req:
   out=g.search("def remote_file repo:o/r",kind="code")
  self.assertIn("search/code?",req.call_args.args[1])
  self.assertEqual(out["kind"],"code")
  self.assertEqual(out["sort"],"best-match")
  self.assertEqual(out["items"][0],{"repository":"o/r","kind":"code","path":"src/gh_identity.py","name":"gh_identity.py","sha":"a"*40,"url":"https://github.com/o/r/blob/main/src/gh_identity.py"})
  self.assertEqual(g.render_search(out),"o/r:src/gh_identity.py aaaaaaaa")

 def test_search_repo_kind_and_render(self):
  payload={
   "total_count":1,"incomplete_results":False,
   "items":[{"id":99,"full_name":"o/r","private":False,"visibility":"public","archived":False,
             "default_branch":"main","description":"Bio tool","html_url":"https://github.com/o/r",
             "stargazers_count":12,"forks_count":3,"language":"Python","pushed_at":"2026-10-01T00:00:00Z",
             "fork":False,"license":{"spdx_id":"MIT"}}]
  }
  with mock.patch.object(g,"request",return_value=payload) as req:
   out=g.search("org:o topic:bio",kind="repo",sort="stars")
  self.assertIn("search/repositories?",req.call_args.args[1])
  self.assertEqual(out["kind"],"repo")
  self.assertEqual(out["sort"],"stars")
  self.assertEqual(out["items"][0]["repository"],"o/r")
  self.assertEqual(out["items"][0]["stars"],12)
  self.assertEqual(out["items"][0]["license"],"MIT")
  self.assertEqual(g.render_search(out),"o/r stars=12 forks=3 lang=Python Bio tool")

 def test_workflows_and_render(self):
  payload={"total_count":2,"workflows":[
   {"id":10,"name":"CI","path":".github/workflows/ci.yml","state":"active","html_url":"https://github.com/o/r/actions/workflows/ci.yml"},
   {"id":20,"name":"Nightly","path":".github/workflows/nightly.yml","state":"disabled_manually","html_url":"https://github.com/o/r/actions/workflows/nightly.yml"}
  ]}
  with mock.patch.object(g,"request",return_value=payload):
   out=g.workflows("o/r")
  self.assertEqual(out["schema"],"gh-identity-workflows/1")
  self.assertTrue(out["complete"])
  self.assertEqual(out["count"],2)
  self.assertIn("10 active .github/workflows/ci.yml CI",g.render_workflows(out))

 def test_failed_job_logs_and_render(self):
  def req(method,path,**kw):
   if path=="repos/o/r/actions/runs/500":
    return {"id":500,"workflow_id":10,"run_attempt":2,"head_sha":"a"*40,"event":"pull_request","status":"completed","conclusion":"failure"}
   if path=="repos/o/r/actions/runs/500/attempts/2/jobs?per_page=100&page=1":
    return {"total_count":2,"jobs":[
     {"id":70,"run_id":500,"run_attempt":2,"head_sha":"a"*40,"name":"lint","status":"completed","conclusion":"success","steps":[{"number":1,"name":"ruff","status":"completed","conclusion":"success"}]},
     {"id":71,"run_id":500,"run_attempt":2,"head_sha":"a"*40,"name":"pytest","status":"completed","conclusion":"failure","html_url":"https://github.com/o/r/actions/runs/500/job/71","steps":[
      {"number":1,"name":"Checkout","status":"completed","conclusion":"success"},
      {"number":2,"name":"Run tests","status":"completed","conclusion":"failure"}
     ]}
    ]}
   raise AssertionError(path)
  log_res={"complete":True,"truncated":False,"text":"line 1\nline 2\nFAIL: test_foo\nAssertionError: boom\n","text_sha256":"f"*64,"bytes_read":50,"redaction":{"applied":True,"replacements":0}}
  with mock.patch.object(g,"request",side_effect=req),mock.patch.object(g,"_download_job_log",return_value=log_res) as dl:
   out=g.failed_job_logs("o/r",500,tail_lines=2)
  dl.assert_called_once()
  self.assertEqual(out["schema"],"gh-identity-failed-logs/1")
  self.assertEqual(out["attempt"],2)
  self.assertEqual(out["failed_job_count"],1)
  self.assertTrue(out["complete"])
  job=out["jobs"][0]
  self.assertEqual(job["job_id"],71)
  self.assertEqual(job["failed_steps"][0]["name"],"Run tests")
  self.assertEqual(job["failed_steps"][0]["url"],"https://github.com/o/r/actions/runs/500/job/71#step:2")
  self.assertTrue(job["tail_truncated"])
  self.assertEqual(job["tail"],"FAIL: test_foo\nAssertionError: boom")
  rendered=g.render_failed_job_logs(out)
  self.assertIn("=== job 71 pytest (failure) steps=[Run tests] ===",rendered)
  self.assertIn("AssertionError: boom",rendered)

 def test_render_runs_and_jobs(self):
  runs_obs={"schema":"gh-identity-runs/1","repository":"o/r","runs":[{"run_id":12,"attempt":1,"name":"CI","head_branch":"main","status":"completed","conclusion":"success","head_sha":"a"*40,"event":"push"}]}
  self.assertEqual(g.render_runs(runs_obs),"12 #1 success push aaaaaaaa main CI")
  jobs_obs={"schema":"gh-identity-jobs/1","repository":"o/r","run_id":12,"jobs":[{"job_id":71,"name":"pytest","status":"completed","conclusion":"failure","steps":[{"number":1,"name":"Checkout","status":"completed","conclusion":"success"},{"number":2,"name":"Run pytest","status":"completed","conclusion":"failure"}]}]}
  self.assertEqual(g.render_jobs(jobs_obs),"71 failure pytest\n  step 2: failure Run pytest")


# ──── capabilities_versions ───────────────────────────────────────

def test_capabilities_versions_present(monkeypatch):
    monkeypatch.setattr(g, "tool_versions", lambda commands: {"git": "2.47.1", "gh": "2.65.0"})
    monkeypatch.setattr(g, "gh_available", lambda: True)
    monkeypatch.setattr(g.shutil, "which", lambda command: "/usr/bin/" + command)
    out = g.capabilities()
    assert out["schema"] == "gh-identity-capabilities/1"
    assert out["git"] is True and out["gh"] is True
    assert out["git_version"] == "2.47.1"
    assert out["gh_version"] == "2.65.0"


def test_capabilities_missing_versions_are_null(monkeypatch):
    monkeypatch.setattr(g, "tool_versions", lambda commands: {})
    monkeypatch.setattr(g, "gh_available", lambda: False)
    monkeypatch.setattr(g.shutil, "which", lambda command: None)
    out = g.capabilities()
    assert out["git"] is False and out["gh"] is False
    assert out["git_version"] is None and out["gh_version"] is None


def test_capabilities_version_probe_is_bounded_and_nonfatal(monkeypatch):
    class Result:
        returncode = 1
        stdout = ""
        stderr = "error"

    monkeypatch.setattr(g.shutil, "which", lambda command: "/bin/" + command)
    monkeypatch.setattr(g, "gh_available", lambda: True)
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return Result()

    monkeypatch.setattr(g.subprocess, "run", run)
    out = g.capabilities()
    assert out["git_version"] is None and out["gh_version"] is None
    assert len(calls) == 2
    assert all(kwargs["timeout"] == 5.0 for _, kwargs in calls)
    assert all(kwargs["check"] is False for _, kwargs in calls)


def test_capabilities_malformed_and_timeout_nonfatal(monkeypatch):
    monkeypatch.setattr(g.shutil, "which", lambda command: "/bin/" + command)
    monkeypatch.setattr(g, "gh_available", lambda: True)

    def run(args, **kwargs):
        if args[0].endswith("/git"):
            raise g.subprocess.TimeoutExpired(args, kwargs["timeout"])
        return g.subprocess.CompletedProcess(args, 0, "unexpected version", "")

    monkeypatch.setattr(g.subprocess, "run", run)
    out = g.capabilities()
    assert out["git_version"] is None and out["gh_version"] is None


# ──── cli_exception_merge ─────────────────────────────────────────

# Preserve CLI error contracts when content and local Git features combine.


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (g.GitInspectionError("git failed"), "git_inspection_failed"),
        (OSError("cannot read saved HTML"), "invalid_argument"),
        (ValueError("invalid input"), "invalid_argument"),
        (g.Error("content_unavailable"), "content_unavailable"),
    ],
)
def test_cli_reports_merged_exception_types(monkeypatch, capsys, error, expected):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(g, "_html_content_file", fail)
    assert g.main(["html-content", "saved.html", "--selector", "#body"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"status": "error", "error": expected}


# ──── operation_limits ────────────────────────────────────────────

# Operation budgets, injected pagination and real bounded transport tests.


SHA = 'a' * 40


PATH = f'repos/o/r/commits/{SHA}/check-runs'


def row(n=1, conclusion='success'):
    return {'id': n, 'name': 'unit', 'status': 'completed', 'conclusion': conclusion,
            'html_url': f'https://github.com/o/r/check/{n}', 'output': {'annotations_count': n}}


def payload(rows, total=None):
    return {'check_runs': rows, 'total_count': len(rows) if total is None else total}


@pytest.mark.parametrize('max_pages,max_items,error', [(1, 1000, 'pages_limit'), (10, 99, 'items_limit')])
def test_never_ending_pagination_stops_before_false_green(max_pages, max_items, error):
    def get(*args, **kwargs):
        return payload([row(n) for n in range(100)], 10000)
    with g.operation(max_pages=max_pages, max_items=max_items):
        with pytest.raises(g.Error, match=error):
            g.checks_for_sha('o/r', SHA, requester=get)


def test_pages_limit_is_shared_across_calls():
    with g.operation(max_pages=1), patch.object(g, 'request', return_value=[]):
        assert g.pages('repos/o/r/issues') == []
        with pytest.raises(g.Error, match='pages_limit'):
            g.comments('o/r', 1)


def test_item_limit_counts_filtered_out_history():
    batch = {'workflow_runs': [{'head_sha': 'b' * 40}] * 100}
    with g.operation(max_items=99), patch.object(g, 'request', return_value=batch):
        with pytest.raises(g.Error, match='items_limit'):
            g.run_history('o/r', head_sha=SHA)


def test_operation_deadline_not_reset_per_page():
    clock = [100.0]
    timeouts = []
    def get(*args, **kwargs):
        timeouts.append(kwargs['timeout'])
        clock[0] += .6
        return [1] * 100
    with patch.object(g.time, 'monotonic', side_effect=lambda: clock[0]):
        with g.operation(timeout=1), pytest.raises(g.Error, match='operation_timeout'):
            g.pages('repos/o/r/issues', requester=get)
    assert timeouts == pytest.approx([1, .4])


@pytest.mark.parametrize('limits', [{'max_pages': 0}, {'max_items': True}, {'max_bytes': -1},
    {'timeout': 0}, {'timeout': float('nan')}, {'timeout': float('inf')}])
def test_invalid_budget(limits):
    with pytest.raises(ValueError):
        with g.operation(**limits):
            pytest.fail('invalid limits accepted')


def test_nested_operation_cannot_reset_limits():
    with g.operation(max_pages=1), pytest.raises(ValueError):
        with g.operation():
            pass


def test_link_pagination_keeps_injectable_client_and_normalized_identity():
    # Legacy Client adapters expose data/headers, preserving offline injection.
    responses = [
        g.Page(payload([row(1)], 2), {'Link': f'<https://api.github.com/{PATH}?per_page=100&page=2>; rel="next"'}),
        g.Page(payload([row(2, 'failure')], 2), {'link': ''}),
    ]
    with patch.object(g, 'request', side_effect=AssertionError('default transport used')):
        with patch('builtins.print'):
            calls = []
            def injected(method, path, **kwargs):
                calls.append((method, path))
                return responses.pop(0)
            result = g.checks_for_sha('o/r', SHA, requester=injected)
    assert result['complete'] is True
    assert result['state'] == 'failed'
    assert [r['id'] for r in result['checks']] == [1, 2]
    assert [r['annotations_count'] for r in result['checks']] == [1, 2]
    assert calls[-1] == ('GET', PATH + '?per_page=100&page=2')


def test_101_rows_without_link_uses_numbered_pagination():
    batches = [payload([row(n) for n in range(100)], 101), payload([row(101, 'failure')], 101)]
    with patch.object(g, 'request', side_effect=batches):
        result = g.checks_for_sha('o/r', SHA)
    assert result['count'] == 101 and result['state'] == 'failed'


@pytest.mark.parametrize('target', [
    'https://evil.invalid/anything?per_page=100&page=2',
    f'https://api.github.com/{PATH}?per_page=100&page=1',
    f'https://api.github.com/{PATH}?per_page=100&page=3',
    f'https://api.github.com/{PATH}?per_page=100&page=2&other=1',
])
def test_unsafe_or_cyclic_link_rejected_before_followup(target):
    calls = []
    def get(*args, **kwargs):
        calls.append(args)
        return g.Page(payload([row()], 2), {'link': f'<{target}>; rel="next"'})
    with pytest.raises(g.Error, match='invalid_pagination_link'):
        g.checks_for_sha('o/r', SHA, requester=get)
    assert len(calls) == 1


@pytest.mark.parametrize('total', [None, True, -1, '1'])
def test_missing_or_invalid_total_cannot_be_complete(total):
    with pytest.raises(g.Error, match='pagination_incomplete'):
        g.checks_for_sha('o/r', SHA, requester=lambda *a, **k: payload([row()], total) if total is not None else {'check_runs': [row()]})


def test_changed_total_rejected():
    batches = [payload([row(n) for n in range(100)], 101), payload([row(101)], 102)]
    with patch.object(g, 'request', side_effect=batches), pytest.raises(g.Error, match='pagination_incomplete'):
        g.checks_for_sha('o/r', SHA)


def test_short_incomplete_response_is_not_green():
    result = g.checks_for_sha('o/r', SHA, requester=lambda *a, **k: payload([row()], 2))
    assert result['state'] == 'incomplete' and not result['complete']


def test_injected_http_error_propagates_without_partial_success():
    def get(*a, **k):
        raise g.Error('permission_or_rate_limit')
    with pytest.raises(g.Error, match='permission_or_rate_limit'):
        g.checks_for_sha('o/r', SHA, requester=get)


def test_injected_payload_bytes_are_counted():
    with g.operation(max_bytes=10), pytest.raises(g.Error, match='bytes_limit'):
        g.pages('repos/o/r/issues', requester=lambda *a, **k: ['long' * 20])


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_real_process_overflow_stops_both_pipes(stream):
    code = f'import sys,time;sys.{stream}.buffer.write(b"x"*1000000);sys.{stream}.flush();time.sleep(10)'
    started = time.monotonic()
    with g.operation(max_bytes=128, timeout=3), pytest.raises(g.Error, match='bytes_limit'):
        g._process([sys.executable, '-c', code], None, 3)
    assert time.monotonic() - started < 3


def test_real_process_timeout_marks_write_uncertain():
    with g.operation(timeout=.4), pytest.raises(g.Error) as exc:
        g._process([sys.executable, '-c', 'import time;time.sleep(10)'], None, 10, mutating=True)
    assert exc.value.code == 'operation_timeout' and exc.value.uncertain


def test_byte_budget_is_cumulative_across_processes():
    with g.operation(max_bytes=3):
        assert g._process([sys.executable, '-c', 'print(1)'], None, 3)[1] == b'1\n'
        with pytest.raises(g.Error, match='bytes_limit'):
            g._process([sys.executable, '-c', 'print(1)'], None, 3)


@pytest.mark.parametrize('output,expected', [('print("{}")', {}), ('print("日本語")', 'invalid_json')])
def test_gh_actual_pipe_decode(output, expected):
    real = g._process
    def fake_gh(argv, stdin, timeout, env=None, mutating=False):
        assert argv[:4] == ['gh', 'api', '--method', 'GET']
        assert env['GH_PROMPT_DISABLED'] == '1' and 'GH_REPO' not in env
        return real([sys.executable, '-c', output], stdin, timeout, env, mutating)
    with patch.object(g, '_process', side_effect=fake_gh):
        if isinstance(expected, str):
            with pytest.raises(g.Error, match=expected):g._gh('GET', 'repos/o/r')
        else:assert g._gh('GET', 'repos/o/r') == expected


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):pass
    def do_GET(self):
        if self.path == '/slow':time.sleep(2)
        status = int(self.path[1:]) if self.path[1:].isdigit() else 200
        body = b'x' * 10000 if self.path == '/large' else b'{"ok":true}'
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):pass


@pytest.fixture(scope='module')
def local_http():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}'
    server.shutdown();server.server_close();thread.join()


def test_real_urllib_worker_and_json(local_http):
    with patch.object(g, 'API', local_http), patch.object(g, 'token', return_value=None):
        assert g.request('GET', 'ok', transport='urllib') == {'ok': True}


@pytest.mark.parametrize('status,code', [(401, 'authentication_required'), (403, 'permission_or_rate_limit'),
    (404, 'not_found_or_inaccessible'), (422, 'rejected'), (500, 'server_error')])
def test_urllib_http_error_mapping(local_http, status, code):
    with patch.object(g, 'API', local_http), patch.object(g, 'token', return_value=None):
        with pytest.raises(g.Error, match=code):g.request('GET', str(status), transport='urllib')


def test_urllib_response_capacity(local_http):
    with patch.object(g, 'API', local_http), patch.object(g, 'token', return_value=None):
        with g.operation(max_bytes=128), pytest.raises(g.Error, match='bytes_limit'):
            g.request('GET', 'large', transport='urllib')


def test_urllib_hard_wall_deadline(local_http):
    started = time.monotonic()
    with patch.object(g, 'API', local_http), patch.object(g, 'token', return_value=None):
        with g.operation(timeout=.5), pytest.raises(g.Error):
            g.request('GET', 'slow', transport='urllib')
    assert time.monotonic() - started < 1.8


def test_cli_limit_is_applied_without_success_output():
    with patch.object(g, 'request', return_value=[{}] * 100):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            assert g.main(['comments', 'o/r', '1', '--max-pages', '1']) == 2
    assert out.getvalue() == ''
    assert json.loads(err.getvalue())['error'] == 'pages_limit'


def test_explicit_headers_without_next_finish_exactly_full_page():
    calls = []
    def get(*args, **kwargs):
        calls.append(args)
        return g.Page(payload([row(n) for n in range(100)]), {})
    with g.operation(max_pages=1):
        result = g.checks_for_sha('o/r', SHA, requester=get)
    assert result['complete'] and result['count'] == 100 and len(calls) == 1


def test_explicit_link_end_with_count_mismatch_is_incomplete():
    result = g.checks_for_sha('o/r', SHA,
        requester=lambda *a, **k: g.Page(payload([row()], 2), {}))
    assert result['state'] == 'incomplete'


def test_auto_fallback_shares_deadline_and_bytes():
    seen = []
    def gh(*a):
        g._BUDGET.get().charge('bytes', 5)
        raise g.Error('authentication_required')
    def url(*a):
        seen.append(g._BUDGET.get().bytes)
        g._BUDGET.get().charge('bytes', 6)
    with patch.object(g, 'gh_available', return_value=True), patch.object(g, '_gh', side_effect=gh), patch.object(g, '_url', side_effect=url):
        with g.operation(max_bytes=10), pytest.raises(g.Error, match='bytes_limit'):
            g.request('GET', 'repos/o/r')
    assert seen == [5]


@pytest.mark.parametrize('timeout', [-1, True, float('nan'), float('inf')])
def test_nested_request_validates_timeout_before_transport(timeout):
    with g.operation(), patch.object(g, '_gh') as transport:
        with pytest.raises(ValueError):
            g.request('GET', 'repos/o/r', timeout=timeout)
        transport.assert_not_called()


@pytest.mark.parametrize('limits,data,error', [
    ({'max_items': 1}, [1, 2], 'items_limit'),
    ({'max_bytes': 1}, [1], 'bytes_limit'),
    ({'max_pages': 1}, [1] * 100, 'pages_limit'),
])
def test_caught_collection_overflow_blocks_all_further_transport(limits, data, error):
    with g.operation(**limits):
        with pytest.raises(g.Error, match=error):
            g.pages('repos/o/r/issues', requester=lambda *a, **k: data)
        with patch.object(g, '_gh') as transport:
            with pytest.raises(g.Error, match=error) as exc:
                g.request('POST', 'repos/o/r/issues', transport='gh', mutating=True)
            assert not exc.value.uncertain  # This second mutation never started.
            transport.assert_not_called()
    with g.operation(), patch.object(g, '_gh', return_value={}) as transport:
        assert g.request('GET', 'repos/o/r', transport='gh') == {}
        transport.assert_called_once()


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_caught_pipe_overflow_blocks_new_process(stream):
    with g.operation(max_bytes=16):
        with pytest.raises(g.Error, match='bytes_limit') as exc:
            g._process([sys.executable, '-c', f'import sys;sys.{stream}.write("x"*100)'], None, 3, mutating=True)
        assert exc.value.uncertain
        with patch.object(g.subprocess, 'Popen') as spawn:
            with pytest.raises(g.Error, match='bytes_limit'):
                g._process([sys.executable, '-c', 'pass'], None, 3)
            spawn.assert_not_called()


def test_caught_worker_overflow_blocks_new_transport(local_http):
    with patch.object(g, 'API', local_http), patch.object(g, 'token', return_value=None):
        with g.operation(max_bytes=128):
            with pytest.raises(g.Error, match='bytes_limit'):
                g.request('GET', 'large', transport='urllib')
            with patch.object(g, '_process') as process:
                with pytest.raises(g.Error, match='bytes_limit'):
                    g.request('GET', 'ok', transport='urllib')
                process.assert_not_called()


# ──── benchmark_validation ────────────────────────────────────────

# Ignore-filter benchmark (synthetic, no GitHub requests); fleet_check.run_main runs it under -S and -S -O.


def ignore_filter_baseline(checks, ignore):
    return [c for c in checks if c['name'] not in set(ignore)]


def ignore_filter_indexed(checks, ignore):
    ignored = set(ignore)
    return [c for c in checks if c['name'] not in ignored]


def ignore_filter_benchmark():
    """Time both filters on 5000 checks / 1000 ignored names; print JSON. Exits on a result mismatch."""
    checks = [{'name': str(i)} for i in range(5000)]
    ignore = [str(i) for i in range(1000)]
    expected = ignore_filter_baseline(checks, ignore)
    samples = {'baseline': [], 'indexed': []}
    for repeat in range(8):
        for name in (list(samples) if repeat % 2 else list(reversed(samples))):
            start = time.perf_counter_ns()
            result = (ignore_filter_baseline if name == 'baseline' else ignore_filter_indexed)(checks, ignore)
            elapsed = time.perf_counter_ns() - start
            if not (result == expected):
                raise SystemExit('benchmark_ignore_filter: result mismatch')
            if repeat:
                samples[name].append(elapsed)
    print(json.dumps({'python': sys.version, 'checks': 5000, 'ignored_names': 1000, 'samples_ns': samples,
                      'median_ns': {k: statistics.median(v) for k, v in samples.items()}}, indent=2))


main = ignore_filter_benchmark  # the entry fleet_check.run_main calls


# ──── library_budget_and_git_runner ───────────────────────────────

# Library calls outside operation() and the pluggable git runner for first_parent_check.


M3 = "3" * 40


REPOS = [{"full_name": "me/a", "id": 1, "visibility": "public", "private": False, "archived": False, "fork": False}]


def stub(method, path, **kw):
    if path.startswith(("users/me/repos", "orgs/me/repos")):
        return REPOS if "page=1" in path else []
    if path.startswith("repos/me/a/commits/" + M3 + "/pulls"):
        return [{"number": 4, "merged_at": "t", "base": {"ref": "main"}, "head": {"sha": M3}}]
    if path == "repos/me/a":
        return {"id": 1, "visibility": "public", "private": False, "archived": False, "default_branch": "main"}
    if path == "repos/me/a/pulls/4":
        return {"state": "closed", "merged_at": "t", "merge_commit_sha": M3, "head": {}, "base": {"ref": "main"}}
    if path.startswith("repos/me/a/commits/main"):
        return {"sha": M3}
    raise g.Error("not_found_or_inaccessible")


def test_paginated_library_calls_work_without_operation():
    assert g._BUDGET.get() is None
    with g.request_stub(stub):
        assert g.pages("users/me/repos") == REPOS
        items, complete, n = g.pages_bounded("users/me/repos", max_pages=3)
        assert items == REPOS and complete and n == 1
        assert g.list_owner_repositories("me", kind="user")["count"] == 1
        assert g.list_owner_repositories("me")["count"] == 1
        assert g.commit_pulls("me/a", M3)["pulls"][0]["number"] == 4
        assert g.pr_identity("me/a", 4)["merge_commit_sha"] == M3
        assert g.repo_identity("me/a")["head"] == M3
    assert g._BUDGET.get() is None


def test_library_calls_reuse_an_explicit_operation():
    with g.operation(max_pages=1):
        with g.request_stub(stub):
            g.pages_bounded("users/me/repos")
            with pytest.raises(g.Error) as e:
                g.pages_bounded("users/me/repos")
    assert e.value.code == "pages_limit"


class FakeGit:
    def __init__(self, listed, ls_code=0):
        self.calls, self.listed, self.ls_code = [], listed, ls_code

    def __call__(self, args, cwd):
        self.calls.append((list(args), cwd))
        verb = next(a for a in args if not a.startswith("-") and "=" not in a)
        if verb == "ls-remote":
            return self.ls_code, b"ref: refs/heads/trunk\tHEAD\n" + M3.encode() + b"\tHEAD\n"
        if verb == "rev-list":
            return 0, ("\n".join(self.listed) + "\n").encode()
        return 0, b""


def test_git_runner_is_injectable_offline_and_credential_free(monkeypatch):
    token = "ghp_" + "x" * 36
    monkeypatch.setenv("GH_TOKEN", token)
    monkeypatch.setattr(g, "_git_run", lambda *a, **k: pytest.fail("default runner used"))
    fake = FakeGit([M3])
    out = g.first_parent_check("me/a", M3, exact=True, git_runner=fake,
                               remote="https://github.com/me/a.git")
    assert out["contains"] is True and out["branch"] == "trunk"
    dumped = json.dumps([c for c, _ in fake.calls])
    assert token not in dumped and "x-access-token" not in dumped.lower() and "authorization" not in dumped.lower()
    assert "https://github.com/me/a.git" in dumped
    for args, cwd in fake.calls:
        assert args[:6] == ["-c", "credential.helper=", "-c", "core.askPass=", "-c", "http.extraHeader="]
        assert cwd == fake.calls[0][1]
    assert not os.path.exists(fake.calls[0][1])
    assert g.first_parent_check("me/a", M3, exact=True, git_runner=FakeGit(["1" * 40]))["contains"] is False


def test_git_runner_remote_failure_and_bad_remotes():
    with pytest.raises(g.Error) as e:
        g.first_parent_check("me/a", M3, exact=True, git_runner=FakeGit([], ls_code=128))
    assert e.value.code == "git_remote_unavailable"
    for bad in ("https://user:pw@github.com/me/a", "https://tok@github.com/me/a", "ssh://github.com/me/a",
                "-upload-pack=x", "relative/path"):
        with pytest.raises(ValueError):
            g.first_parent_check("me/a", M3, exact=True, remote=bad, git_runner=FakeGit([M3]))
    with pytest.raises(ValueError):
        g.first_parent_check("me/a", M3, exact=True, branch="--evil", git_runner=FakeGit([M3]))


def test_custom_subprocess_runner_against_local_remote(tmp_path):
    up = tmp_path / "up"
    up.mkdir()
    def run(*a):
        return subprocess.run(["git", "-C", str(up), "-c", "user.email=t@e", "-c", "user.name=t", *a],
                              check=True, capture_output=True, text=True).stdout.strip()
    run("init", "-q", "-b", "main"); (up / "f").write_text("1"); run("add", "-A"); run("commit", "-qm", "1")
    run("switch", "-q", "-c", "pr"); (up / "g").write_text("2"); run("add", "-A"); run("commit", "-qm", "2")
    inter = run("rev-parse", "HEAD")
    run("switch", "-q", "main"); run("merge", "-q", "--no-ff", "-m", "m", "pr")
    merge = run("rev-parse", "HEAD")
    def runner(args, cwd):
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                           env={"PATH": os.environ["PATH"], "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
                                "GIT_CONFIG_GLOBAL": os.devnull, "HOME": str(tmp_path)})
        return p.returncode, p.stdout
    assert g.first_parent_check("me/a", merge, exact=True, remote=str(up), git_runner=runner)["contains"] is True
    assert g.first_parent_check("me/a", inter, exact=True, remote=str(up), git_runner=runner)["contains"] is False


# ──── identity_api ────────────────────────────────────────────────

# Generic GitHub identity APIs: repo / owner / PR / commit-pulls / first-parent / request_stub.


M1, M2, M3_identity_api, X2 = "1" * 40, "2" * 40, "3" * 40, "5" * 40


def commit(sha, *parents):
    return {"sha": sha, "parents": [{"sha": p} for p in parents]}


HISTORY = [commit(M3_identity_api, M2), commit(M2, M1, X2), commit(X2, M1), commit(M1)]


def test_request_stub_overrides_transport():
    with g.request_stub(lambda *a, **k: {"ok": True, "path": a[1]}):
        assert g.request("GET", "repos/x/y") == {"ok": True, "path": "repos/x/y"}
    with patch.object(g, "_gh", side_effect=AssertionError("live")), \
         patch.object(g, "_url", side_effect=AssertionError("live")), \
         pytest.raises(AssertionError):
        g.request("GET", "repos/x/y", transport="urllib")


def test_repo_identity_public_and_not_public():
    def fake(method, path, **kw):
        if path == "repos/me/pub":
            return {"id": 1, "visibility": "public", "archived": False, "fork": False, "default_branch": "main",
                    "owner": {"type": "User"}, "private": False}
        if path == "repos/me/pub/commits/main":
            return {"sha": M3_identity_api}
        if path == "repos/me/hid":
            return {"id": 2, "visibility": "private", "private": True, "archived": False, "default_branch": "main"}
        raise g.Error("not_found_or_inaccessible")
    with g.request_stub(fake):
        out = g.repo_identity("me/pub")
        assert out["repository_id"] == 1 and out["head"] == M3_identity_api and out["write"] is False
        with pytest.raises(g.Error) as e:
            g.repo_identity("me/hid")
        assert e.value.code == "not_public"


def test_list_owner_repositories_user_and_org():
    user = [{"full_name": "me/a", "id": 1, "visibility": "public", "archived": False, "fork": False, "default_branch": "main"},
            {"full_name": "me/secret", "visibility": "private", "private": True},
            {"full_name": "me/old", "visibility": "public", "archived": True},
            {"full_name": "me/fork", "visibility": "public", "fork": True, "archived": False}]
    with patch.object(g, "pages_bounded", return_value=(user, True, 1)) as pages:
        out = g.list_owner_repositories("me", kind="user")
    assert pages.call_args.args[0].startswith("users/me/repos")
    assert [r["full_name"] for r in out["repositories"]] == ["me/a"] and out["count"] == 1
    with patch.object(g, "pages_bounded", return_value=(user, True, 1)) as pages:
        out = g.list_owner_repositories("me", kind="org", include_forks=True, include_archived=True)
    assert pages.call_args.args[0].startswith("orgs/me/repos") and out["count"] == 3


def test_list_owner_auto_falls_back_to_org():
    def pages(path, *a, **k):
        if path.startswith("users/"):
            raise g.Error("not_found_or_inaccessible")
        return [{"full_name": "org/x", "id": 9, "visibility": "public", "archived": False, "fork": False}], True, 1
    with patch.object(g, "pages_bounded", side_effect=pages):
        out = g.list_owner_repositories("org", kind="auto")
    assert out["kind"] == "orgs" and out["repositories"][0]["full_name"] == "org/x"


def test_pr_identity_merged_and_open():
    def fake(method, path, **kw):
        if path == "repos/me/r":
            return {"id": 7, "default_branch": "main"}
        if path == "repos/me/r/pulls/3":
            return {"merged_at": "t", "merge_commit_sha": M3_identity_api, "state": "closed",
                    "head": {"sha": X2, "ref": "pr"}, "base": {"sha": M1, "ref": "main"}}
        if path == "repos/me/r/pulls/4":
            return {"state": "open", "head": {"sha": X2, "ref": "pr"}, "base": {"sha": M2, "ref": "main"}}
        raise g.Error("not_found_or_inaccessible")
    with g.request_stub(fake):
        m = g.pr_identity("me/r", 3)
        assert m["state"] == "merged" and m["merge_commit_sha"] == M3_identity_api and m["merged_into_default"] is True
        o = g.pr_identity("me/r", 4)
        assert o["state"] == "open" and o["merge_commit_sha"] is None and o["head"]["sha"] == X2


def test_commit_pulls():
    with g.request_stub(lambda m, p, **k: [{"number": 9, "merged_at": "t", "merge_commit_sha": M3_identity_api,
                                             "base": {"ref": "main"}, "head": {"sha": X2}}]):
        out = g.commit_pulls("me/r", X2)
    assert out["count"] == 1 and out["pulls"][0]["number"] == 9 and out["pulls"][0]["state"] == "merged"
    with pytest.raises(ValueError):
        g.commit_pulls("me/r", "short")


def test_first_parent_api_mode():
    def fake(method, path, **kw):
        if path == "repos/me/up":
            return {"default_branch": "main"}
        if path.startswith("repos/me/up/commits?sha=main"):
            return HISTORY if path.endswith("page=1") else []
        raise g.Error("not_found_or_inaccessible")
    with g.request_stub(fake):
        assert g.first_parent_check("me/up", M3_identity_api)["contains"] is True
        assert g.first_parent_check("me/up", X2)["contains"] is False


def test_first_parent_exact_local_remote(tmp_path):
    up = tmp_path / "remotes" / "me" / "up"
    up.mkdir(parents=True)
    def git(*a):
        return subprocess.run(["git", "-C", str(up), "-c", "user.email=t@e", "-c", "user.name=t", *a],
                              check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    (up / "a").write_text("a\n"); git("add", "-A"); git("commit", "-qm", "one")
    main1 = git("rev-parse", "HEAD")
    git("switch", "-q", "-c", "pr"); (up / "b").write_text("b\n"); git("add", "-A"); git("commit", "-qm", "pr")
    pr = git("rev-parse", "HEAD")
    git("switch", "-q", "main"); git("merge", "-q", "--no-ff", "-m", "merge", "pr")
    merge = git("rev-parse", "HEAD")
    base = str(tmp_path / "remotes") + "/"
    assert g.first_parent_check("me/up", merge, exact=True, remote_base=base)["contains"] is True
    assert g.first_parent_check("me/up", pr, exact=True, remote_base=base)["contains"] is False
    assert g.first_parent_check("me/up", main1, exact=True, remote_base=base)["contains"] is True


def _words(args):
    words, index = [], 0
    while index < len(args):
        if args[index] == "-c":
            index += 2
            continue
        words.append(args[index])
        index += 1
    return words


class DispatchGit:
    def __init__(self, **kw):
        self.calls, self.kw = [], kw

    def __call__(self, args, cwd):
        self.calls.append(list(args))
        words = _words(args)
        verb = words[0]
        if verb == "init":
            return self.kw.get("init", (0, b""))
        if verb == "ls-remote":
            return self.kw.get("ls", (0, b"ref: refs/heads/trunk\tHEAD\n" + M3.encode() + b"\tHEAD\n"))
        if verb == "fetch":
            return self.kw.get("fetch", (0, b""))
        if verb == "rev-list":
            return self.kw.get("rev", (0, (M3 + "\n").encode()))
        if verb == "cat-file" and "-p" in words:
            return self.kw.get("cat_p", (0, b"tree t\nparent a\nparent b\n\n"))
        if verb == "cat-file":
            return self.kw.get("cat_e", (0, b""))
        return 0, b""


def test_exact_git_failure_modes():
    with pytest.raises(g.Error) as init_error:
        g.first_parent_check("me/a", M3, exact=True, git_runner=DispatchGit(init=(1, b"")))
    assert init_error.value.code == "git_failed"
    with pytest.raises(g.Error) as fetch_error:
        g.first_parent_check("me/a", M3, exact=True, git_runner=DispatchGit(fetch=(1, b"")))
    assert fetch_error.value.code == "git_remote_unavailable"
    with pytest.raises(g.Error) as rev_error:
        g.first_parent_check("me/a", M3, exact=True, git_runner=DispatchGit(rev=(1, b"")))
    assert rev_error.value.code == "git_failed"
    with pytest.raises(TypeError):
        g.first_parent_check("me/a", M3, exact=True, git_runner=object())
    with pytest.raises(ValueError):
        g.first_parent_check("me/a", M3, exact=True, remote_base="ssh://github.com/")
    # Commit-not-found needs the branch fetch to succeed and the sha fetch to fail.

    def staged(args, cwd):
        words = _words(args)
        if words[0] == "fetch" and not any("refs/heads/" in word for word in words):
            return 1, b""
        if words[0] == "cat-file":
            return 1, b""
        if words[0] == "ls-remote":
            return 0, b"abc\tHEAD\n"
        if words[0] == "rev-list":
            return 0, b"\n"
        return 0, b""

    out = g.first_parent_check("me/a", M3, exact=True, git_runner=staged)
    assert out["contains"] is False and out["reason"] == "commit_not_found" and out["branch"] == "main"
    dev = DispatchGit()
    assert g.first_parent_check("me/a", M3, exact=True, branch="dev", git_runner=dev)["contains"] is True
    fetched = " ".join(" ".join(call) for call in dev.calls)
    assert "refs/heads/dev" in fetched and "refs/heads/trunk" not in fetched
    found = DispatchGit(cat_e=(1, b""), rev=(0, b"dddddddddddddddddddddddddddddddddddddddd\n"))
    # sha fetch succeeds (default), object was absent, commit is not on the listed line
    assert g.first_parent_check("me/a", M3, exact=True, git_runner=found)["contains"] is False
    assert g._exact_parent_count("me/a", M3, remote="https://github.com/me/a", remote_base="https://github.com/",
                                 git_runner=DispatchGit(cat_p=(0, b"tree t\nparent a\nparent b\nparent c\n\n")),
                                 timeout=30) == 3
    assert g._exact_parent_count("me/a", M3, remote=None, remote_base="https://github.com/",
                                 git_runner=DispatchGit(cat_e=(1, b""), fetch=(1, b"")), timeout=30) is None
    assert g._exact_parent_count("me/a", M3, remote="https://github.com/me/a", remote_base="https://github.com/",
                                 git_runner=DispatchGit(cat_p=(1, b"")), timeout=30) is None
    with pytest.raises(TypeError):
        g._exact_parent_count("me/a", M3, remote="https://github.com/me/a", remote_base="https://github.com/",
                              git_runner=object(), timeout=30)


def _page(n=100):
    rows = []
    for index in range(n):
        rows.append({"sha": f"{index:040x}", "parents": [{"sha": f"{index + 1:040x}"}]})
    return rows


def test_dot_repo_is_kept_and_traversal_never_calls_git(tmp_path, monkeypatch):
    sha = "ab" * 20
    (tmp_path / "vendor.lock.json").write_text(json.dumps({
        "files": [
            {"repository": "acme/.github", "commit": sha, "source": "a"},
            {"repository": "acme/widgets", "commit": sha},
        ]}))
    found = {p["repository"] for p in g.collect_pins(tmp_path)}
    assert found == {"acme/.github", "acme/widgets"}
    seen = []

    def fake(repository, pins, remote, timeout, require_merge=False):
        seen.append((repository, remote))
        return [{"repository": repository, "commit": sha, "branch": "main", "final": None,
                 "parent_count": 2, "kind": "merge", "state": "ok", "pins": [], "detail": ""}]

    monkeypatch.setattr(g, "_audit_repository", fake)
    assert g.pin_audit(tmp_path)["ok"] is True
    assert ("acme/.github", "https://github.com/acme/.github") in seen
    (tmp_path / "vendor.lock.json").write_text(json.dumps({"repository": "a/b/c", "commit": sha}))
    with pytest.raises(g.PinInputError, match="invalid_repository"):
        g.collect_pins(tmp_path)
    cli = run_cli(str(Path(g.__file__)), 'pin-audit', '--root', str(tmp_path))
    assert cli.returncode == 2
    assert "invalid_repository" in cli.stderr
    assert "b/c" not in cli.stderr
    (tmp_path / "vendor.lock.json").write_text(json.dumps({"a/b/c": {"commit": sha}}))
    assert g.collect_pins(tmp_path) == []
    (tmp_path / "vendor.lock.json").write_text(json.dumps(
        {"acme/widgets": {"repository": " ", "commit": sha}}))
    assert g.collect_pins(tmp_path) == []
    (tmp_path / "vendor.lock.json").write_text(json.dumps(
        {"https://evil.example/acme/widgets": {"commit": sha}}))
    assert g.collect_pins(tmp_path) == []

    def boom(args, cwd):
        raise AssertionError("git")

    for name in ("../secret", "./secret", "owner/..", "owner/."):
        with pytest.raises(ValueError):
            g.first_parent_check(name, sha, exact=True, remote_base=str(tmp_path) + "/", git_runner=boom)


def test_commit_classify_exact_uses_one_fetch():
    fake = DispatchGit(cat_p=(0, b"tree t\nparent a\nparent b\n\n"))

    def pulls(method, path, **kw):
        if path.endswith("/pulls?per_page=100"):
            return []
        raise g.Error("not_found_or_inaccessible")

    with g.request_stub(pulls):
        out = g.commit_classify("me/a", M3, exact=True, remote="https://github.com/me/a", git_runner=fake, branch="main")
    assert out["on_first_parent"] is True and out["parent_count"] == 2 and out["kind"] == "merge"
    assert sum("init" in call for call in fake.calls) == 1
    assert sum("fetch" in call for call in fake.calls) == 1


def test_commit_classify_exact_local_and_missing_default(tmp_path):
    up = tmp_path / "up"
    up.mkdir()

    def run(*args):
        return subprocess.run(["git", "-C", str(up), "-c", "user.email=t@e", "-c", "user.name=t", *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    run("init", "-q", "-b", "main")
    (up / "f").write_text("1\n")
    run("add", "-A")
    run("commit", "-qm", "root")
    root = run("rev-parse", "HEAD")
    run("switch", "-q", "-c", "pr")
    (up / "g").write_text("2\n")
    run("add", "-A")
    run("commit", "-qm", "pr")
    run("switch", "-q", "main")
    run("merge", "-q", "--no-ff", "-m", "m", "pr")
    merge = run("rev-parse", "HEAD")

    def pulls(method, path, **kw):
        if path.endswith("/pulls?per_page=100"):
            return [{"number": 9, "merged_at": "t", "merge_commit_sha": merge,
                     "head": {"sha": root}, "base": {"ref": "main"}}]
        raise g.Error("not_found_or_inaccessible")

    with g.request_stub(pulls):
        out = g.commit_classify("me/a", " " + merge + " ", exact=True, remote=str(up), branch=" main ")
    assert out["on_first_parent"] is True and out["parent_count"] == 2 and out["kind"] == "merge"
    assert out["branch"] == "main" and out["pulls"][0]["is_merge_commit"] is True and out["pulls"][0]["is_head"] is False
    with g.request_stub(pulls):
        missing = g.commit_classify("me/a", "ab" * 20, exact=True, remote=str(up), branch="main")
    assert missing["on_first_parent"] is False and missing["parent_count"] is None and missing["kind"] == "unknown"
    assert g._exact_parent_count("me/a", merge, remote=str(up), remote_base="https://github.com/",
                                 git_runner=None, timeout=30) == 2

    def no_default(method, path, **kw):
        if path == "repos/me/a":
            return ["nope"]
        if path.startswith("repos/me/a/commits?"):
            return [{"sha": "1" * 40, "parents": []}]
        if path == f"repos/me/a/commits/{merge}":
            return {"parents": []}
        if path.endswith("/pulls?per_page=100"):
            return []
        raise g.Error("not_found_or_inaccessible")

    with g.request_stub(no_default):
        odd = g.commit_classify("me/a", merge)
    assert odd["branch"] is None and odd["parent_count"] == 0 and odd["kind"] == "root"
    assert odd["on_first_parent"] is None


def test_commit_classify_api_and_cli():
    sha = "ab" * 20
    pulls = [
        {"number": 3, "state": "open", "merge_commit_sha": "ff" * 20, "head": {"sha": sha}, "base": {"ref": "dev"}},
        {"number": None, "state": "closed"},
        {"number": 1, "merged_at": "t", "merge_commit_sha": sha, "head": {"sha": "ee" * 20}, "base": {"ref": "main"}},
    ]

    def fake(method, path, **kw):
        if path == "repos/me/r":
            return {"default_branch": "  trunk  "}
        if path.startswith("repos/me/r/commits?sha="):
            return [ {"sha": sha, "parents": [{"sha": "1" * 40}, {"sha": "2" * 40}]} ] if path.endswith("page=1") else []
        if path == f"repos/me/r/commits/{sha}":
            return {"sha": sha, "parents": [{"sha": "1" * 40}]}
        if path == f"repos/me/r/commits/{sha}/pulls?per_page=100":
            return pulls
        raise g.Error("not_found_or_inaccessible")

    with g.request_stub(fake):
        out = g.commit_classify("  me/r  ", "  " + sha + "  ", branch="   ")
    assert out["repository"] == "me/r" and out["sha"] == sha and out["branch"] == "trunk"
    assert out["on_first_parent"] is True and out["parent_count"] == 1 and out["kind"] == "squash_or_single"
    assert [p["number"] for p in out["pulls"]] == [1, 3, None]
    assert out["pulls"][0]["is_merge_commit"] is True and out["pulls"][0]["is_head"] is False
    assert out["pulls"][1]["is_merge_commit"] is False and out["pulls"][1]["is_head"] is True
    assert out["pulls"][2]["base_ref"] is None
    assert out["write"] is False and out["schema"] == "gh-identity-commit-classify/1"
    text = g.render_commit_classify(out)
    assert "on_first_parent=yes" in text and "kind=squash_or_single" in text and "#1 merged" in text
    assert "merge_commit=yes" in text and "head=yes" in text and "head=no" in text and "base=?" in text

    def missing_branch(method, path, **kw):
        if path == "repos/me/r":
            return {"default_branch": "   "}
        if path.startswith("repos/me/r/commits?sha="):
            return _page()
        if path == f"repos/me/r/commits/{sha}":
            return {"parents": "nope"}
        if path.endswith("/pulls?per_page=100"):
            return []
        raise g.Error("permission_or_rate_limit")

    with g.request_stub(missing_branch):
        truncated = g.commit_classify("me/r", sha, max_pages=1)
    assert truncated["on_first_parent"] is None and truncated["branch"] is None
    assert truncated["parent_count"] is None and truncated["kind"] == "unknown" and truncated["pulls"] == []
    assert "on_first_parent=unknown" in g.render_commit_classify(truncated)
    assert "branch=?" in g.render_commit_classify(truncated) and "parent_count=?" in g.render_commit_classify(truncated)

    def not_dict(method, path, **kw):
        if path == "repos/me/r":
            return ["nope"]
        if path.startswith("repos/me/r/commits?sha="):
            return [{"sha": "1" * 40, "parents": []}]
        if path == f"repos/me/r/commits/{sha}":
            return ["nope"]
        if path.endswith("/pulls?per_page=100"):
            return []
        raise g.Error("not_found_or_inaccessible")

    with g.request_stub(not_dict):
        odd = g.commit_classify("me/r", sha, branch="main")
    assert odd["on_first_parent"] is False and odd["parent_count"] is None and odd["branch"] == "main"

    def denied(method, path, **kw):
        if path == f"repos/me/r/commits/{sha}":
            raise g.Error("permission_or_rate_limit")
        if path.startswith("repos/me/r/commits?sha=") or path == "repos/me/r":
            return {"default_branch": "main"} if path == "repos/me/r" else []
        raise g.Error("not_found_or_inaccessible")

    with g.request_stub(denied), pytest.raises(g.Error) as denied_error:
        g.commit_classify("me/r", sha)
    assert denied_error.value.code == "permission_or_rate_limit"

    def absent(method, path, **kw):
        if path == "repos/me/r":
            return {"default_branch": "main"}
        if path.startswith("repos/me/r/commits?sha="):
            return [{"sha": "1" * 40, "parents": []}]
        if path == f"repos/me/r/commits/{sha}":
            raise g.Error("not_found_or_inaccessible")
        if path.endswith("/pulls?per_page=100"):
            return []
        raise g.Error("not_found_or_inaccessible")

    with g.request_stub(absent):
        assert g.commit_classify("me/r", sha)["parent_count"] is None
    with pytest.raises(ValueError):
        g.commit_classify("me/r", "  nope  ")
    with pytest.raises(ValueError):
        g.commit_classify("   ", sha)
    proc = run_cli(str(Path(g.__file__)), 'commit-classify', 'me/r', 'nope')
    exact = run_cli(str(Path(g.__file__)), 'commit-classify', 'me/r', 'nope', '--exact', '--branch', '  ')
    assert proc.returncode == 2 and exact.returncode == 2 and not proc.stdout and not exact.stdout
    with g.request_stub(fake):
        with contextlib.redirect_stdout(io.StringIO()) as text_out:
            assert g.main(["commit-classify", "me/r", sha, "--text"]) == 0
        assert "on_first_parent=yes" in text_out.getvalue()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            assert g.main(["commit-classify", " me/r ", " " + sha + " ", "--branch", " main "]) == 0
    payload = json.loads(buffer.getvalue())
    assert list(payload) == sorted(payload) and payload["kind"] == "squash_or_single"
    assert payload["on_first_parent"] is True


def test_cli_repo_identity():
    with g.request_stub(lambda m, p, **k: {"id": 1, "visibility": "public", "archived": False, "fork": False,
                                            "default_branch": "main", "owner": {"type": "User"}, "private": False}
                         if p == "repos/me/pub" else {"sha": M3_identity_api}):
        import subprocess, sys
        p = subprocess.run([sys.executable, "-c",
                            "import gh_identity as g\n"
                            "with g.request_stub(lambda m,p,**k: "
                            "{'id':1,'visibility':'public','archived':False,'fork':False,"
                            "'default_branch':'main','owner':{'type':'User'},'private':False} "
                            "if p=='repos/me/pub' else {'sha':'" + M3_identity_api + "'}):\n"
                            " print(g.repo_identity('me/pub')['head'])"],
                           capture_output=True, text=True)
        assert p.returncode == 0 and p.stdout.strip() == M3_identity_api


def test_list_owner_repositories_max_pages_truncates():
    calls = []
    def page(path, transport, timeout, requester):
        calls.append(path)
        return ([{"full_name": f"me/r{len(calls)}", "id": len(calls), "visibility": "public",
                  "archived": False, "fork": False}] * 1, {"link": '<https://api.github.com/x?page=2>; rel="next"'})
    with patch.object(g, "_page", side_effect=page), \
         patch.object(g, "_next_page", side_effect=lambda p, h, n: p + "&more"):
        out = g.list_owner_repositories("me", kind="user", max_pages=1)
    assert len(calls) == 1
    assert out["complete"] is False and out["pages_fetched"] == 1 and out["truncated_reason"] == "max_pages"
    with patch.object(g, "_page", return_value=([], {})), patch.object(g, "_next_page", return_value=None):
        out = g.list_owner_repositories("me", kind="user", max_pages=1)
    assert out["complete"] is True and out["truncated_reason"] is None
    with pytest.raises(ValueError):
        g.list_owner_repositories("me", kind="user", max_pages=0)


def test_repo_identity_public_archived_allowed():
    def fake(method, path, **kw):
        if path == "repos/me/old":
            return {"id": 3, "visibility": "public", "private": False, "archived": True, "fork": False,
                    "default_branch": "main", "owner": {"type": "User"}}
        return {"sha": M3_identity_api}
    with g.request_stub(fake):
        out = g.repo_identity("me/old")
    assert out["archived"] is True and out["repository_id"] == 3 and out["head"] == M3_identity_api
# ──── request_stub routes ────────────────────────────────────────────

class RequestStubRoutes(unittest.TestCase):
    """request_stub({path: value}, calls): the shared read-only double for any test or consumer."""

    def test_routes_values_errors_and_calls(self):
        calls = []
        with g.request_stub({"repos/o/r": {"id": 1}, "repos/o/r/languages": g.Error("permission_or_rate_limit")}, calls):
            self.assertEqual(g.request("GET", "repos/o/r"), {"id": 1})
            with self.assertRaises(g.Error) as denied:
                g.request("GET", "repos/o/r/languages")
            with self.assertRaises(g.Error) as missing:
                g.request("GET", "repos/o/r/topics")
            with self.assertRaises(AssertionError):
                g.request("POST", "repos/o/r")
        self.assertEqual((denied.exception.code, missing.exception.code), ("permission_or_rate_limit", "not_found_or_inaccessible"))
        self.assertEqual(calls, ["repos/o/r", "repos/o/r/languages", "repos/o/r/topics", "repos/o/r"])

    def test_callable_handler_with_calls(self):
        calls = []
        with g.request_stub(lambda method, path, **kw: {"path": path}, calls):
            self.assertEqual(g.repository("o/r")["repository"], "o/r")
        self.assertEqual(calls, ["repos/o/r"])


# ──── shared parsers (_is_sha, _when) ─────────────────────────────────

class SharedParsers(unittest.TestCase):
    """One commit-id check and one timestamp parser serve every section of gh_identity.py."""

    def test_is_sha(self):
        self.assertTrue(g._is_sha("a" * 40))
        self.assertFalse(g._is_sha("a" * 64))
        self.assertTrue(g._is_sha("a" * 64, oid=True))
        for bad in ("A" * 40, "a" * 39, "g" * 40, None, 40, "a" * 40 + "\n"):
            with self.subTest(bad=bad):
                self.assertFalse(g._is_sha(bad))

    def test_when(self):
        self.assertEqual(g._when("2026-11-15T00:00:00Z").utcoffset().total_seconds(), 0)
        self.assertIsNone(g._when("2026-11-15T00:00:00").tzinfo)  # naive kept; callers decide
        for bad in (None, "", "yesterday", 1):
            with self.subTest(bad=bad):
                self.assertIsNone(g._when(bad))


def test_branch_ref_existing_and_missing():
    def fake(method, path, **kw):
        if path == "repos/me/r/git/ref/heads/feat/x":
            return {"ref": "refs/heads/feat/x", "object": {"sha": M1, "type": "commit"}}
        if path.startswith("repos/me/r/branches"):
            return []
        raise g.Error("not_found_or_inaccessible")
    with g.request_stub(fake):
        out = g.branch_ref("me/r", "feat/x")
        assert out["exists"] is True and out["sha"] == M1 and out["write"] is False
        assert out["schema"] == "gh-identity-branch/1" and out["branch"] == "feat/x"
        out = g.branch_ref("me/r", "gone")
        assert out["exists"] is False and out["sha"] is None
    with pytest.raises(ValueError):
        g.branch_ref("me/r", " ")


@pytest.mark.parametrize("payload", [
    {"ref": "refs/heads/main", "object": ["bad"]},
    {"ref": "refs/heads/main", "object": {"type": "commit", "sha": "not-a-sha"}},
    {"ref": "refs/heads/elsewhere", "object": {"type": "commit", "sha": M1}},
    {"ref": "refs/heads/main", "object": {"type": "tag", "sha": M1}},
])
def test_branch_ref_rejects_malformed_ref_payload(payload):
    with g.request_stub(lambda *a, **k: payload):
        with pytest.raises(g.Error) as e:
            g.branch_ref("me/r", "main")
        assert e.value.code == "invalid_ref"


def test_branch_ref_absence_needs_a_complete_listing():
    def listed(method, path, **kw):
        if path.startswith("repos/me/r/branches"):
            return [{"name": "feat/x", "commit": {"sha": M1}}]
        raise g.Error("not_found_or_inaccessible")
    with g.request_stub(listed):
        out = g.branch_ref("me/r", "feat/x")
        assert out["exists"] is True and out["sha"] == M1

    def inaccessible(method, path, **kw):
        if path.startswith("repos/me/r/branches"):
            raise g.Error("permission_or_rate_limit")
        raise g.Error("not_found_or_inaccessible")
    with g.request_stub(inaccessible):
        with pytest.raises(g.Error) as e:
            g.branch_ref("me/r", "feat/x")
        assert e.value.code == "permission_or_rate_limit"
