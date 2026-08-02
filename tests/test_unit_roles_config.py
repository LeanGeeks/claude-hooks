#!/usr/bin/env python3
"""Unit tests for roles_config.py (task 15-01).

Isolation rules (see task file "Testing" section):
- CLAUDE_PROJECT_DIR is pointed at a temp dir for the entire module so
  find_roles_file never escapes into the developer's real tree.
- workspace_dir is always a tmp_path, never os.getcwd() or a repo path.
- All fixtures are written as temporary TOML files.
"""

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────

_HOOKS = Path(__file__).parent.parent / ".claude" / "hooks"
sys.path.insert(0, str(_HOOKS))

# ── Test-module-level isolation: suppress find_roles_file's upward walk ────────
# Must be set before importing roles_config (the env var is read at call time,
# but doing it here matches the pattern in run_all_tests.py and conftest.py).
_ISOLATION_DIR = tempfile.mkdtemp(prefix="roles-config-test-")
# Unconditional assignment: setdefault gives no isolation when the var is
# already set (e.g. inside a Claude Code session). Save the original so
# tearDownModule can restore it.
_ORIGINAL_PROJECT_DIR = os.environ.get("CLAUDE_PROJECT_DIR")
os.environ["CLAUDE_PROJECT_DIR"] = _ISOLATION_DIR

import roles_config as rc  # noqa: E402


# ── TOML fixture helpers ───────────────────────────────────────────────────────

# The worked-example roles.toml from the task spec §Worked example
_EXAMPLE_ROLES_TOML = """\
workspace_id   = "leangeeks"
default        = "operator"
escalate_after = "30m"

[role.operator]
aliases = ["op"]
title   = "Operator"

[role.ux]
aliases = ["ux", "design"]
title   = "UX/UI designer"
escalate_after = "15m"

[role.architect]
aliases = ["arch"]
title   = "Tech lead / architect"
escalate_after = false

[role.prod]
title = "Product lead"
"""

# The worked-example config.toml from the task spec
_EXAMPLE_CONFIG_TOML = """\
installation_token = "rly_operator"

[roles]
ux   = "rly_designer"
arch = "operator"

[workspace.leangeeks.escalate_after]
ux = "5m"
"""


def _write(tmp: Path, name: str, content: str) -> Path:
    p = tmp / name
    p.write_text(textwrap.dedent(content))
    return p


def _make_workspace(tmp: Path, roles_toml: str) -> Path:
    """Create a workspace dir with .claude/roles.toml and return workspace dir."""
    ws = tmp / "workspace"
    ws.mkdir()
    dot_claude = ws / ".claude"
    dot_claude.mkdir()
    (dot_claude / "roles.toml").write_text(textwrap.dedent(roles_toml))
    return ws


def _example_catalog(tmp: Path) -> rc.RoleCatalog:
    ws = _make_workspace(tmp, _EXAMPLE_ROLES_TOML)
    cat = rc.load_catalog(str(ws), path=ws / ".claude" / "roles.toml")
    assert cat is not None, "example catalog must load"
    return cat


def _example_bindings(tmp: Path) -> rc.Bindings:
    cfg = _write(tmp, "config.toml", _EXAMPLE_CONFIG_TOML)
    return rc.load_bindings(cfg)


# ── parse_header_alias ─────────────────────────────────────────────────────────


class TestParseHeaderAlias(unittest.TestCase):
    """Every row of the alias-parsing table in the task spec."""

    def _check(self, header: str, expected_alias, expected_cleaned: str):
        alias, cleaned = rc.parse_header_alias(header)
        self.assertEqual(alias, expected_alias, f"alias for {header!r}")
        self.assertEqual(cleaned, expected_cleaned, f"cleaned for {header!r}")

    def test_at_alias_space_text(self):
        self._check("@ux Layout", "ux", "Layout")

    def test_at_alias_colon_space_text(self):
        self._check("@ux: Layout", "ux", "Layout")

    def test_at_alias_only(self):
        self._check("@ux", "ux", "")

    def test_at_alias_uppercase(self):
        self._check("@UX Layout", "ux", "Layout")

    def test_no_tag_plain_text(self):
        self._check("Layout", None, "Layout")

    def test_trailing_tag_not_recognised(self):
        self._check("Layout @ux", None, "Layout @ux")

    def test_lone_at(self):
        self._check("@ Layout", None, "@ Layout")

    def test_empty_string(self):
        self._check("", None, "")


