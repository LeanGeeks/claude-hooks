#!/usr/bin/env python3
"""Unit tests for questions_store.py (epic 23, task 23-03).

Every fixture in this file is **written to the contract**
(``tasks/23_async_questions/architecture.md`` §3.2) — none is copied from any
live workspace file, and no test reads anything outside its own temp dir.

Isolation rules:
- ``CLAUDE_PROJECT_DIR`` is pinned to a temp dir for the whole module so
  ``roles_config.find_roles_file`` can never walk into the developer's tree.
- Every workspace is built under ``tempfile.mkdtemp``; nothing touches
  ``~/.claude``.
- No network, no git binary: anchor resolution reads ``.git`` metadata
  directly, so the git fixtures here are plain directories and files.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────

_HOOKS = Path(__file__).parent.parent / ".claude" / "hooks"
sys.path.insert(0, str(_HOOKS))

# Must be set before any call into roles_config: pointing it at an empty temp
# dir suppresses the upward filesystem walk entirely.
_ISOLATION_DIR = tempfile.mkdtemp(prefix="questions-store-test-")
_ORIGINAL_PROJECT_DIR = os.environ.get("CLAUDE_PROJECT_DIR")
os.environ["CLAUDE_PROJECT_DIR"] = _ISOLATION_DIR

import questions_store as qs  # noqa: E402


def tearDownModule():
    if _ORIGINAL_PROJECT_DIR is None:
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
    else:
        os.environ["CLAUDE_PROJECT_DIR"] = _ORIGINAL_PROJECT_DIR
    shutil.rmtree(_ISOLATION_DIR, ignore_errors=True)


# ── Fixture helpers ───────────────────────────────────────────────────────────

ROLES_TOML = """\
workspace_id = "demo-ws"
default      = "hpl"

[role.hpl]
title = "Product lead"

[role.htl]
title = "Tech lead"

[role.operator]
title = "Operator"

[questions]
dir    = "docs/questions"
anchor = "worktree"

