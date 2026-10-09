"""Small live DOM captures outside GitHub; no repeated site access."""
import json
from pathlib import Path
import pytest
import gh_identity as ghi

CAPTURE=json.loads((Path(__file__).parent/'fixtures/browser_dom/simple-sites.json').read_text(encoding='utf-8'))

@pytest.mark.parametrize('case',CAPTURE['cases'])
def test_live_simple_site_fragments(case):
    result=ghi.html_content(case['html'],[case['selector']],source_kind='dom')
    if case['comparison']=='exact':
        assert result['body']==case['visible']==case['expected']
    else:
        assert ' '.join(result['body'].split())==case['expected']
        assert result['body'].strip()!=case['visible']
    assert result['visibility']=='structural_only'


def test_frameset_is_not_a_captured_body():
    # Structural model of observed parent; child documents require separate capture.
    parent='<frameset cols="18,82"><frame src="menu.htm" name="left"><frame src="top.htm" name="right"></frameset>'
    with pytest.raises(ghi.Error,match='html_body_not_found'):
        ghi.html_content(parent,['article','main','body'],source_kind='dom')
