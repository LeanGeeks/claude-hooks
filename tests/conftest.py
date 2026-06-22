"""Pytest session setup: isolate the permission state store.

Without this, tests call ``create_request`` / ``update_request_state`` against
the real ``~/.claude/permission_requests.jsonl`` (and audit/debug logs),
polluting a developer's live store with ``test-*`` rows. ``permission_state_store``
reads these paths from env vars at import, so we point them at a throwaway temp
dir *before* any test module imports the store.

``setdefault`` is used so an explicit outer override (e.g. CI) still wins.
"""

import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="claude-hooks-tests-"))
os.environ.setdefault("CLAUDE_PERMISSION_STATE_FILE", str(_tmp / "permission_requests.jsonl"))
os.environ.setdefault("CLAUDE_PERMISSION_AUDIT_FILE", str(_tmp / "permission_actions.jsonl"))
os.environ.setdefault("CLAUDE_PERMISSION_DEBUG_LOG", str(_tmp / "permission_state_debug.log"))
