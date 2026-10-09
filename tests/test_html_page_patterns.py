"""Modeled page families, not captured GitHub DOM compatibility evidence."""
import pytest
import gh_identity as ghi


@pytest.mark.parametrize('page,selectors,expected,excluded', [
    ('<main><article id="wiki-body"><h1>Wiki</h1><p>導入 &amp; 設定</p><ul><li>手順1</li></ul></article><aside>Pages一覧</aside></main>', ['#wiki-body'], ['Wiki','導入 & 設定','手順1'], ['Pages一覧']),
    ('<main><nav>main / src</nav><pre id="file-content"><code>def main():\n    return 1\n</code></pre><footer>Edit file</footer></main>', ['#file-content'], ['def main():\n    return 1\n'], ['main / src','Edit file']),
    ('<article id="description"><p>最初の本文</p></article><article class="comment-body">返信A</article><article class="comment-body">返信B</article>', ['#description'], ['最初の本文'], ['返信A','返信B']),
    ('<section id="commit-message"><h2>変更の要約</h2><pre>理由\n\nSigned-off-by: Example</pre></section><div>diff stats</div>', ['#commit-message'], ['変更の要約','理由\n\nSigned-off-by: Example'], ['diff stats']),
    ('<section class="release-notes"><h2>v1</h2><ul><li>修正</li></ul><table><tr><th>項目</th><th>値</th></tr><tr><td>A</td><td>1</td></tr></table></section><aside>Assets download</aside>', ['#missing','.release-notes'], ['v1','修正','項目\t値','A\t1'], ['Assets download']),
    ('<article id="readme"><details><summary>設定</summary><p>詳細</p></details><p><code>#CSS</code>を保持</p></article>', ['#readme'], ['設定','詳細','#CSS'], []),
])
def test_modeled_page_families(page, selectors, expected, excluded):
    out=ghi.html_content(page, selectors)
    for text in expected:
        assert text in out['body']
    for text in excluded:
        assert text not in out['body']
    assert out['visibility']=='structural_only'
    assert out['identity_verified'] is False


def test_multiple_wiki_containers_fail_closed():
    with pytest.raises(ghi.Error,match='ambiguous_html_body'):
        ghi.html_content('<div id="wiki-body">A</div><div id="wiki-body">B</div>', ['#wiki-body'])


def test_login_page_is_not_a_successful_body():
    with pytest.raises(ghi.Error,match='html_body_not_found'):
        ghi.html_content('<main><h1>Sign in</h1><form>Login</form></main>', ['#wiki-body'])
