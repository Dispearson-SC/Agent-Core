"""Vertical: Glazed store copilot - shared HTTP client and every tool function.

The vertical is split into one toolset package per agent role (`glazed_orchestrator`,
`glazed_present`, ...) so a profile selects exactly one package (the contract test) and
each agent holds only its own tools. This package holds what they share; see
docs/VERTICAL_GLAZED.md.
"""
