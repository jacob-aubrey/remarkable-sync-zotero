# reMarkable Sync for Zotero: bundled components

This Windows package reuses Zotero's native plugin and local API interfaces.
Zotero itself is installed separately and is AGPL-3.0-or-later:
https://github.com/zotero/zotero

The managed worker includes PyMuPDF (AGPL-3.0), rmscene (MIT), Pyzotero
(BlueOak-1.0.0), Pillow (MIT-CMU), pypdf (BSD-3-Clause), fontTools (MIT),
python-docx (MIT), RapidOCR (Apache-2.0), ONNX Runtime (MIT), and their
runtime dependencies including NumPy (BSD-3-Clause) and OpenCV (Apache-2.0).
Installed distribution metadata, available license files
and notices are copied into the worker package. PyInstaller's bootloader uses
its GPL exception permitting bundled applications.

The OCR weights are the pinned RapidOCR conversions of PaddleOCR weights
(Apache-2.0); exact attribution and hashes accompany the models. Printed OCR
uses Tesseract's tessdata_fast English model (Apache-2.0), with its license.

The interface reuses the existing application's Lucide SVG icons (ISC license),
from revision `f12b0de177fbc2a6795e99be065887e72b237123` of
https://github.com/lucide-icons/lucide . Their license and source attribution are
included under `icons/` in the plugin. Native popup styling is loaded from the
installed Zotero/Gecko runtime rather than copied into the plugin.

No code from zotero-remarkable-sync or a third-party plugin template is bundled.
Those projects informed the integration design; the existing local bridge,
review and OCR components provide the implementation.

The public plugin source is available under the GNU Affero General Public
License, version 3. Third-party components retain their own licenses.
The matching source, build instructions, and bundled dependency sources
are available on the [release page](https://github.com/jacob-aubrey/remarkable-sync-zotero/releases).
# Local keyword suggestions added in 0.5.0

YAKE 0.7.3 provides local statistical keyphrase suggestions. Its wheel metadata
says LGPLv3, but the actual bundled LICENSE explicitly grants AGPL-3.0-or-later;
the actual LICENSE is retained as the governing notice. No YAKE code was changed.
Source: [INESCTEC/yake](https://github.com/INESCTEC/yake), with the corresponding
installed source and data included in the companion source archive. No model,
hosted service, or additional account is used. Suggestions are optional and do
not become tags until the user accepts them.

New dependencies are Click 8.5.0 (BSD-3-Clause), jellyfish 1.3.0 (MIT), NetworkX
3.7 (BSD-3-Clause), segtok 1.5.11 (MIT), tabulate 0.10.0 (MIT), and regex
2026.9.29 (Apache-2.0 AND CNRI-Python). Their original licenses and distribution
metadata are bundled with the helper. NumPy is the already pinned dependency.
The modified tablet SVG retains its Lucide attribution; its bottom dot is
replaced by an R drawn as vector strokes, without an external font or logo.

