# topos

Topos describes a system as one model: its entities and their states, the transitions between
states, what each transition reads and writes, and who is allowed to do what. It then checks what
that model **allows** against what its owner **intended**.

The guiding position is that effect and side effect, feature and bug, have the same structure. A
path through a system is only a path. What makes one path a feature and another a bug is the
intent it is measured against. So topos takes intent as an explicit input, and reports two
differences:

- **reachable but not intended:** a risk, or a valuable path that nobody named;
- **intended but not reachable:** a bug, or a gap in the model.

Each finding carries a concrete path through the model that reproduces it.

## One model, many readings

The same model answers different questions depending on how it is read:

| Reading | Question |
|---|---|
| Reachability (Boolean closure) | Can this ever happen? |
| Cheapest path (min-plus closure) | What is the cheapest way to make it happen? |
| Absorbing Markov chain | Of runs that start here, how many end well, and how long do they take? |
| Recurrent classes | Where can a run get stuck going round? |
| Coupling residual | Where does changing one thing quietly change another? |
| Homology (H₀, H₁) | Which parts never interact, and where are the independent loops? |

Reachability comes first. The other readings are added only when they produce findings.

## Status

Early. What exists:

- [`schema/`](schema/): JSON Schemas, version 0.2, for the documents topos exchanges. Components
  meet only at these documents.
  - The **system model**: entities, states, transitions, grants, and an access relation between
    situated actors, actions and objects.
  - The **declared intent**, and the **findings report** that compares a model with it. Every
    observed interaction is reported as intended, contrary to intent, or unqualified.
  - The **structural topology**: the whole-system dependency graph that clustering reads.
  - The **clustering**: clusters proposed from a topology by several algorithms, the edges that
    cross between them, and where the algorithms disagree.
  - The **area of concern**: the part of the system under analysis, and every edge that crosses
    its boundary.
- [`src/topos/`](src/topos/): the first components.
  - `topos.extract.postgres` reads a PostgreSQL database's applied schema into a structural
    topology.
  - `topos.cluster` clusters a topology.
  - `topos.area` derives an area of concern from a clustering, and verifies one against its
    inputs.
- [`examples/`](examples/): a small synthetic system written in those schemas.

## Crossing-edge classes

An area of concern lists every edge of the structural topology with exactly one end inside it.
Clustering lists the edges between its clusters the same way. Each such edge is classed from the
topology alone, by `topos.boundary`:

| Edge | Class |
|---|---|
| `writes` | state-carrying: a write into the other side |
| `reads` of a table | state-carrying if the table is written on its own side, or has no extracted writer (it is then written outside the extracted schema); reference-only if every extracted writer is on the reader's side |
| `reads` of a view | state-carrying: a view exposes the state of what it reads |
| `calls` | state-carrying unless the callee is immutable, whose answer depends only on its arguments |
| `trigger_on`, `executes` | state-carrying: a trigger fires on a write and passes the row on |
| `policy_on` | state-carrying: a policy guards the rows of its table |
| `foreign_key`, `typed_by` | reference-only: a key or a type names the other side without reading its state |

## Scope

Software comes first, and other rule systems later. Targets are systems owned by tasker-systems.
Analyses of real targets, and any statement of a defect in one, are kept out of this repository.
The tests and examples here use synthetic systems only.

## Conventions

This repository consumes the [tasker-systems conventions](https://github.com/tasker-systems/conventions)
baseline, pinned in [`.conventions/consumer.toml`](.conventions/consumer.toml).

## License

See [LICENSE](LICENSE).
