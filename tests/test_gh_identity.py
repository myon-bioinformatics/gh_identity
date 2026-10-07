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

if __name__=="__main__": unittest.main()
