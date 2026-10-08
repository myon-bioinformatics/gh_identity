import urllib.parse
import json, unittest
from unittest import mock
import gh_identity as g

class T(unittest.TestCase):
 def test_repo_validation(self):
  self.assertEqual(g.repo("o/r"),"o/r")
  with self.assertRaises(ValueError): g.repo("bad")
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
   req.assert_called_once_with("GET","repos/o/r/actions/runs/20/attempts/2/jobs?per_page=100&page=1",transport="auto",timeout=30)
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
      self.assertEqual(call.kwargs,{"transport":"urllib","timeout":7})

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

if __name__ == "__main__":
 unittest.main()
