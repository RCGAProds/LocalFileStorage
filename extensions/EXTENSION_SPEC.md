# LocalFileHub — Extension Developer Specification

> **Target audience:** AI agents and experienced developers building extensions.
> **Constraint:** Do NOT modify core files. Read this spec, ship a working extension.

---

## 1. Architecture Overview

```
LocalFileHub/
├── server.py              # Core Flask app, hook registry, frontend injection (READ-ONLY)
├── load_extensions.py     # Auto-discovery loader (READ-ONLY)
├── index.html             # Base frontend — single-page app (READ-ONLY)
├── Launch.py              # GUI launcher wrapper (READ-ONLY)
├── database.db            # SQLite (WAL mode, FK enabled)
├── uploads/               # File storage root (subfolders per folder entity)
└── extensions/
    ├── __init__.py        # Re-exports shared utilities (do not modify)
    ├── shared.py          # Optional: shared utilities across extensions
    └── <ext_name>/
        ├── __init__.py    # REQUIRED: must expose register(app)
        └── ...            # Any additional files
```

**Boot order:**

```
_bootstrap() [at server.py import time]
    └── load_extensions(app)   → calls register(app) on every extension
    └── init_db()              → creates core tables, then fires on_db_init hooks
Flask starts (threaded=True, port 5000)
```

Extensions are loaded **before** `init_db()`, so `on_db_init` hooks are registered
in time for Step 4 of `init_db()` to fire them.

---

## 2. Discovery & Registration

**Auto-discovery rules (load_extensions.py):**

- Must be a directory under `extensions/`
- Must contain `__init__.py`
- Must expose `register(app: Flask) -> None`
- Directories starting with `_` or `.` are skipped
- Loaded alphabetically by directory name

**Registration pattern:**

```python
# extensions/myext/__init__.py
from flask import jsonify, request
from server import register_hook, get_db, register_frontend_extension

def register(app):
    # 1. Register lifecycle hooks
    register_hook('on_db_init', _on_db_init)
    register_hook('on_image_uploaded', _on_image_uploaded)

    # 2. Add API routes
    @app.route('/api/myext/data')
    def myext_data():
        ...

    # 3. Register frontend UI fragment (optional)
    register_frontend_extension({...})
```

**Failure behaviour:** Exception in `register()` → extension skipped, traceback printed,
server continues. All other extensions are unaffected.

**`server` module aliasing:** `_bootstrap()` calls
`sys.modules.setdefault('server', sys.modules[__name__])` before loading extensions,
so extensions doing `from server import ...` always get the single real module
regardless of launch mode.

---

## 3. Imports Pattern

Import `server` symbols **lazily** (inside functions), not at module level, to avoid
circular import issues at load time:

```python
# ✅ Correct — lazy import inside function body
def _on_image_uploaded(file_id, save_path, tags_raw, conn):
    from server import get_db, file_disk_path, IMAGE_MIMETYPES
    ...

# ❌ Wrong — top-level import may cause circular import
from server import get_db
```

`extensions.shared` is pre-loaded by `load_extensions.py` before extensions run,
so it is safe to import at module level:

```python
from extensions.shared import my_shared_utility
```

---

## 4. Hook System

### Registration

```python
from server import register_hook

register_hook('on_db_init',        my_db_init_fn)
register_hook('on_image_uploaded', my_upload_fn)
```

All hook registrations **must happen inside `register(app)`**, not at module level.

### Available Hooks

#### `on_db_init(conn: sqlite3.Connection)`

Fired inside `init_db()` after all core tables are created. Use it to CREATE extension
tables or ALTER existing ones.

**Rules:**

- Use `conn.execute()` per statement — never `conn.executescript()` (causes implicit COMMIT)
- Do NOT call `conn.commit()` — the caller commits after all hooks run
- Do NOT call `conn.close()`
- Safe to use `ALTER TABLE ADD COLUMN` with existence checks via `PRAGMA table_info`

