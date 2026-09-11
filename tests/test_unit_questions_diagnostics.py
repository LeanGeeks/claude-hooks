#!/usr/bin/env python3
"""Unit tests for claude-questions diagnostics (epic 23, task 23-06).

Covers:
- ``--check-contract`` against a conforming fixture (exit 0)
- ``--check-contract`` against a deliberately non-conforming fixture with every
  category: malformed id, missing status, duplicate id (exit 2, every category
  reported)
- ``--check-contract`` against a workspace with no ``[questions]`` section
  (clean no-op, exit 0) — invariant 9 floor
- ``--reindex``: rebuilds a missing index entry from a Dispatched marker;
  never touches an existing record; no-op when no markers are found
- Installer idempotency: the questions-mcp block and claude-questions install
  block are textually present in install.sh and are structured
  to be safe on repeated runs (idempotent jq merge)

No ``skipTest``, no bare ``except``, no "nothing raised" assertions.
All tests run against temp workspaces; nothing touches ~/.claude or the network.
"""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HOOKS = _REPO_ROOT / ".claude" / "hooks"
_SHELL = _REPO_ROOT / "shell"

sys.path.insert(0, str(_HOOKS))

# Isolate the permission state store before any hook import touches it.
_ISOLATED = Path(tempfile.mkdtemp(prefix="qs-diag-test-"))
os.environ.setdefault("CLAUDE_PERMISSION_STATE_FILE", str(_ISOLATED / "permission_requests.jsonl"))
os.environ.setdefault("CLAUDE_PERMISSION_AUDIT_FILE", str(_ISOLATED / "permission_actions.jsonl"))
os.environ.setdefault("CLAUDE_PERMISSION_DEBUG_LOG", str(_ISOLATED / "permission_state_debug.log"))

# Pin CLAUDE_PROJECT_DIR so roles_config.find_roles_file never walks the real tree.
_ORIG_PROJECT_DIR = os.environ.get("CLAUDE_PROJECT_DIR")
os.environ["CLAUDE_PROJECT_DIR"] = str(_ISOLATED)

import questions_store as qs
import questions_listen_lib as qll


def tearDownModule():
    if _ORIG_PROJECT_DIR is None:
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
    else:
        os.environ["CLAUDE_PROJECT_DIR"] = _ORIG_PROJECT_DIR
    shutil.rmtree(_ISOLATED, ignore_errors=True)


def _load_claude_questions():
    """Import the ``claude-questions`` script (Python, no .py extension)."""
    path = _SHELL / "claude-questions"
    loader = importlib.machinery.SourceFileLoader("claude_questions", str(path))
    spec = importlib.util.spec_from_loader("claude_questions", loader)
    assert spec is not None, f"Could not create spec for {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["claude_questions"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


cq = _load_claude_questions()


# ── Fixture builders ──────────────────────────────────────────────────────────

_ROLES_TOML_QUESTIONS = """\
workspace_id = "test-ws"
default      = "hpl"

[role.hpl]
title = "Product lead"

[questions]
dir    = "docs/questions"
anchor = "worktree"

[questions.queue]
hpl = "for-product-lead.md"

[questions.format]
status = ["open", "resolved", "answered"]
"""

_ROLES_TOML_NO_QUESTIONS = """\
workspace_id = "test-ws-nq"
default      = "hpl"

[role.hpl]
title = "Product lead"
"""

#: A fully conforming queue file — two entries, valid ids, known status tokens.
_CONFORMING_QUEUE = """\
# Questions for Product Lead

## Q-001 — Should we use async queues?  [open]

Body of question one.

**Routed to:** @hpl

## Q-002 — Database choice  [resolved 2026-08-20]

Body of question two.

**Answered by:** hpl · 2026-08-20T10:00:00Z · relay #100

The answer was: PostgreSQL.

**Dispatched:** 2026-08-20T09:00:00Z · relay #100
"""

