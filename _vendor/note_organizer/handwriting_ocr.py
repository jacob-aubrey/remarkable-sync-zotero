"""Optional local ink transcription using existing RapidOCR/PaddleOCR models.

Explicit local model paths prohibit model downloads. Ink, clean paper bytes and
reviewed Notable transcripts stay unchanged. Machine text is a search aid, not
an authoritative reading of scientific notation.
"""
import base64
import hashlib
from importlib import metadata
from io import BytesIO
import json
from pathlib import Path

import pymupdf

from .extraction import EXTRACTION_REVISION, annotation_key, ink_only
from .remarkable_cloud import atomic_write, trace_event


# RapidOCR 3.10.0 default_models.yaml and MODEL_LICENSES.md. Apache-2.0;
# upstream PaddleOCR weights belong to Baidu/respective rights holders.
MODEL_FILES = {
    "PP-OCRv6_det_small.onnx": "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f",
    "PP-OCRv6_rec_small.onnx": "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884",
    "ch_ppocr_mobile_v2.0_cls_mobile.onnx": "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
}


def versions():
    try:
        result = {name: metadata.version(name) for name in ("rapidocr", "onnxruntime")}
    except metadata.PackageNotFoundError:
        raise ValueError("Install requirements-handwriting-pilot.txt in a separate environment.") from None
    if result != {"rapidocr": "3.10.0", "onnxruntime": "1.31.0"}:
        raise ValueError("Use the validated RapidOCR 3.10.0 / ONNX Runtime 1.31.0 pilot versions.")
    return result


def checksum(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class HandwritingOCR:
    def __init__(self, models, cache, *, cancelled=lambda: False):
        self.models, self.cache = Path(models).resolve(strict=True), Path(cache).resolve()
        for name, expected in MODEL_FILES.items():
            path = self.models / name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("A required local handwriting model is missing or has the wrong checksum.")
        self.profile = checksum({"schema": 1, "models": MODEL_FILES, "versions": versions(),
                                 "renderer": pymupdf.VersionBind, "extractor_revision": EXTRACTION_REVISION,
                                 "crop": "all-page-ink-8pt-padding",
                                 "minimum_text_score": .5})
        self.cancelled, self.warnings, self._engine = cancelled, [], None

    def _recognize(self, image):
        if self._engine is None:
            import onnxruntime
            from rapidocr import RapidOCR
            onnxruntime.disable_telemetry_events()
            # Supplying every model_path bypasses RapidOCR's download branch.
            self._engine = RapidOCR(params={
                "Det.model_path": str(self.models / "PP-OCRv6_det_small.onnx"),
                "Rec.model_path": str(self.models / "PP-OCRv6_rec_small.onnx"),
                "Cls.model_path": str(self.models / "ch_ppocr_mobile_v2.0_cls_mobile.onnx"),
                "Global.log_level": "warning", "Global.text_score": .5,
                "EngineConfig.onnxruntime.intra_op_num_threads": 2,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            })
        result = self._engine(image)
        texts = list(result.txts) if result.txts is not None else []
        return [{"text": str(text), "confidence": float(score),
                 "box": [[float(x), float(y)] for x, y in box]}
                for text, score, box in zip(texts, result.scores, result.boxes)] if texts else []

    def content(self, path):
        data = Path(path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        target = self.cache / self.profile / (digest + ".json")
        self.warnings = []
        with pymupdf.open(stream=data, filetype="pdf") as document:
            if document.needs_pass:
                raise ValueError("Unlock the PDF before handwriting transcription.")
            if target.exists():
                try:
                    cached = json.loads(target.read_text(encoding="utf-8"))
                    records = cached["records"]
                    if (cached["pdf_sha256"] == digest and cached["profile"] == self.profile
                            and cached["records_sha256"] == checksum(records)
                            and len(records) == len(document)
                            and all(isinstance(row["text"], str) for row in records)):
                        self.warnings = [r["warning"] for r in records if r.get("warning")]
                        trace_event(getattr(self, 'diagnostic', None), 'handwriting_ocr_cache_hit', pages=len(records))
                        return [row["text"] for row in records]
                except (ValueError, KeyError, TypeError):
                    pass  # Corrupt rebuildable text never overrides retained ink.
            records = []
            for number, page in enumerate(document, 1):
                trace_event(getattr(self, 'diagnostic', None), 'handwriting_ocr_page_started', page=number, total=len(document))
                if self.cancelled():
                    raise InterruptedError("Handwriting transcription stopped.")
                members = [a for a in page.annots() or () if a.type[1] == "Ink"]
                record = {"page": number, "text": "", "lines": [], "kind": "machine_transcription",
                          "reviewed": False, "annotation_ids": [annotation_key(a) for a in members]}
                if members:
                    region = pymupdf.Rect(members[0].rect)
                    for member in members[1:]:
                        region |= member.rect
                    region += (-8, -8, 8, 8)
                    image = base64.b64decode(ink_only(page, members, region))
                    try:
                        record["lines"] = self._recognize(image)
                    except Exception:
                        raise ValueError("Local handwriting recognition failed; retained ink is unchanged. Retry indexing.") from None
                    from PIL import Image
                    with Image.open(BytesIO(image)) as bitmap:
                        record["image_pixels"] = list(bitmap.size)
                    record.update(region=list(region), box_space="ink crop pixels",
                                  image_sha256=hashlib.sha256(image).hexdigest())
                    words = "\n".join(line["text"] for line in record["lines"])
                    if words.strip():
                        record["text"] = f"Machine-read handwriting (unverified), page {number}:\n{words}"
                    else:
                        record["warning"] = {"page": number, "reason": "No handwriting text recognized; ink retained."}
                        self.warnings.append(record["warning"])
                records.append(record)
                trace_event(getattr(self, 'diagnostic', None), 'handwriting_ocr_page_finished', page=number, ink_annotations=len(members))
        atomic_write(target, json.dumps({"schema": 1, "pdf_sha256": digest, "profile": self.profile,
            "records_sha256": checksum(records), "records": records}, ensure_ascii=False).encode("utf-8"))
        return [row["text"] for row in records]