```python
def _on_db_init(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS myext_items (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
            data    TEXT,
            created TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Safe column migration
    cols = {r[1] for r in conn.execute("PRAGMA table_info(myext_items)").fetchall()}
    if 'extra_col' not in cols:
        conn.execute("ALTER TABLE myext_items ADD COLUMN extra_col TEXT")
```

#### `on_image_uploaded(file_id: int, save_path: str, tags_raw: str, conn: sqlite3.Connection)`

Fired in `upload_file()` **after** the file row is committed. Only fires for image
mimetypes (`IMAGE_MIMETYPES` set in server.py).

**Parameters:**

- `file_id` — the new row's PK in `files`
- `save_path` — absolute path to the saved file on disk
- `tags_raw` — raw comma-separated tag string from the upload form (unparsed)
- `conn` — an open, already-committed read connection

**Rules:**

- ✅ Read freely from the provided `conn`
- ✅ Open a **new** `get_db()` connection for any writes
- ❌ Do NOT write through the provided `conn` (already committed)
- ❌ Do NOT block — runs synchronously in the request thread
- ✅ Spawn a thread for heavy work (encoding, network calls, etc.)

```python
def _on_image_uploaded(file_id, save_path, tags_raw, conn):
    import threading
    from server import get_db
    def _worker(fid, path):
        own_conn = get_db()
        try:
            own_conn.execute(
                'INSERT INTO myext_items (file_id, data) VALUES (?, ?)',
                (fid, 'processed')
            )
            own_conn.commit()
        finally:
            own_conn.close()
    threading.Thread(target=_worker, args=(file_id, save_path), daemon=True).start()
```

---

## 5. Database API

```python
from server import get_db

conn = get_db()
rows = conn.execute('SELECT ...', params).fetchall()
conn.commit()   # if you wrote anything
conn.close()
```

`get_db()` returns a connection configured with:

- `row_factory = sqlite3.Row` (access columns by name: `row['id']`)
- `PRAGMA foreign_keys = ON`
- `PRAGMA journal_mode = WAL`
- `PRAGMA synchronous = NORMAL`
- `PRAGMA cache_size = -8000`
- `PRAGMA temp_store = MEMORY`
- `PRAGMA mmap_size = 134217728`

**Thread safety:** Flask runs `threaded=True`. Open a new `get_db()` per request/thread;
never share connections across threads.

### Core Schema (read-only reference)

```sql
files(
    id            INTEGER PK,
    filename      TEXT,          -- stored UUID-like name on disk
    original_name TEXT,          -- original upload name
    folder_id     INTEGER,       -- FK → folders(id) ON DELETE SET NULL
    size          INTEGER,       -- bytes
    mimetype      TEXT,
    uploaded_at   TEXT,
    sha256        TEXT,          -- exact duplicate detection
    phash         TEXT,          -- perceptual hash (imagehash, may be NULL)
)

folders(id, name, created_at)

tags(id, name UNIQUE)

file_tags(file_id, tag_id)      -- M2M; both FK with ON DELETE CASCADE

rules(id, position, enabled, condition, action, created_at)
```

### Extension Table Conventions

```sql
-- Always prefix table names with your extension slug
CREATE TABLE IF NOT EXISTS myext_data (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
    ...
);
```

### Useful server helpers importable by extensions

```python
from server import (
    get_db,                  # open a configured SQLite connection
    file_disk_path,          # (stored_name, folder_name=None) → absolute path
    folder_disk_path,        # (folder_name) → absolute dir path
    get_thumbnail_bytes,     # (src_path, stored_name) → WebP bytes | None
    get_file_tags,           # (conn, file_id) → [tag_name, ...]
    get_tags_for_ids,        # (conn, [file_id, ...]) → {file_id: [tag, ...]}
    file_to_dict,            # (row, tags=None) → dict  (standard file payload)
    compute_sha256,          # (path) → hex str
    compute_phash,           # (path) → hex str | None  (requires imagehash)
    PIL_AVAILABLE,           # bool
    IMAGEHASH_AVAILABLE,     # bool
    IMAGE_MIMETYPES,         # set of image mimetype strings
    VIDEO_MIMETYPES,         # set of video mimetype strings
    UPLOAD_FOLDER,           # absolute path to uploads/
    BASE_DIR,                # absolute path to project root
)
```

