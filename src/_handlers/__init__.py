"""Internal handler-helper package for handlers.py (P3-G split).

handlers.py historically wrapped a 1900-LOC ``_register_process_model_
handlers`` function with deeply nested closures. P3-G keeps the
closure structure (closures still capture router/supervisor/etc. from
the outer scope) but extracts the LARGE bodies into helper functions
here. Each closure in handlers.py becomes a thin wrapper that calls
into the corresponding module here, passing the captured deps as
explicit args.

This split:
- Drops handlers.py from 1878 → ~1100 LOC.
- Keeps the registrar's scope-capture pattern intact (no behavioral
  change at the IPC boundary).
- Each helper module is independently testable.
- The plan's named modules (tasks, mcp, office_lifecycle,
  setup, requests) live here under leading-underscore names so
  importers see ``src._handlers._mcp`` rather than the more
  ambiguous ``src.handlers`` namespace.
"""
