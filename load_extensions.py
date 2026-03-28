"""
load_extensions.py
──────────────────
Discovers and loads extension packages from the `extensions/` directory.

Each extension is a Python package (a folder with __init__.py) that must
expose a `register(app)` function. That function receives the Flask app
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
import traceback
import logging

logger = logging.getLogger(__name__)

# Track loaded extensions for status reporting
_loaded_extensions: list = []


def load_extensions(app) -> list:
    """
    Scan extensions/ and call register(app) on every valid package found.
    The extensions/ directory is added to sys.path so that packages can do
    plain `import server` without needing relative imports.

    Returns:
        List of successfully loaded extension names.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    ext_dir = os.path.join(base_dir, "extensions")

    if not os.path.isdir(ext_dir):
        logger.info("[EXT] No extensions directory found.")
        return []

    # Ensure base_dir is on sys.path so extensions can import server
    if base_dir not in sys.path:
        sys.path.insert(0, base_dir)

    # Ensure extensions package is importable
    try:
        import extensions

        # Pre-load shared module to avoid circular import issues
        try:
            from extensions import shared

            logger.debug("[EXT] shared module loaded successfully")
        except ImportError as e:
            logger.warning(f"[EXT] Could not import extensions.shared: {e}")
    except ImportError as e:
        logger.error(f"[EXT] Could not import extensions package: {e}")
        return []

    loaded = []
    errors = []

    # Scan for extension subpackages
    for entry in sorted(os.scandir(ext_dir), key=lambda e: e.name):
        if not entry.is_dir():
            continue

        name = entry.name

        # Skip __pycache__ and other special directories
        if name.startswith("_") or name.startswith("."):
            continue

        init_py = os.path.join(entry.path, "__init__.py")

        if not os.path.isfile(init_py):
            logger.debug(f'[EXT] Skipping "{name}" — no __init__.py')
            continue

        module_name = f"extensions.{name}"

        try:
            # Import or reload the extension module
            if module_name in sys.modules:
                module = sys.modules[module_name]
            else:
                module = importlib.import_module(module_name)

            # Find the register function
            register_fn = getattr(module, "register", None)

            if callable(register_fn):
                # Call the extension's register function
                register_fn(app)
                loaded.append(name)
                logger.info(f"[EXT] Loaded extension: {name}")
            else:
                logger.warning(
                    f'[EXT] Extension "{name}" has no register() function — skipped'
                )

        except ImportError as e:
            error_msg = f"Import error: {e}"
            errors.append((name, error_msg))
            logger.error(f'[EXT] Failed to load extension "{name}": {error_msg}')
            traceback.print_exc()

        except Exception as e:
            error_msg = str(e)
            errors.append((name, error_msg))
            logger.error(f'[EXT] Failed to load extension "{name}": {error_msg}')
            traceback.print_exc()

    # Store loaded list for status reporting
    global _loaded_extensions
    _loaded_extensions = loaded

    # Print summary
    if loaded:
        print(f'[EXT] Extensions loaded: {", ".join(loaded)}')
    else:
        print("[EXT] No extensions found.")

    if errors:
        print(f"[EXT] Extension errors: {len(errors)} failed to load")

    return loaded


def get_loaded_extensions() -> list:
    """Return list of successfully loaded extension names."""
    return list(_loaded_extensions)


def reload_extension(app, name: str) -> bool:
    """
    Reload a single extension by name.
    Useful for development.

    Args:
        app: Flask application instance
        name: Extension name (e.g., 'face', 'instagram')

    Returns:
        True if reload successful, False otherwise
    """
    module_name = f"extensions.{name}"

    try:
        # Remove from sys.modules to force reload
        if module_name in sys.modules:
            del sys.modules[module_name]

        # Re-import
        module = importlib.import_module(module_name)
        register_fn = getattr(module, "register", None)

        if callable(register_fn):
            register_fn(app)
            print(f"[EXT] Reloaded extension: {name}")
            return True
        else:
            print(f'[EXT] Extension "{name}" has no register() function')
            return False

    except Exception as e:
        print(f'[EXT] Failed to reload extension "{name}": {e}')
        traceback.print_exc()
        return False
