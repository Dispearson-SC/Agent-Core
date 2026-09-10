"""Port: SkillRegistry - which skills exist, and what is this one's body?

Phase:      F6
Tasks:      docs/TASKS.md#t-f6-01
Adapter:    adapters/driven/skills_fs/
Per-vertical: NO (the CONTENT changes, the code does not)

THE ENTIRE IDEA IN ONE RULE
    Only the INDEX goes into the system prompt - name, one-line description, path. The
    model reads the full body with a tool when it decides it needs it.

    OpenClaw injects roughly one hundred characters per skill. With fifty skills that is
    about five thousand characters instead of two hundred thousand. Best
    effort-to-benefit ratio of any token optimisation in this project.

    If you ever find the full SKILL.md body in the system prompt, the optimisation has
    been silently undone and the only symptom is a larger bill.

FILE SHAPE
    Core/skills/<name>/SKILL.md, YAML frontmatter plus a markdown body:

        ---
        name: delivery-zone-rules
        description: Zone surcharges and cutoff times for delivery pricing.
        namespaces: [delivery]
        requires:
          env: [ZONE_API_URL]
        ---
        # ... body the model reads on demand ...
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from agent_core.domain.profile import AgentProfile


@dataclass(frozen=True, slots=True)
class SkillMeta:
    """What goes into the prompt. Nothing else.

    Keep `description` to one line. It is repeated for every skill in every request of
    every turn, so a three-line description is a three-fold cost multiplier on the one
    thing this design exists to keep small."""

    name: str
    description: str
    path: str


class SkillRegistry(Protocol):
    def index(self, profile: AgentProfile) -> tuple[SkillMeta, ...]:
        """PSEUDO-CODE - F6.

        1. Scan the skills root for SKILL.md files.
        2. Parse frontmatter only - NEVER read the body here.
        3. Filter by `profile.skill_namespaces`. The fraud agent must not receive the
           delivery agent's index.
        4. Filter by `requires` - missing binary, missing env var, wrong platform. A skill
           whose preconditions fail is not advertised: offering the model a skill it
           cannot use wastes tokens and produces confident wrong answers.
        5. Return metadata only.

        MUST be cheap and cached. This runs on every turn.
        """
        ...

    def read(self, name: str) -> str:
        """PSEUDO-CODE - F6. Backs the `skill_view` tool.

        1. Resolve `name` against the index. Unknown -> raise; do not fall through to a
           filesystem path.
        2. Return the body WITHOUT the frontmatter.

        PATH TRAVERSAL: `name` comes from the MODEL, which may have read untrusted text.
        Resolve strictly through the index, never by joining `name` onto a directory. A
        model that has read a hostile web page will happily try `../../.env`.

        UNTRUSTED CONTENT: a skill body is trusted (we wrote it). A skill body that
        INTERPOLATES fetched data is not. Keep skills static; if one ever needs live data,
        it calls a tool for it, and that tool's result gets the untrusted wrapper.
        """
        ...
