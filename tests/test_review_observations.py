"""Offline review identity, bounded pagination and failure contracts."""
import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import gh_identity as g

FIXTURE=json.loads((Path(__file__).parent/'fixtures/review_observations/responses.json').read_text())


def responses():
    return copy.deepcopy([FIXTURE[k] for k in ('conversation','review_submission','inline_comment','review_thread')])


def thread_page(ids=(3,), *, more=False, cursor=None, total=None, thread='T1'):
    result=copy.deepcopy(FIXTURE['review_thread'])
    connection=result['data']['repository']['pullRequest']['reviewThreads']
    row=connection['nodes'][0]
    row['id']=thread
    row['comments']['nodes']=[{'databaseId':i,'url':f'https://github.com/o/r/pull/7#discussion_r{i}'} for i in ids]
    row['comments']['totalCount']=len(ids) if total is None else total
    row['comments']['pageInfo']={'hasNextPage':more,'endCursor':cursor}
    return result


def test_four_kinds_reuse_apis_and_preserve_observed_links():
    with patch.object(g,'request',side_effect=responses()) as request, \
         patch.object(g,'comments',wraps=g.comments) as comments, \
         patch.object(g,'reviews',wraps=g.reviews) as reviews:
        result=g.review_observations('o/r',7,transport='urllib')
    assert result['complete'] and not result['truncated']
    comments.assert_called_once();reviews.assert_called_once()
    assert [x[0][0] for x in request.call_args_list]==['GET','GET','GET','POST']
    gql=request.call_args_list[-1]
    assert gql.args[1]=='graphql' and gql.args[2]['query'].startswith('query(')
    assert 'mutation' not in gql.args[2]['query']
    assert gql.kwargs['transport']=='urllib'
    assert result['inline_comment'][0]['thread_id']=='T1'
    assert result['inline_comment'][0]['diff_hunk'].startswith('@@')
    assert result['review_submission'][0]['url'].endswith('review-2')
    assert result['review_submission'][0]['severity_labels']==[]
    assert [x['label'] for x in result['inline_comment'][0]['severity_labels']]==['Blocking','Nit']
    assert result['review_thread'][0]['is_resolved'] is True
    assert result['review_thread'][0]['url'].endswith('discussion_r3')
    assert result['conversation'][0]['preview'].startswith('説明です 😀')


@pytest.mark.parametrize('body,labels',[
    ('This should be blocking, but is not labelled.',[]),
    ('CHANGES_REQUESTED',[]),('Blocking 0 / Should 1',[]),
    ('**Should**: handle this',['Should']),('Non-blocking: optional',['Non-blocking']),
    ('- Nit: spelling',['Nit']),('### Blocking: failure',['Blocking']),
    ('Blocking\nShould',['Blocking','Should']),
    ('```text\nBlocking: example\n```',[]),
])
def test_only_explicit_labels(body,labels):
    assert [x['label'] for x in g._explicit_severity(body)]==labels


@pytest.mark.parametrize('api', [g.inline_comments,g.review_threads,g.review_observations])
@pytest.mark.parametrize('kw',[{'max_items':0},{'max_pages':True},{'max_items':1.2}])
def test_invalid_limits_before_transport(api,kw):
    with patch.object(g,'request') as request:
        with pytest.raises(ValueError):api('o/r',7,**kw)
    request.assert_not_called()


def test_rest_link_pagination_and_exact_terminal_limit():
    first=g.Page([{'id':1,'body':'a'}],{'Link':'<https://api.github.com/repos/o/r/pulls/7/comments?per_page=100&page=2>; rel="next"'})
    second=g.Page([{'id':2,'body':'b'}],{})
    with patch.object(g,'request',side_effect=[first,second]) as request:
        result=g.inline_comments('o/r',7,max_items=2)
    assert result['complete'] and len(result['comments'])==2
    assert request.call_args_list[1].args[1].endswith('page=2')


@pytest.mark.parametrize('limit,reason',[({'max_items':1},'max_items'),({'max_pages':1,'max_items':200},'max_pages')])
def test_rest_partial(limit,reason):
    first=[{'id':i} for i in range(1,101)]
    with patch.object(g,'request',return_value=first):
        result=g.inline_comments('o/r',7,**limit)
    assert not result['complete'] and result['truncated']
    assert result['limit_reason']==reason


