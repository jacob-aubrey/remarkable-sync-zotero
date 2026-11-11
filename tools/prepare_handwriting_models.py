"""Explicitly prepare the optional, pinned local handwriting models.

Reuses RapidOCR's existing downloader. This command processes no PDFs or notes;
ordinary companion runs only load already validated local model files.
"""
import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from note_organizer.handwriting_ocr import MODEL_FILES, versions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        runtime = versions()
        root = args.output.resolve()
        root.mkdir(parents=True, exist_ok=True)
        for name, expected in MODEL_FILES.items():
            existing = root / name
            if existing.exists() and hashlib.sha256(existing.read_bytes()).hexdigest() != expected:
                raise ValueError("An existing model has an unexpected checksum. Choose a new model directory.")
        import onnxruntime
        from rapidocr import RapidOCR
        onnxruntime.disable_telemetry_events()
        engine = RapidOCR(params={"Global.model_root_dir": str(root), "Global.log_level": "warning",
            "EngineConfig.onnxruntime.intra_op_num_threads": 2, "EngineConfig.onnxruntime.inter_op_num_threads": 1})
        # This version loads lazily. Preload its three default models without
        # submitting a document, URL, or ink image for recognition.
        engine._load_det_model()
        engine._load_cls_model()
        engine._load_rec_model()
        files = []
        for name, expected in MODEL_FILES.items():
            path = root / name
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                raise ValueError("A model download failed its pinned checksum. It cannot be used for recognition.")
            files.append({"file": name, "bytes": path.stat().st_size, "sha256": actual})
        distribution = metadata.distribution("rapidocr")
        license_file = next(file for file in distribution.files if file.name == "LICENSE")
        (root / "LICENSE-RapidOCR-Apache-2.0.txt").write_bytes(distribution.locate_file(license_file).read_bytes())
        model_license = next(file for file in distribution.files if file.name == "MODEL_LICENSES.md")
        (root / "MODEL_LICENSES-RapidOCR.md").write_bytes(distribution.locate_file(model_license).read_bytes())
        (root / "ATTRIBUTION.txt").write_text(
            "RapidOCR code: Copyright (c) 2021 RapidOCR Authors. Apache-2.0.\n"
            "Model weights: upstream PaddleOCR, Baidu/respective rights holders. Apache-2.0.\n"
            "ONNX conversions supplied by RapidOCR under the upstream license.\n"
            "Sources: https://github.com/RapidAI/RapidOCR and https://github.com/PaddlePaddle/PaddleOCR\n"
            "Model attribution/hashes: https://rapidai.github.io/RapidOCRDocs/main/model_licenses/\n"
            "The installed RapidOCR 3.10.0 model license record is copied alongside this file.\n",
            encoding="utf-8")
        provenance = {"schema": 1, "runtime": runtime, "models": files,
                      "license": "Apache-2.0", "source": "RapidOCR 3.10.0 default_models.yaml"}
        (root / "model-provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
        print(json.dumps({"status": "ready", "models": len(files), "bytes": sum(f["bytes"] for f in files)}))
        return 0
    except Exception:
        print("Model preparation failed. Check the pilot versions, network and selected model directory.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