[questions.queue]
hpl = "for-product-lead.md"
htl = "for-tech-lead.md"
"""


class StoreTestCase(unittest.TestCase):
    """Builds a throwaway workspace per test."""

    roles_toml = ROLES_TOML

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="qs-ws-"))
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        (self.ws / ".claude").mkdir(parents=True, exist_ok=True)
        self.roles_path = self.ws / ".claude" / "roles.toml"
        self.roles_path.write_text(self.roles_toml, encoding="utf-8")

    # ── helpers ──

    def store(self, roles_toml: str | None = None) -> qs.QuestionsStore:
        if roles_toml is not None:
            self.roles_path.write_text(roles_toml, encoding="utf-8")
        config = qs.load_questions_config(str(self.ws), roles_path=self.roles_path)
        self.assertIsNotNone(config, "expected a [questions] section")
        return qs.store_for_root(config, self.ws)

    def write_queue(self, name: str, body: str) -> Path:
        path = self.ws / "docs" / "questions" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        return path


# ── Contract rule 1 — an entry BEGINS with a well-formed id ───────────────────


class TestRule1EntryBeginsWithId(StoreTestCase):
    MENTIONS = """\
        # Questions for the product lead

        ## Q-388 — Delete-account modal: revisit after Q-253  [open]

        Body of 388.

        ## Q-142 — Old thing  [open]  [SUPERSEDED by Q-269]

        Body of 142.

        ## Q-253 — The real entry for 253  [open]

        Body of 253.

        ## Q-269 — The real entry for 269  [open]

        Body of 269.
        """

    def test_mentioned_id_resolves_to_its_own_entry(self):
        path = self.write_queue("for-product-lead.md", self.MENTIONS)
        store = self.store()

        result = store.apply_answer("Q-253", "yes", "@hpl", 11, role="hpl")
        self.assertIs(result, qs.ApplyResult.APPLIED)

        text = path.read_text(encoding="utf-8")
        # The mentioning entry is untouched…
        self.assertIn("## Q-388 — Delete-account modal: revisit after Q-253  [open]", text)
        # …and the mentioned entry is the one that flipped.
        self.assertIn("## Q-253 — The real entry for 253  [resolved]", text)

    def test_bracket_mention_does_not_capture_the_id(self):
        path = self.write_queue("for-product-lead.md", self.MENTIONS)
        store = self.store()

        self.assertIs(store.apply_answer("Q-269", "ok", "@hpl", 12, role="hpl"), qs.ApplyResult.APPLIED)
        text = path.read_text(encoding="utf-8")
        self.assertIn("## Q-142 — Old thing  [open]  [SUPERSEDED by Q-269]", text)
        self.assertIn("## Q-269 — The real entry for 269  [resolved]", text)

    def test_answer_lands_under_the_right_entry(self):
        path = self.write_queue("for-product-lead.md", self.MENTIONS)
        store = self.store()
        store.apply_answer("Q-253", "the answer text", "@hpl", 13, role="hpl")

        lines = path.read_text(encoding="utf-8").split("\n")
        heading = next(i for i, l in enumerate(lines) if l.startswith("## Q-253"))
        boundary = next(i for i, l in enumerate(lines) if l.startswith("## Q-269"))
        answered = next(i for i, l in enumerate(lines) if l.startswith("<!-- answer:13"))
        self.assertTrue(heading < answered < boundary)

    def test_heading_at_another_level_is_not_an_entry(self):
        self.write_queue(
            "for-product-lead.md",
            """\
            ### Q-450 item 4 — MOVED to the tech-lead queue  [open]

            Not an entry: wrong level.
            """,
        )
        store = self.store()
        self.assertEqual(store.entries_in(self.ws / "docs/questions/for-product-lead.md"), [])
        self.assertIs(
            store.apply_answer("Q-450", "x", "@hpl", 14, role="hpl"), qs.ApplyResult.NOT_FOUND
        )


# ── Contract rule 2 — ids, variants and max + 1 allocation ────────────────────


class TestRule2Ids(StoreTestCase):
    def test_variant_suffix_is_a_distinct_id(self):
        path = self.write_queue(
            "for-product-lead.md",
            """\
            ## Q-200 — Base entry  [open]

            Base.

            ## Q-200-ds — Designer split  [open]

            Split.
            """,
        )
        store = self.store()
        entries = store.entries_in(path)
        self.assertEqual([e.qid for e in entries], ["Q-200", "Q-200-ds"])
        self.assertEqual([e.key for e in entries], [(200, ""), (200, "ds")])

        self.assertIs(store.apply_answer("Q-200-ds", "a", "@hpl", 20, role="hpl"), qs.ApplyResult.APPLIED)
        text = path.read_text(encoding="utf-8")
        self.assertIn("## Q-200 — Base entry  [open]", text)
        self.assertIn("## Q-200-ds — Designer split  [resolved]", text)

    def test_allocation_is_max_plus_one_and_never_backfills_a_gap(self):
        self.write_queue(
            "for-product-lead.md",
            """\
            ## Q-001 — one  [resolved]

            ## Q-005 — five  [resolved]

            ## Q-471 — high water  [open]
            """,
        )
        store = self.store()
        self.assertEqual(store.next_id(), "Q-472")
        created = store.create_entry(title="new", body="b", role="hpl", routed_to="hpl")
        self.assertEqual(created.qid, "Q-472")

    def test_allocation_spans_every_queue_file(self):
        self.write_queue("for-product-lead.md", "## Q-003 — a  [open]\n")
        self.write_queue("for-tech-lead.md", "## Q-050 — b  [open]\n")
        store = self.store()
        self.assertEqual(store.create_entry(title="n", role="hpl", routed_to="hpl").qid, "Q-051")

    def test_answered_directory_participates_in_allocation(self):
        self.write_queue("for-product-lead.md", "## Q-003 — a  [open]\n")
        answered = self.ws / "docs" / "questions" / "answered"
        answered.mkdir(parents=True)
        (answered / "Q-300.md").write_text("## Q-300 — archived  [resolved]\n", encoding="utf-8")
        store = self.store()
        self.assertEqual(store.next_id(), "Q-301")

    def test_answered_sidecar_filename_alone_participates(self):
        self.write_queue("for-product-lead.md", "## Q-003 — a  [open]\n")
        answered = self.ws / "docs" / "questions" / "answered"
        answered.mkdir(parents=True)
        # A sidecar written by 23-05's fallback: a note, no entry heading.
        (answered / "Q-412.md").write_text("Answer for a question that vanished.\n", encoding="utf-8")
        store = self.store()
        self.assertEqual(store.next_id(), "Q-413")

    def test_malformed_id_contributes_nothing_to_the_high_water_mark(self):
        self.write_queue(
            "for-product-lead.md",
            """\
            ## Q-NNN — <short title>  [open]  [blocks: iter-NNN/task-NNN-MMM]

            ## Q-007 — real  [open]
            """,
        )
        store = self.store()
        self.assertEqual(store.next_id(), "Q-008")

    def test_answer_text_cannot_inflate_the_high_water_mark(self):
        # Ids are only ever collected from heading lines, and no escaped answer
        # line can start with '#'.
        self.write_queue("for-product-lead.md", "## Q-003 — a  [open]\n")
        store = self.store()
        store.apply_answer("Q-003", "## Q-9999 — injected  [open]", "@hpl", 30, role="hpl")
        self.assertEqual(store.next_id(), "Q-004")

    def test_first_allocation_in_an_empty_workspace(self):
        store = self.store()
        self.assertEqual(store.create_entry(title="first", role="hpl", routed_to="hpl").qid, "Q-001")


# ── Contract rule 3 — status token, first word, free-text suffix ──────────────


WIDE_STATUS_TOML = ROLES_TOML + """
[questions.format]
status = ["open", "resolved", "answered", "closed", "superseded"]
"""


class TestRule3Status(StoreTestCase):
    def test_free_text_suffix_parses_and_survives_rewrite(self):
        path = self.write_queue(
            "for-product-lead.md",
            """\
            ## Q-013 — Dated  [open 2026-08-13]

            Body.
            """,
        )
        store = self.store()
        entry = store.entries_in(path)[0]
        self.assertEqual(entry.status, "open")

        store.apply_answer("Q-013", "x", "@hpl", 40, role="hpl")
        self.assertIn("## Q-013 — Dated  [resolved 2026-08-13]", path.read_text(encoding="utf-8"))

    def test_decorated_resolved_parses_as_resolved(self):
        path = self.write_queue(
            "for-product-lead.md",
            "## Q-014 — Decorated  [resolved · design-addressed @v14, pin-pending]\n",
        )
        store = self.store()
        self.assertEqual(store.entries_in(path)[0].status, "resolved")

    def test_status_is_not_necessarily_the_first_bracket(self):
        path = self.write_queue(
            "for-product-lead.md",
            "## Q-015 — Decision  [D1]  [open]  [blocks: iter-7]\n",
        )
        store = self.store()
        entry = store.entries_in(path)[0]
        self.assertEqual(entry.status, "open")

        store.apply_answer("Q-015", "x", "@hpl", 41, role="hpl")
        self.assertIn(
            "## Q-015 — Decision  [D1]  [resolved]  [blocks: iter-7]",
            path.read_text(encoding="utf-8"),
        )

    def test_declared_vocabulary_recognises_extra_tokens(self):
        path = self.write_queue(
            "for-product-lead.md",
            """\
            ## Q-016 — Was answered by hand  [answered]

            ## Q-017 — Superseded  [SUPERSEDED by Q-269]

            ## Q-018 — Closed  [closed — superseded]
            """,
        )
        store = self.store(WIDE_STATUS_TOML)
        self.assertEqual([e.status for e in store.entries_in(path)], ["answered", "superseded", "closed"])

    def test_declared_vocabulary_flips_to_the_second_token(self):
        path = self.write_queue("for-product-lead.md", "## Q-016 — Handwritten  [answered]\n")
        store = self.store(WIDE_STATUS_TOML)
        store.apply_answer("Q-016", "x", "@hpl", 42, role="hpl")
        self.assertIn("## Q-016 — Handwritten  [resolved]", path.read_text(encoding="utf-8"))

    def test_default_set_does_not_recognise_an_undeclared_token(self):
        path = self.write_queue("for-product-lead.md", "## Q-016 — Handwritten  [answered]\n")
        store = self.store()  # default set: open, resolved
        entry = store.entries_in(path)[0]
        self.assertIsNone(entry.status)

    def test_open_count_uses_the_open_token_only(self):
        self.write_queue(
            "for-product-lead.md",
            """\
            ## Q-001 — a  [open]

            ## Q-002 — b  [resolved]

            ## Q-NNN — <short title>  [open]

            ## Q-003 — c  [open]
            """,
        )
        store = self.store()
        # The Q-NNN template line is not an entry (rule 6) so it is not counted.
        self.assertEqual(store.count_open(), 2)


# ── Contract rule 4 — boundary ────────────────────────────────────────────────


class TestRule4Boundary(StoreTestCase):
    FIXTURE = """\
        ## Q-001 — first  [open]

        Body one.

        ### A sub-heading inside the entry

        Still entry one.

        ## Q-002 — second  [open]

        Body two.

        # A level-1 heading ends everything
        """

    def test_lower_level_heading_does_not_end_the_entry(self):
        path = self.write_queue("for-product-lead.md", self.FIXTURE)
        store = self.store()
        entries = {e.qid: e for e in store.entries_in(path)}
        lines = path.read_text(encoding="utf-8").split("\n")
        self.assertEqual(lines[entries["Q-001"].end], "## Q-002 — second  [open]")

    def test_answer_is_inserted_before_the_boundary(self):
        path = self.write_queue("for-product-lead.md", self.FIXTURE)
        store = self.store()
        store.apply_answer("Q-001", "answer", "@hpl", 50, role="hpl")
        lines = path.read_text(encoding="utf-8").split("\n")
        answered = next(i for i, l in enumerate(lines) if l.startswith("<!-- answer:50"))
        sub = next(i for i, l in enumerate(lines) if l.startswith("### A sub-heading"))
        boundary = next(i for i, l in enumerate(lines) if l.startswith("## Q-002"))
        self.assertTrue(sub < answered < boundary)

    def test_higher_level_heading_ends_the_last_entry(self):
        path = self.write_queue("for-product-lead.md", self.FIXTURE)
        store = self.store()
        store.apply_answer("Q-002", "answer", "@hpl", 51, role="hpl")
        lines = path.read_text(encoding="utf-8").split("\n")
        answered = next(i for i, l in enumerate(lines) if l.startswith("<!-- answer:51"))
        boundary = next(i for i, l in enumerate(lines) if l.startswith("# A level-1"))
        self.assertLess(answered, boundary)

    def test_last_entry_ends_at_eof(self):
        path = self.write_queue(
            "for-product-lead.md",
            """\
            ## Q-001 — only  [open]

            Body.
            """,
        )
        store = self.store()
        store.apply_answer("Q-001", "answer", "@hpl", 52, role="hpl")
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.endswith("```\n"), text[-40:])


# ── Contract rule 5 — ambiguity refuses ───────────────────────────────────────


class TestRule5AmbiguityRefuses(StoreTestCase):
    DUPLICATE = """\
        ## Q-291 — The original question  [open]

        Body.

        ## Q-291 — ANSWERED: a summary someone pasted in  [resolved]

        Summary.
        """

    def test_two_entry_headings_return_conflict(self):
        path = self.write_queue("for-product-lead.md", self.DUPLICATE)
        before = path.read_bytes()
        store = self.store()
        self.assertIs(
            store.apply_answer("Q-291", "x", "@hpl", 60, role="hpl"), qs.ApplyResult.CONFLICT
        )
        self.assertEqual(path.read_bytes(), before, "a conflict must modify nothing")

    def test_conflict_across_two_queue_files(self):
        a = self.write_queue("for-product-lead.md", "## Q-077 — here  [open]\n")
        b = self.write_queue("for-tech-lead.md", "## Q-077 — and here  [open]\n")
        before = (a.read_bytes(), b.read_bytes())
        store = self.store()
        self.assertIs(store.apply_answer("Q-077", "x", "@hpl", 61), qs.ApplyResult.CONFLICT)
        self.assertEqual((a.read_bytes(), b.read_bytes()), before)

    def test_zero_entries_return_not_found(self):
        path = self.write_queue("for-product-lead.md", "## Q-001 — only  [open]\n")
        before = path.read_bytes()
        store = self.store()
        self.assertIs(
            store.apply_answer("Q-404", "x", "@hpl", 62, role="hpl"), qs.ApplyResult.NOT_FOUND
        )
        self.assertEqual(path.read_bytes(), before)

    def test_mark_dispatched_refuses_the_same_way(self):
        path = self.write_queue("for-product-lead.md", self.DUPLICATE)
        before = path.read_bytes()
        store = self.store()
        self.assertIs(store.mark_dispatched("Q-291", 63, role="hpl"), qs.ApplyResult.CONFLICT)
        self.assertIs(store.mark_dispatched("Q-999", 63, role="hpl"), qs.ApplyResult.NOT_FOUND)
        self.assertEqual(path.read_bytes(), before)

    def test_conflict_is_a_return_value_not_an_exception(self):
        self.write_queue("for-product-lead.md", self.DUPLICATE)
        store = self.store()
        try:
            result = store.apply_answer("Q-291", "x", "@hpl", 64, role="hpl")
        except Exception as exc:  # pragma: no cover - the assertion is the point
            self.fail(f"ambiguity must not raise: {exc!r}")
        self.assertIs(result, qs.ApplyResult.CONFLICT)

    def test_idempotency_outranks_a_later_conflict(self):
        # Applied once, then a duplicate heading appears: the retry must still
        # report success rather than getting stuck on conflict forever.
        path = self.write_queue("for-product-lead.md", "## Q-291 — original  [open]\n")
        store = self.store()
        self.assertIs(store.apply_answer("Q-291", "x", "@hpl", 65, role="hpl"), qs.ApplyResult.APPLIED)
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n## Q-291 — ANSWERED: duplicate  [resolved]\n")
        before = path.read_bytes()
        self.assertIs(store.apply_answer("Q-291", "x", "@hpl", 65, role="hpl"), qs.ApplyResult.APPLIED)
        self.assertEqual(path.read_bytes(), before)


# ── Contract rule 6 — non-conforming headings are ignored, never repaired ─────


class TestRule6NonConforming(StoreTestCase):
    #: The deliberately non-conforming fixture required by the task file: a
    #: malformed id, a duplicate id and a missing status token, side by side
    #: with two entries that do conform.
    FIXTURE = """\
        # Queue for the product lead

        Schema, for humans:

        ## Q-NNN — <short title>  [open]  [blocks: iter-NNN/task-NNN-MMM]

        ## ✅ ANSWER (HTL, 2026-08-19) — Q-265 is CLOSED.

        Not an entry: does not begin with an id.

        ### Q-450 item 4 — MOVED  [open]

        Not an entry: wrong level.

        ## Q-500-toolong — malformed variant  [open]

        Not an entry: the alpha suffix is too long to be an id variant.

        ## Q-291 — duplicate one  [open]

        ## Q-291 — duplicate two  [open]

        ## Q-600 — conforming but status-less

        Body with no status bracket at all.

        ## Q-601 — conforming  [open]

        Body.
        """

    def setUp(self):
        super().setUp()
        self.path = self.write_queue("for-product-lead.md", self.FIXTURE)
        self.before = self.path.read_bytes()
        self.st = self.store()

    def test_only_conforming_headings_are_entries(self):
        self.assertEqual(
            [e.qid for e in self.st.entries_in(self.path)],
            ["Q-291", "Q-291", "Q-600", "Q-601"],
        )

    def test_malformed_ids_are_not_found_and_modify_nothing(self):
        for qid in ("Q-NNN", "Q-265", "Q-450", "Q-500"):
            with self.subTest(qid=qid):
                self.assertIs(
                    self.st.apply_answer(qid, "x", "@hpl", 70, role="hpl"),
                    qs.ApplyResult.NOT_FOUND,
                )
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_duplicate_id_conflicts_and_modifies_nothing(self):
        self.assertIs(
            self.st.apply_answer("Q-291", "x", "@hpl", 71, role="hpl"), qs.ApplyResult.CONFLICT
        )
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_missing_status_token_is_never_repaired(self):
        self.assertIs(
            self.st.apply_answer("Q-600", "x", "@hpl", 72, role="hpl"), qs.ApplyResult.APPLIED
        )
        text = self.path.read_text(encoding="utf-8")
        # The heading is byte-identical: no status bracket was invented.
        self.assertIn("## Q-600 — conforming but status-less\n", text)
        self.assertIn("<!-- answer:72 -->", text)

    def test_non_entry_headings_survive_an_unrelated_write(self):
        self.st.apply_answer("Q-601", "x", "@hpl", 73, role="hpl")
        text = self.path.read_text(encoding="utf-8")
        for line in (
            "## Q-NNN — <short title>  [open]  [blocks: iter-NNN/task-NNN-MMM]",
            "## ✅ ANSWER (HTL, 2026-08-19) — Q-265 is CLOSED.",
            "### Q-450 item 4 — MOVED  [open]",
            "## Q-500-toolong — malformed variant  [open]",
        ):
            self.assertIn(line, text)

    def test_allocation_ignores_malformed_ids_but_not_real_ones(self):
        self.assertEqual(self.st.next_id(), "Q-602")


# ── apply_answer semantics ────────────────────────────────────────────────────


class TestApplyAnswer(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.path = self.write_queue(
            "for-product-lead.md",
            """\
            ## Q-042 — A question  [open]

            Body.

            **Routed to:** @hpl

            ## Q-043 — Another  [open]

            Body.
            """,
        )
        self.st = self.store()

    def test_apply_flips_status_and_inserts_one_block(self):
        self.assertIs(
            self.st.apply_answer("Q-042", "Use JWT.", "@hpl", 80, role="hpl"),
            qs.ApplyResult.APPLIED,
        )
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("## Q-042 — A question  [resolved]", text)
        self.assertEqual(text.count("<!-- answer:80 -->"), 1)
        self.assertIn("**Answered by:** @hpl", text)
        self.assertIn("relay #80", text)
        self.assertIn("Use JWT.", text)

    def test_apply_is_idempotent_on_message_id(self):
        self.st.apply_answer("Q-042", "Use JWT.", "@hpl", 81, role="hpl")
        snapshot = self.path.read_bytes()
        for _ in range(3):
            self.assertIs(
                self.st.apply_answer("Q-042", "Use JWT.", "@hpl", 81, role="hpl"),
                qs.ApplyResult.APPLIED,
            )
        self.assertEqual(self.path.read_bytes(), snapshot)

    def test_a_different_message_id_is_a_second_answer(self):
        self.st.apply_answer("Q-042", "first", "@hpl", 82, role="hpl")
        self.st.apply_answer("Q-042", "second", "@hpl", 83, role="hpl")
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("<!-- answer:82 -->", text)
        self.assertIn("<!-- answer:83 -->", text)

    def test_apply_moves_and_deletes_nothing(self):
        before = self.path.read_text(encoding="utf-8").split("\n")
        self.st.apply_answer("Q-042", "answer", "@hpl", 84, role="hpl")
        after = self.path.read_text(encoding="utf-8").split("\n")

        # Every original line still present, in order, except the one heading
        # whose status token was flipped.
        changed = [l for l in before if l not in after]
        self.assertEqual(changed, ["## Q-042 — A question  [open]"])
        added = [l for l in after if l not in before]
        self.assertIn("## Q-042 — A question  [resolved]", added)
        self.assertIn("<!-- answer:84 -->", added)

    def test_round_trip_allocate_append_answer_reread(self):
        store = self.st
        created = store.create_entry(
            title="Round trip",
            body="Which way?",
            role="hpl",
            options=["Left", "Right"],
            tags=["blocks: iter-7"],
            routed_to="hpl",
        )
        self.assertIs(store.mark_dispatched(created.qid, 85, role="hpl"), qs.ApplyResult.APPLIED)
        self.assertIs(
            store.apply_answer(created.qid, "Left.", "@hpl", 85, role="hpl"), qs.ApplyResult.APPLIED
        )

        entries = {e.qid: e for e in store.entries_in(created.path)}
        self.assertIn(created.qid, entries)
        self.assertEqual(entries[created.qid].status, "resolved")
        text = created.path.read_text(encoding="utf-8")
        self.assertIn("**Options:**\n1. Left\n2. Right", text)
        self.assertIn("**Routed to:** @hpl", text)
        self.assertIn("[blocks: iter-7]", text)

    def test_mark_dispatched_is_idempotent(self):
        self.st.mark_dispatched("Q-042", 86, role="hpl", dispatched_at="2026-08-26T00:00:00Z")
        snapshot = self.path.read_bytes()
        self.st.mark_dispatched("Q-042", 86, role="hpl", dispatched_at="2026-08-26T00:00:00Z")
        self.assertEqual(self.path.read_bytes(), snapshot)
        self.assertEqual(
            self.path.read_text(encoding="utf-8").count("**Dispatched:**"), 1
        )

    def test_mark_dispatched_replaces_a_stale_marker(self):
        self.st.mark_dispatched("Q-042", 87, role="hpl", dispatched_at="2026-08-26T00:00:00Z")
        self.st.mark_dispatched("Q-042", 88, role="hpl", dispatched_at="2026-08-27T00:00:00Z")
        text = self.path.read_text(encoding="utf-8")
        self.assertEqual(text.count("**Dispatched:**"), 1)
        self.assertIn("relay #88", text)

    def test_rel_path_targets_a_specific_file(self):
        self.write_queue("for-tech-lead.md", "## Q-042 — a clash in another file  [open]\n")
        st = self.store()
        result = st.apply_answer(
            "Q-042", "x", "@hpl", 89, rel_path="docs/questions/for-product-lead.md"
        )
        self.assertIs(result, qs.ApplyResult.APPLIED)
        self.assertIn("[resolved]", self.path.read_text(encoding="utf-8"))
        self.assertIn(
            "[open]", (self.ws / "docs/questions/for-tech-lead.md").read_text(encoding="utf-8")
        )

    def test_missing_file_is_not_found(self):
        st = self.store()
        self.assertIs(
            st.apply_answer("Q-042", "x", "@hpl", 90, rel_path="docs/questions/gone.md"),
            qs.ApplyResult.NOT_FOUND,
        )


# ── Escaping — state.md invariant 11 ──────────────────────────────────────────


HOSTILE_ANSWER = """\
Sure, do that.

