import json
import pytest
import gh_identity as g

REPO='myon-bioinformatics/mcp-toolcall-lab'

@pytest.mark.parametrize('args,path,ident', [
 (['review',REPO,'119','5495899602'],f'repos/{REPO}/pulls/119/reviews/5495899602',5495899602),
 (['inline-comment',REPO,'4252282158'],f'repos/{REPO}/pulls/comments/4252282158',4252282158),
 (['read-comment',f'https://github.com/{REPO}/pull/119#pullrequestreview-5495899602'],f'repos/{REPO}/pulls/119/reviews/5495899602',5495899602),
 (['read-comment',f'https://github.com/{REPO}/pull/119#discussion_r4252282158'],f'repos/{REPO}/pulls/comments/4252282158',4252282158),
 (['read-comment',f'https://github.com/{REPO}/issues/115#issuecomment-123'],f'repos/{REPO}/issues/comments/123',123),
])
def test_body_shortcuts(args,path,ident,monkeypatch,capsys):
 seen=[]
 def request(method,url,**kw):
  seen.append((method,url,kw['transport']))
  return {'id':ident,'body':'日本語\nsecond line','html_url':'https://github.com/example'}
 monkeypatch.setattr(g,'request',request)
 assert g.main(['--transport','urllib',*args])==0
 assert capsys.readouterr().out=='日本語\nsecond line\n'
 assert seen==[('GET',path,'urllib')]
 assert g.main([*args,'--json'])==0
 assert json.loads(capsys.readouterr().out)['body']=='日本語\nsecond line'

@pytest.mark.parametrize('args',[
 ['read-comment','https://evil.invalid/o/r/pull/1#discussion_r1'],
 ['read-comment','https://github.com/o/r/pull/1'],
 ['read-comment','https://github.com/o/r/pull/1?x=y#discussion_r1'],
 ['review','https://github.com/o/r/pull/1#discussion_r1'],
 ['inline-comment','o/r','0'],['review','o/r','1','-2'],
 ['review','../r','1','2'],['read-comment','o/r','2'],
])
def test_invalid_targets_do_not_request(args,monkeypatch,capsys):
 monkeypatch.setattr(g,'request',lambda *a,**k:pytest.fail('must not request'))
 assert g.main(args)==2
 assert json.loads(capsys.readouterr().err)['error']=='invalid_argument'

@pytest.mark.parametrize('data',[{}, {'id':1,'body':None},{'id':2,'body':'wrong'},{'id':True,'body':''}])
def test_bad_response_fails_closed(data,monkeypatch,capsys):
 monkeypatch.setattr(g,'request',lambda *a,**k:data)
 assert g.main(['inline-comment','o/r','1'])==2
 assert json.loads(capsys.readouterr().err)['error']=='invalid_comment_response'
