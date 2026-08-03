#!/usr/bin/env python3
"""
Unit Tests: terminal-answer propagation (epic 15 / task 15-04)

Three things are covered here:

* ``posttool_hook.reduce_tool_response`` — what actually gets written onto the
  state-store row.
* ``permission_request_hook.parse_terminal_answers`` — the three-tier read back.
* the ``terminal_answers`` field's round trip through the JSONL row.

Every payload-shaped test is driven from
``tasks/15_human_roles/fixtures/posttool_askuserquestion.json`` — real captured
``PostToolUse`` payloads — rather than hand-written examples. An assumption
about this payload's shape was already wrong once (a regex written against the
prose form matched 2 of 4 answers on a real call), which is why the fixtures
exist.

No network, no ``RelayClient``, no real ``.claude/roles.toml``.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / ".claude" / "hooks"))

from permission_request_hook import (  # noqa: E402
    TERMINAL_ANSWER_PLACEHOLDERS,
    parse_terminal_answers,
)
from posttool_hook import (  # noqa: E402
    MAX_TERMINAL_ANSWERS_BYTES,
    reduce_tool_response,
)
from permission_state_store import (  # noqa: E402
    RequestState,
    create_request,
    get_request,
    resolve_via_terminal,
)


FIXTURE_PATH = (
    Path(__file__).parent.parent
    / "tasks" / "15_human_roles" / "fixtures" / "posttool_askuserquestion.json"
)

with open(FIXTURE_PATH, encoding="utf-8") as _f:
    _FIXTURE = json.load(_f)

CASES = _FIXTURE["cases"]
CASE_IDS = ("1_options_selected", "2_free_text_answer", "3_notes_only_mixed_with_selected")


def _tool_response(case_id):
    return CASES[case_id]["tool_response"]


def _question_texts(case_id):
    """The question strings, taken from the fixture's own ``questions`` list —
    which is also exactly how ``handle_ask_user_question`` identifies children."""
    return [q["question"] for q in _tool_response(case_id)["questions"]]


def _stored(case_id):
    """What ``posttool_hook`` would write onto the row for this fixture case."""
    return reduce_tool_response(_tool_response(case_id))


class TestFixtureIntegrity(unittest.TestCase):
    """Guard rails: the tests below are only meaningful if the fixture still
    carries the three answer shapes they were written against."""

    def test_all_three_cases_present(self):
        self.assertEqual(set(CASES), set(CASE_IDS))

    def test_case_three_has_a_notes_only_placeholder_and_previews(self):
        tr = _tool_response("3_notes_only_mixed_with_selected")
        self.assertIn("(notes only)", tr["answers"].values())
        self.assertTrue(
            any("notes" in a for a in tr["annotations"].values()),
            "case 3 must carry real notes",
        )
        self.assertTrue(
            any("preview" in a for a in tr["annotations"].values()),
            "case 3 must carry previews for the reducer to drop",
        )


class TestReduceToolResponse(unittest.TestCase):
    """The reducer runs on every real fixture case."""

    def test_keeps_answers_verbatim(self):
        for case_id in CASE_IDS:
            with self.subTest(case=case_id):
                reduced = json.loads(_stored(case_id))
                self.assertEqual(
                    reduced["answers"], _tool_response(case_id)["answers"]
                )

    def test_extracts_notes_from_annotations(self):
        reduced = json.loads(_stored("3_notes_only_mixed_with_selected"))
        annotations = _tool_response("3_notes_only_mixed_with_selected")["annotations"]
        expected = {
            q: a["notes"] for q, a in annotations.items() if "notes" in a
        }
        self.assertEqual(reduced["notes"], expected)
        self.assertEqual(len(expected), 2)

    def test_cases_without_annotations_get_empty_notes(self):
        for case_id in ("1_options_selected", "2_free_text_answer"):
            with self.subTest(case=case_id):
                self.assertEqual(json.loads(_stored(case_id))["notes"], {})

    def test_drops_questions_and_previews(self):
        for case_id in CASE_IDS:
            with self.subTest(case=case_id):
                stored = _stored(case_id)
                reduced = json.loads(stored)
                self.assertEqual(set(reduced), {"answers", "notes"})
                self.assertNotIn("questions", reduced)
                # The previews are the single biggest thing dropped from case 3.
                self.assertNotIn("preview", stored)

    def test_materially_shrinks_every_real_payload(self):
        for case_id in CASE_IDS:
            with self.subTest(case=case_id):
                raw = json.dumps(_tool_response(case_id), ensure_ascii=False)
                reduced = _stored(case_id)
                self.assertLess(len(reduced.encode()), len(raw.encode()))
        # The rich case is where it matters: previews + option descriptions are
        # most of the payload.
        rich = "3_notes_only_mixed_with_selected"
        raw_len = len(json.dumps(_tool_response(rich), ensure_ascii=False).encode())
        red_len = len(_stored(rich).encode())
        self.assertLess(red_len, raw_len / 2)

    def test_survives_a_json_round_trip(self):
        for case_id in CASE_IDS:
            with self.subTest(case=case_id):
                stored = _stored(case_id)
                self.assertEqual(json.loads(json.dumps(json.loads(stored))),
                                 json.loads(stored))

    def test_every_real_case_is_well_under_the_backstop(self):
        for case_id in CASE_IDS:
            with self.subTest(case=case_id):
                self.assertLess(
                    len(_stored(case_id).encode()), MAX_TERMINAL_ANSWERS_BYTES / 2
                )

    def test_does_not_mutate_the_payload(self):
        """The reducer copies ``answers`` — the backstop path deletes entries."""
        tr = json.loads(json.dumps(_tool_response("1_options_selected")))
        before = json.dumps(tr, sort_keys=True)
        reduce_tool_response(tr)
        self.assertEqual(json.dumps(tr, sort_keys=True), before)

    def test_non_dict_tool_response_is_stringified_unchanged(self):
        """Older Claude Code (or a shape change) hands us the prose string; it
        is stored verbatim and left to the parser's prose tier."""
        prose = '"Pick a color"="Red"\n"Pick a size"=(no option selected)'
        self.assertEqual(reduce_tool_response(prose), prose)
        self.assertEqual(reduce_tool_response(None), "None")
        self.assertEqual(reduce_tool_response(["a"]), "['a']")

    def test_missing_keys_are_tolerated(self):
        self.assertEqual(json.loads(reduce_tool_response({})),
                         {"answers": {}, "notes": {}})
        self.assertEqual(
            json.loads(reduce_tool_response({"answers": None, "annotations": None})),
            {"answers": {}, "notes": {}},
        )