## Q-999 — injected entry  [open]

[resolved] and now a status token at line start
**Answered by:** someone-else · relay #1
<!-- answer:999 -->
```
a fenced block of their own
```
~~~
and a tilde fence
~~~
### another heading
---
resolved: a bare status word
   ## indented heading
"""


class TestAnswerEscaping(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.path = self.write_queue(
            "for-product-lead.md",
            """\
            ## Q-001 — first  [open]

            Body one.

            ## Q-002 — second  [open]

            Body two.
            """,
        )
        self.st = self.store()

    def test_no_produced_line_forges_structure(self):
        self.st.apply_answer("Q-001", HOSTILE_ANSWER, "@hpl", 100, role="hpl")
        text = self.path.read_text(encoding="utf-8")

        frame = {
            "## Q-001 — first  [resolved]",
            "## Q-002 — second  [open]",
        }
        for line in text.split("\n"):
            if line in frame or line.startswith("<!-- answer:100 -->"):
                continue
            if line.startswith("**Answered by:** @hpl"):
                continue
            stripped = line.lstrip()
            self.assertFalse(line.startswith("#"), f"forged heading: {line!r}")
            self.assertFalse(stripped.startswith("#"), f"forged heading after lstrip: {line!r}")
            self.assertFalse(
                stripped.startswith("**Answered by:"), f"forged answered-by field: {line!r}"
            )
            self.assertFalse(
                stripped.startswith("<!-- answer:"), f"forged answer marker: {line!r}"
            )
            # Rule 3 reads *bracketed* tokens only — guard applies only to
            # the bracketed shape; bare words are already structurally inert
            # under the 4-space indent (see Issue 2 in the review report).
            for token in ("open", "resolved"):
                self.assertFalse(
                    stripped.lower().startswith(f"[{token}"),
                    f"forged bracketed status token: {line!r}",
                )

    def test_parse_resolves_identically_after_a_hostile_answer(self):
        before = [(e.qid, e.status, e.start) for e in self.st.entries_in(self.path)]
        self.st.apply_answer("Q-001", HOSTILE_ANSWER, "@hpl", 101, role="hpl")
        after = self.st.entries_in(self.path)
        self.assertEqual([e.qid for e in after], [q for q, _, _ in before])
        self.assertEqual([e.status for e in after], ["resolved", "open"])
        # No new entry was created by the injected heading.
        self.assertEqual(len(after), 2)

    def test_hostile_answer_cannot_forge_the_idempotency_marker(self):
        # The answer text contains "<!-- answer:999 -->"; a *later* answer with
        # message id 999 must still be applied, not skipped as already-done.
        self.st.apply_answer("Q-001", HOSTILE_ANSWER, "@hpl", 102, role="hpl")
        self.assertIs(
            self.st.apply_answer("Q-002", "the real 999", "@hpl", 999, role="hpl"),
            qs.ApplyResult.APPLIED,
        )
        self.assertIs(
            self.st.apply_answer("Q-001", "another", "@hpl", 999, role="hpl"),
            qs.ApplyResult.APPLIED,
        )
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("the real 999", text)

    def test_answer_text_survives_verbatim_inside_the_fence(self):
        self.st.apply_answer("Q-001", "Use option 2 — the JWT one.", "@hpl", 103, role="hpl")
        self.assertIn("    Use option 2 — the JWT one.", self.path.read_text(encoding="utf-8"))

    def test_fence_grows_past_the_longest_backtick_run(self):
        block = qs.escape_answer_text("a ```` b ``` c")
        self.assertEqual(block[0], "`````text")
        self.assertEqual(block[-1], "`````")

    def test_answered_by_cannot_inject_lines(self):
        self.st.apply_answer(
            "Q-001", "ok", "@hpl\n## Q-998 — injected  [open]", 104, role="hpl"
        )
        after = self.st.entries_in(self.path)
        self.assertEqual([e.qid for e in after], ["Q-001", "Q-002"])

    def test_control_characters_are_stripped(self):
        self.st.apply_answer("Q-001", "a\x00b\x07c", "@hpl", 105, role="hpl")
        self.assertNotIn("\x00", self.path.read_text(encoding="utf-8"))

    def test_empty_answer_is_a_well_formed_block(self):
        self.assertIs(
            self.st.apply_answer("Q-001", "", "@hpl", 106, role="hpl"), qs.ApplyResult.APPLIED
        )
        self.assertEqual(len(self.st.entries_in(self.path)), 2)

    def test_composed_title_cannot_forge_a_status_bracket(self):
        created = self.st.create_entry(title="Bad [resolved] title", role="hpl", routed_to="hpl")
        entries = {e.qid: e for e in self.st.entries_in(created.path)}
        self.assertEqual(entries[created.qid].status, "open")

    # ── Issue 2: narrowed guard (bracket-only for status vocabulary) ──

    def test_bare_status_word_is_not_backslash_guarded(self):
        """An unbracketed status word in answer text must NOT be backslash-prefixed.

        Rule 3 only reads *bracketed* tokens on heading lines, so a bare word
        (``Open the file…``, ``Resolved: proceed``) inside a 4-space-indented
        fence is already structurally inert — no backslash needed.
        """
        fmt = qs.QuestionsFormat()  # status_set = frozenset({"open", "resolved"})
        block = qs.escape_answer_text("Open the file by clicking the button.", fmt)
        self.assertIn("    Open the file by clicking the button.", block)

        block2 = qs.escape_answer_text("Resolved: proceed with the JWT approach.", fmt)
        self.assertIn("    Resolved: proceed with the JWT approach.", block2)

    def test_bracketed_status_token_is_still_guarded(self):
        """A bracketed status token at line start must still be backslash-prefixed."""
        fmt = qs.QuestionsFormat()
        block = qs.escape_answer_text("[open] see below", fmt)
        self.assertIn("    \\[open] see below", block)

        block2 = qs.escape_answer_text("[OPEN] caps variant", fmt)
        self.assertIn("    \\[OPEN] caps variant", block2)

        block3 = qs.escape_answer_text("[resolved 2026-08-13] free-text suffix", fmt)
        self.assertIn("    \\[resolved 2026-08-13] free-text suffix", block3)


# ── Write discipline ──────────────────────────────────────────────────────────


class TestWriteDiscipline(StoreTestCase):
    def test_a_killed_writer_leaves_the_file_untouched(self):
        path = self.write_queue("for-product-lead.md", "## Q-001 — a  [open]\n\nBody.\n")
        before = path.read_bytes()

        script = self.ws / "killer.py"
        script.write_text(
            textwrap.dedent(
                f"""\
                import os, pathlib, signal, sys
                sys.path.insert(0, {str(_HOOKS)!r})
                import questions_store as qs

                def boom(*a, **k):
                    os.kill(os.getpid(), signal.SIGKILL)

                os.replace = boom
                config = qs.load_questions_config({str(self.ws)!r}, roles_path=pathlib.Path({str(self.roles_path)!r}))
                store = qs.store_for_root(config, {str(self.ws)!r})
                store.create_entry(title="doomed", body="b", role="hpl", routed_to="hpl")
                """
            ),
            encoding="utf-8",
        )
        proc = subprocess.run(
            [sys.executable, str(script)], capture_output=True, env={**os.environ}
        )
        self.assertEqual(proc.returncode, -9, proc.stderr.decode())
        self.assertEqual(path.read_bytes(), before, "the replace never happened")

        # The temp file it left behind is hidden and outside the queue set.
        store = self.store()
        self.assertNotIn(path.parent / "doomed", store.scan_files())
        self.assertEqual([e.qid for e in store.entries_in(path)], ["Q-001"])

    def test_a_failed_replace_cleans_up_and_changes_nothing(self):
        path = self.write_queue("for-product-lead.md", "## Q-001 — a  [open]\n")
        before = path.read_bytes()
        directory = path.parent
        original = os.replace

        def boom(src, dst):
            raise OSError("simulated replace failure")

        store = self.store()
        qs.os.replace = boom
        try:
            with self.assertRaises(OSError):
                store.create_entry(title="doomed", role="hpl", routed_to="hpl")
        finally:
            qs.os.replace = original

        self.assertEqual(path.read_bytes(), before)
        leftovers = [p.name for p in directory.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_corrupt_file_degrades_to_a_clear_error(self):
        path = self.ws / "docs" / "questions" / "for-product-lead.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"## Q-001 \xff\xfe not utf-8  [open]\n")
        store = self.store()

        with self.assertRaises(qs.QuestionsStoreError) as ctx:
            store.entries_in(path)
        self.assertIn("not valid UTF-8", str(ctx.exception))

        with self.assertRaises(qs.QuestionsStoreError):
            store.apply_answer("Q-001", "x", "@hpl", 110, role="hpl")

    def test_write_preserves_bytes_outside_the_change(self):
        original = "## Q-001 — a  [open]\r\n\r\nCRLF body.\r\n\r\n## Q-002 — b  [open]\r\n"
        path = self.ws / "docs" / "questions" / "for-product-lead.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(original, encoding="utf-8", newline="")
        store = self.store()
        store.apply_answer("Q-002", "ok", "@hpl", 111, role="hpl")
        text = path.read_text(encoding="utf-8", newline="")
        self.assertIn("CRLF body.\r\n", text)
        self.assertIn("## Q-001 — a  [open]\r\n", text)

    def test_lock_file_lives_beside_the_queue(self):
        store = self.store()
        store.create_entry(title="x", role="hpl", routed_to="hpl")
        self.assertTrue((store.queue_dir / qs.LOCK_FILENAME).exists())
        # …and is not mistaken for a queue file.
        self.assertNotIn(store.queue_dir / qs.LOCK_FILENAME, store.scan_files())

    def test_concurrent_writers_allocate_unique_ids(self):
        self.write_queue("for-product-lead.md", "## Q-010 — seed  [open]\n")
        script = self.ws / "writer.py"
        script.write_text(
            textwrap.dedent(
                f"""\
                import pathlib, sys
                sys.path.insert(0, {str(_HOOKS)!r})
                import questions_store as qs
                config = qs.load_questions_config({str(self.ws)!r}, roles_path=pathlib.Path({str(self.roles_path)!r}))
                store = qs.store_for_root(config, {str(self.ws)!r})
                created = store.create_entry(title="writer " + sys.argv[1], body="b",
                                             role="hpl", routed_to="hpl")
                print(created.qid)
                """
            ),
            encoding="utf-8",
        )

        procs = [
            subprocess.Popen(
                [sys.executable, str(script), str(i)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for i in range(8)
        ]
        ids = []
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err.decode())
            ids.append(out.decode().strip())

        self.assertEqual(len(set(ids)), 8, f"duplicate ids allocated: {ids}")
        store = self.store()
        entries = store.entries_in(self.ws / "docs/questions/for-product-lead.md")
        self.assertEqual(len(entries), 9, "a concurrent append was lost")
        self.assertEqual(sorted(e.qid for e in entries if e.qid != "Q-010"), sorted(ids))
        self.assertEqual(sorted(e.number for e in entries), list(range(10, 19)))


# ── Anchor resolution — architecture §3.1 ─────────────────────────────────────


class TestAnchorResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="qs-anchor-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _config(self, anchor: str, path: str = "") -> qs.QuestionsConfig:
        return qs.QuestionsConfig(anchor=anchor, path=path, workspace_id="demo-ws")

    def _primary_checkout(self, name: str = "repo", bare: bool = False) -> Path:
        root = self.tmp / name
        git_dir = root / ".git"
        git_dir.mkdir(parents=True)
        (git_dir / "config").write_text(
            f"[core]\n\trepositoryformatversion = 0\n\tbare = {'true' if bare else 'false'}\n",
            encoding="utf-8",
        )
        (root / "src").mkdir()
        return root

    def _linked_worktree(self, common_git_dir: Path, name: str) -> Path:
        worktree = self.tmp / name
        worktree.mkdir(parents=True)
        wt_git = common_git_dir / "worktrees" / name
        wt_git.mkdir(parents=True)
        rel = os.path.relpath(common_git_dir, wt_git)
        (wt_git / "commondir").write_text(rel + "\n", encoding="utf-8")
        (worktree / ".git").write_text(f"gitdir: {wt_git}\n", encoding="utf-8")
        return worktree

    def test_primary_checkout_repo_anchor(self):
        root = self._primary_checkout()
        result = qs.resolve_anchor(root / "src", self._config("repo"))
        self.assertEqual(result.root, root.resolve())
        self.assertEqual(result.mode, "repo")
        self.assertEqual(result.workspace_id, "demo-ws")

    def test_worktree_resolves_to_the_primary_checkout(self):
        root = self._primary_checkout()
        worktree = self._linked_worktree(root / ".git", "repo--hf-042")
        result = qs.resolve_anchor(worktree, self._config("repo"))
        self.assertEqual(result.root, root.resolve())

    def test_worktree_anchor_stays_in_the_worktree(self):
        root = self._primary_checkout()
        worktree = self._linked_worktree(root / ".git", "repo--hf-043")
        result = qs.resolve_anchor(worktree, self._config("worktree"))
        self.assertEqual(result.root, worktree.resolve())
        self.assertEqual(result.mode, "worktree")

    def test_bare_repository_falls_through_to_the_worktree(self):
        bare = self.tmp / "repo.git"
        bare.mkdir()
        (bare / "config").write_text("[core]\n\tbare = true\n", encoding="utf-8")
        worktree = self._linked_worktree(bare, "bare-wt")
        result = qs.resolve_anchor(worktree, self._config("repo"))
        self.assertEqual(result.root, worktree.resolve())
        self.assertEqual(result.mode, "repo→worktree")

    def test_dot_git_named_bare_repository_also_falls_through(self):
        root = self._primary_checkout(name="barerepo", bare=True)
        worktree = self._linked_worktree(root / ".git", "barerepo-wt")
        result = qs.resolve_anchor(worktree, self._config("repo"))
        self.assertEqual(result.root, worktree.resolve())

    def test_non_git_uses_the_nearest_claude_ancestor(self):
        project = self.tmp / "plain"
        (project / ".claude").mkdir(parents=True)
        (project / "deep" / "nested").mkdir(parents=True)
        result = qs.resolve_anchor(project / "deep" / "nested", self._config("repo"))
        self.assertEqual(result.root, project.resolve())
        self.assertEqual(result.mode, "non-git")

    def test_non_git_without_a_marker_uses_cwd(self):
        lonely = self.tmp / "lonely"
        lonely.mkdir()
        result = qs.resolve_anchor(lonely, self._config("repo"))
        self.assertEqual(result.root, lonely.resolve())

    def test_path_anchor(self):
        target = self.tmp / "elsewhere"
        result = qs.resolve_anchor(self.tmp, self._config("path", str(target)))
        self.assertEqual(result.root, target)
        self.assertEqual(result.mode, "path")

    def test_path_anchor_expands_tilde(self):
        result = qs.resolve_anchor(self.tmp, self._config("path", "~/queues"))
        self.assertEqual(result.root, Path.home() / "queues")

    def test_resolution_is_side_effect_free(self):
        root = self._primary_checkout(name="pure")
        worktree = self._linked_worktree(root / ".git", "pure-wt")
        before = sorted(str(p) for p in self.tmp.rglob("*"))
        for anchor in ("repo", "worktree", "path"):
            qs.resolve_anchor(worktree, self._config(anchor, str(self.tmp / "nowhere")))
        after = sorted(str(p) for p in self.tmp.rglob("*"))
        self.assertEqual(before, after, "anchor resolution created or removed something")

    def test_bare_in_non_core_section_is_not_treated_as_bare(self):
        """bare = true under a non-[core] section must not cause false bare detection.

        Issue 3: _is_bare used to match bare=true on every non-comment line
        regardless of [section].  A git config with ``bare = true`` under
        ``[branch "main"]`` would previously trigger the bare fall-through.
        """
        root = self._primary_checkout()
        git_config = root / ".git" / "config"
        existing = git_config.read_text(encoding="utf-8")
        # Append a non-core section that happens to contain bare = true.
        git_config.write_text(
            existing + '\n[branch "main"]\n\tbare = true\n',
            encoding="utf-8",
        )
        result = qs.resolve_anchor(root / "src", self._config("repo"))
        # The repo has [core] bare = false, so it must not fall through.
        self.assertEqual(result.root, root.resolve())
        self.assertEqual(result.mode, "repo")


# ── Config loading ────────────────────────────────────────────────────────────


class TestConfig(StoreTestCase):
    def test_absent_section_makes_the_store_inert(self):
        self.roles_path.write_text(
            'workspace_id = "demo-ws"\ndefault = "hpl"\n\n[role.hpl]\ntitle = "PL"\n',
            encoding="utf-8",
        )
        self.assertIsNone(qs.load_questions_config(str(self.ws), roles_path=self.roles_path))

        os.environ["CLAUDE_PROJECT_DIR"] = str(self.ws)
        try:
            self.assertIsNone(qs.open_store(self.ws))
        finally:
            os.environ["CLAUDE_PROJECT_DIR"] = _ISOLATION_DIR
        # Nothing was read, nothing was created.
        self.assertFalse((self.ws / "docs").exists())

    def test_missing_roles_file_is_inert(self):
        self.roles_path.unlink()
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.ws)
        try:
            self.assertIsNone(qs.open_store(self.ws))
        finally:
            os.environ["CLAUDE_PROJECT_DIR"] = _ISOLATION_DIR

    def test_unparseable_roles_file_is_inert(self):
        self.roles_path.write_text("this is not = = toml\n", encoding="utf-8")
        self.assertIsNone(qs.load_questions_config(str(self.ws), roles_path=self.roles_path))

    def test_defaults(self):
        self.roles_path.write_text(
            'default = "hpl"\n\n[role.hpl]\ntitle = "PL"\n\n[questions]\ndir = "docs/questions"\n',
            encoding="utf-8",
        )
        config = qs.load_questions_config(str(self.ws), roles_path=self.roles_path)
        self.assertEqual(config.dir, "docs/questions")
        self.assertEqual(config.anchor, "repo")
        self.assertEqual(config.nudge, qs.DEFAULT_NUDGE)
        self.assertEqual(config.escalate_after, 24 * 3600.0)
        self.assertEqual(config.max_open, 20)
        self.assertEqual(config.min_interval_s, 30.0)
        self.assertEqual(config.fmt.id, qs.DEFAULT_ID)
        self.assertEqual(config.fmt.status, ("open", "resolved"))
        self.assertEqual(config.fmt.heading, qs.DEFAULT_HEADING)
        self.assertEqual(config.fmt.glob, qs.DEFAULT_GLOB)
        self.assertEqual(config.fmt.level, 2)
        self.assertEqual(config.workspace_id, self.ws.name)
        self.assertEqual(config.default_role, "hpl")
        self.assertEqual(config.errors, ())

    def test_every_format_override(self):
        self.roles_path.write_text(
            textwrap.dedent(
                """\
                default = "htl"

                [role.htl]
                title = "Tech lead"

                [questions]
                dir            = "queue"
                anchor         = "worktree"
                nudge          = "1h,2h"
                escalate_after = "2h"
                max_open       = 3
                min_interval_s = 5

                [questions.queue]
                htl = "q-tech.md"

                [questions.format]
                id              = "QQ{n}"
                status          = ["todo", "done", "parked"]
                heading         = "### {id} :: {title}  [{status}]{tags}"
                answer_template = "Reply from {answered_by} (#{message_id}):\\n\\n{answer}"
                glob            = "q-*.md"
                """
            ),
            encoding="utf-8",
        )
        config = qs.load_questions_config(str(self.ws), roles_path=self.roles_path)
        self.assertEqual(config.errors, ())
        self.assertEqual(config.dir, "queue")
        self.assertEqual(config.anchor, "worktree")
        self.assertEqual(config.nudge, "1h,2h")
        self.assertEqual(config.escalate_after, 7200.0)
        self.assertEqual(config.max_open, 3)
        self.assertEqual(config.min_interval_s, 5.0)
        self.assertEqual(config.fmt.status, ("todo", "done", "parked"))
        self.assertEqual(config.fmt.level, 3)

        store = qs.store_for_root(config, self.ws)
        created = store.create_entry(title="Custom", body="Body.", role="htl", routed_to="htl")
        self.assertEqual(created.qid, "QQ1")
        text = created.path.read_text(encoding="utf-8")
        self.assertIn("### QQ1 :: Custom  [todo]", text)
        self.assertEqual(created.rel_path, "queue/q-tech.md")

        self.assertIs(store.apply_answer("QQ1", "Done it.", "@htl", 120, role="htl"), qs.ApplyResult.APPLIED)
        text = created.path.read_text(encoding="utf-8")
        self.assertIn("### QQ1 :: Custom  [done]", text)
        self.assertIn("Reply from @htl (#120):", text)
        self.assertIn("    Done it.", text)  # bare word: no backslash guard since Issue 2 fix

        # The custom glob is what the id scan uses.
        (self.ws / "queue" / "not-a-queue.md").write_text("## QQ900 — noise\n", encoding="utf-8")
        self.assertEqual(store.next_id(), "QQ2")

    def test_unbracketed_status_placeholder_is_reported(self):
        self.roles_path.write_text(
            textwrap.dedent(
                """\
                default = "hpl"

                [role.hpl]
                title = "PL"

                [questions]

                [questions.format]
                heading = "## {id} — {title} <{status}>"
                """
            ),
            encoding="utf-8",
        )
        config = qs.load_questions_config(str(self.ws), roles_path=self.roles_path)
        self.assertTrue(
            any("must be bracketed" in e for e in config.errors), config.errors
        )

    def test_day_durations_are_accepted(self):
        self.roles_path.write_text(
            'default = "hpl"\n\n[role.hpl]\ntitle = "PL"\n\n[questions]\nescalate_after = "3d"\n',
            encoding="utf-8",
        )
        config = qs.load_questions_config(str(self.ws), roles_path=self.roles_path)
        self.assertEqual(config.escalate_after, 3 * 86400.0)
        self.assertEqual(config.errors, ())

    def test_invalid_values_are_recorded_and_defaulted(self):
        self.roles_path.write_text(
            textwrap.dedent(
                """\
                default = "hpl"

                [role.hpl]
                title = "PL"

                [questions]
                anchor         = "sideways"
                escalate_after = "banana"

                [questions.format]
                id     = "no-placeholder"
                status = []
                """
            ),
            encoding="utf-8",
        )
        config = qs.load_questions_config(str(self.ws), roles_path=self.roles_path)
        self.assertEqual(config.anchor, "repo")
        self.assertEqual(config.escalate_after, 24 * 3600.0)
        self.assertEqual(config.fmt.id, qs.DEFAULT_ID)
        self.assertEqual(config.fmt.status, qs.DEFAULT_STATUS)
        self.assertEqual(len(config.errors), 4, config.errors)

    def test_path_anchor_without_a_path_is_recorded(self):
        self.roles_path.write_text(
            'default = "hpl"\n\n[role.hpl]\ntitle = "PL"\n\n[questions]\nanchor = "path"\n',
            encoding="utf-8",
        )
        config = qs.load_questions_config(str(self.ws), roles_path=self.roles_path)
        self.assertEqual(config.anchor, "repo")
        self.assertTrue(any("path" in e for e in config.errors))

    def test_role_absent_from_the_queue_map_is_chat_only(self):
        store = self.store()
        self.assertIsNone(store.queue_file_for_role("operator"))
        with self.assertRaises(qs.QuestionsStoreError):
            store.create_entry(title="x", role="operator", routed_to="operator")
        self.assertFalse((self.ws / "docs" / "questions" / "operator.md").exists())

    def test_default_role_is_used_when_none_is_given(self):
        store = self.store()
        self.assertEqual(
            store.queue_file_for_role(None), self.ws / "docs/questions/for-product-lead.md"
        )

    def test_open_store_uses_the_project_dir_env(self):
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.ws)
        try:
            store = qs.open_store(self.ws)
        finally:
            os.environ["CLAUDE_PROJECT_DIR"] = _ISOLATION_DIR
        self.assertIsNotNone(store)
        self.assertEqual(store.workspace_id, "demo-ws")
        self.assertEqual(store.queue_dir, self.ws / "docs" / "questions")


# ── The greenfield adoption test (brd §5) ─────────────────────────────────────


class TestFlatAdoption(StoreTestCase):
    #: "A greenfield project with a flat questions.md, no roles, no worktrees
    #:  and no PM loop must be able to adopt this with dir = … and nothing else."
    roles_toml = '[questions]\ndir = "docs/questions"\n'

    def test_one_file_one_destination(self):
        config = qs.load_questions_config(str(self.ws), roles_path=self.roles_path)
        self.assertIsNotNone(config)
        self.assertEqual(config.queue, {})
        self.assertIsNone(config.default_role)
        # workspace_id falls back to the workspace directory name.
        self.assertEqual(config.workspace_id, self.ws.name)

        store = qs.store_for_root(config, self.ws)
        target = store.queue_file_for_role(None)
        self.assertEqual(target, self.ws / "docs" / "questions" / "questions.md")

        first = store.create_entry(title="Adoption", body="Does this work?", routed_to="human")
        second = store.create_entry(title="Second", body="And again?")
        self.assertEqual([first.qid, second.qid], ["Q-001", "Q-002"])
        self.assertEqual(first.path, target)

        self.assertIs(store.mark_dispatched("Q-001", 200), qs.ApplyResult.APPLIED)
        self.assertIs(store.apply_answer("Q-001", "It does.", "@human", 200), qs.ApplyResult.APPLIED)

        entries = {e.qid: e for e in store.entries_in(target)}
        self.assertEqual(entries["Q-001"].status, "resolved")
        self.assertEqual(entries["Q-002"].status, "open")
        self.assertEqual(store.count_open(), 1)

    def test_any_role_name_lands_in_the_single_file(self):
        store = self.store()
        created = store.create_entry(title="x", role="whoever", routed_to="whoever")
        self.assertEqual(created.rel_path, "docs/questions/questions.md")


# ── Composition — invariant 6, frame only ─────────────────────────────────────


class TestComposition(StoreTestCase):
    def test_body_is_passed_through_verbatim(self):
        store = self.store()
        body = "**Class:** design-fixture\n**Proof:** screenshot.png\n\n- bullet\n- bullet"
        created = store.create_entry(title="T", body=body, role="hpl", routed_to="hpl")
        self.assertIn(body, created.path.read_text(encoding="utf-8"))

    def test_frame_contains_nothing_else(self):
        store = self.store()
        created = store.create_entry(
            title="T", body="B", role="hpl", options=["a", "b"], tags=["t1"], routed_to="hpl"
        )
        text = created.path.read_text(encoding="utf-8").rstrip("\n")
        self.assertEqual(
            text.split("\n"),
            [
                "## Q-001 — T  [open]  [t1]",
                "",
                "B",
                "",
                "**Options:**",
                "1. a",
                "2. b",
                "",
                "**Routed to:** @hpl",
            ],
        )

    def test_appending_keeps_one_blank_line_between_entries(self):
        store = self.store()
        store.create_entry(title="one", body="a", role="hpl", routed_to="hpl")
        store.create_entry(title="two", body="b", role="hpl", routed_to="hpl")
        text = (self.ws / "docs/questions/for-product-lead.md").read_text(encoding="utf-8")
        self.assertIn("**Routed to:** @hpl\n\n## Q-002 — two", text)

    def test_options_are_single_line(self):
        store = self.store()
        created = store.create_entry(
            title="T", role="hpl", options=["multi\nline option"], routed_to="hpl"
        )
        self.assertIn("1. multi line option", created.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
