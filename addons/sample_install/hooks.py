"""The sample `post_init`: runs once for a new or upgraded addon, inside the install transaction."""

from __future__ import annotations


def post_init(context):  # type: ignore[no-untyped-def]
    """Record how the addon arrived. Safe to repeat: the row is keyed by its text."""
    arrived = context.previous_version or "first install"
    context.connection.execute(
        "INSERT INTO sample_install_notes (note) VALUES (%s) ON CONFLICT DO NOTHING",
        (f"post_init: {arrived} -> {context.version}",),
    )
