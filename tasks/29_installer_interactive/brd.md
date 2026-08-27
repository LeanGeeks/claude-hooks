# Epic 29 — Interactive installer

**Status:** draft · **Owner:** Anton · **Created:** 2026-08-27 · **Rev:** 0

**This is an early draft and deliberately incomplete.** It records one fact so
it is not lost. The rest of the requirements come later, when this epic gets
focus. Do not implement from this document.

## Thesis

`install-claude-config.sh` has accumulated enough independent toggles that
running it is no longer a single decision. It should become **interactive, with
choices** — asking what to install and enable on this machine — rather than a
straight-line script whose behaviour is steered by hand-edited config files read
at run time.

## Fact 1 — the `[questions_listen]` opt-in is the shape of the problem

Enabling epic 23's answer listener today takes two steps in two places:

1. hand-edit `~/.config/claude-tg-relay/config.toml` to add

   ```toml
   [questions_listen]
   enabled = true
   ```

2. re-run the **entire** installer, because that is the only thing that reads the
   key and calls `systemctl --user enable` (`install-claude-config.sh:896-956`).

The guard itself is right and must survive into whatever replaces it: the
installer runs on any machine, and a resident daemon that polls a relay must
never start just because someone installed hooks. The comment in the script says
so explicitly — *"Never enable it silently."*

What is wrong is the **location** of the guard. A whole-installer re-run is a
heavy, wide-blast-radius operation to perform in order to flip one boolean, and
the two steps are in two unrelated files, so neither one alone is discoverable.
An operator who edits the TOML and does not know about step 2 gets silence.

This generalises: the installer currently mixes *"copy these artifacts into
place"* (idempotent, safe, always wanted) with *"decide what this machine runs"*
(a policy choice, occasional, per-machine). The interactive version should
separate them, and per-toggle commands should exist for the second kind so that
changing one's mind never requires a full re-install.

Related, same shape, not yet folded in: the epic-22 daily-reviewer crontab line
is documented but installed entirely by hand (`tasks/22_agent_permission_flows/`
22-05 §3), for the defensible reason that it schedules a job that edits
`settings.json` and commits. That is another policy choice the installer
currently cannot express, so it punts it to prose.

## Not yet written

Everything else: the interaction model, how choices are persisted and replayed
for unattended re-runs, what happens on upgrade, and the full toggle inventory.