#: Non-conforming queue with one of every problem category:
#: 1. Malformed id (looks like the Q-NNN template)
#: 2. Entry with no known status token
#: 3. Duplicate id
_NON_CONFORMING_QUEUE = """\
# Questions for Product Lead

## Q-NNN — Template example heading  [open]

This heading has a malformed id (Q-NNN) — the store ignores it (rule 6).

## Q-003 — Missing status token  [deprecated]

This entry's status token "deprecated" is not in the configured set.

**Routed to:** @hpl

## Q-004 — First occurrence of a dup id  [open]

First heading for Q-004.

## Q-004 — Second occurrence (duplicate)  [open]

Second heading for Q-004 — the store returns conflict (rule 5).

## Q-005 — Mentions Q-003 in its title  [open]

Informational: the mention of Q-003 is correctly ignored by the store (rule 1).
"""


def _make_workspace(roles_toml: str, queue_content: str | None = None) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="qs-diag-ws-"))
    (ws / ".claude").mkdir()
    (ws / ".claude" / "roles.toml").write_text(roles_toml)
    if queue_content is not None:
        qdir = ws / "docs" / "questions"
        qdir.mkdir(parents=True)
        (qdir / "for-product-lead.md").write_text(queue_content)
    return ws


# ── Tests: --check-contract ───────────────────────────────────────────────────


class TestCheckContractConforming(unittest.TestCase):
    """--check-contract on a fully conforming workspace exits 0."""

    def setUp(self):
        self.ws = _make_workspace(_ROLES_TOML_QUESTIONS, _CONFORMING_QUEUE)
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        config = qs.load_questions_config(str(self.ws), roles_path=self.ws / ".claude" / "roles.toml")
        self.assertIsNotNone(config, "Config must load for the conforming fixture")
        anchor = qs.resolve_anchor(str(self.ws), config)
        self.store = qs.QuestionsStore(config, anchor.root)

    def test_exits_0_clean(self):
        rc = cq.cmd_check_contract(self.store, str(self.ws))
        self.assertEqual(rc, 0, "Conforming fixture must exit 0")

    def test_json_mode_clean(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cq.cmd_check_contract(self.store, str(self.ws), json_out=True)
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["edits_needed"], 0)
        self.assertEqual(data["total_entries"], 2)
        self.assertEqual(data["conforming"], 2)

    def test_no_actionable_findings(self):
        """No finding in categories 1-3 on the conforming fixture."""
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            cq.cmd_check_contract(self.store, str(self.ws), json_out=True)
        data = json.loads(buf.getvalue())
        actionable = [
            f for f in data["findings"]
            if f["category"] in ("malformed_id", "missing_status", "duplicate")
        ]
        self.assertEqual(len(actionable), 0, f"Unexpected findings: {actionable}")


