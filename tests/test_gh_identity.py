import json, subprocess, sys, unittest
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
  with mock.patch.object(g,"pages",return_value=[{"id":7,"body":"<!-- k -->","html_url":"u"}]), mock.patch.object(g,"request") as q:
   x=g.post_comment("o/r",1,"body",write=True,marker="<!-- k -->"); self.assertEqual(x["status"],"already_exists"); q.assert_not_called()
 def test_variable_dry_run(self):
  x=g.set_variable("o/r","A","B"); self.assertEqual(x["status"],"planned")
 def test_uncertain_comment(self):
  with mock.patch.object(g,"request",side_effect=g.Error("timeout",True)):
   x=g.post_comment("o/r",1,"x",write=True); self.assertEqual(x["status"],"mutation_uncertain")
 def test_pr_identity(self):
  raw={"state":"open","draft":False,"merged":False,"mergeable":True,"head":{"sha":"h","ref":"f"},"base":{"sha":"b","ref":"main"},"html_url":"u"}
  with mock.patch.object(g,"request",return_value=raw):
   x=g.pr("o/r",2); self.assertEqual((x["head_sha"],x["base_sha"]),("h","b"))

 def test_zero_checks_not_green(self):
  with mock.patch.object(g,"request",return_value={"check_runs":[]}):
   self.assertEqual(g.checks_for_sha("o/r","a"*40)["state"],"pending")
 def test_observe_pr_stale(self):
  p1={"schema":"x","repository":"o/r","number":1,"state":"open","draft":False,"mergeable":True,"head_sha":"a"*40,"base_sha":"b"*40}
  p2=dict(p1,head_sha="c"*40)
  with mock.patch.object(g,"pr",side_effect=[p1,p2]), mock.patch.object(g,"checks_for_sha",return_value={"state":"green"}), mock.patch.object(g,"comments",return_value={}), mock.patch.object(g,"reviews",return_value={}):
   self.assertTrue(g.observe_pr("o/r",1)["stale"])
 def test_resolve_ref(self):
  with mock.patch.object(g,"request",return_value={"sha":"A"*40}):
   self.assertEqual(g.resolve_ref("o/r","main")["sha"],"a"*40)
if __name__=="__main__": unittest.main()
