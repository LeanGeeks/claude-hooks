#!/usr/bin/env python3
"""
Unit Tests: Per-session YOLO (allow-all) store

Tests the session_yolo_store module:
- enable / is_enabled roundtrip keyed by session_id
- empty session_id is a no-op / never enabled
- missing or corrupt store file degrades gracefully
- prune drops aged-out entries but keeps fresh ones
- concurrent enable() calls don't clobber each other
"""

import importlib
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add paths for imports
sys.path.insert(0, str(Path(__file__).parent.parent / ".claude" / "hooks"))


class TestSessionYoloStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        self.tmp.close()
        os.unlink(self.tmp.name)  # start with no file
        os.environ["CLAUDE_SESSION_YOLO_FILE"] = self.tmp.name
        # Reload so STORE_FILE picks up the env override.
        import session_yolo_store
        self.store = importlib.reload(session_yolo_store)

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except FileNotFoundError:
            pass
        os.environ.pop("CLAUDE_SESSION_YOLO_FILE", None)

    def test_enable_then_is_enabled(self):
        self.assertFalse(self.store.is_enabled("sess-a"))
        self.store.enable("sess-a")
        self.assertTrue(self.store.is_enabled("sess-a"))
        # Distinct session is unaffected (per-session scope).
        self.assertFalse(self.store.is_enabled("sess-b"))

    def test_disable_clears_flag(self):
        self.store.enable("sess-a")
        self.store.enable("sess-b")
        self.store.disable("sess-a")
        self.assertFalse(self.store.is_enabled("sess-a"))
        self.assertTrue(self.store.is_enabled("sess-b"))
        # Disabling an absent session is a harmless no-op.
        self.store.disable("never-enabled")

    def test_cli_enable_status_disable(self):
        self.assertEqual(self.store._main(["status", "sess-a"]), 0)
        self.assertEqual(self.store._main(["enable", "sess-a"]), 0)
        self.assertTrue(self.store.is_enabled("sess-a"))
        self.assertEqual(self.store._main(["disable", "sess-a"]), 0)
        self.assertFalse(self.store.is_enabled("sess-a"))

    def test_cli_rejects_unsubstituted_template(self):
        # If ${CLAUDE_SESSION_ID} wasn't substituted, fail loudly and write nothing.
        self.assertEqual(self.store._main(["enable", "${CLAUDE_SESSION_ID}"]), 1)
        self.assertEqual(self.store._main(["enable", ""]), 1)
        self.assertFalse(Path(self.tmp.name).exists())

    def test_cli_bad_usage(self):
        self.assertEqual(self.store._main(["frobnicate", "x"]), 2)
        self.assertEqual(self.store._main(["enable"]), 2)

    def test_empty_session_id_is_noop(self):
        self.store.enable("")
        self.assertFalse(self.store.is_enabled(""))
        self.assertFalse(Path(self.tmp.name).exists())

    def test_missing_file_is_not_enabled(self):
        self.assertFalse(self.store.is_enabled("whoever"))

    def test_corrupt_file_degrades_gracefully(self):
        Path(self.tmp.name).write_text("}{ not json")
        self.assertFalse(self.store.is_enabled("sess-a"))
        # And enable() still recovers, overwriting the garbage.
        self.store.enable("sess-a")
        self.assertTrue(self.store.is_enabled("sess-a"))

    def test_enable_is_idempotent(self):
        self.store.enable("sess-a")
        self.store.enable("sess-a")
        data = json.loads(Path(self.tmp.name).read_text())
        self.assertEqual(list(data.keys()), ["sess-a"])

    def test_prune_drops_aged_entries(self):
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        fresh = datetime.now(timezone.utc).isoformat()
        Path(self.tmp.name).write_text(json.dumps({
            "old-sess": {"enabled_at": old},
            "fresh-sess": {"enabled_at": fresh},
        }))
        self.store.prune()
        self.assertFalse(self.store.is_enabled("old-sess"))
        self.assertTrue(self.store.is_enabled("fresh-sess"))

    def test_concurrent_enable_no_clobber(self):
        # Barrier-synchronized start maximizes contention so a read-modify-write
        # race (e.g. flushing the write after releasing the lock) is caught
        # deterministically rather than only under incidental load.
        ids = [f"sess-{i}" for i in range(40)]
        barrier = threading.Barrier(len(ids))

        def worker(sid):
            barrier.wait()
            self.store.enable(sid)

        threads = [threading.Thread(target=worker, args=(sid,)) for sid in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for sid in ids:
            self.assertTrue(self.store.is_enabled(sid), f"{sid} lost to a clobber")


if __name__ == "__main__":
    unittest.main(verbosity=2)
