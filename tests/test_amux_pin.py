#!/usr/bin/env python3
"""Pinned-amux drift checks (task 20-05).

Epic 20 builds the Codex worker path against one pinned amux revision on the
fork branch (state.md Phase 0; the sibling repo documents the consumer
contract in ``docs/codex-provider.md`` §6 "Pinning this contract"). The pin is
a CONTAINMENT requirement, not an exact-HEAD equality: the sibling checkout
may sit on newer commits (docs, later epics) as long as the pinned revision is
an ancestor of its HEAD — exactly what ``install-amux.sh`` already treats as
"ahead of pin" rather than drift. Real drift (rewritten branch, wrong branch,
stale clone) must FAIL with instructions, not a bare git error.

When the sibling checkout does not exist on this machine, the containment
check SKIPS: this repository's suite must never require a sibling checkout to
be present. The pin's cross-surface consistency (tests, install-amux.sh,
operator docs, state.md) is checked unconditionally — those files are here.
"""

import os
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: The amux revision epic 20 is verified against (state.md Phase 0): sibling
#: epic 01 complete, amux suite 398 passed / 0 failed, on branch
#: feat/epic-10-amux-extensions. Keep the FULL 40-hex sha — a short prefix
#: could resolve ambiguously in a clone that gained a colliding object.
AMUX_PIN = "11a8426a014e8b9ca30134758e66e3912628b647"
AMUX_BRANCH = "feat/epic-10-amux-extensions"

#: Where the sibling checkout lives (install-amux.sh default; overridable).
AMUX_DIR = Path(os.environ.get("AMUX_DIR", REPO.parent / "amux"))

OPERATOR_DOC = REPO / "docs" / "amux-spawn-codex-workers.md"
STATE_MD = REPO / "tasks" / "20_codex_background_workers" / "state.md"
INSTALL_AMUX = REPO / "install-amux.sh"

DRIFT_MESSAGE = f"""\
amux drift: the sibling checkout at {{dir}} does not contain the pinned amux \
revision {AMUX_PIN}
(branch {AMUX_BRANCH}) that this repository's Codex workers are verified \
against. The pinned
contract — provider argv, <name>.env keys, codex_session_id capture, .rc \
semantics, rc-66
mismatch quarantine — is what amux-spawn is tested on; an uncontained \
checkout may not provide it.

Either (a) bring the checkout back onto the pinned lineage:
    git -C {{dir}} fetch --all
    git -C {{dir}} checkout {AMUX_BRANCH}
    git -C {{dir}} merge {AMUX_PIN}        # or rebase your work onto it

or (b) deliberately move the pin (only after re-verifying BOTH suites):
    1. update AMUX_PIN in tests/test_amux_pin.py
    2. update AMUX_PIN in install-amux.sh
    3. update the revision in docs/amux-spawn-codex-workers.md
    4. update Phase 0 in tasks/20_codex_background_workers/state.md
    then run this repository's full suite AND the amux suite at the new \
revision."""


def _git(amux_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(amux_dir), *args],
        capture_output=True, text=True, timeout=60)


def _checkout_is_amux(amux_dir: Path) -> bool:
    """A git repo whose HEAD tree has the ``amux`` script (same probe
    install-amux.sh uses), so a random directory never counts as a sibling."""
    if not (amux_dir / ".git").exists():
        return False
    return _git(amux_dir, "cat-file", "-e", "HEAD:amux").returncode == 0


class TestPinnedAmuxRevision(unittest.TestCase):

    def test_pin_is_a_full_sha(self):
        """A short-hash pin makes cat-file/merge-base ambiguous; keep 40 hex."""
        self.assertRegex(AMUX_PIN, r"^[0-9a-f]{40}$")

    def test_sibling_checkout_contains_the_pinned_revision(self):
        """Containment (ancestor-of-HEAD), skip when the sibling is absent."""
        if not AMUX_DIR.is_dir():
            self.skipTest(
                f"sibling amux checkout not present at {AMUX_DIR} — "
                "skipping the pinned-revision containment check "
                "(this suite must run without a sibling checkout)")
        if not _checkout_is_amux(AMUX_DIR):
            self.skipTest(
                f"{AMUX_DIR} is not an amux git checkout — skipping the "
                "pinned-revision containment check")

        head = _git(AMUX_DIR, "rev-parse", "HEAD")
        self.assertEqual(
            head.returncode, 0,
            f"cannot resolve HEAD in {AMUX_DIR}: {head.stderr.strip()}")
        head_sha = head.stdout.strip()

        # The pin object must exist here at all (else: never fetched, wrong
        # clone, or a rewritten branch) before ancestry can be asked.
        have = _git(AMUX_DIR, "cat-file", "-e", f"{AMUX_PIN}^{{commit}}")
        ancestor = _git(AMUX_DIR, "merge-base", "--is-ancestor",
                        AMUX_PIN, "HEAD")
        if have.returncode != 0 or ancestor.returncode != 0:
            self.fail(DRIFT_MESSAGE.format(dir=AMUX_DIR)
                      + f"\n(observed HEAD: {head_sha})")

    # ── the pin is recorded consistently on every surface that names it ─────

    def test_install_amux_defaults_to_the_pin(self):
        text = INSTALL_AMUX.read_text()
        self.assertIn(
            f'AMUX_PIN="${{AMUX_PIN:-{AMUX_PIN}}}"', text,
            "install-amux.sh's default AMUX_PIN drifted from the epic-20 pin; "
            "update it together with tests/test_amux_pin.py (see the drift "
            "message above for the full checklist)")

    def test_operator_doc_records_the_pin(self):
        self.assertTrue(
            OPERATOR_DOC.is_file(),
            f"operator doc missing: {OPERATOR_DOC}")
        self.assertIn(AMUX_PIN, OPERATOR_DOC.read_text())

    def test_state_md_phase0_records_the_pin(self):
        self.assertIn(AMUX_PIN, STATE_MD.read_text())


if __name__ == "__main__":
    unittest.main()
