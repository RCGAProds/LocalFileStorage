"""
extensions/shared.py
────────────────────
Shared utilities used by multiple extensions.

Place here any logic that more than one extension needs, to avoid duplication.
Extensions can import from this module at module level — it is pre-loaded by
load_extensions.py before any extension's register() is called.

Importing:
    from extensions.shared import my_utility

IMPORTANT: Do NOT import server or Flask from here. This module must remain
dependency-free so it can be loaded early, before the Flask app is initialised.
"""