def test_nested_comment_pagination():
    first=thread_page((3,),more=True,cursor='C1',total=2)
    second={'data':{'node':{'comments':{'nodes':[{'databaseId':4,'url':'https://github.com/o/r/pull/7#discussion_r4'}],'totalCount':2,'pageInfo':{'hasNextPage':False,'endCursor':'C2'}}}}}
    with patch.object(g,'request',side_effect=[first,second]) as request:
        result=g.review_threads('o/r',7)
    assert result['complete']
    assert result['threads'][0]['comment_ids']==[3,4]
    assert request.call_args_list[1].args[2]['variables']=={'id':'T1','cursor':'C1'}


def test_nested_limit_never_claims_complete():
    with patch.object(g,'request',return_value=thread_page((3,),more=True,cursor='C1',total=2)):
        result=g.review_threads('o/r',7,max_pages=1)
    assert result['limit_reason']=='thread_comments'
    assert result['threads'][0]['comments_complete'] is False


def test_outer_thread_pagination():
    first=thread_page(thread='T1');second=thread_page((4,),thread='T2')
    c=first['data']['repository']['pullRequest']['reviewThreads']
    c['pageInfo']={'hasNextPage':True,'endCursor':'Tcursor'}
    with patch.object(g,'request',side_effect=[first,second]) as request:
        result=g.review_threads('o/r',7)
    assert result['complete'] and len(result['threads'])==2
    assert request.call_args_list[1].args[2]['variables']['cursor']=='Tcursor'


@pytest.mark.parametrize('error',['authentication_required','permission_or_rate_limit','not_found_or_inaccessible'])
def test_failed_component_is_unknown_not_empty_complete(error):
    values=responses();values[-1]=g.Error(error)
    with patch.object(g,'request',side_effect=values):result=g.review_observations('o/r',7)
    assert not result['complete'] and not result['truncated']
    assert result['components']['review_thread']['error']==error
    assert result['inline_comment'][0]['thread_id'] is None


def test_graphql_partial_data_and_errors_are_not_complete():
    values=responses();values[-1]['errors']=[{'type':'FORBIDDEN','message':'private details'}]
    with patch.object(g,'request',side_effect=values):result=g.review_observations('o/r',7)
    assert result['components']['review_thread']['error']=='permission_or_rate_limit'
    assert 'private details' not in json.dumps(result)


@pytest.mark.parametrize('bad,code',[
    ({'data':None},'invalid_json'),
    ({'data':{'repository':None}},'not_found_or_inaccessible'),
    ({'errors':[{'type':'OTHER'}]},'graphql_error'),
])
def test_graphql_invalid_response(bad,code):
    with patch.object(g,'request',return_value=bad):
        with pytest.raises(g.Error,match=code):g.review_threads('o/r',7)


def test_missing_cursor_and_wrong_member_count_fail_closed():
    for value in (thread_page(more=True),thread_page(total=2)):
        with patch.object(g,'request',return_value=value):
            with pytest.raises(g.Error,match='pagination_incomplete'):g.review_threads('o/r',7)


def test_duplicate_rest_identity_rejected():
    with patch.object(g,'request',return_value=[{'id':1},{'id':1}]):
        with pytest.raises(g.Error,match='ambiguous_comment_identity'):g.inline_comments('o/r',7)


@pytest.mark.parametrize('limit', ['max_pages','max_items','max_bytes'])
def test_shared_operation_budget_fails_closed(limit):
    with patch.object(g,'request',side_effect=responses()):
        with g.operation(**{limit:1}):
            with pytest.raises(g.Error,match=limit[4:]+'_limit'):
                # Mock request bypasses real byte accounting; pages still charge.
                if limit=='max_bytes':
                    g.pages('repos/o/r/issues/7/comments',requester=lambda *a,**k:FIXTURE['conversation'])
                else:g.review_observations('o/r',7)


def test_diff_tracks_resolution_and_preserves_unknown_removal():
    with patch.object(g,'request',side_effect=responses()):before=g.review_observations('o/r',7)
    after=copy.deepcopy(before)
    after['review_thread'][0]['is_resolved']=False
    diff=g.diff_pr_observations(before,after)
    assert diff['changes']['review_thread']['changed']==['T1']
    after['inline_comment']=[];after['components']['inline_comment']['complete']=False;after['complete']=False
    assert g.diff_pr_observations(before,after)['changes']['inline_comment']['removed']==[]
    after['number']=8
    with pytest.raises(ValueError,match='identity mismatch'):g.diff_pr_observations(before,after)


