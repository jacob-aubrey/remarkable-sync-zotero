import json
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

from note_organizer.zotero_pilot import AuthorizationRequired, LocalZotero
from tools.zotero_companion import watch


class CompanionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stop = threading.Event()
        self.report = {"review": [], "actions": [], "documents": 0, "folders": 1}

    def status(self):
        return json.loads((self.root / "status.json").read_text(encoding="utf-8"))

    def test_finite_watch_stops_and_keeps_last_result(self):
        with patch("tools.zotero_companion.cycle", return_value=self.report) as call:
            self.assertEqual(watch(self.root, {}, self.stop, cycles=2, wait=lambda _: False), 0)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(self.status()["state"], "stopped")
        self.assertEqual(self.status()["previous"]["result"], self.report)
        self.assertFalse(call.call_args.kwargs.get("interactive", False))

    def test_offline_backoff_is_bounded_and_does_not_log_raw_exception(self):
        sleeps = []
        with patch("tools.zotero_companion.cycle", side_effect=OSError("secret token and private title")):
            watch(self.root, {}, self.stop, cycles=5, wait=lambda seconds: sleeps.append(seconds) or False)
        self.assertEqual(sleeps, [120, 240, 300, 300])
        self.assertNotIn("secret token", (self.root / "status.json").read_text())
        self.assertEqual(self.status()["previous"]["state"], "waiting")

    def test_missing_or_revoked_grant_requires_connect_without_retry_prompt(self):
        with patch("tools.zotero_companion.cycle", side_effect=AuthorizationRequired("Connect once")) as call:
            self.assertEqual(watch(self.root, {}, self.stop, cycles=4, wait=lambda _: False), 2)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(self.status()["state"], "connect_required")

    def test_stop_file_prevents_another_cycle(self):
        (self.root / "STOP").touch()
        with patch("tools.zotero_companion.cycle") as call:
            watch(self.root, {}, self.stop, cycles=1)
        call.assert_not_called()

    def test_background_client_never_opens_grant_dialog(self):
        client = LocalZotero.__new__(LocalZotero)
        client.interactive = False
        client.zot = Mock(local_api_key=None)
        with self.assertRaises(AuthorizationRequired):
            client.authorize()
        client.zot.authorize_local.assert_not_called()

    def test_only_remembered_grant_is_persisted(self):
        client = LocalZotero.__new__(LocalZotero)
        client.interactive = True
        client.server_id = "synthetic-server"
        client.grant_store = Mock()
        client.zot = Mock(local_api_key=None)
        client.zot.authorize_local.return_value = {"key": "synthetic", "remember": False}
        client.authorize()
        client.grant_store.save.assert_not_called()
        client.zot.authorize_local.return_value["remember"] = True
        client.authorize()
        saved = json.loads(client.grant_store.save.call_args.args[0])
        self.assertEqual(saved, {"key": "synthetic", "server": "synthetic-server"})

    def test_corrupt_grant_can_be_repaired_by_interactive_connect(self):
        http = Mock()
        http.get.return_value.headers = {"Zotero-Server-ID": "synthetic-server"}
        vault = Mock()
        vault.load.return_value = json.dumps({"server": "synthetic-server", "key": "malformed"})
        modules = {"httpx2": types.SimpleNamespace(Client=Mock(return_value=http)),
                   "pyzotero": types.SimpleNamespace(Zotero=Mock(return_value=Mock(local_api_key=None)))}
        with patch.dict("sys.modules", modules):
            with self.assertRaises(AuthorizationRequired):
                LocalZotero(grant_store=vault, interactive=False)
            vault.forget.assert_not_called()
            repaired = LocalZotero(grant_store=vault, interactive=True)
            repaired.close()
        vault.forget.assert_called_once()
        self.assertGreaterEqual(http.close.call_count, 2)
