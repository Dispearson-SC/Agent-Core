"""Vertical: fraud analyst tools.

Phase:   after F4 - the second vertical is the REAL proof the core did not change
Tasks:   docs/TASKS.md#t-later-02
Per-vertical: YES

TOOLS
    sql_readonly(query)                  read-only, against a REPLICA with a read-only role
    account_history(account_id)          read-only
    case_notes_append(case_id, note)     mutating, low risk
    freeze_account(account_id)           MUTATING, HIGH RISK - always requires approval

sql_readonly IS THE MOST DANGEROUS TOOL IN THE PROJECT
    The model writes the query. Three layers, all of them required:
      1. A database role that physically cannot write. Not a convention - a GRANT.
      2. A statement timeout and a row cap. An unbounded query from a model is a denial of
         service against your own warehouse.
      3. The result passes through the untrusted-content wrapper: rows contain user-supplied
         text, which is an indirect prompt-injection vector straight from your own database.

    Point 3 is the one people miss. Data in your own warehouse is not trusted input just
    because it is yours - a customer typed it.

AUDIT IS EVIDENCE HERE, NOT TELEMETRY
    Retention policy, and possibly a legal reader. Confirm the retention requirement before
    this vertical goes anywhere near production data.
"""