class TestEightKilobyteBackstop(unittest.TestCase):
    """The 8 KB cap applies to the *reduced* value, never the raw payload —
    capping the raw payload would truncate a real capture into invalid JSON and
    silently disable the structured path.

    No committed fixture is large enough to exercise the cap (the largest
    reduces to ~1.5 KB; the fixture's own ``_note`` records that case 3's option
    descriptions were trimmed). So the oversized payload here is built by
    inflating a *real* case back to realistic length — padded option
    descriptions and ``annotations[*].preview`` — keeping the structure real
    even though the bulk is synthetic.
    """

    def _inflated(self):
        tr = json.loads(json.dumps(_tool_response("3_notes_only_mixed_with_selected")))
        for question in tr["questions"]:
            for opt in question["options"]:
                opt["description"] = "Trade-off prose. " * 200
        for annotation in tr["annotations"].values():
            annotation["preview"] = "PREVIEW LINE\n" * 400
        # Notes are what push the *reduced* value over the cap.
        for question, annotation in tr["annotations"].items():
            annotation["notes"] = "Long-form reasoning from the operator. " * 200
        return tr

    def test_raw_payload_would_blow_the_cap(self):
        raw = json.dumps(self._inflated(), ensure_ascii=False)
        self.assertGreater(len(raw.encode()), MAX_TERMINAL_ANSWERS_BYTES)

    def test_reducer_brings_it_under_the_cap(self):
        reduced = reduce_tool_response(self._inflated())
        self.assertLessEqual(len(reduced.encode()), MAX_TERMINAL_ANSWERS_BYTES)

    def test_capped_value_is_still_valid_json_with_the_answers_kept(self):
        """The backstop sheds notes first, so the answers — the thing the chat
        actually renders — survive and the value stays parseable."""
        reduced = json.loads(reduce_tool_response(self._inflated()))
        self.assertEqual(set(reduced), {"answers", "notes"})
        self.assertEqual(
            reduced["answers"],
            _tool_response("3_notes_only_mixed_with_selected")["answers"],
        )

    def test_answers_alone_over_the_cap_are_shed_until_it_fits(self):
        payload = {
            "answers": {f"Question {i}?": "A" * 2000 for i in range(10)},
            "annotations": {},
        }
        reduced = reduce_tool_response(payload)
        self.assertLessEqual(len(reduced.encode()), MAX_TERMINAL_ANSWERS_BYTES)
        parsed = json.loads(reduced)  # still valid JSON, not a truncated string
        self.assertGreater(len(parsed["answers"]), 0)
        self.assertLess(len(parsed["answers"]), 10)


