"""Composition root - the ONE place adapters are chosen and wired.

Phase:   F0 (minimal) / grows every phase
Tasks:   docs/TASKS.md#t-f0-02

WHY A SINGLE FILE
    Every `import` of a concrete adapter lives here and nowhere else. That is what makes
    the layer rule checkable by reading one file instead of grepping the tree.

    If a use case ever imports an adapter directly, the hexagon has a hole. The ruff
    banned-api config in pyproject.toml catches the common cases; this file catches the
    rest by being the only legitimate importer.

PSEUDO-CODE - F0, extended every phase
    def build_container(settings) -> Container:
        model    = LiteLLMGateway(settings)                  # F0
        tools    = CompositeToolProvider(local=..., mcp=...)  # F0 local, F6 mcp
        policy   = PgToolPolicy(pool)                         # F1
        store    = PgConversationStore(pool)                  # F1
        audit    = PgAuditSink(separate_pool)                 # F1 - SEPARATE POOL, see below
        context  = LadderContextEngine(aux_model)             # F5
        skills   = FsSkillRegistry(root)                      # F6
        media    = FsMediaStore(root)                         # F7
        human    = ChannelHumanGateway(...)                   # F3
        runner   = PydanticAgentRunner(model, policy, audit)  # F1
        profiles = load_profiles(Path("Core/profiles"))       # F1
        return Container(start_turn=StartTurn(...), ...)

    AuditSink gets its OWN connection pool on purpose. It must write outside the domain
    transaction so a rolled-back turn still leaves a trace. CLAUDE.md non-negotiable #6.
    Sharing the pool is the easy mistake and it silently erases the evidence of exactly
    the turns you most need to explain.
"""
