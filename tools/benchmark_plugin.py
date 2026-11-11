"""Measure synthetic bridge sync and cancellation without contacting an account."""
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymupdf
from note_organizer.remarkable_pdf import annotated_pdf
from tests.test_zotero_bridge import BridgeTests


def multi_page(item, bundle, pages):
    baseline = item.id + '.pdf'
    layer = next(value for name, value in bundle.items() if name.endswith('.rm'))
    ids = [str(uuid.uuid4()) for _ in range(pages)]
    with pymupdf.open(stream=bundle[baseline], filetype='pdf') as original, pymupdf.open() as document:
        for _ in ids:
            document.insert_pdf(original, from_page=0, to_page=0)
        data = document.tobytes()
    return {baseline: data, item.id + '.content': json.dumps({'fileType': 'pdf', 'pages': ids}).encode(),
            **{item.id + '/' + pid + '.rm': layer for pid in ids}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--papers', type=int, default=100)
    parser.add_argument('--pages', type=int, default=16)
    args = parser.parse_args()
    if not 1 <= args.papers <= 5000 or not 2 <= args.pages <= 500:
        parser.error('Choose 1–5,000 papers and 2–500 pages per paper.')
    fixture = BridgeTests()
    try:
        fixture.setUp()
        for _ in range(args.papers):
            item, bundle = fixture.tablet()
            long_pdf = multi_page(item, bundle, args.pages)
            item = replace(item, hash=hashlib.sha256(b''.join(long_pdf.values())).hexdigest())
            fixture.cloud.add(item, long_pdf)
        timings = {}
        for label in ('first_sync_seconds', 'repeat_sync_seconds'):
            started = time.perf_counter()
            report = fixture.engine.run(apply=True)
            timings[label] = round(time.perf_counter() - started, 3)
            if report['review']:
                raise RuntimeError('The synthetic sync raised unexpected review findings.')
        cancelled = threading.Event()
        requested = None

        def stop_at_first_page(event, **fields):
            nonlocal requested
            if event == 'render_page_finished' and requested is None:
                requested = time.perf_counter()
                cancelled.set()

        try:
            annotated_pdf(item, long_pdf, cancelled.is_set, diagnostic=stop_at_first_page)
        except InterruptedError:
            if requested is None:
                raise
            timings['cancel_after_page_ms'] = round((time.perf_counter() - requested) * 1000, 3)
        else:
            raise RuntimeError('PDF rendering ignored cancellation.')
        print(json.dumps({'papers': args.papers, 'pages_per_paper': args.pages, **timings}, indent=2))
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