class TestParseTerminalAnswersStructured(unittest.TestCase):
    """Tier 1 — the real path, driven by the fixtures."""

    def test_options_selected_reads_every_answer(self):
        case = "1_options_selected"
        parsed = parse_terminal_answers(_stored(case), _question_texts(case))
        self.assertEqual(parsed, {"Pick a color": "Red", "Pick a size": "Large"})

    def test_free_text_answer_needs_no_special_handling(self):
        case = "2_free_text_answer"
        parsed = parse_terminal_answers(_stored(case), _question_texts(case))
        self.assertEqual(parsed["Pick a size"], "I'd prefer medium")
        self.assertEqual(parsed["Pick a color"], "Red")

    def test_single_question_matches_by_exact_text(self):
        case = "1_options_selected"
        parsed = parse_terminal_answers(_stored(case), ["Pick a size"])
        self.assertEqual(parsed, {"Pick a size": "Large"})

    def test_notes_only_renders_the_notes_not_the_placeholder(self):
        case = "3_notes_only_mixed_with_selected"
        questions = _question_texts(case)
        parsed = parse_terminal_answers(_stored(case), questions)
        tr = _tool_response(case)

        self.assertEqual(len(questions), 4)
        self.assertEqual(len(parsed), 4)
        for question in questions:
            answer = tr["answers"][question]
            if answer == "(notes only)":
                expected = tr["annotations"][question]["notes"]
                self.assertEqual(parsed[question], expected)
                self.assertNotIn("(notes only)", parsed[question])
            else:
                self.assertEqual(parsed[question], answer)

    def test_question_text_mismatch_is_simply_absent(self):
        case = "1_options_selected"
        parsed = parse_terminal_answers(
            _stored(case), ["Pick a color", "A question nobody asked"]
        )
        self.assertEqual(parsed, {"Pick a color": "Red"})

    def test_partial_answers_are_not_assumed_one_per_question(self):
        """askUserQuestionTimeout auto-continues with whatever is selected so
        far, so PostToolUse can carry a subset of the questions."""
        case = "3_notes_only_mixed_with_selected"
        questions = _question_texts(case)
        tr = json.loads(json.dumps(_tool_response(case)))
        keep = questions[0]
        tr["answers"] = {keep: tr["answers"][keep]}
        parsed = parse_terminal_answers(reduce_tool_response(tr), questions)
        self.assertEqual(list(parsed), [keep])

    def test_reads_a_raw_unreduced_payload_too(self):
        """Defensive: the reduced shape is a subset of the raw one, so a row
        written by some other producer still reads (minus the notes)."""
        case = "1_options_selected"
        raw = json.dumps(_tool_response(case), ensure_ascii=False)
        self.assertEqual(
            parse_terminal_answers(raw, _question_texts(case)),
            {"Pick a color": "Red", "Pick a size": "Large"},
        )


