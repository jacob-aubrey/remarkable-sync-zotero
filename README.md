# reMarkable Sync for Zotero

Sync PDFs, tablet annotations, and tags between Zotero and reMarkable. Review incoming papers, search your library, and resolve sync conflicts in Zotero.

## Install on Windows

The plugin supports Zotero 10.0.6 and later in the 10.0 series.

1. Download the XPI from the [latest release](https://github.com/jacob-aubrey/remarkable-sync-zotero/releases/latest).
2. In Zotero, open **Tools → Plugins**. From the gear menu, choose **Install Plugin From File** and select the XPI. You can install it over an earlier local copy.
3. Open the plugin's details and set **Allow automatic updates** to **On**.
4. Open **Zotero Settings → reMarkable Sync → Open Sync Setup** to connect your folders and manage automatic sync.

The XPI includes the Windows helper and OCR models. Zotero handles future updates through its plugin manager.

## Build from source

Use Windows x64 and Python 3.13. Download **third-party-source.zip** from the matching GitHub release and extract it. Its `ocr-build-resources` directory contains the pinned models and their notices.

```powershell
py -3.13 -m venv .venv
$python = '.\.venv\Scripts\python.exe'
& $python -m pip install --require-hashes -r requirements-windows.lock
& $python tools/verify_vendored_core.py
$env:NOTABLE_BUILD_RESOURCES = (Resolve-Path './ocr-build-resources').Path
& $python -m PyInstaller --noconfirm --distpath build/runtime 'Notable Zotero Worker.spec'
& $python tools/build_zotero_support.py --output dist/reMarkable-Sync-for-Zotero-0.19.9.xpi --runtime build/runtime/notable-worker.exe --verify-runtime --update-manifest
```

The build generates `updates.json` beside the XPI. For a release, tag the matching source, attach both files and the corresponding source to the GitHub release, and verify the downloads before publishing it as the latest release. Keep the version in the filename and the plugin manifest in sync.

Run `python tools/run_test_batches.py` for Python tests. Run the scripts matching `tools/test_*.cjs` with Node.js for JavaScript tests.

## License

The project's source code is licensed under the [GNU Affero General Public License, version 3](LICENSE). Third-party components keep their own licenses. The release includes the [bundled component notices](documentation/ZOTERO_PLUGIN_NOTICES.md) and matching third-party source.
