"""Cached, index-only printed OCR using PyMuPDF's existing Tesseract binding.

No source PDF bytes, page geometry or annotation layers are rewritten. This is
printed OCR, not a handwriting recognizer. See documentation/ZOTERO_PILOT.md.
"""
import hashlib
import json
from pathlib import Path
import re

import pymupdf

from .remarkable_cloud import atomic_write, trace_event


class PrintedOCR:
    def __init__(self, tessdata, cache, *, language="eng", dpi=200, cancelled=lambda: False):
        if not re.fullmatch(r"[a-zA-Z0-9_]+(?:\+[a-zA-Z0-9_]+)*", language):
            raise ValueError("Choose installed OCR language names.")
        if not isinstance(dpi, int) or not 72 <= dpi <= 300:
            raise ValueError("OCR resolution must be between 72 and 300 DPI.")
        self.tessdata, self.cache = Path(tessdata).resolve(strict=True), Path(cache).resolve()
        models = [(name, hashlib.sha256((self.tessdata / (name + ".traineddata")).read_bytes()).hexdigest())
                  for name in language.split("+")]
        self.language, self.dpi, self.cancelled = language, dpi, cancelled
        self.profile = hashlib.sha256(json.dumps([3, pymupdf.VersionBind, models, dpi]).encode()).hexdigest()
        self.warnings = []
        self.complete = True

    def content(self, path):
        """Return body text per page; cache complete results by PDF/model hashes."""
        data = Path(path).read_bytes()
        self.complete = False
        digest = hashlib.sha256(data).hexdigest()
        target = self.cache / self.profile / (digest + ".json")
        if target.exists():
            try:
                cached = json.loads(target.read_text(encoding="utf-8"))
                payload_hash = hashlib.sha256(json.dumps([cached["pages"], cached["warnings"]],
                                                          ensure_ascii=False).encode()).hexdigest()
                with pymupdf.open(stream=data, filetype="pdf") as document:
                    count = len(document)
                if (cached["sha256"] == digest and cached.get("content_sha") == payload_hash
                        and isinstance(cached["pages"], list) and len(cached["pages"]) == count
                        and all(isinstance(t, str) for t in cached["pages"])):
                    self.warnings = cached["warnings"]
                    self.complete = True
                    trace_event(getattr(self, 'diagnostic', None), 'printed_ocr_cache_hit', pages=count)
                    return cached["pages"]
            except (ValueError, KeyError, TypeError):
                pass  # A corrupt rebuildable cache must not prevent source retrieval.
        texts, warnings, complete = [], [], True
        with pymupdf.open(stream=data, filetype="pdf") as document:
            if document.needs_pass:
                raise ValueError("Unlock the PDF before indexing.")
            for index, page in enumerate(document):
                trace_event(getattr(self, 'diagnostic', None), 'printed_ocr_page_started', page=index + 1, total=len(document))
                if self.cancelled():
                    raise InterruptedError("Printed OCR stopped.")
                text = page.get_text()
                # Partial OCR includes native text and recognizes raster images.
                # Limit it to substantial images, not logos or small figure icons.
                areas = [(pymupdf.Rect(info["bbox"]) & (page.rect * page.derotation_matrix)).get_area()
                         for info in page.get_image_info()]
                large_image = (max(areas, default=0) >= page.rect.get_area() * .45
                               or sum(areas) >= page.rect.get_area() * .75)
                # A screenshot or outlined text may occupy little of an
                # otherwise empty page. It still needs recognition; genuine
                # blank pages and small logos beside native text do not.
                needs_ocr = large_image or (not text.strip() and (bool(areas) or bool(page.get_drawings())))
                if needs_ocr:
                    rotation = page.rotation
                    try:
                        if page.mediabox.get_area() * (self.dpi / 72) ** 2 > 40_000_000:
                            raise ValueError("Page exceeds the OCR pixel budget")
                        # PDF rotation metadata confuses this binding. Normalize
                        # only this in-memory document, then restore immediately.
                        page.set_rotation(0)
                        tp = page.get_textpage_ocr(language=self.language, dpi=self.dpi,
                                                  full=not text.strip(), tessdata=str(self.tessdata))
                        text = page.get_text(textpage=tp)
                    except InterruptedError:
                        raise
                    except Exception:
                        complete = False
                        warnings.append({"page": index + 1, "reason": "Printed OCR failed; native text retained."})
                    finally:
                        page.set_rotation(rotation)
                if not text.strip():
                    warnings.append({"page": index + 1, "reason": "No printed text recognized."})
                texts.append(text)
                trace_event(getattr(self, 'diagnostic', None), 'printed_ocr_page_finished', page=index + 1, needs_ocr=needs_ocr)
        self.warnings = warnings
        self.complete = complete
        if complete:
            # Rebuildable cache only. An incomplete result must be retried.
            content_sha = hashlib.sha256(json.dumps([texts, warnings], ensure_ascii=False).encode()).hexdigest()
            atomic_write(target, json.dumps({"sha256": digest, "pages": texts, "warnings": warnings, "content_sha": content_sha},
                                           ensure_ascii=False).encode("utf-8"))
        return texts
