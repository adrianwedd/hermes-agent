# Studio project layout

On Adrian's Studio, work in the established canonical checkout and existing
working branch. Preserve unrelated edits and serialize writers. Do not create
automatic worktrees, duplicate clones, scratch source copies, or project evidence
roots under the home directory or Documents/Codex.

The installed Hermes source remains this repository,
`/Users/adrian/.hermes/hermes-agent`, on its established Studio branch.
A project registered in Hermes must point at its normal project folder; project
metadata is not authority to create an isolated checkout. Local tasks without a
repository use the configured persistent `/Users/adrian/repos/kanban` directory.

Keep useful reports, recordings, benchmark results and evidence inside their
owning project. Publishable documentation belongs in the project's existing docs
layout; private/generated material belongs in ignored `var/` with an index.
Retain unfinished changes with their base revision, patch, original task status
and next action. Preserve immutable historical receipts; current path mappings
explain where their original bytes now live.

Hermes source and task reconciliation records are under
`var/consolidation-20261024/`, `var/unfinished/consolidation-20261024/` and
`var/retained-tasks/`. These are private local evidence, not a publication or
acceptance verdict. Do not restore old source snapshots over current source.

Before retiring a copy, check current remote history and patch equivalence,
preserve unique source and evidence, inspect active owners and open handles,
and update live workspace references. Dependency installs and reproducible test
estates can be removed only after the retained source and results are accounted
for. Keep established recovery backups and refs.
