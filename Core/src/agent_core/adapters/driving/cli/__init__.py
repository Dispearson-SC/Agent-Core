"""Driving adapters that drive the core from a terminal.

`console.py` is the operator REPL (docs/TASKS.md#t-f0-07). It sits beside `http/`,
`channels/`, `workflow/` and `scheduler/` because it is the same kind of thing: something
outside the hexagon calling a use case. It imports no concrete adapter - `main.py` fills
its seats from the one container `composition.py` built.
"""
