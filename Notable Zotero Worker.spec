# Build with the pinned requirements-zotero-plugin environment.
from pathlib import Path
import os
from importlib import metadata
from PyInstaller.utils.hooks import collect_data_files, copy_metadata
from tools.build_zotero_support import validate_build_environment

validate_build_environment()

root = Path(SPECPATH)
resources = Path(os.environ.get('NOTABLE_BUILD_RESOURCES', str(root.parent / '.test-tmp')))
model_root = resources / 'handwriting-rapidocr/models'
printed_root = resources / 'ocr-route-preflight/lang'
datas = []
for name in ['PP-OCRv6_det_small.onnx', 'PP-OCRv6_rec_small.onnx', 'ch_ppocr_mobile_v2.0_cls_mobile.onnx',
             'ATTRIBUTION.txt', 'LICENSE-RapidOCR-Apache-2.0.txt', 'MODEL_LICENSES-RapidOCR.md', 'model-provenance.json']:
    datas.append((str(model_root / name), 'models/handwriting'))
for name in ['eng.traineddata', 'LICENSE.tessdata_fast']:
    datas.append((str(printed_root / name), 'models/printed'))
for package in ['rapidocr', 'onnxruntime', 'pyzotero', 'yake']:
    datas += collect_data_files(package)
for distribution in metadata.distributions():
    name = distribution.metadata['Name']
    # Recognition validates its runtime version through importlib.metadata.
    datas += copy_metadata(name)
    for file in distribution.files or []:
        if any(word in file.name.lower() for word in ('license', 'copying', 'notice')) and distribution.locate_file(file).is_file():
            datas.append((str(distribution.locate_file(file)), 'licenses/' + name))
datas += [(str(root / 'documentation/ZOTERO_PLUGIN_NOTICES.md'), 'licenses')]
a = Analysis([str(root / 'tools/notable_worker.py')], pathex=[str(root), str(root / '_vendor')], binaries=[], datas=datas,
    hiddenimports=['rapidocr', 'onnxruntime', 'pypdf', 'fontTools', 'docx', 'yake'],
    hookspath=[str(root / 'hooks')], hooksconfig={}, runtime_hooks=[],
    excludes=['tkinter', 'tkinterdnd2', 'webview', 'google.genai', 'watchdog', 'matplotlib', 'pytest', 'IPython'],
    noarchive=False, optimize=0)
pyz = PYZ(a.pure)
# A console subsystem preserves the private stdio transport. Firefox launches
# it with CREATE_NO_WINDOW; the user never sees or operates a console.
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='notable-worker',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=True)
