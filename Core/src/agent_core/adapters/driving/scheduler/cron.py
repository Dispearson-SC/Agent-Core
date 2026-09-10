"""Driving adapter: scheduled turns.

Phase:   later - not required before D2
Tasks:   docs/TASKS.md#t-later-01

DBOS provides scheduled workflows with cron syntax, so this adapter is thin: resolve the
profile, mint a synthetic session, start the same workflow the HTTP adapter starts.

TWO THINGS TO GET RIGHT
    - A cron run needs a CallerIdentity too. Give it a dedicated service identity with its
      own policy rows. Reusing a human's identity means a scheduled job silently inherits
      that person's permissions.
    - Each run gets a FRESH session. Sharing one session across runs grows a conversation
      that nobody reads and that compaction then pays to summarise forever.
"""
