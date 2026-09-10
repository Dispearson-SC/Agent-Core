"""Driven adapter: SkillRegistry over SKILL.md files.

Phase:   F6
Tasks:   docs/TASKS.md#t-f6-02
Implements: ports/skill_registry.py

LAYOUT
    Core/skills/<name>/SKILL.md - YAML frontmatter + markdown body.

THE RULE THAT MAKES THIS WORTH BUILDING
    index() parses FRONTMATTER ONLY and never touches the body. Roughly one line per skill
    reaches the system prompt; the model calls `skill_view` when it wants more.

    Fifty skills: ~5K characters instead of ~200K. If a body ever reaches the prompt, the
    optimisation is silently undone and the only symptom is a larger bill.

CACHE
    index() runs every turn. Cache by (path, mtime). Re-reading fifty files per turn is
    real latency for information that changes weekly at most.

PATH TRAVERSAL - read() takes a name from the MODEL
    Resolve strictly through the index. NEVER join the name onto a directory. A model that
    has read a hostile web page will try `../../.env`, and it costs nothing to try.
"""