# ── parse_duration ─────────────────────────────────────────────────────────────


class TestParseDuration(unittest.TestCase):
    def test_minutes(self):
        self.assertAlmostEqual(rc.parse_duration("30m"), 1800.0)

    def test_hours(self):
        self.assertAlmostEqual(rc.parse_duration("2h"), 7200.0)

    def test_seconds_suffix(self):
        self.assertAlmostEqual(rc.parse_duration("90s"), 90.0)

    def test_bare_number_string(self):
        self.assertAlmostEqual(rc.parse_duration("45"), 45.0)

    def test_toml_false(self):
        self.assertIsNone(rc.parse_duration(False))

    def test_string_false(self):
        self.assertIsNone(rc.parse_duration("false"))

    def test_string_off(self):
        self.assertIsNone(rc.parse_duration("off"))

    def test_string_zero(self):
        self.assertIsNone(rc.parse_duration("0"))

    def test_integer_zero(self):
        self.assertIsNone(rc.parse_duration(0))

    def test_integer_seconds(self):
        self.assertAlmostEqual(rc.parse_duration(120), 120.0)

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            rc.parse_duration("garbage")

    def test_empty_string_raises(self):
        with self.assertRaises(ValueError):
            rc.parse_duration("")

    def test_true_raises(self):
        # True is a bool — only False means "never"
        with self.assertRaises(ValueError):
            rc.parse_duration(True)


# ── find_roles_file ────────────────────────────────────────────────────────────


class TestFindRolesFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.tmp = Path(self._tmp)
        # Temporarily override CLAUDE_PROJECT_DIR with a known-empty dir so the
        # upward walk is suppressed for each test that sets it explicitly.
        self._orig = os.environ.get("CLAUDE_PROJECT_DIR")

    def tearDown(self):
        if self._orig is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = self._orig

    # ── CLAUDE_PROJECT_DIR branch ───────────────────────────────────────────

    def test_env_var_set_file_exists(self):
        dot_claude = self.tmp / ".claude"
        dot_claude.mkdir()
        roles = dot_claude / "roles.toml"
        roles.write_text('default = "op"\n[role.op]\n')
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.tmp)

        result = rc.find_roles_file(str(self.tmp / "sub"))
        self.assertEqual(result, roles)

    def test_env_var_set_file_missing_returns_none(self):
        # Even if a roles.toml exists elsewhere, the env var suppresses the walk.
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.tmp)
        result = rc.find_roles_file(str(self.tmp))
        self.assertIsNone(result)

    # ── upward walk branch ──────────────────────────────────────────────────

    def test_walk_finds_in_workspace_root(self):
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        ws = self.tmp / "ws"
        ws.mkdir()
        dot_claude = ws / ".claude"
        dot_claude.mkdir()
        roles = dot_claude / "roles.toml"
        roles.write_text('default = "op"\n[role.op]\n')

        result = rc.find_roles_file(str(ws))
        self.assertEqual(result, roles)

    def test_walk_finds_from_nested_subdir(self):
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        ws = self.tmp / "ws"
        nested = ws / "a" / "b" / "c"
        nested.mkdir(parents=True)
        dot_claude = ws / ".claude"
        dot_claude.mkdir()
        roles = dot_claude / "roles.toml"
        roles.write_text('default = "op"\n[role.op]\n')

        result = rc.find_roles_file(str(nested))
        self.assertEqual(result, roles)

    def test_walk_returns_none_when_no_roles_toml(self):
        """CLAUDE_PROJECT_DIR pinned to a clean tmp dir must return None.

        Pins CLAUDE_PROJECT_DIR to self.tmp (no .claude/roles.toml there),
        which suppresses the upward walk entirely so find_roles_file returns
        None unconditionally — not dependent on the developer's real tree.
        """
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.tmp)
        ws = self.tmp / "ws"
        ws.mkdir()
        result = rc.find_roles_file(str(ws))
        self.assertIsNone(result,
            f"find_roles_file escaped into the real tree: {result}")


