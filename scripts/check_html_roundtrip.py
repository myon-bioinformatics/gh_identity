#!/usr/bin/env python3
"""Small HTML/Markdown semantic round-trip probe; explicit external markdown.py.
Exit 0: matched, 1: semantic drift, 2: runner error. No network or screenshots.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys

CASES = {
    'paragraph_link': '<p>日本語 &amp; <a href="https://github.com/example/repo">リンク</a></p>',
    'heading_emphasis': '<h2>見出し</h2><p><strong>重要</strong>と<em>補足</em></p>',
    'code': '<pre><code>def main():\n    return "#CSS &amp; HTML"\n</code></pre>',
    'list': '<ul><li>一つ目</li><li>二つ目</li></ul>',
    'quote': '<blockquote><p>引用です</p></blockquote>',
    'table': '<table><thead><tr><th>名前</th><th>値</th></tr></thead><tbody><tr><td>A</td><td>10</td></tr></tbody></table>',
}


def signature(node, pre=False):
    # Formatting whitespace outside pre is not semantic; inline spacing is kept.
    if node.kind == 'text':
        text = node.text if pre else re.sub(r'\s+', ' ', node.text)
        return ('text', text) if text.strip() or pre else None
    pre = pre or node.tag == 'pre'
    children = [s for child in node.children or [] if (s := signature(child, pre)) is not None]
    # Table section wrappers are optional in HTML; compare cells and rows.
    if node.tag in ('thead', 'tbody', 'tfoot'):
        return ('group', children)
    flat = []
    for child in children:
        flat.extend(child[1] if child[0] == 'group' else [child])
    attrs = {k: v for k, v in (node.attrs or {}).items() if k in ('href', 'src', 'alt', 'start', 'type', 'checked')}
    return (node.tag or 'root', attrs, flat)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('markdown_module', type=Path)
    parser.add_argument("--web-ui-module", type=Path, help="optional trusted web_ui.py for CSS/script wrapper integration")
    args = parser.parse_args()
    try:
        spec = importlib.util.spec_from_file_location('roundtrip_markdown', args.markdown_module)
        md = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = md
        spec.loader.exec_module(md)
        web = ghi = None
        if args.web_ui_module:
            for name, path in [('probe_web_ui', args.web_ui_module), ('probe_ghi', Path(__file__).resolve().parents[1] / 'gh_identity.py')]:
                extra = importlib.util.spec_from_file_location(name, path)
                module = importlib.util.module_from_spec(extra)
                sys.modules[name] = module
                extra.loader.exec_module(module)
                if name == 'probe_web_ui': web = module
                else: ghi = module
        print(json.dumps({'markdown_sha256': hashlib.sha256(args.markdown_module.read_bytes()).hexdigest(),
                          'web_ui_sha256': hashlib.sha256(args.web_ui_module.read_bytes()).hexdigest() if args.web_ui_module else None}))
        failures = 0
        for name, source in CASES.items():
            markdown = md.html_to_markdown(source)
            restored = md.markdown_to_html(markdown)
            again = md.html_to_markdown(restored)
            same = signature(md.parse_html_dom(source)) == signature(md.parse_html_dom(restored))
            stable = markdown == again
            wrapper_match = None
            if web:
                fragment = '<article id="probe-body">' + source + '</article>'
                page = web.render_document(trusted_html=fragment,
                    css='#probe-body { color: #abcdef; }',
                    stylesheets=web.shared_stylesheets('/assets'),
                    scripts=web.shared_scripts('/assets'))
                wrapper_match = ghi.html_content(fragment, ['#probe-body'])['body'] == ghi.html_content(page, ['#probe-body'])['body']
            failures += not (same and stable and wrapper_match is not False)
            print(json.dumps({'case': name, 'semantic_match': same, 'markdown_stable': stable, 'web_ui_wrapper_match': wrapper_match}, ensure_ascii=False))
        return 1 if failures else 0
    except Exception as error:
        print(json.dumps({'error': str(error)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
