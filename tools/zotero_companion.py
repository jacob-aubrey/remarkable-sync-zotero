"""Set up, inspect and run the scoped Zotero/reMarkable companion.

No production migration, automatic login registration, or app installation.
Connect once interactively; watch never opens authorization dialogs.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from note_organizer.remarkable_cloud import CloudClient, CloudError, TokenVault, atomic_write
from note_organizer.zotero_bridge import Bridge, BridgeState
from note_organizer.zotero_pilot import LocalZotero, PilotError
from tools.zotero_pilot import tablet_saved_text


def write_json(path, value):
    atomic_write(path, json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8"))


def load_config(directory):
    value = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    if value.get("schema") != 1 or not value.get("server_id"):
        raise PilotError("Configure this companion before starting it.")
    return value


def clients(directory, config, stop, *, interactive=False):
    from note_organizer.printed_ocr import PrintedOCR
    tessdata = config.get("tessdata")
    ocr = PrintedOCR(tessdata, directory / "ocr-cache", cancelled=stop.is_set) if tessdata else None
    handwriting = None
    if config.get("handwriting_models"):
        from note_organizer.handwriting_ocr import HandwritingOCR
        try:
            handwriting = HandwritingOCR(config["handwriting_models"], directory / "handwriting-cache", cancelled=stop.is_set)
        except (ValueError, OSError) as error:
            raise PilotError(str(error)) from None
    zot = LocalZotero(config["port"], printed_ocr=ocr, handwriting_ocr=handwriting,
                      grant_store=TokenVault(directory / "zotero-grant.dpapi"), interactive=interactive)
    try:
        if zot.server_id != config["server_id"]:
            raise PilotError("A different Zotero instance is running. The saved roots were not used.")
        token = TokenVault(config["tablet_vault"]).load()
        if not token:
            raise PilotError("Pair reMarkable in Notable before connecting this companion.")
        return zot, CloudClient(token, cache_dir=directory / "blobs", stop=stop)
    except BaseException:
        zot.close()
        raise


def cycle(directory, config, stop, *, apply=False, interactive=False):
    zot, cloud = clients(directory, config, stop, interactive=interactive)
    try:
        library = config.get("notable_library")
        provider = (lambda identity: tablet_saved_text(library, identity)) if library else None
        result = Bridge(zot, cloud, BridgeState(directory / "bridge"), config["zotero_root"],
                        config["tablet_root"], saved_annotations=provider).run(apply=apply)
        if zot.printed_ocr:
            result["ocr_warnings"] = zot.printed_ocr.warnings
        if zot.handwriting_ocr:
            result["handwriting_warnings"] = [{"attachment": row["attachment"], **warning}
                for row in zot.index_diagnostics for warning in row["handwriting"]]
            result["handwriting_mode"] = "Local machine transcription; unverified text can be incorrect."
        return result
    finally:
        zot.close()


def status(directory, state, **values):
    write_json(directory / "status.json", {"state": state,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), **values})


def watch(directory, config, stop, *, interval=60, cycles=None, wait=None):
    """Bounded backoff, one process, no background authorization prompt spam."""
    def interruptible_wait(seconds):
        import time
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if (directory / "STOP").exists() or stop.wait(min(1, deadline - time.monotonic())):
                return True
        return False
    wait = wait or interruptible_wait
    count, failures = 0, 0
    # Hold the whole watcher lease, not just a single transfer cycle. Explicit
    # one-shot runs are still serialized by the nested bridge's existing lease.
    with BridgeState(directory / "watcher").lock():
        while not stop.is_set() and not (directory / "STOP").exists():
            try:
                report = cycle(directory, config, stop, apply=True)
                status(directory, "needs_review" if report["review"] else "ready", result=report)
                failures = 0
            except InterruptedError:
                break
            except Exception as error:
                # Do not log raw HTTP errors, URLs/headers, account tokens or
                # private paper text. Authorization failures require Connect.
                name = type(error).__name__
                if name in {"LocalAPIKeyRequiredError", "LocalAPIDeniedError", "AuthorizationRequired"}:
                    TokenVault(directory / "zotero-grant.dpapi").forget()
                    status(directory, "connect_required", message="Zotero write permission expired or was revoked. Run Connect again.")
                    return 2
                failures += 1
                if isinstance(error, CloudError) and "authorization expired" in str(error):
                    status(directory, "pair_required", message="Reconnect reMarkable in Notable, then restart the companion.")
                    return 2
                message = str(error) if isinstance(error, (PilotError, CloudError)) else "Zotero or reMarkable is unavailable; retrying quietly."
                status(directory, "waiting", message=message, failures=failures)
            count += 1
            if cycles is not None and count >= cycles:
                break
            if wait(min(300, interval * 2 ** min(failures, 4))):
                break
    previous = json.loads((directory / "status.json").read_text(encoding="utf-8")) if (directory / "status.json").exists() else {}
    status(directory, "stopped", previous=previous)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("configure", help="Select existing roots; reads catalogs but writes only local helper configuration")
    setup.add_argument("--zotero-root", required=True)
    setup.add_argument("--tablet-root", required=True)
    setup.add_argument("--notable-library", type=Path, required=True)
    setup.add_argument("--port", type=int, default=23119)
    setup.add_argument("--tessdata", type=Path)
    setup.add_argument("--handwriting-models", type=Path,
                       help="Optional validated local RapidOCR models; machine text aids search and can be wrong")
    commands.add_parser("connect", help="Ask Zotero once for remembered write permission")
    commands.add_parser("inspect", help="Print selected configuration and latest local status")
    run = commands.add_parser("run", help="Plan by default; --apply synchronizes only the selected subtrees")
    run.add_argument("--apply", action="store_true")
    daemon = commands.add_parser("watch", help="Run quietly until Ctrl+C or a STOP file appears")
    daemon.add_argument("--interval", type=int, default=60)
    daemon.add_argument("--cycles", type=int, help="Optional finite run for diagnostics")
    commands.add_parser("stop", help="Request a watcher stop after the current operation")
    args = parser.parse_args(argv)
    directory = args.state_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        if args.command == "configure":
            path = directory / "config.json"
            if path.exists():
                raise PilotError("This directory is already configured. Use a separate state directory for different roots.")
            library = args.notable_library.resolve(strict=True)
            vault = library.parent / "remarkable/device-token.dpapi"
            if not vault.exists():
                raise PilotError("The selected Notable library is not paired with reMarkable.")
            zot = LocalZotero(args.port, interactive=False)
            try:
                config = {"schema": 1, "port": args.port, "server_id": zot.server_id,
                    "zotero_root": args.zotero_root, "tablet_root": args.tablet_root,
                    "notable_library": str(library), "tablet_vault": str(vault),
                    "tessdata": str(args.tessdata.resolve(strict=True)) if args.tessdata else "",
                    "handwriting_models": str(args.handwriting_models.resolve(strict=True)) if args.handwriting_models else ""}
            finally:
                zot.close()
            # Validate both selected catalogs in preview before accepting roots.
            preview = cycle(directory, config, stop)
            write_json(path, config)
            status(directory, "configured", preview=preview)
            print(json.dumps({"state": "configured", "status_file": str(directory / "status.json")}))
        elif args.command == "stop":
            (directory / "STOP").touch()
        elif args.command == "inspect":
            print(json.dumps({"config": load_config(directory), "status": json.loads((directory / "status.json").read_text(encoding="utf-8"))
                              if (directory / "status.json").exists() else {"state": "unknown"}}, indent=2))
        else:
            config = load_config(directory)
            if args.command == "connect":
                zot, _ = clients(directory, config, stop, interactive=True)
                try:
                    # Explicit Connect also repairs a revoked saved grant. A
                    # background cycle never does this or opens a dialog.
                    zot.zot.local_api_key = None
                    TokenVault(directory / "zotero-grant.dpapi").forget()
                    zot.authorize()
                    if not (directory / "zotero-grant.dpapi").exists():
                        raise PilotError("Choose Always Allow in Zotero to enable quiet multi-request synchronization.")
                finally:
                    zot.close()
                status(directory, "connected")
            elif args.command == "run":
                print(json.dumps(cycle(directory, config, stop, apply=args.apply), indent=2))
            elif args.command == "watch":
                if not 30 <= args.interval <= 300 or (args.cycles is not None and args.cycles < 1):
                    raise PilotError("Use a 30–300 second interval and a positive cycle count.")
                (directory / "STOP").unlink(missing_ok=True)
                return watch(directory, config, stop, interval=args.interval, cycles=args.cycles)
        return 0
    except Exception as error:
        message = str(error) if isinstance(error, PilotError) else "The companion could not complete this operation. Its saved state is available for retry."
        print(json.dumps({"error": message}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