---

## 6. HTTP Routes

Register routes inside `register(app)`:

```python
def register(app):
    @app.route('/api/myext/items', methods=['GET'])
    def myext_list():
        conn = get_db()
        rows = conn.execute('SELECT * FROM myext_items ORDER BY created DESC').fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])

    @app.route('/api/myext/items', methods=['POST'])
    def myext_create():
        data = request.json or {}
        conn = get_db()
        conn.execute('INSERT INTO myext_items (file_id, data) VALUES (?,?)',
                     (data['file_id'], data.get('data', '')))
        conn.commit()
        conn.close()
        return jsonify({'ok': True}), 201
```

**Namespace rule:** Always prefix routes with `/api/<extname>/` to avoid collisions
with core routes and other extensions.

### Core API routes (already registered — do not redeclare)

| Method | Path                         | Description                                                                     |
| ------ | ---------------------------- | ------------------------------------------------------------------------------- |
| GET    | `/api/files`                 | List files (supports `q`, `folder_id`, `tag`, `sort`, `dir`, `limit`, `offset`) |
| POST   | `/api/files/upload`          | Upload a file (multipart: `file`, `folder_id`, `tags`)                          |
| GET    | `/api/files/<id>`            | Single file metadata                                                            |
| PUT    | `/api/files/<id>`            | Update file (`folder_id`, `tags`, `original_name`)                              |
| DELETE | `/api/files/<id>`            | Delete file                                                                     |
| PUT    | `/api/files/batch`           | Batch update (`ids`, `folder_id`, `add_tags`, `remove_tags`)                    |
| DELETE | `/api/files/batch`           | Batch delete (`ids`)                                                            |
| GET    | `/api/files/<id>/preview`    | Serve thumbnail (WebP) or original; `?full=1` bypasses thumb                    |
| GET    | `/api/files/<id>/download`   | Download original with `Content-Disposition: attachment`                        |
| GET    | `/api/folders`               | List folders                                                                    |
| POST   | `/api/folders`               | Create folder                                                                   |
| PUT    | `/api/folders/<id>`          | Rename folder                                                                   |
| DELETE | `/api/folders/<id>`          | Delete folder                                                                   |
| GET    | `/api/folders/<id>/stats`    | `{total, size}` for a folder                                                    |
| GET    | `/api/folders/<id>/download` | Download folder as ZIP                                                          |
| GET    | `/api/tags`                  | All tags with usage counts                                                      |
| GET    | `/api/duplicates`            | Duplicate groups (`?type=exact\|similar`)                                       |
| GET    | `/api/extensions/frontend`   | List of registered frontend extension configs                                   |
| GET    | `/api/thumbnails/cache-info` | LRU cache stats                                                                 |
| DELETE | `/api/thumbnails/cache`      | Flush thumbnail cache                                                           |

---

## 7. Frontend Extension System

The SPA (`index.html`) calls `GET /api/extensions/frontend` at startup and dynamically
injects each registered extension's UI fragments. Extensions declare their UI by
calling `register_frontend_extension(config)` inside `register(app)`.

### Registration

```python
from server import register_frontend_extension

register_frontend_extension({
    'id':             'myext',           # REQUIRED — unique slug; used as page id
    'tab_icon':       '🔧',             # emoji/text for the tab button
    'tab_label':      'My Extension',   # human-readable tab label
    'page_html':      '...',            # inner HTML for <div class="page" id="page-myext">
    'overlay_html':   '...',            # (optional) HTML injected before </body>
    'card_actions':   '...',            # (optional) HTML injected in every image card's .file-actions
    'edit_modal_btn': '...',            # (optional) HTML appended inside the Edit-file modal
    'css':            '...',            # CSS rules injected into <head> (no <style> wrapper)
    'js':             '...',            # JS injected at end of <body> (no <script> tags)
})
```