@pytest.mark.parametrize('complete,exit_code',[(True,0),(False,1)])
def test_cli_result_and_exit(capsys,complete,exit_code):
    result={'complete':complete}
    with patch.object(g,'review_observations',return_value=result) as api:
        assert g.main(['review-observations','o/r','7','--limit','4','--page-limit','2','--transport','urllib'])==exit_code
    assert json.loads(capsys.readouterr().out)==result
    assert api.call_args.kwargs['max_items']==4


def test_cli_permission_error_exit(capsys):
    with patch.object(g,'request',side_effect=g.Error('permission_or_rate_limit')):
        assert g.main(['review-threads','o/r','7'])==2
    assert 'permission_or_rate_limit' in capsys.readouterr().err


@pytest.mark.parametrize('transport',['gh','urllib'])
def test_graphql_uses_existing_transport_and_is_nonmutating(transport):
    with patch.object(g,'_gh' if transport=='gh' else '_url',return_value=thread_page()) as backend:
        assert g.review_threads('o/r',7,transport=transport)['complete']
    args=backend.call_args.args
    assert args[0:2]==('POST','graphql')
    assert args[2]['query'].startswith('query(')
    assert len(args)<5 or not args[4]


def test_exact_thread_limit_complete_at_terminal_page():
    with patch.object(g,'request',return_value=thread_page()):
        assert g.review_threads('o/r',7,max_items=1)['complete']


def test_thread_limit_with_observed_continuation_is_partial():
    page=thread_page()
    page['data']['repository']['pullRequest']['reviewThreads']['pageInfo']={'hasNextPage':True,'endCursor':'next'}
    with patch.object(g,'request',return_value=page):
        result=g.review_threads('o/r',7,max_items=1)
    assert result['limit_reason']=='max_items' and not result['complete']


def test_duplicate_threads_and_member_ids_rejected():
    page=thread_page();nodes=page['data']['repository']['pullRequest']['reviewThreads']['nodes']
    nodes.append(copy.deepcopy(nodes[0]))
    with patch.object(g,'request',return_value=page):
        with pytest.raises(g.Error,match='ambiguous_thread_identity'):g.review_threads('o/r',7)
    with patch.object(g,'request',return_value=thread_page((3,3))):
        with pytest.raises(g.Error,match='ambiguous_comment_identity'):g.review_threads('o/r',7)


@pytest.mark.parametrize('bad',[{'id':1,'body':5},{'id':1,'user':[]},None])
def test_malformed_rest_rows_raise_classified_error(bad):
    with patch.object(g,'request',return_value=[bad]):
        with pytest.raises(g.Error,match='invalid_json'):g.inline_comments('o/r',7)


@pytest.mark.parametrize('api',[g.inline_comments,g.review_threads,g.review_observations])
@pytest.mark.parametrize('number',[True,0,'bad',-1])
def test_invalid_pr_identity_before_transport(api,number):
    with patch.object(g,'request') as request:
        with pytest.raises(ValueError):api('o/r',number)
    request.assert_not_called()


def test_global_timeout_propagates_not_partial_success():
    values=responses();values[-1]=g.Error('operation_timeout')
    with patch.object(g,'request',side_effect=values):
        with pytest.raises(g.Error,match='operation_timeout'):g.review_observations('o/r',7)


def test_diff_suppresses_invented_additions_for_incomplete_baseline():
    with patch.object(g,'request',side_effect=responses()):after=g.review_observations('o/r',7)
    before=copy.deepcopy(after);before['inline_comment']=[]
    before['complete']=False;before['components']['inline_comment']['complete']=False
    assert g.diff_pr_observations(before,after)['changes']['inline_comment']['added']==[]


@pytest.mark.parametrize('command',['inline-comments','review-threads'])
def test_thin_cli_other_components(command,capsys):
    api='inline_comments' if command=='inline-comments' else 'review_threads'
    with patch.object(g,api,return_value={'complete':True}) as fn:
        assert g.main([command,'o/r','7','--limit','2'])==0
    assert fn.call_args.kwargs['max_items']==2
    assert json.loads(capsys.readouterr().out)['complete']


def test_numeric_string_pr_normalized_for_graphql_int_variable():
    with patch.object(g,'request',return_value=thread_page()) as request:
        result=g.review_threads('o/r','7')
    assert result['number']==7
    assert request.call_args.args[2]['variables']['number']==7
