# AGENTS.md

Router for agents working in this repository. [CLAUDE.md](CLAUDE.md) carries the same content.

## What this repository is

Topos builds a model of what a system allows (entities, states, guarded transitions, reads and
writes, grants) and checks it against what the system's owner declared as intended. See
[README.md](README.md). Its goals, clauses, decisions and experiments live in the
`+tasker-systems/topos` context in Temper. Read the active goal there before starting work.

| Topic | Read |
|---|---|
| The documents topos exchanges (draft) | [`schema/`](schema/) |
| A synthetic worked example | [`examples/`](examples/) |
| The tasker-systems engineering baseline, and what a gate enforces here | [`.claude/skills/tasker-systems-conventions`](.claude/skills/tasker-systems-conventions/SKILL.md) |

## Rules

- **This repository is public. No statement of a security or system defect in any target goes
  here.** That covers code, tests, fixtures, examples, docs, commit messages and pull requests.
  Such statements go only in Temper (`+tasker-systems/topos`, or the target's own context) or in
  [`tasker-systems/temper-artifacts`](https://github.com/tasker-systems/temper-artifacts).
  Tests and examples here use synthetic systems. A run against a real target keeps its inputs and
  outputs outside this repository.
- **Intent is an input, never an output.** Code that classifies a path as intended or unintended
  takes the declared intent as a parameter. Nothing infers intent from structure and presents it
  as fact.
- **Extracted and inferred are always distinguishable.** Every model element carries provenance.
  An element that was not read directly from the target records who or what proposed it, and with
  what confidence.
- **Specs, plans, reviews, spikes and handoffs go in `tasker-systems/temper-artifacts`**, never
  here.
- **The baseline** is pinned in `.conventions/consumer.toml`. Never edit `.conventions/baseline/`.
  Move the pin with `python3 -I .conventions/baseline/tools/conventions.py bump --revision <sha|main>`.
  *(gated: `ci.yml#conventions`)*

## Implementation language

Python, for now: extraction, probing, clustering and the numeric readings, under the adopted
`ruff.toml`. Rust enters with the first extractor that needs it, together with the baseline's Rust
configuration. The decision is in `+tasker-systems/topos`. Components still meet only at the
versioned documents in [`schema/`](schema/).

## Tools and commands

[mise](https://mise.jdx.dev) pins Python and uv in [`mise.toml`](mise.toml). uv installs the
Python dependencies from [`uv.lock`](uv.lock). CI runs the same commands.

```sh
mise install                                   # Python and uv, at the pinned versions
uv sync                                        # the project and every dependency group
uv run pytest                                  # tests; database tests need TOPOS_TEST_DSN
uv run ruff check . && uv run ruff format --check .
uv run python -I tools/check_examples.py
python3 -I .conventions/baseline/tools/conventions.py check
python3 -I .conventions/baseline/tools/conventions.py witness
```

- Add or change a dependency with `uv add` or `uv lock`, and commit `uv.lock` with
  `pyproject.toml`. CI runs with `--locked`, so a stale lock fails. *(gated: `ci.yml#python-tests`)*
- uv resolves only releases at least 7 days old (`exclude-newer` in `pyproject.toml`), and mise
  holds new tool releases for 7 days (SC-IN-1).

## Commits and PRs

Conventional commits (`feat:`, `fix:`, `docs:`, `chore:`). A PR body says what changed, why, and
how it was verified. No session narrative.
