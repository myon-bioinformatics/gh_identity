#!/usr/bin/env python3
"""Optional local Gradio DOM lab. No external page fetch or CORS workaround."""
import argparse
import importlib.util
from pathlib import Path
import sys


def render_fixture(markdown_path):
    spec = importlib.util.spec_from_file_location('dom_lab_markdown', markdown_path)
    md = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = md
    spec.loader.exec_module(md)
    source = '# DOM検証\n\n日本語 **本文** と [ローカルリンク](#dom-lab-body)。\n\n- 項目1\n- 項目2\n\n```python\ndef main():\n    return "#CSS"\n```\n'
    return '<article id="dom-lab-body">' + md.markdown_to_html(source) + '</article>'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('markdown_module', type=Path, help='trusted existing markdown.py')
    parser.add_argument('--port', type=int, default=7860)
    parser.add_argument('--export-html', type=Path, help='write fixture without requiring Gradio')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be 1024..65535')
    html = render_fixture(args.markdown_module)
    if args.export_html:
        args.export_html.write_text(html, encoding='utf-8')
        return 0
    try:
        import gradio as gr
    except ImportError:
        parser.error('optional dependency missing: install gradio in a disposable test environment')
    with gr.Blocks() as demo:
        gr.Markdown('Local DOM lab: inspect #dom-lab-body; no remote fetch, login, or public share.')
        gr.HTML(html)
    demo.launch(server_name='127.0.0.1', server_port=args.port, share=False, inbrowser=False)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
