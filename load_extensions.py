"""
load_extensions.py
──────────────────
Discovers and loads extension packages from the `extensions/` directory.

Each extension is a Python package (a folder with __init__.py) that must
expose a `register(app)` function.  That function receives the Flask app
instance and is responsible for:

  • Registering new Flask routes  (e.g.  @app.route('/api/persons', ...))
  • Calling register_hook() from server.py to subscribe to lifecycle events
  • Any other one-time setup the extension needs

Extension lifecycle hooks available (import from server):
  register_hook('on_db_init',        fn(conn))
  register_hook('on_image_uploaded', fn(file_id, save_path, tags_raw, conn))

Usage (already called in server.py __main__):
    from load_extensions import load_extensions
    load_extensions(app)         # call BEFORE init_db()
"""

import os
import sys
import importlib


def load_extensions(app):
    """
    Scan extensions/ and call register(app) on every valid package found.
    The extensions/ directory is added to sys.path so that packages can do
    plain `import server` without needing relative imports.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    ext_dir = os.path.join(base_dir, "extensions")

    if not os.path.isdir(ext_dir):
        return  # no extensions folder → nothing to do

    # Make sure the project root is on sys.path so extensions can import server
    if base_dir not in sys.path:
        sys.path.insert(0, base_dir)

    loaded = []
    for name in sorted(os.listdir(ext_dir)):
        pkg_path = os.path.join(ext_dir, name)
        init_py = os.path.join(pkg_path, "__init__.py")

        if not os.path.isdir(pkg_path) or not os.path.isfile(init_py):
            continue  # skip files and dirs without __init__.py

        module_name = f"extensions.{name}"
        try:
            module = importlib.import_module(module_name)
            if hasattr(module, "register"):
                module.register(app)
                loaded.append(name)
                print(f"[EXT] ✅  Loaded extension: {name}")
            else:
                print(
                    f'[EXT] ⚠️   Extension "{name}" has no register() function — skipped'
                )
        except Exception as e:
            print(f'[EXT] ❌  Failed to load extension "{name}": {e}')

    if not loaded:
        print("[EXT] No extensions found.")
