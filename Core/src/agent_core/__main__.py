"""`python -m agent_core` - the shortest way to start the process.

Phase:   F0
Tasks:   docs/TASKS.md#t-f0-05
Status:  DONE - three lines, and it must stay that way

Deliberately nothing but a delegation. `python -m agent_core` requires a `__main__`
submodule; every decision about what the process IS belongs to `main.py`, which belongs in
turn to `composition.py`. Logic here would be a third place that knows how the system
starts.

It delegates to `main()`, not to `serve()`. `main()` is the subcommand dispatch, so
`python -m agent_core console` reaches the operator REPL and `python -m agent_core`
still serves. Importing `serve` directly - which this file did - meant the dispatch
existed and the shortest way in could never reach it: the console was only addressable
as `python -m agent_core.main console`. A subcommand nobody can type is not a
subcommand. Parsing the command line HERE would be the third place; calling the one
function that already does it is not.
"""

from __future__ import annotations

from agent_core.main import main

main()