# ── load_catalog ───────────────────────────────────────────────────────────────


class TestLoadCatalogNoneReturns(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.tmp = Path(self._tmp)
        # Route find_roles_file to a controlled dir so we can pass explicit path
        os.environ["CLAUDE_PROJECT_DIR"] = _ISOLATION_DIR

    def test_no_file_returns_none(self):
        result = rc.load_catalog(str(self.tmp))
        self.assertIsNone(result)

    def test_unparseable_toml_returns_none(self):
        ws = _make_workspace(self.tmp, "not valid toml [[[")
        result = rc.load_catalog(str(ws), path=ws / ".claude" / "roles.toml")
        self.assertIsNone(result)

    def test_missing_default_returns_none(self):
        ws = _make_workspace(self.tmp, "[role.op]\ntitle = 'Op'\n")
        result = rc.load_catalog(str(ws), path=ws / ".claude" / "roles.toml")
        self.assertIsNone(result)

    def test_default_names_undefined_role_returns_none(self):
        content = 'default = "ghost"\n[role.op]\ntitle = "Op"\n'
        ws = _make_workspace(self.tmp, content)
        result = rc.load_catalog(str(ws), path=ws / ".claude" / "roles.toml")
        self.assertIsNone(result)


class TestLoadCatalogHappyPath(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.tmp = Path(self._tmp)
        os.environ["CLAUDE_PROJECT_DIR"] = _ISOLATION_DIR

    def test_example_catalog_loads(self):
        cat = _example_catalog(self.tmp)
        self.assertEqual(cat.workspace_id, "leangeeks")
        self.assertEqual(cat.default_role, "operator")
        self.assertIn("operator", cat.roles)
        self.assertIn("ux", cat.roles)
        self.assertIn("architect", cat.roles)
        self.assertIn("prod", cat.roles)

    def test_role_id_always_in_aliases(self):
        cat = _example_catalog(self.tmp)
        for role_id, role in cat.roles.items():
            self.assertIn(role_id.lower(), role.aliases,
                          f"role_id {role_id!r} not in aliases {role.aliases!r}")

    def test_alias_index_populated(self):
        cat = _example_catalog(self.tmp)
        self.assertEqual(cat.alias_index["op"], "operator")
        self.assertEqual(cat.alias_index["operator"], "operator")
        self.assertEqual(cat.alias_index["ux"], "ux")
        self.assertEqual(cat.alias_index["design"], "ux")
        self.assertEqual(cat.alias_index["arch"], "architect")

    def test_escalate_after_top_level_merged(self):
        # ux overrides to 15m, architect to false (None), prod gets the 30m default
        cat = _example_catalog(self.tmp)
        self.assertAlmostEqual(cat.roles["ux"].escalate_after, 900.0)        # 15m
        self.assertIsNone(cat.roles["architect"].escalate_after)              # false
        self.assertAlmostEqual(cat.roles["prod"].escalate_after, 1800.0)     # top-level 30m
        self.assertAlmostEqual(cat.roles["operator"].escalate_after, 1800.0) # top-level 30m

    def test_workspace_id_defaults_to_dir_name(self):
        content = 'default = "op"\n[role.op]\ntitle = "Op"\n'
        ws = _make_workspace(self.tmp, content)
        cat = rc.load_catalog(str(ws), path=ws / ".claude" / "roles.toml")
        self.assertIsNotNone(cat)
        self.assertEqual(cat.workspace_id, "workspace")  # _make_workspace uses "workspace"

    def test_explicit_workspace_id_used(self):
        cat = _example_catalog(self.tmp)
        self.assertEqual(cat.workspace_id, "leangeeks")

    def test_title_defaults_to_role_id(self):
        content = """\
default = "op"

[role.op]
title = "Operator"

[role.notitled]
aliases = ["nt"]
"""
        ws = _make_workspace(self.tmp, content)
        cat = rc.load_catalog(str(ws), path=ws / ".claude" / "roles.toml")
        self.assertIsNotNone(cat)
        self.assertEqual(cat.roles["notitled"].title, "notitled")

    def test_path_recorded(self):
        ws = _make_workspace(self.tmp, _EXAMPLE_ROLES_TOML)
        cat = rc.load_catalog(str(ws), path=ws / ".claude" / "roles.toml")
        self.assertEqual(cat.path, ws / ".claude" / "roles.toml")

    def test_errors_empty_on_clean_config(self):
        cat = _example_catalog(self.tmp)
        self.assertEqual(cat.errors, ())


class TestLoadCatalogRecoverableErrors(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.tmp = Path(self._tmp)
        os.environ["CLAUDE_PROJECT_DIR"] = _ISOLATION_DIR

    def test_duplicate_alias_recorded_in_errors(self):
        content = """\
default = "a"

[role.a]
aliases = ["shared"]
title = "A"

[role.b]
aliases = ["shared"]
title = "B"
"""
        ws = _make_workspace(self.tmp, content)
        cat = rc.load_catalog(str(ws), path=ws / ".claude" / "roles.toml")
        self.assertIsNotNone(cat)
        self.assertTrue(any("shared" in e for e in cat.errors),
                        f"expected 'shared' in errors: {cat.errors}")
        # First definition wins
        self.assertEqual(cat.alias_index["shared"], "a")

    def test_bad_per_role_duration_recorded_in_errors(self):
        content = """\
default = "op"
escalate_after = "30m"

[role.op]
title = "Op"
escalate_after = "badvalue"
"""
        ws = _make_workspace(self.tmp, content)
        cat = rc.load_catalog(str(ws), path=ws / ".claude" / "roles.toml")
        self.assertIsNotNone(cat)
        self.assertTrue(any("escalate_after" in e for e in cat.errors),
                        f"expected escalate_after error: {cat.errors}")

    def test_bad_top_level_duration_recorded_in_errors(self):
        content = """\
default = "op"
escalate_after = "invalid"

[role.op]
title = "Op"
"""
        ws = _make_workspace(self.tmp, content)
        cat = rc.load_catalog(str(ws), path=ws / ".claude" / "roles.toml")
        self.assertIsNotNone(cat)
        self.assertTrue(cat.errors)


# ── load_bindings ──────────────────────────────────────────────────────────────


class TestLoadBindings(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.tmp = Path(self._tmp)

    def test_missing_file_returns_empty_bindings(self):
        b = rc.load_bindings(self.tmp / "nonexistent.toml")
        self.assertIsNone(b.server_url)
        self.assertIsNone(b.default_token)
        self.assertEqual(b.roles, {})
        self.assertEqual(b.workspace_roles, {})
        self.assertEqual(b.escalate_after, {})
        self.assertEqual(b.workspace_escalate_after, {})
        self.assertEqual(b.errors, ())

    def test_example_config_loads(self):
        b = _example_bindings(self.tmp)
        self.assertEqual(b.default_token, "rly_operator")
        self.assertIsNone(b.server_url)  # not in example config

    def test_server_url_loaded(self):
        cfg = _write(self.tmp, "config.toml", """\
server_url = "https://relay.example.com"
installation_token = "rly_abc"
""")
        b = rc.load_bindings(cfg)
        self.assertEqual(b.server_url, "https://relay.example.com")
        self.assertEqual(b.default_token, "rly_abc")

    def test_roles_table_loaded(self):
        b = _example_bindings(self.tmp)
        self.assertEqual(b.roles["ux"], "rly_designer")
        self.assertEqual(b.roles["arch"], "operator")

    def test_workspace_roles_loaded(self):
        cfg = _write(self.tmp, "config.toml", """\
installation_token = "rly_x"

[workspace.acme.roles]
ux = "rly_ux_acme"
""")
        b = rc.load_bindings(cfg)
        self.assertEqual(b.workspace_roles["acme"]["ux"], "rly_ux_acme")

    def test_workspace_escalate_after_loaded(self):
        b = _example_bindings(self.tmp)
        self.assertAlmostEqual(b.workspace_escalate_after["leangeeks"]["ux"], 300.0)  # 5m

    def test_escalate_after_table_loaded(self):
        cfg = _write(self.tmp, "config.toml", """\
installation_token = "rly_x"

[escalate_after]
ux = "45m"
""")
        b = rc.load_bindings(cfg)
        self.assertAlmostEqual(b.escalate_after["ux"], 2700.0)  # 45m

    def test_invalid_duration_recorded_in_errors(self):
        cfg = _write(self.tmp, "config.toml", """\
installation_token = "rly_x"

[escalate_after]
ux = "notavalue"
""")
        b = rc.load_bindings(cfg)
        self.assertTrue(b.errors)

    def test_invalid_workspace_duration_recorded_in_errors(self):
        cfg = _write(self.tmp, "config.toml", """\
installation_token = "rly_x"

[workspace.acme.escalate_after]
ux = "bad"
""")
        b = rc.load_bindings(cfg)
        self.assertTrue(b.errors)


# ── resolve_destination — worked example ──────────────────────────────────────


class TestResolveDestinationWorkedExample(unittest.TestCase):
    """Every row of the worked-example table in the task spec."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.tmp = Path(self._tmp)
        os.environ["CLAUDE_PROJECT_DIR"] = _ISOLATION_DIR
        self.cat = _example_catalog(self.tmp)
        self.b = _example_bindings(self.tmp)

    def _dest(self, alias):
        return rc.resolve_destination(self.cat, self.b, alias)

    # alias=None → operator
    def test_no_alias_goes_to_default(self):
        d = self._dest(None)
        self.assertEqual(d.role_id, "operator")
        self.assertEqual(d.token, "rly_operator")
        self.assertIsNone(d.escalate_after)
        self.assertEqual(d.notes, ())
        self.assertTrue(d.is_default)

    # alias="design" → ux → rly_designer, escalate 300s (workspace override 5m)
    def test_design_alias_goes_to_ux(self):
        d = self._dest("design")
        self.assertEqual(d.role_id, "ux")
        self.assertEqual(d.token, "rly_designer")
        self.assertAlmostEqual(d.escalate_after, 300.0)
        self.assertEqual(d.notes, ())
        self.assertFalse(d.is_default)

    # alias="arch" → architect → reference "operator" → rly_operator, escalate None (false)
    def test_arch_alias_via_reference(self):
        d = self._dest("arch")
        self.assertEqual(d.role_id, "architect")
        self.assertEqual(d.token, "rly_operator")
        self.assertIsNone(d.escalate_after)  # false at role level, also same token
        self.assertEqual(d.notes, ())

    # alias="prod" → prod → no binding → fallback to operator + note
    def test_prod_alias_falls_back_to_operator(self):
        d = self._dest("prod")
        self.assertEqual(d.role_id, "operator")
        self.assertEqual(d.token, "rly_operator")
        self.assertIsNone(d.escalate_after)
        self.assertEqual(len(d.notes), 1)
        self.assertIn("Product lead", d.notes[0])
        self.assertIn("not reachable from this machine", d.notes[0])

    # alias="nope" → unknown alias → operator + note
    def test_unknown_alias_routes_to_default(self):
        d = self._dest("nope")
        self.assertEqual(d.role_id, "operator")
        self.assertEqual(d.token, "rly_operator")
        self.assertIsNone(d.escalate_after)
        self.assertEqual(len(d.notes), 1)
        self.assertIn("@nope", d.notes[0])
        self.assertIn("Operator", d.notes[0])


# ── resolve_destination — reference chains & cycles ───────────────────────────


class TestResolveDestinationChains(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.tmp = Path(self._tmp)
        os.environ["CLAUDE_PROJECT_DIR"] = _ISOLATION_DIR

    def _catalog(self, toml: str) -> rc.RoleCatalog:
        ws = _make_workspace(self.tmp, toml)
        cat = rc.load_catalog(str(ws), path=ws / ".claude" / "roles.toml")
        self.assertIsNotNone(cat)
        return cat

    def _bindings(self, toml: str) -> rc.Bindings:
        cfg = _write(self.tmp, "config.toml", toml)
        return rc.load_bindings(cfg)

    def test_two_role_cycle_falls_back_to_default(self):
        cat = self._catalog("""\
default = "op"

[role.op]
title = "Operator"

[role.arch]
aliases = ["arch"]
title = "Tech lead"

[role.prod]
aliases = ["prod"]
title = "Product lead"
""")
        b = self._bindings("""\
installation_token = "rly_op"

[roles]
arch = "prod"
prod = "arch"
""")
        d = rc.resolve_destination(cat, b, "arch")
        self.assertEqual(d.role_id, "op")
        self.assertEqual(d.token, "rly_op")
        self.assertEqual(len(d.notes), 1)
        note = d.notes[0]
        self.assertIn("Tech lead", note)
        self.assertIn("alias cycle", note)
        # The cycle should reference arch and prod
        self.assertIn("arch", note)
        self.assertIn("prod", note)

    def test_unknown_reference_falls_back_to_default(self):
        cat = self._catalog("""\
default = "op"

[role.op]
title = "Operator"

[role.ux]
aliases = ["ux"]
title = "Designer"
""")
        b = self._bindings("""\
installation_token = "rly_op"

[roles]
ux = "designr"
""")
        d = rc.resolve_destination(cat, b, "ux")
        self.assertEqual(d.role_id, "op")
        self.assertEqual(d.token, "rly_op")
        self.assertEqual(len(d.notes), 1)
        note = d.notes[0]
        self.assertIn("Designer", note)
        self.assertIn("unknown role", note)
        self.assertIn("designr", note)

    def test_reference_chain_resolves(self):
        """A → B → rly_token: two-hop chain resolves to the token."""
        cat = self._catalog("""\
default = "op"

[role.op]
title = "Operator"

[role.lead]
aliases = ["lead"]
title = "Lead"

[role.backup]
aliases = ["backup"]
title = "Backup"
""")
        b = self._bindings("""\
installation_token = "rly_op"

[roles]
lead   = "backup"
backup = "rly_backup_token"
""")
        d = rc.resolve_destination(cat, b, "lead")
        self.assertEqual(d.role_id, "lead")
        self.assertEqual(d.token, "rly_backup_token")
        self.assertEqual(d.notes, ())

    def test_unreachable_default_returns_none_token(self):
        """Step 5: when even the default has no token, token=None."""
        cat = self._catalog("""\
default = "op"

[role.op]
title = "Operator"
""")
        b = rc.load_bindings(self.tmp / "nonexistent.toml")
        d = rc.resolve_destination(cat, b, None)
        self.assertEqual(d.role_id, "op")
        self.assertIsNone(d.token)


# ── resolve_destination — escalation ──────────────────────────────────────────


class TestResolveDestinationEscalation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.tmp = Path(self._tmp)
        os.environ["CLAUDE_PROJECT_DIR"] = _ISOLATION_DIR

    def _catalog(self, toml: str) -> rc.RoleCatalog:
        ws = _make_workspace(self.tmp, toml)
        cat = rc.load_catalog(str(ws), path=ws / ".claude" / "roles.toml")
        self.assertIsNotNone(cat)
        return cat

    def _bindings(self, toml: str) -> rc.Bindings:
        cfg = _write(self.tmp, "config.toml", toml)
        return rc.load_bindings(cfg)

    def test_escalation_none_after_fallback(self):
        """Fallback cancels escalation."""
        cat = self._catalog("""\
default = "op"

[role.op]
title = "Operator"

[role.ux]
aliases = ["ux"]
title = "Designer"
escalate_after = "15m"
""")
        b = self._bindings("installation_token = \"rly_op\"\n")
        d = rc.resolve_destination(cat, b, "ux")
        self.assertEqual(d.role_id, "op")  # fell back
        self.assertIsNone(d.escalate_after)

    def test_escalation_none_when_role_is_default(self):
        cat = self._catalog("""\
default = "op"
escalate_after = "30m"

[role.op]
title = "Operator"
""")
        b = self._bindings("installation_token = \"rly_op\"\n")
        d = rc.resolve_destination(cat, b, None)
        self.assertTrue(d.is_default)
        self.assertIsNone(d.escalate_after)

    def test_escalation_none_when_same_token_as_default(self):
        """arch references operator → same token → escalation=None."""
        cat = self._catalog("""\
default = "op"

[role.op]
title = "Operator"
escalate_after = "30m"

[role.arch]
aliases = ["arch"]
title = "Architect"
escalate_after = "30m"
""")
        b = self._bindings("""\
installation_token = "rly_op"

[roles]
arch = "op"
""")
        d = rc.resolve_destination(cat, b, "arch")
        self.assertEqual(d.token, "rly_op")
        self.assertIsNone(d.escalate_after)

    def test_workspace_escalate_after_beats_role_level(self):
        """Workspace [escalate_after] overrides role-level escalate_after."""
        cat = self._catalog("""\
default = "op"

[role.op]
title = "Operator"

[role.ux]
aliases = ["ux"]
title = "Designer"
escalate_after = "15m"
""")
        b = self._bindings("""\
installation_token = "rly_op"

[roles]
ux = "rly_ux"

[workspace.ws1.escalate_after]
ux = "5m"
""")
        # Simulate workspace_id = "ws1"
        ws = self.tmp / "workspace"
        ws.mkdir(exist_ok=True)
        (ws / ".claude").mkdir(exist_ok=True)
        toml_content = """\
workspace_id = "ws1"
default = "op"

[role.op]
title = "Operator"

[role.ux]
aliases = ["ux"]
title = "Designer"
escalate_after = "15m"
"""
        (ws / ".claude" / "roles.toml").write_text(toml_content)
        cat2 = rc.load_catalog(str(ws), path=ws / ".claude" / "roles.toml")
        self.assertIsNotNone(cat2)
        d = rc.resolve_destination(cat2, b, "ux")
        # workspace override is 5m = 300s
        self.assertAlmostEqual(d.escalate_after, 300.0)

    def test_explicit_false_at_higher_level_beats_duration_at_lower(self):
        """An explicit false in [escalate_after] wins over role-level duration."""
        cat = self._catalog("""\
default = "op"

[role.op]
title = "Operator"

[role.ux]
aliases = ["ux"]
title = "Designer"
escalate_after = "15m"
""")
        b = self._bindings("""\
installation_token = "rly_op"

[roles]
ux = "rly_ux"

[escalate_after]
ux = false
""")
        d = rc.resolve_destination(cat, b, "ux")
        self.assertIsNone(d.escalate_after)

    def test_invalid_duration_falls_through_to_next_level(self):
        """Invalid duration in roles.toml is dropped; catalog-level default applies."""
        content = """\
default = "op"
escalate_after = "30m"

[role.op]
title = "Operator"

[role.ux]
aliases = ["ux"]
title = "Designer"
escalate_after = "bad_value"
"""
        ws = _make_workspace(self.tmp, content)
        cat = rc.load_catalog(str(ws), path=ws / ".claude" / "roles.toml")
        # Bad per-role duration: should fall through to top-level 30m default
        self.assertIsNotNone(cat)
        # The Role should carry the top-level default (30m = 1800s), not the bad value
        self.assertAlmostEqual(cat.roles["ux"].escalate_after, 1800.0)

    def test_four_level_precedence_workspace_wins(self):
        """Worked example: workspace overrides bindings, role, top-level."""
        d = rc.resolve_destination(
            _example_catalog(self.tmp),
            _example_bindings(self.tmp),
            "design",
        )
        # workspace.leangeeks.escalate_after.ux = "5m" = 300s
        # beats role-level "15m" = 900s
        self.assertAlmostEqual(d.escalate_after, 300.0)

    def test_escalation_lookup_by_alias(self):
        """[escalate_after] keyed by alias resolves the same as by role_id."""
        cat = self._catalog("""\
default = "op"

[role.op]
title = "Operator"

[role.architect]
aliases = ["arch"]
title = "Architect"
""")
        b = self._bindings("""\
installation_token = "rly_op"

[roles]
architect = "rly_arch"

[escalate_after]
arch = "45m"
""")
        d = rc.resolve_destination(cat, b, "arch")
        self.assertEqual(d.role_id, "architect")
        self.assertAlmostEqual(d.escalate_after, 2700.0)  # 45m = 2700s


# ── Exact note wording ─────────────────────────────────────────────────────────


class TestNoteWording(unittest.TestCase):
    """Assert the exact note sentences from the step-4 table (§Token resolution rules)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.tmp = Path(self._tmp)
        os.environ["CLAUDE_PROJECT_DIR"] = _ISOLATION_DIR

    def _base_catalog(self) -> rc.RoleCatalog:
        return _example_catalog(self.tmp)

    def test_unknown_alias_note(self):
        d = rc.resolve_destination(
            self._base_catalog(),
            _example_bindings(self.tmp),
            "nope",
        )
        self.assertEqual(d.notes[0], "Unknown role @nope — routed to Operator.")

    def test_not_reachable_note(self):
        """prod has no binding → 'not reachable from this machine'."""
        d = rc.resolve_destination(
            self._base_catalog(),
            _example_bindings(self.tmp),
            "prod",
        )
        self.assertEqual(d.notes[0], "Intended for Product lead — not reachable from this machine.")

    def test_cycle_note(self):
        ws = _make_workspace(self.tmp, """\
default = "op"

[role.op]
title = "Operator"

[role.arch]
aliases = ["arch"]
title = "Tech lead / architect"

[role.prod]
aliases = ["prod"]
title = "Product lead"
""")
        cat = rc.load_catalog(str(ws), path=ws / ".claude" / "roles.toml")
        cfg = _write(self.tmp, "cycle_config.toml", """\
installation_token = "rly_op"

[roles]
arch = "prod"
prod = "arch"
""")
        b = rc.load_bindings(cfg)
        d = rc.resolve_destination(cat, b, "arch")
        self.assertEqual(
            d.notes[0],
            "Intended for Tech lead / architect — its binding on this machine is broken (alias cycle: arch → prod → arch).",
        )

    def test_unknown_reference_note(self):
        ws = _make_workspace(self.tmp, """\
default = "op"

[role.op]
title = "Operator"

[role.arch]
aliases = ["arch"]
title = "Tech lead / architect"
""")
        cat = rc.load_catalog(str(ws), path=ws / ".claude" / "roles.toml")
        cfg = _write(self.tmp, "ref_config.toml", """\
installation_token = "rly_op"

[roles]
arch = "designr"
""")
        b = rc.load_bindings(cfg)
        d = rc.resolve_destination(cat, b, "arch")
        self.assertEqual(
            d.notes[0],
            'Intended for Tech lead / architect — its binding on this machine is broken (points at unknown role "designr").',
        )


def tearDownModule():
    """Restore CLAUDE_PROJECT_DIR to its pre-module value."""
    if _ORIGINAL_PROJECT_DIR is None:
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
    else:
        os.environ["CLAUDE_PROJECT_DIR"] = _ORIGINAL_PROJECT_DIR


if __name__ == "__main__":
    unittest.main()