class TestParseTerminalAnswersPlaceholders(unittest.TestCase):
    """The placeholder rule is an exact-literal match, never a heuristic."""

    QUESTION = "Which one?"

    def _payload(self, answer, notes=None):
        body = {"answers": {self.QUESTION: answer}, "notes": {}}
        if notes is not None:
            body["notes"][self.QUESTION] = notes
        return json.dumps(body)

    def test_known_placeholders(self):
        self.assertEqual(
            TERMINAL_ANSWER_PLACEHOLDERS,
            frozenset({"(notes only)", "(no option selected)"}),
        )

    def test_no_option_selected_with_notes_renders_the_notes(self):
        parsed = parse_terminal_answers(
            self._payload("(no option selected)", "It doesn't have to be CLAUDE.md"),
            [self.QUESTION],
        )
        self.assertEqual(parsed[self.QUESTION], "It doesn't have to be CLAUDE.md")

    def test_empty_answer_with_notes_renders_the_notes(self):
        parsed = parse_terminal_answers(
            self._payload("", "just my thoughts"), [self.QUESTION]
        )
        self.assertEqual(parsed[self.QUESTION], "just my thoughts")

    def test_placeholder_without_notes_falls_through_to_the_generic_text(self):
        """Patching ``✅ (notes only)`` into someone's chat is worse than
        useless — the question must be left unmatched instead."""
        for answer in ("(notes only)", "(no option selected)", "", "   "):
            with self.subTest(answer=answer):
                parsed = parse_terminal_answers(
                    self._payload(answer), [self.QUESTION]
                )
                self.assertEqual(parsed, {})

    def test_a_parenthesised_free_text_answer_is_left_alone(self):
        """A user can legitimately type ``(none of these)``; a
        'any parenthesised value' heuristic would swallow it."""
        parsed = parse_terminal_answers(
            self._payload("(none of these)", "ignored"), [self.QUESTION]
        )
        self.assertEqual(parsed[self.QUESTION], "(none of these)")

    def test_placeholder_with_blank_notes_falls_through(self):
        parsed = parse_terminal_answers(
            self._payload("(notes only)", "   "), [self.QUESTION]
        )
        self.assertEqual(parsed, {})

    def test_non_string_answer_is_coerced(self):
        parsed = parse_terminal_answers(
            json.dumps({"answers": {self.QUESTION: 42}, "notes": {}}), [self.QUESTION]
        )
        self.assertEqual(parsed[self.QUESTION], "42")