**`id` is always required.** If both `tab_icon` and `tab_label` are empty strings,
no tab is created — useful for extensions that only inject overlays, card buttons,
or edit-modal buttons.

### Config fields reference

| Key              | Type | Required | Description                                                                                                                                                                                                        |
| ---------------- | ---- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `id`             | str  | ✅       | Unique slug. Becomes the DOM id `page-{id}` and the `switchTab()` argument.                                                                                                                                        |
| `tab_icon`       | str  | —        | Emoji or text shown in the tab button.                                                                                                                                                                             |
| `tab_label`      | str  | —        | Human-readable label.                                                                                                                                                                                              |
| `page_html`      | str  | —        | Complete inner HTML for the extension's page `<div>`.                                                                                                                                                              |
| `overlay_html`   | str  | —        | Modals, lightboxes, or any top-level HTML injected before `</body>`.                                                                                                                                               |
| `card_actions`   | str  | —        | HTML injected into every image file-card's `.file-actions` div. Can reference JS functions defined in the extension's `js` block. Template literals `${f.id}`, `${f.original_name}` etc. are evaluated by the SPA. |
| `edit_modal_btn` | str  | —        | HTML appended inside the edit-file modal after the Save button.                                                                                                                                                    |
| `css`            | str  | —        | Raw CSS (no wrapper tag).                                                                                                                                                                                          |
| `js`             | str  | —        | Raw JavaScript (no wrapper tag).                                                                                                                                                                                   |

### Frontend constraints

- Use relative URLs (`/api/myext/...`) — never hardcode `localhost:5000`
- Do not assume base `index.html` DOM structure beyond the documented injection points
- Do not inject `<script>` or `<style>` tags — the SPA wraps `js` and `css` fields itself
- The SPA calls `switchTab(id)` to show a page; your `js` can call this function

---

## 8. Static Files

```python
import os
from flask import send_from_directory

_EXT_DIR = os.path.dirname(os.path.abspath(__file__))

def register(app):
    @app.route('/ext/myext/<path:filename>')
    def myext_static(filename):
        return send_from_directory(os.path.join(_EXT_DIR, 'static'), filename)
```

Reference from frontend: `<img src="/ext/myext/logo.png">`

---

## 9. Shared Utilities (`extensions/shared.py`)

`shared.py` is the place for utilities that multiple extensions need. It is
pre-loaded by `load_extensions.py` before any extension runs, making it safe
to import at module level (unlike `server` symbols, which must be imported lazily):

```python
# Safe at module level — shared.py is pre-loaded
from extensions.shared import my_shared_utility
```

**Contract: `shared.py` must remain dependency-free.** No Flask, no `server`
imports, no third-party packages. It must be importable before the Flask app
is fully initialised.

If you are adding a new extension that needs a utility that another extension
already implements, move it to `shared.py` instead of duplicating it. This
file is the single source of truth for cross-extension logic.

---

## 10. Minimal Valid Extension

```python
# extensions/myext/__init__.py

def _on_db_init(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS myext_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id   INTEGER REFERENCES files(id) ON DELETE CASCADE,
            logged_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')


def _on_image_uploaded(file_id, save_path, tags_raw, conn):
    from server import get_db
    own = get_db()
    try:
        own.execute('INSERT INTO myext_log (file_id) VALUES (?)', (file_id,))
        own.commit()
    finally:
        own.close()


def register(app):
    from flask import jsonify
    from server import register_hook, get_db

    register_hook('on_db_init', _on_db_init)
    register_hook('on_image_uploaded', _on_image_uploaded)

    @app.route('/api/myext/log')
    def myext_log():
        conn = get_db()
        rows = conn.execute(
            'SELECT ml.*, f.original_name '
            'FROM myext_log ml JOIN files f ON f.id = ml.file_id '
            'ORDER BY ml.logged_at DESC LIMIT 50'
        ).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
```

---

## 11. Extension with Frontend Tab

