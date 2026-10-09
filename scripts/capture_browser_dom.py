#!/usr/bin/env python3
"""Open one URL in optional Playwright Chromium and save a selected DOM snapshot.

Exit 0: captured (not site/content validation); 1: ambiguous selector; 2: error.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit


def capture(page, url, selector, timeout_ms, max_bytes):
    response = page.goto(url, wait_until='domcontentloaded', timeout=timeout_ms)
    if response is not None and response.status >= 400:
        raise ValueError('HTTP status ' + str(response.status))
    target = page.locator(selector)
    target.first.wait_for(state='attached', timeout=timeout_ms)
    if target.count() != 1:
        return None
    data = target.evaluate('e => ({html:e.outerHTML, text:e.textContent, visible:e.innerText ?? null})', timeout=timeout_ms)
    encoded = json.dumps(data, ensure_ascii=False).encode('utf-8')
    if len(encoded) > max_bytes:
        raise ValueError('captured data exceeds byte limit')
    return {'schema':'ghi-browser-dom/1', 'requested_url':url, 'url':page.url,
            'title':page.title(), 'selector':selector,
            'captured_at':datetime.now(timezone.utc).isoformat(),
            'source_kind':'dom', 'html_sha256':hashlib.sha256(data['html'].encode()).hexdigest(),
            'content_verified':False, **data}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    parser.add_argument('--selector', default='body')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout-ms', type=int, default=30000)
    parser.add_argument('--max-bytes', type=int, default=2000000)
    parser.add_argument('--headed', action='store_true')
    args = parser.parse_args(argv)
    parsed = urlsplit(args.url)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
        parser.error('HTTP(S) URL without embedded credentials required')
    if args.timeout_ms < 1 or args.max_bytes < 1:
        parser.error('limits must be positive')
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as runtime:
            browser = runtime.chromium.launch(headless=not args.headed, timeout=args.timeout_ms)
            try:
                context = browser.new_context(accept_downloads=False)
                page = context.new_page()
                page.set_default_timeout(args.timeout_ms)
                result = capture(page,args.url,args.selector,args.timeout_ms,args.max_bytes)
            finally:
                browser.close()
        if result is None:
            print(json.dumps({'error':'ambiguous_selector'}),file=sys.stderr)
            return 1
        # Exclusive create: never silently overwrite earlier evidence.
        with args.output.open('x',encoding='utf-8') as stream:
            json.dump(result,stream,ensure_ascii=False,indent=2)
            stream.write('\n')
        print(json.dumps({'status':'captured','output':str(args.output),'content_verified':False}))
        return 0
    except ImportError:
        print('Install optional playwright and Chromium; see README.',file=sys.stderr)
        return 2
    except Exception as error:
        print(json.dumps({'error':str(error)}),file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