class TestParseTerminalAnswersProseFallback(unittest.TestCase):
    """Tier 2 — defensive only, exact matching, and never a positional zip."""

    def test_prose_pairs_match_by_exact_question_text(self):
        stored = '"Pick a color"="Red"\n"Pick a size"="Large"'
        parsed = parse_terminal_answers(stored, ["Pick a color", "Pick a size"])
        self.assertEqual(parsed, {"Pick a color": "Red", "Pick a size": "Large"})

    def test_does_not_zip_by_position(self):
        """The observed failure: on a real 4-question call the quoted-pair
        regex matched 2 pairs. Zipping 2 pairs onto 4 questions would
        mis-assign answers; only the exact text matches may be used."""
        questions = _question_texts("3_notes_only_mixed_with_selected")
        matched = questions[0]
        stored = (
            f'"{matched}"="@alias in header (Recommended)"\n'
            '"How should a role resolve to a Telegram destination?"="One token per role"\n'
            f'"{questions[2]}"=(no option selected) notes: It doesn\'t have to be\n'
            f'"{questions[3]}"=(no option selected) notes: 1. Whait indefinitely'
        )
        parsed = parse_terminal_answers(stored, questions)
        # Exactly the two quoted-and-quoted pairs, each on its own question.
        self.assertEqual(
            parsed,
            {
                matched: "@alias in header (Recommended)",
                questions[1]: "One token per role",
            },
        )
        # The unquoted answers are not invented, and nothing shifted position.
        self.assertNotIn(questions[2], parsed)
        self.assertNotIn(questions[3], parsed)

    def test_prose_tier_does_not_fire_for_valid_json(self):
        """A JSON payload is owned by tier 1 even when it holds no match; the
        regex must not scavenge question text out of the serialized dict."""
        stored = json.dumps({"answers": {}, "notes": {}})
        self.assertEqual(parse_terminal_answers(stored, ["Pick a color"]), {})

    def test_empty_prose_answer_is_ignored(self):
        self.assertEqual(parse_terminal_answers('"Q"=""', ["Q"]), {})


class TestParseTerminalAnswersDegradation(unittest.TestCase):
    """Never raises; anything unrecognised is ``{}`` and the caller renders the
    generic ``Answered in the terminal``."""

    def test_none_empty_invalid_and_unrelated(self):
        questions = _question_texts("1_options_selected")
        for stored in (
            None,
            "",
            "{not json",
            '{"answers": [1, 2, 3]}',
            '"just a json string"',
            "42",
            "Tool ran without output",
            "[]",
        ):
            with self.subTest(stored=stored):
                self.assertEqual(parse_terminal_answers(stored, questions), {})

    def test_no_questions_asked(self):
        self.assertEqual(parse_terminal_answers(_stored("1_options_selected"), []), {})


class TestTerminalAnswersRowRoundTrip(unittest.TestCase):
    """``terminal_answers`` survives the JSONL row, and a later sweep with no
    value cannot blank it."""

    def _create(self):
        return create_request(
            session_id="terminal-answers-test",
            cwd="/test",
            tool_name="AskUserQuestion",
            tool_input={"question": "Pick a color"},
            permission_suggestions=[],
            ttl_seconds=300,
        )

    def test_round_trips_through_the_jsonl_row(self):
        stored = _stored("3_notes_only_mixed_with_selected")
        request = self._create()
        updated = resolve_via_terminal(request.request_id, stored)

        self.assertIsNotNone(updated)
        self.assertEqual(updated.state, RequestState.RESOLVED_TERMINAL.value)
        reloaded = get_request(request.request_id)
        self.assertEqual(reloaded.terminal_answers, stored)
        # And it still parses off the row, unicode em-dashes and all.
        self.assertEqual(
            parse_terminal_answers(
                reloaded.terminal_answers,
                _question_texts("3_notes_only_mixed_with_selected"),
            ),
            parse_terminal_answers(
                stored, _question_texts("3_notes_only_mixed_with_selected")
            ),
        )

    def test_defaults_to_none(self):
        request = self._create()
        self.assertIsNone(get_request(request.request_id).terminal_answers)
        resolve_via_terminal(request.request_id)
        self.assertIsNone(get_request(request.request_id).terminal_answers)

    def test_a_later_sweep_without_answers_leaves_the_value_intact(self):
        """The hook's own ``resolve_via_terminal(sibling_id)`` sweep passes no
        answers and must not blank out what PostToolUse just recorded."""
        stored = _stored("1_options_selected")
        request = self._create()
        resolve_via_terminal(request.request_id, stored)

        self.assertIsNone(resolve_via_terminal(request.request_id))  # already terminal
        self.assertEqual(get_request(request.request_id).terminal_answers, stored)


if __name__ == "__main__":
    unittest.main(verbosity=2)