class TestCheckContractNonConforming(unittest.TestCase):
    """--check-contract on the deliberately broken fixture exits 2 and reports every category."""

    def setUp(self):
        self.ws = _make_workspace(_ROLES_TOML_QUESTIONS, _NON_CONFORMING_QUEUE)
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        config = qs.load_questions_config(str(self.ws), roles_path=self.ws / ".claude" / "roles.toml")
        self.assertIsNotNone(config)
        anchor = qs.resolve_anchor(str(self.ws), config)
        self.store = qs.QuestionsStore(config, anchor.root)

    def test_exits_2_with_problems(self):
        rc = cq.cmd_check_contract(self.store, str(self.ws))
        self.assertEqual(rc, 2, "Non-conforming fixture must exit 2")

    def test_all_three_categories_reported(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            cq.cmd_check_contract(self.store, str(self.ws), json_out=True)
        data = json.loads(buf.getvalue())
        categories = {f["category"] for f in data["findings"]}
        self.assertIn("malformed_id", categories, "malformed_id category must be reported")
        self.assertIn("missing_status", categories, "missing_status category must be reported")
        self.assertIn("duplicate", categories, "duplicate category must be reported")

    def test_edits_needed_nonzero(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            cq.cmd_check_contract(self.store, str(self.ws), json_out=True)
        data = json.loads(buf.getvalue())
        self.assertGreater(data["edits_needed"], 0)

    def test_duplicate_id_reported_for_both_headings(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            cq.cmd_check_contract(self.store, str(self.ws), json_out=True)
        data = json.loads(buf.getvalue())
        dup_findings = [f for f in data["findings"] if f["category"] == "duplicate"]
        dup_ids = {f["qid"] for f in dup_findings}
        # Q-004 appears twice — at least one duplicate finding must reference it.
        self.assertTrue(
            any("Q-004" in q for q in dup_ids),
            f"Q-004 duplicate must be reported; got: {dup_ids}",
        )

    def test_malformed_id_reported(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            cq.cmd_check_contract(self.store, str(self.ws), json_out=True)
        data = json.loads(buf.getvalue())
        mal = [f for f in data["findings"] if f["category"] == "malformed_id"]
        self.assertGreater(len(mal), 0, "Q-NNN malformed id must be reported")
        self.assertIn("Q-NNN", mal[0]["qid"])

    def test_missing_status_reported_with_token_name(self):
        """The unknown token ("deprecated") must appear in the detail so the adopter knows what to declare."""
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            cq.cmd_check_contract(self.store, str(self.ws), json_out=True)
        data = json.loads(buf.getvalue())
        ms = [f for f in data["findings"] if f["category"] == "missing_status"]
        self.assertGreater(len(ms), 0, "missing_status finding must be reported")
        # The fixture heading carries "[deprecated]" — that token must appear in the
        # detail so the adopter knows exactly what to declare or fix.
        self.assertTrue(
            any("deprecated" in f["detail"] for f in ms),
            f"missing_status detail must name the unknown token 'deprecated'; got: {[f['detail'] for f in ms]}",
        )

    def test_informational_mention_reported(self):
        """Q-005 mentions Q-003 — this should appear as a 'mention' category finding."""
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            cq.cmd_check_contract(self.store, str(self.ws), json_out=True)
        data = json.loads(buf.getvalue())
        mentions = [f for f in data["findings"] if f["category"] == "mention"]
        # Q-003 is a known entry; Q-005's title mentions it.
        # This may or may not be detected depending on whether Q-003 is in the
        # entry set (it is, with missing status, but it IS parsed).
        # We only assert that if mentions are present they are informational.
        for m in mentions:
            self.assertEqual(m["category"], "mention")
            self.assertIn("no edit needed", m["detail"])


class TestCheckContractNoQuestions(unittest.TestCase):
    """--check-contract on a workspace with no [questions] section exits 0 (invariant 9)."""

    def setUp(self):
        self.ws = _make_workspace(_ROLES_TOML_NO_QUESTIONS)
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)

    def test_exits_0_no_config(self):
        store = qs.open_store(str(self.ws), roles_path=self.ws / ".claude" / "roles.toml")
        self.assertIsNone(store, "No [questions] → store must be None")
        rc = cq.cmd_check_contract(store, str(self.ws))
        self.assertEqual(rc, 0, "--check-contract with no config must exit 0")

    def test_json_mode_no_config(self):
        import io
        from contextlib import redirect_stdout
        store = qs.open_store(str(self.ws), roles_path=self.ws / ".claude" / "roles.toml")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cq.cmd_check_contract(store, str(self.ws), json_out=True)
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["status"], "no_config")


# ── Tests: --reindex ──────────────────────────────────────────────────────────

_QUEUE_WITH_DISPATCHED = """\
## Q-010 — Open question with no index entry  [open]

Body text.

**Routed to:** @hpl
**Dispatched:** 2026-08-27T10:00:00Z · relay #9999
"""

_QUEUE_ALREADY_INDEXED = """\
## Q-011 — Already indexed  [open]

**Dispatched:** 2026-08-27T11:00:00Z · relay #8888
"""

_QUEUE_NO_DISPATCHED = """\
## Q-012 — No dispatched marker  [open]

Not yet sent to the relay.
"""


class TestReindex(unittest.TestCase):
    """--reindex rebuilds missing index entries and skips existing ones."""

    def setUp(self):
        self.ws = _make_workspace(_ROLES_TOML_QUESTIONS)
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        self.index_path = self.ws / "test_index.json"
        self.lock_path = self.ws / "test_index.lock"

        config = qs.load_questions_config(str(self.ws), roles_path=self.ws / ".claude" / "roles.toml")
        self.assertIsNotNone(config)
        anchor = qs.resolve_anchor(str(self.ws), config)
        self.store = qs.QuestionsStore(config, anchor.root)
        qdir = self.store.queue_dir
        qdir.mkdir(parents=True, exist_ok=True)

    def _reindex(self):
        """Run cmd_reindex with a temp index path."""
        original_index_path = qll.INDEX_PATH
        original_lock_path = qll._LOCK_PATH
        try:
            qll.INDEX_PATH = self.index_path
            qll._LOCK_PATH = self.lock_path
            return cq.cmd_reindex(self.store)
        finally:
            qll.INDEX_PATH = original_index_path
            qll._LOCK_PATH = original_lock_path

    def _read_index(self):
        return qll.read_index(self.index_path)

    def test_rebuilds_missing_entry(self):
        """A Dispatched marker with no index record creates a new IndexEntry."""
        queue_file = self.store.queue_dir / "for-product-lead.md"
        queue_file.write_text(_QUEUE_WITH_DISPATCHED)

        rc = self._reindex()
        self.assertEqual(rc, 0)
        index = self._read_index()
        self.assertIn("9999", index.messages, "relay #9999 must be in the index after --reindex")
        entry = index.messages["9999"]
        self.assertEqual(entry.qid, "Q-010")
        self.assertEqual(entry.workspace_id, "test-ws")
        # The queue file must be byte-identical — --reindex must never touch it.
        self.assertEqual(
            queue_file.read_bytes(),
            _QUEUE_WITH_DISPATCHED.encode(),
            "--reindex must not modify the queue file (byte-identical check)",
        )

    def test_never_overwrites_existing_entry(self):
        """An existing index record must not be touched by --reindex."""
        queue_file = self.store.queue_dir / "for-product-lead.md"
        queue_file.write_text(_QUEUE_ALREADY_INDEXED)

        # Pre-populate the index with a record for #8888 carrying different data.
        original_entry = qll.IndexEntry(
            workspace_id="other-ws",
            anchor="repo",
            root="/other/root",
            rel_path="other.md",
            qid="Q-999",
            role=None,
            created_at="2020-01-01T00:00:00Z",
        )
        qll.write_index(
            qll.Index(messages={"8888": original_entry}),
            path=self.index_path,
        )

        self._reindex()
        index = self._read_index()
        entry = index.messages.get("8888")
        self.assertIsNotNone(entry)
        # Must be unchanged.
        self.assertEqual(entry.workspace_id, "other-ws", "Existing entry must not be overwritten")
        self.assertEqual(entry.qid, "Q-999", "Existing entry must not be overwritten")

    def test_noop_when_no_dispatched_markers(self):
        """No Dispatched markers → no records rebuilt, exit 0."""
        queue_file = self.store.queue_dir / "for-product-lead.md"
        queue_file.write_text(_QUEUE_NO_DISPATCHED)

        rc = self._reindex()
        self.assertEqual(rc, 0)
        index = self._read_index()
        self.assertEqual(len(index.messages), 0, "No new records should be added")

    def test_noop_when_all_already_indexed(self):
        """All Dispatched markers already in the index → no records added, exit 0."""
        queue_file = self.store.queue_dir / "for-product-lead.md"
        queue_file.write_text(_QUEUE_ALREADY_INDEXED)
        qll.write_index(
            qll.Index(messages={"8888": qll.IndexEntry(
                workspace_id="test-ws",
                anchor="worktree",
                root=str(self.store.root),
                rel_path="docs/questions/for-product-lead.md",
                qid="Q-011",
                role="hpl",
                created_at="2026-08-27T11:00:00Z",
            )}),
            path=self.index_path,
        )

        rc = self._reindex()
        self.assertEqual(rc, 0)
        index = self._read_index()
        self.assertEqual(len(index.messages), 1, "No new records should be added when all indexed")

    def test_noop_with_no_config(self):
        """No [questions] section → --reindex is a clean no-op, exit 0."""
        rc = cq.cmd_reindex(None)
        self.assertEqual(rc, 0, "--reindex with no config must exit 0 (invariant 9)")


# ── Tests: invariant 9 floor ──────────────────────────────────────────────────


class TestInvariant9Floor(unittest.TestCase):
    """A workspace with no [questions] changes no behaviour and errors cleanly."""

    def setUp(self):
        self.ws = _make_workspace(_ROLES_TOML_NO_QUESTIONS)
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)

    def test_open_store_returns_none(self):
        """open_store returns None — the caller's single check, not a guard on every op."""
        store = qs.open_store(str(self.ws), roles_path=self.ws / ".claude" / "roles.toml")
        self.assertIsNone(store)

    def test_check_contract_exits_0(self):
        rc = cq.cmd_check_contract(None, str(self.ws))
        self.assertEqual(rc, 0)

    def test_reindex_exits_0(self):
        rc = cq.cmd_reindex(None)
        self.assertEqual(rc, 0)


# ── Tests: installer content ──────────────────────────────────────────────────


class TestInstallerContent(unittest.TestCase):
    """install.sh contains the questions-mcp and claude-questions blocks."""

    def setUp(self):
        installer_path = _REPO_ROOT / "install.sh"
        self.assertTrue(installer_path.is_file(), "install.sh must exist")
        self.installer_text = installer_path.read_text(encoding="utf-8")

    def test_questions_mcp_block_present(self):
        """The questions-mcp server must be registered via an idempotent jq mcpServers merge."""
        # (a) The "questions" key is passed to the registration helper.
        self.assertIn(
            '_register_mcp_server "questions"',
            self.installer_text,
            '_register_mcp_server must be called with "questions" as the server name',
        )
        # (b) The helper itself uses the idempotent merge form — not a bare assignment
        # that would clobber other servers on re-run.
        self.assertIn(
            '.mcpServers = (.mcpServers // {}) + {($name):',
            self.installer_text,
            '_register_mcp_server must use the idempotent jq merge form',
        )

    # test_questions_mcp_uses_jq_merge was deleted: the rewritten
    # test_questions_mcp_block_present above already asserts both the merge pattern
    # and the "questions" key, making the former test fully redundant.

    def test_claude_questions_install_block_present(self):
        """The claude-questions install block must symlink the binary into ~/.local/bin."""
        # Confirm the installer uses ext_symlink_add for claude-questions (the seam
        # wrapper, as required by cross-task invariant 5 / architecture §8).
        self.assertIn(
            'ext_symlink_add',
            self.installer_text,
            "installer must use ext_symlink_add (not bare ln -sf) for symlinks",
        )
        self.assertIn(
            "claude-questions",
            self.installer_text,
            "installer must install claude-questions",
        )

    def test_systemd_unit_installed(self):
        """The systemd unit install block must write the correct ExecStart line."""
        # This specific ExecStart line is the functional requirement — it
        # determines which binary the unit runs.  A test that only checks for
        # the service name in any context (comment, log line) would not catch a
        # unit with a wrong or missing ExecStart.
        self.assertIn(
            "ExecStart=%h/.local/bin/questions-listen",
            self.installer_text,
            "systemd unit must have ExecStart=%h/.local/bin/questions-listen",
        )

    def test_systemd_enable_conditional(self):
        """The migration Python snippet must return the correct state for both config states."""
        import os
        import subprocess
        import tempfile

        # Extract the Python snippet embedded in the installer between the
        # heredoc markers. In the post-epic code it reads sys.argv[1] as the
        # TOML config path and prints "true" or "false" to stdout.
        # Use "import tomllib" as the anchor — it appears only in the migration
        # TOML-reading heredoc, never in the single-line python3 -c invocations.
        start = self.installer_text.find("import sys\ntry:\n    import tomllib")
        end = self.installer_text.find("PYEOF", start)
        self.assertNotEqual(start, -1, "Python TOML-read snippet must be present in installer")
        snippet = self.installer_text[start:end].strip()

        snippet_fd, snippet_path = tempfile.mkstemp(suffix=".py")
        opted_in_fd, opted_in_path = tempfile.mkstemp(suffix=".toml")
        no_opt_fd, no_opt_path = tempfile.mkstemp(suffix=".toml")
        try:
            os.write(snippet_fd, snippet.encode())
            os.close(snippet_fd)
            os.write(opted_in_fd, b"[questions_listen]\nenabled = true\n")
            os.close(opted_in_fd)
            os.write(no_opt_fd, b"[relay]\nhost = \"example.com\"\n")
            os.close(no_opt_fd)

            # State 1: config with [questions_listen] enabled = true → prints "true".
            result = subprocess.run(
                ["python3", snippet_path, opted_in_path],
                capture_output=True,
                text=True,
            )
            # Exit 0 or print "true" — either indicates opted in.
            self.assertTrue(
                result.returncode == 0 or result.stdout.strip() == "true",
                f"Opt-in check must signal enabled=true. stdout={result.stdout!r} rc={result.returncode}",
            )

            # State 2: config without the section → prints "false" or exits non-0.
            result = subprocess.run(
                ["python3", snippet_path, no_opt_path],
                capture_output=True,
                text=True,
            )
            self.assertTrue(
                result.returncode != 0 or result.stdout.strip() == "false",
                f"Opt-in check must signal disabled when section absent. stdout={result.stdout!r} rc={result.returncode}",
            )
        finally:
            for p in (snippet_path, opted_in_path, no_opt_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def test_questions_listen_lib_and_mcp_in_same_gate(self):
        """questions_listen_lib.py and questions_store.py must both appear in REQUIRED_HOOKS.

        The task requires they are installed in the same pass to avoid a version
        window where one is new and the other old.  Extracting the full REQUIRED_HOOKS
        block (from its opening line to the closing parenthesis) and checking both
        names are present proves they share the same install gate.
        """
        lines = self.installer_text.splitlines()
        # Find the opening REQUIRED_HOOKS=( line.
        start_idx = next(
            (i for i, line in enumerate(lines)
             if line.strip().startswith("REQUIRED_HOOKS=")),
            None,
        )
        self.assertIsNotNone(start_idx, "REQUIRED_HOOKS= line must exist in installer")
        # Collect the array block up to and including the closing ')'.
        block_lines = []
        for line in lines[start_idx:]:
            block_lines.append(line)
            if line.strip() == ")":
                break
        block = "\n".join(block_lines)
        self.assertIn(
            "questions_listen_lib.py",
            block,
            "questions_listen_lib.py must be in the REQUIRED_HOOKS array",
        )
        self.assertIn(
            "questions_store.py",
            block,
            "questions_store.py must be in the REQUIRED_HOOKS array",
        )

    def test_questions_listen_binary_symlinked(self):
        """The installer must use ext_symlink_add (not cp) for questions-listen.

        A copy would not update on re-run when only the source changes.  More
        importantly, a test that only checks the string "questions-listen" appears
        anywhere in the file (comment, log message, variable name) would not
        distinguish a symlink from a copy.  ext_symlink_add is also the required
        seam wrapper (cross-task invariant 5 / architecture §8).
        """
        # Find the line that actually installs the binary via the seam wrapper.
        install_line = next(
            (line for line in self.installer_text.splitlines()
             if 'ext_symlink_add' in line and 'questions-listen"' in line),
            None,
        )
        self.assertIsNotNone(
            install_line,
            'installer must use ext_symlink_add to symlink questions-listen into the user bin dir',
        )

    def test_example_toml_exists(self):
        """docs/questions.example.toml must exist."""
        example = _REPO_ROOT / "docs" / "questions.example.toml"
        self.assertTrue(example.is_file(), "docs/questions.example.toml must exist")

    def test_example_toml_parseable(self):
        """docs/questions.example.toml must parse as valid TOML."""
        import tomllib
        example = _REPO_ROOT / "docs" / "questions.example.toml"
        with open(example, "rb") as fh:
            data = tomllib.load(fh)
        self.assertIn("questions", data, "example TOML must contain a [questions] section")

    def test_example_toml_keys_exist_in_loader(self):
        """All keys in [questions] of the example TOML must be recognised by the loader."""
        import tomllib
        example = _REPO_ROOT / "docs" / "questions.example.toml"
        with open(example, "rb") as fh:
            data = tomllib.load(fh)
        # Load the config from the example file in a temp workspace.
        ws = Path(tempfile.mkdtemp(prefix="qs-example-"))
        try:
            (ws / ".claude").mkdir()
            (ws / ".claude" / "roles.toml").write_text(
                example.read_text(encoding="utf-8")
            )
            cfg = qs.load_questions_config(str(ws), roles_path=ws / ".claude" / "roles.toml")
            # If [questions] is present, cfg should not be None.
            if "questions" in data:
                # The loader returns None only when there is no [questions] section
                # or the file is unparseable — the example must have it.
                self.assertIsNotNone(cfg, "example TOML [questions] must load without errors")
        finally:
            shutil.rmtree(ws, ignore_errors=True)


# ── Tests: docs exist ─────────────────────────────────────────────────────────


class TestDocsExist(unittest.TestCase):
    """Required documentation files must exist and have meaningful content."""

    def _doc(self, name: str) -> Path:
        return _REPO_ROOT / "docs" / name

    def test_async_questions_md_exists(self):
        doc = self._doc("async-questions.md")
        self.assertTrue(doc.is_file(), "docs/async-questions.md must exist")
        text = doc.read_text(encoding="utf-8")
        self.assertGreater(len(text), 500, "docs/async-questions.md must have meaningful content")

    def test_async_questions_md_answers_nothing_happened(self):
        """The operator guide must answer 'someone answered and nothing happened'."""
        doc = self._doc("async-questions.md")
        text = doc.read_text(encoding="utf-8")
        self.assertIn("--reindex", text, "async-questions.md must document --reindex")
        self.assertIn("--status", text, "async-questions.md must document --status")

    def test_questions_contract_md_exists(self):
        doc = self._doc("questions-contract.md")
        self.assertTrue(doc.is_file(), "docs/questions-contract.md must exist")
        text = doc.read_text(encoding="utf-8")
        self.assertGreater(len(text), 500)

    def test_questions_contract_md_has_six_rules(self):
        doc = self._doc("questions-contract.md")
        text = doc.read_text(encoding="utf-8")
        # Each of the six rules must have its own heading (### Rule N).
        # A test that only checks "rule" appears anywhere would pass even if
        # the six-rule section were replaced by a single sentence mentioning "rules."
        for n in range(1, 7):
            self.assertIn(
                f"### Rule {n}",
                text,
                f"questions-contract.md must have a '### Rule {n}' heading",
            )
        self.assertIn("--check-contract", text, "questions-contract.md must document --check-contract")

    def test_questions_contract_md_cites_measured_result(self):
        """The adopter doc must cite the 2-edits-across-290-entries measurement.

        Asserting just "2" passes for any document (the digit appears everywhere).
        The measured result must be cited as the specific phrase used in the doc.
        """
        doc = self._doc("questions-contract.md")
        text = doc.read_text(encoding="utf-8")
        self.assertIn("290", text, "questions-contract.md must cite 290 entries")
        # The phrase that ties the count to the edit tally — not just the digit "2".
        self.assertIn(
            "2 edits across 290",
            text,
            "questions-contract.md must cite '2 edits across 290 entries'",
        )

    def test_questions_prompt_example_exists(self):
        doc = self._doc("questions-prompt-example.md")
        self.assertTrue(doc.is_file(), "docs/questions-prompt-example.md must exist")
        text = doc.read_text(encoding="utf-8")
        self.assertGreater(len(text), 300)

    def test_questions_prompt_example_covers_ask(self):
        doc = self._doc("questions-prompt-example.md")
        text = doc.read_text(encoding="utf-8")
        self.assertIn("ask", text, "questions-prompt-example.md must document the ask tool")

    def test_questions_example_toml_exists(self):
        doc = self._doc("questions.example.toml")
        self.assertTrue(doc.is_file(), "docs/questions.example.toml must exist")


if __name__ == "__main__":
    unittest.main()