```python
# extensions/myext/__init__.py

_PAGE_HTML = '''
<div style="padding:1rem">
  <h2>My Extension</h2>
  <div id="myext-content">Loading...</div>
</div>
'''

_JS = '''
async function myextLoad() {
  const res = await fetch('/api/myext/log');
  const data = await res.json();
  document.getElementById('myext-content').textContent = JSON.stringify(data);
}
document.addEventListener('DOMContentLoaded', myextLoad);
'''

_CSS = '''
#page-myext { background: var(--bg); color: var(--text); }
'''

def register(app):
    from server import register_hook, get_db, register_frontend_extension

    register_hook('on_db_init', _on_db_init)

    @app.route('/api/myext/log')
    def myext_log():
        ...

    register_frontend_extension({
        'id':         'myext',
        'tab_icon':   '🔧',
        'tab_label':  'MyExt',
        'page_html':  _PAGE_HTML,
        'css':        _CSS,
        'js':         _JS,
    })
```

---

## 12. Environment & Constraints

| Constraint             | Detail                                                   |
| ---------------------- | -------------------------------------------------------- |
| Python                 | 3.8+                                                     |
| Flask                  | 3.0+                                                     |
| SQLite                 | WAL mode, FK enforced — always use `ON DELETE CASCADE`   |
| Threading              | `threaded=True`; one `get_db()` per thread, never shared |
| Max upload             | 500 MB per file                                          |
| File paths             | Always `os.path.join()` — never hardcode separators      |
| Side effects at import | None — all setup must be inside `register()`             |
| Inter-extension comms  | Not supported directly — use DB as shared state          |
| Optional deps          | Wrap in `try/except ImportError`; degrade gracefully     |

### Optional dependencies available in the runtime environment

```python
# PIL / Pillow (thumbnails, image processing)
from server import PIL_AVAILABLE   # check before use

# imagehash (perceptual hashing)
from server import IMAGEHASH_AVAILABLE

# flask_limiter (rate limiting — may fall back to built-in)
from server import LIMITER_AVAILABLE
```

---

## 13. Anti-Patterns

| ❌ Don't                                                 | ✅ Do instead                                                         |
| -------------------------------------------------------- | --------------------------------------------------------------------- |
| `import server; server._hooks = ...`                     | Use `register_hook()`                                                 |
| Modify `server.py`, `index.html`, `load_extensions.py`   | Add routes/hooks in `register()`                                      |
| `conn.executescript(...)` in `on_db_init`                | `conn.execute(...)` per statement                                     |
| Write via the `conn` provided in `on_image_uploaded`     | Open a new `get_db()` connection                                      |
| Block in hooks (network, heavy compute)                  | Spawn a daemon thread                                                 |
| Generic table names (`data`, `log`, `users`)             | Prefix: `myext_data`, `myext_log`                                     |
| `from server import ...` at module level                 | Import lazily inside functions                                        |
| `conn.close()` inside `on_db_init`                       | Never close the provided conn                                         |
| Hardcode `localhost:5000` in frontend                    | Use relative URLs (`/api/...`)                                        |
| `conn.executescript()` in `on_db_init`                   | `conn.execute()` per statement — executescript causes implicit COMMIT |
| Share a `get_db()` connection across threads             | One connection per thread/request                                     |
| `<style>` or `<script>` tags in `css`/`js` config fields | Raw CSS/JS only — the SPA wraps them                                  |

---

## 14. Checklist

```
□ extensions/<name>/__init__.py exists
□ register(app) function defined and is the only entry point
□ Hook registrations inside register(), not at module level
□ server symbols imported lazily (inside functions), not at module top
□ Own DB tables prefixed with extension slug
□ API routes prefixed with /api/<extname>/
□ on_db_init uses conn.execute() only — no executescript(), no commit(), no close()
□ on_image_uploaded opens its own get_db() for any writes
□ Blocking operations in hooks run in daemon threads
□ register_frontend_extension called with 'id' field present
□ css/js config fields contain raw code — no wrapper tags
□ Frontend code uses relative URLs only
□ Optional dependencies wrapped in try/except ImportError
□ No modifications to core files
```
