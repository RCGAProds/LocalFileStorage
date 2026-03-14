import os
import io
import sqlite3
import shutil
import hashlib
import time
import threading
from collections import defaultdict, OrderedDict
from flask import Flask, request, jsonify, send_from_directory, send_file
from werkzeug.utils import secure_filename
from datetime import datetime

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB max upload

# ── Optional dependencies ─────────────────────────────────────────────────────
try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import imagehash
    IMAGEHASH_AVAILABLE = True
except ImportError:
    IMAGEHASH_AVAILABLE = False

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    limiter = Limiter(key_func=get_remote_address, app=app,
                      default_limits=[], storage_uri="memory://")
    LIMITER_AVAILABLE = True
except ImportError:
    LIMITER_AVAILABLE = False
    # Dummy decorator so routes don't crash when flask_limiter is absent
    class _DummyLimiter:
        def limit(self, *a, **kw):
            def decorator(f): return f
            return decorator
    limiter = _DummyLimiter()

@app.errorhandler(413)
def too_large(e):
    return jsonify({'error': 'File too large (500 MB limit per upload)'}), 413

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
DB_PATH       = os.path.join(BASE_DIR, 'database.db')

THUMB_SIZE    = (320, 320)   # max thumbnail dimensions
THUMB_QUALITY = 80           # WebP quality (0–100)

# ── Thumbnail LRU cache config ────────────────────────────────────────────────
# Each entry is raw WebP bytes in RAM. 200 entries × ~30 KB avg ≈ ~6 MB max.
# Tune THUMB_CACHE_MAX_ENTRIES to taste (e.g. 500 for a beefier server).
THUMB_CACHE_MAX_ENTRIES = 200

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

IMAGE_MIMETYPES = {
    'image/jpeg', 'image/png', 'image/gif',
    'image/webp', 'image/bmp', 'image/tiff',
}

# ─── Disk helpers ─────────────────────────────────────────────────────────────

def safe_folder_name(name):
    s = secure_filename(name)
    return s if s else 'folder'

def folder_disk_path(folder_name):
    return os.path.join(UPLOAD_FOLDER, safe_folder_name(folder_name))

def file_disk_path(stored_name, folder_name=None):
    if folder_name:
        return os.path.join(folder_disk_path(folder_name), stored_name)
    return os.path.join(UPLOAD_FOLDER, stored_name)

def ensure_folder_on_disk(folder_name):
    os.makedirs(folder_disk_path(folder_name), exist_ok=True)

def move_file_on_disk(stored_name, old_folder_name, new_folder_name):
    src = file_disk_path(stored_name, old_folder_name)
    dst = file_disk_path(stored_name, new_folder_name)
    if os.path.exists(src) and src != dst:
        if new_folder_name:
            ensure_folder_on_disk(new_folder_name)
        shutil.move(src, dst)

def rename_folder_on_disk(old_name, new_name):
    old_path = folder_disk_path(old_name)
    new_path = folder_disk_path(new_name)
    if os.path.exists(old_path) and old_path != new_path:
        shutil.move(old_path, new_path)

def delete_folder_on_disk(folder_name):
    path = folder_disk_path(folder_name)
    if os.path.isdir(path):
        for f in os.listdir(path):
            src = os.path.join(path, f)
            if os.path.isfile(src):
                shutil.move(src, os.path.join(UPLOAD_FOLDER, f))
        try:
            os.rmdir(path)
        except OSError:
            pass

# ─── Thumbnail LRU in-memory cache ───────────────────────────────────────────
#
# Architecture:
#   • Thumbnails are generated on first request (lazy) and stored as raw bytes
#     in an OrderedDict that acts as an LRU cache — no files written to disk.
#   • When the cache reaches THUMB_CACHE_MAX_ENTRIES the least-recently-used
#     entry is evicted automatically, bounding memory usage.
#   • Thread-safe via a single RLock (Flask runs with threaded=True).
#   • delete_thumbnail() simply evicts the key from the dict — O(1).
#   • The old `thumbnails/` directory is no longer created or used.

class _LRUThumbnailCache:
    """
    Thread-safe, size-bounded LRU cache for WebP thumbnail bytes.
    Key   : stored_name (the UUID filename string)
    Value : bytes  (raw WebP image data)
    """
    def __init__(self, max_entries: int):
        self._max  = max_entries
        self._data = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key):
        with self._lock:
            if key not in self._data:
                return None
            self._data.move_to_end(key)
            return self._data[key]

    def put(self, key, value):
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = value
            if len(self._data) > self._max:
                evicted_key, _ = self._data.popitem(last=False)
                print(f'[THUMB] LRU evicted: {evicted_key}')

    def evict(self, key):
        with self._lock:
            self._data.pop(key, None)

    def __len__(self):
        with self._lock:
            return len(self._data)

    @property
    def info(self):
        with self._lock:
            total_bytes = sum(len(v) for v in self._data.values())
            return {
                'entries':    len(self._data),
                'max_entries': self._max,
                'memory_mb':  round(total_bytes / 1_048_576, 2),
            }


_thumb_cache = _LRUThumbnailCache(THUMB_CACHE_MAX_ENTRIES)


def get_thumbnail_bytes(src_path, stored_name):
    """
    Return WebP thumbnail bytes for an image, generating and caching on demand.
    • First call   → open original, resize, encode to bytes, store in LRU cache.
    • Repeat calls → return bytes directly from RAM (no disk I/O, no PIL).
    • On eviction  → next request regenerates transparently.
    Requires Pillow; returns None if unavailable or on error.
    """
    if not PIL_AVAILABLE:
        return None

    cached = _thumb_cache.get(stored_name)
    if cached is not None:
        return cached

    try:
        with Image.open(src_path) as img:
            img = img.convert('RGB')
            img.thumbnail(THUMB_SIZE, Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format='WEBP', quality=THUMB_QUALITY)
            data = buf.getvalue()
        _thumb_cache.put(stored_name, data)
        return data
    except Exception as e:
        print(f'[WARN] Could not generate thumbnail for {src_path}: {e}')
        return None


def delete_thumbnail(stored_name):
    """Evict a thumbnail from the in-memory cache (called on file deletion)."""
    _thumb_cache.evict(stored_name)

# ─── Perceptual hash helpers ──────────────────────────────────────────────────

def compute_phash(src_path):
    """
    Compute a perceptual hash (pHash) for an image file.
    Returns the hex string of the hash, or None if unavailable.
    Requires Pillow + imagehash.
    """
    if not PIL_AVAILABLE or not IMAGEHASH_AVAILABLE:
        return None
    try:
        with Image.open(src_path) as img:
            return str(imagehash.phash(img))
    except Exception as e:
        print(f'[WARN] Could not compute phash for {src_path}: {e}')
        return None

def phash_distance(h1, h2):
    """
    Hamming distance between two hex pHash strings.
    Returns an integer 0–64 (0 = identical, ≤10 = visually similar).
    Returns None if either hash is invalid.
    """
    if not h1 or not h2:
        return None
    try:
        return bin(int(h1, 16) ^ int(h2, 16)).count('1')
    except ValueError:
        return None

# ─── SHA-256 helper (exact duplicate detection) ──────────────────────────────

def compute_sha256(path, chunk=65536):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()

# ─── Simple in-memory rate limiter (fallback when flask_limiter absent) ───────
# Only used for the upload endpoint.

_upload_calls = defaultdict(list)
UPLOAD_RATE_LIMIT = 30   # max calls
UPLOAD_RATE_WINDOW = 60  # seconds

def _check_upload_rate(ip):
    """Returns True if the request is within limits, False if it should be blocked."""
    if LIMITER_AVAILABLE:
        return True  # flask_limiter handles it via decorator
    now = time.time()
    calls = [t for t in _upload_calls[ip] if now - t < UPLOAD_RATE_WINDOW]
    calls.append(now)
    _upload_calls[ip] = calls
    return len(calls) <= UPLOAD_RATE_LIMIT

# ─── Database ─────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Correctness
    conn.execute("PRAGMA foreign_keys = ON")
    # ── Performance PRAGMAs ───────────────────────────────────────────────────
    # WAL mode: concurrent reads + writes without locking the entire DB.
    # Eliminates "database is locked" errors during simultaneous uploads/browsing.
    conn.execute("PRAGMA journal_mode = WAL")
    # NORMAL: fsync only at checkpoints — safe against OS crashes, faster than FULL.
    conn.execute("PRAGMA synchronous = NORMAL")
    # 8 MB page cache kept in RAM (negative value = kibibytes).
    conn.execute("PRAGMA cache_size = -8000")
    # Temporary tables and indices stored in RAM instead of a temp file.
    conn.execute("PRAGMA temp_store = MEMORY")
    # 128 MB memory-mapped I/O — sequential scans bypass read() syscalls.
    conn.execute("PRAGMA mmap_size = 134217728")
    return conn

def init_db():
    conn = get_db()

    # ── Step 1: create tables (never touches existing data) ───────────────────
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS folders (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL UNIQUE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS files (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            filename      TEXT NOT NULL,
            original_name TEXT NOT NULL,
            folder_id     INTEGER,
            size          INTEGER,
            mimetype      TEXT,
            uploaded_at   TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (folder_id) REFERENCES folders(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS tags (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS file_tags (
            file_id INTEGER,
            tag_id  INTEGER,
            PRIMARY KEY (file_id, tag_id),
            FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
            FOREIGN KEY (tag_id)  REFERENCES tags(id)  ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS rules (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            position   INTEGER NOT NULL DEFAULT 0,
            enabled    INTEGER NOT NULL DEFAULT 1,
            condition  TEXT NOT NULL,
            action     TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        -- Indices that are safe on the base schema (no sha256/phash yet).
        CREATE INDEX IF NOT EXISTS idx_files_folder
            ON files(folder_id);
        CREATE INDEX IF NOT EXISTS idx_files_uploaded
            ON files(uploaded_at DESC);
        CREATE INDEX IF NOT EXISTS idx_files_name
            ON files(original_name);
        CREATE INDEX IF NOT EXISTS idx_file_tags_file
            ON file_tags(file_id);
    ''')

    # ── Step 2: migrate columns added after the initial release ───────────────
    # Must happen BEFORE creating indices that reference these columns,
    # because SQLite will error if the column doesn't exist yet.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(files)").fetchall()}
    if 'sha256' not in existing_cols:
        conn.execute("ALTER TABLE files ADD COLUMN sha256 TEXT")
    if 'phash' not in existing_cols:
        conn.execute("ALTER TABLE files ADD COLUMN phash TEXT")
    conn.commit()

    # ── Step 3: indices that depend on the migrated columns ───────────────────
    conn.executescript('''
        CREATE INDEX IF NOT EXISTS idx_files_sha256
            ON files(sha256);
        CREATE INDEX IF NOT EXISTS idx_files_phash
            ON files(phash);
    ''')

    conn.commit()
    conn.close()

# ── Batch tag loader — resolves N+1 query problem ────────────────────────────
# Instead of one query per file, fetch all tags for a list of file IDs in one
# query and group them in Python.

def get_tags_for_ids(conn, file_ids):
    """
    Return a dict {file_id: [tag_name, ...]} for all given file IDs.
    Uses a single SQL query regardless of how many IDs are passed.
    """
    if not file_ids:
        return {}
    placeholders = ','.join('?' * len(file_ids))
    rows = conn.execute(f'''
        SELECT ft.file_id, t.name
        FROM tags t
        JOIN file_tags ft ON ft.tag_id = t.id
        WHERE ft.file_id IN ({placeholders})
        ORDER BY t.name
    ''', file_ids).fetchall()
    result = defaultdict(list)
    for r in rows:
        result[r['file_id']].append(r['name'])
    return result

def get_file_tags(conn, file_id):
    """Single-file helper (kept for backwards compatibility in write paths)."""
    return get_tags_for_ids(conn, [file_id]).get(file_id, [])

def file_to_dict(row, tags=None):
    return {
        'id':            row['id'],
        'filename':      row['filename'],
        'original_name': row['original_name'],
        'folder_id':     row['folder_id'],
        'folder_name':   row['folder_name'] if 'folder_name' in row.keys() else None,
        'size':          row['size'],
        'mimetype':      row['mimetype'],
        'sha256':        row['sha256'] if 'sha256' in row.keys() else None,
        'phash':         row['phash']  if 'phash'  in row.keys() else None,
        'uploaded_at':   row['uploaded_at'],
        'tags':          tags if tags is not None else [],
    }

# ── Sort helper ───────────────────────────────────────────────────────────────

ALLOWED_SORT_FIELDS = {
    'uploaded_at': 'f.uploaded_at',
    'name':        'f.original_name',
    'size':        'f.size',
}
ALLOWED_SORT_DIRS = {'asc', 'desc'}

def resolve_sort(field, direction):
    col = ALLOWED_SORT_FIELDS.get(field, 'f.uploaded_at')
    direction = direction.lower() if direction else 'desc'
    if direction not in ALLOWED_SORT_DIRS:
        direction = 'desc'
    return col, direction

# ─── Frontend ─────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')

# ─── Folders API ──────────────────────────────────────────────────────────────

@app.route('/api/folders', methods=['GET'])
def get_folders():
    conn = get_db()
    folders = conn.execute('SELECT * FROM folders ORDER BY name').fetchall()
    conn.close()
    return jsonify([dict(f) for f in folders])

@app.route('/api/folders', methods=['POST'])
def create_folder():
    data = request.json
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Name is required'}), 400
    try:
        ensure_folder_on_disk(name)
        conn = get_db()
        conn.execute('INSERT INTO folders (name) VALUES (?)', (name,))
        conn.commit()
        folder = conn.execute('SELECT * FROM folders WHERE name = ?', (name,)).fetchone()
        conn.close()
        return jsonify(dict(folder)), 201
    except sqlite3.IntegrityError:
        return jsonify({'error': 'A folder with that name already exists'}), 409

@app.route('/api/folders/<int:folder_id>', methods=['PUT'])
def rename_folder(folder_id):
    data = request.json
    new_name = data.get('name', '').strip()
    if not new_name:
        return jsonify({'error': 'Name is required'}), 400
    conn = get_db()
    old_row = conn.execute('SELECT name FROM folders WHERE id = ?', (folder_id,)).fetchone()
    if not old_row:
        conn.close()
        return jsonify({'error': 'Folder not found'}), 404
    try:
        rename_folder_on_disk(old_row['name'], new_name)
        conn.execute('UPDATE folders SET name = ? WHERE id = ?', (new_name, folder_id))
        conn.commit()
        conn.close()
        return jsonify({'ok': True})
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'error': 'A folder with that name already exists'}), 409

@app.route('/api/folders/<int:folder_id>', methods=['DELETE'])
def delete_folder(folder_id):
    conn = get_db()
    row = conn.execute('SELECT name FROM folders WHERE id = ?', (folder_id,)).fetchone()
    if row:
        delete_folder_on_disk(row['name'])
    conn.execute('UPDATE files SET folder_id = NULL WHERE folder_id = ?', (folder_id,))
    conn.execute('DELETE FROM folders WHERE id = ?', (folder_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/folders/<int:folder_id>/stats')
def folder_stats(folder_id):
    conn = get_db()
    row = conn.execute(
        'SELECT COUNT(*) as total, COALESCE(SUM(size), 0) as size FROM files WHERE folder_id = ?',
        (folder_id,)
    ).fetchone()
    conn.close()
    return jsonify({'total': row['total'], 'size': row['size']})

@app.route('/api/folders/<int:folder_id>/download')
def download_folder_zip(folder_id):
    """Stream a ZIP archive containing all files in the folder."""
    import zipfile, tempfile
    conn = get_db()
    folder_row = conn.execute('SELECT name FROM folders WHERE id = ?', (folder_id,)).fetchone()
    if not folder_row:
        conn.close()
        return jsonify({'error': 'Folder not found'}), 404
    folder_name = folder_row['name']
    files = conn.execute(
        'SELECT filename, original_name FROM files WHERE folder_id = ?', (folder_id,)
    ).fetchall()
    conn.close()

    tmp = tempfile.NamedTemporaryFile(suffix='.zip', delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            src = file_disk_path(f['filename'], folder_name)
            if os.path.exists(src):
                zf.write(src, arcname=f['original_name'])

    return send_file(
        tmp.name,
        as_attachment=True,
        download_name=f'{secure_filename(folder_name)}.zip',
        mimetype='application/zip',
    )

# ─── Files API ────────────────────────────────────────────────────────────────

def build_files_query(search, folder_id, tag):
    conditions, params = [], []

    if search:
        # Strip leading '#' so users can search by hashtag
        search_clean = search.lstrip('#')
        conditions.append('''(
            f.original_name LIKE ? OR
            f.id IN (
                SELECT ft.file_id FROM file_tags ft
                JOIN tags t ON ft.tag_id = t.id
                WHERE t.name LIKE ?
            )
        )''')
        params.extend([f'%{search_clean}%', f'%{search_clean}%'])

    if folder_id == 'none':
        conditions.append('f.folder_id IS NULL')
    elif folder_id:
        conditions.append('f.folder_id = ?')
        params.append(int(folder_id))

    if tag:
        conditions.append('''f.id IN (
            SELECT ft.file_id FROM file_tags ft
            JOIN tags t ON ft.tag_id = t.id
            WHERE t.name = ?
        )''')
        params.append(tag)

    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    return where, params


@app.route('/api/files', methods=['GET'])
def get_files():
    search    = request.args.get('q', '').strip()
    folder_id = request.args.get('folder_id', '')
    tag       = request.args.get('tag', '').strip()

    # Sort params
    sort_field = request.args.get('sort', 'uploaded_at')
    sort_dir   = request.args.get('dir', 'desc')
    sort_col, sort_dir = resolve_sort(sort_field, sort_dir)

    # Pagination — limit=-1 means "no limit" (used by folder-detail view)
    try:
        limit = int(request.args.get('limit', 30))
    except ValueError:
        limit = 30
    try:
        offset = int(request.args.get('offset', 0))
    except ValueError:
        offset = 0

    conn = get_db()
    where, params = build_files_query(search, folder_id, tag)

    total = conn.execute(
        f'SELECT COUNT(*) as cnt FROM files f {where}', params
    ).fetchone()['cnt']

    data_query = f'''
        SELECT f.*, folders.name as folder_name
        FROM files f
        LEFT JOIN folders ON f.folder_id = folders.id
        {where}
        ORDER BY {sort_col} {sort_dir.upper()}
    '''

    if limit > 0:
        data_query += ' LIMIT ? OFFSET ?'
        rows = conn.execute(data_query, params + [limit, offset]).fetchall()
    else:
        rows = conn.execute(data_query, params).fetchall()

    # ── Batch-load tags (single query for all returned files) ─────────────────
    file_ids = [r['id'] for r in rows]
    tags_map = get_tags_for_ids(conn, file_ids)
    conn.close()

    result = [file_to_dict(r, tags_map.get(r['id'], [])) for r in rows]
    return jsonify({'files': result, 'total': total})


@app.route('/api/files/<int:file_id>', methods=['GET'])
def get_single_file(file_id):
    """
    Return metadata for a single file by ID.
    Used by the frontend as a fallback when openEdit() can't find the file in
    the already-loaded state.files array (e.g. files loaded only via folder
    detail view that aren't in the current pagination window).
    """
    conn = get_db()
    row = conn.execute(
        '''SELECT f.*, folders.name as folder_name
           FROM files f
           LEFT JOIN folders ON f.folder_id = folders.id
           WHERE f.id = ?''',
        (file_id,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'File not found'}), 404
    tags = get_file_tags(conn, file_id)
    conn.close()
    return jsonify(file_to_dict(row, tags))


@app.route('/api/files/upload', methods=['POST'])
@limiter.limit("30 per minute")
def upload_file():
    ip = request.remote_addr or '0.0.0.0'
    if not _check_upload_rate(ip):
        return jsonify({'error': 'Upload rate limit exceeded — max 30 per minute'}), 429

    if 'file' not in request.files:
        return jsonify({'error': 'No file found in request'}), 400

    file = request.files['file']
    folder_id = request.form.get('folder_id') or None
    tags_raw  = request.form.get('tags', '')

    if file.filename == '':
        return jsonify({'error': 'Empty filename'}), 400

    original_name = file.filename
    safe_name     = secure_filename(original_name)

    if not safe_name:
        ext_map = {
            'image/jpeg': '.jpg',  'image/png':  '.png',
            'image/gif':  '.gif',  'image/webp': '.webp',
            'image/heic': '.heic', 'video/mp4':  '.mp4',
            'application/pdf': '.pdf',
        }
        ext = ext_map.get(file.content_type or '', '')
    else:
        ext = os.path.splitext(safe_name)[1]

    timestamp    = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    stored_name  = f'{timestamp}{ext}'

    folder_name = None
    if folder_id:
        conn_tmp = get_db()
        frow = conn_tmp.execute('SELECT name FROM folders WHERE id = ?', (folder_id,)).fetchone()
        conn_tmp.close()
        if frow:
            folder_name = frow['name']
            ensure_folder_on_disk(folder_name)

    save_path = file_disk_path(stored_name, folder_name)
    try:
        file.save(save_path)
    except Exception as e:
        print(f'[ERROR] Could not save file: {e}')
        return jsonify({'error': f'Error saving file: {str(e)}'}), 500

    size     = os.path.getsize(save_path)
    mimetype = file.content_type or 'application/octet-stream'

    # ── Compute hashes for duplicate detection ────────────────────────────────
    sha256 = compute_sha256(save_path)
    phash  = None
    if mimetype in IMAGE_MIMETYPES:
        phash = compute_phash(save_path)
        # Pre-generate thumbnail so first load is instant
        generate_thumbnail(save_path, stored_name)

    conn = get_db()

    # ── Check for exact SHA-256 duplicate ────────────────────────────────────
    duplicate_info = None
    if sha256:
        dup = conn.execute(
            '''SELECT f.id, f.original_name, folders.name as folder_name
               FROM files f
               LEFT JOIN folders ON f.folder_id = folders.id
               WHERE f.sha256 = ? LIMIT 1''',
            (sha256,)
        ).fetchone()
        if dup:
            duplicate_info = {
                'type': 'exact',
                'id':   dup['id'],
                'name': dup['original_name'],
                'folder': dup['folder_name'],
            }

    # ── Check for perceptual (near) duplicate ─────────────────────────────────
    if phash and not duplicate_info:
        # Fetch all phashes from the DB and compare in Python.
        # For very large libraries (10k+ images) this could be moved to a
        # background job; for typical personal use it's fast enough inline.
        candidates = conn.execute(
            'SELECT id, original_name, phash FROM files WHERE phash IS NOT NULL'
        ).fetchall()
        for c in candidates:
            dist = phash_distance(phash, c['phash'])
            if dist is not None and dist <= 10:
                dup_row = conn.execute(
                    '''SELECT f.id, f.original_name, folders.name as folder_name
                       FROM files f LEFT JOIN folders ON f.folder_id = folders.id
                       WHERE f.id = ?''',
                    (c['id'],)
                ).fetchone()
                duplicate_info = {
                    'type':     'similar',
                    'distance': dist,
                    'id':       dup_row['id'],
                    'name':     dup_row['original_name'],
                    'folder':   dup_row['folder_name'],
                }
                break

    cur = conn.execute(
        'INSERT INTO files (filename, original_name, folder_id, size, mimetype, sha256, phash) '
        'VALUES (?,?,?,?,?,?,?)',
        (stored_name, original_name,
         int(folder_id) if folder_id else None,
         size, mimetype, sha256, phash)
    )
    file_id = cur.lastrowid

    for tag_name in [t.strip().lstrip('#') for t in tags_raw.split(',') if t.strip()]:
        conn.execute('INSERT OR IGNORE INTO tags (name) VALUES (?)', (tag_name,))
        tag_row = conn.execute('SELECT id FROM tags WHERE name = ?', (tag_name,)).fetchone()
        conn.execute('INSERT OR IGNORE INTO file_tags (file_id, tag_id) VALUES (?,?)',
                     (file_id, tag_row['id']))

    conn.commit()
    tags = get_file_tags(conn, file_id)
    row  = conn.execute(
        '''SELECT f.*, folders.name as folder_name FROM files f
           LEFT JOIN folders ON f.folder_id = folders.id WHERE f.id = ?''',
        (file_id,)
    ).fetchone()
    conn.close()

    resp = file_to_dict(row, tags)
    if duplicate_info:
        resp['duplicate'] = duplicate_info
    return jsonify(resp), 201


@app.route('/api/files/<int:file_id>', methods=['PUT'])
def update_file(file_id):
    data = request.json
    conn = get_db()
    row = conn.execute(
        '''SELECT f.filename, f.folder_id, folders.name as folder_name
           FROM files f LEFT JOIN folders ON f.folder_id = folders.id
           WHERE f.id = ?''',
        (file_id,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'File not found'}), 404

    stored_name     = row['filename']
    old_folder_name = row['folder_name']

    if 'folder_id' in data:
        new_folder_id   = data['folder_id']
        new_folder_name = None
        if new_folder_id:
            frow = conn.execute('SELECT name FROM folders WHERE id = ?', (new_folder_id,)).fetchone()
            if frow:
                new_folder_name = frow['name']
        move_file_on_disk(stored_name, old_folder_name, new_folder_name)
        conn.execute('UPDATE files SET folder_id = ? WHERE id = ?', (new_folder_id, file_id))

    if 'tags' in data:
        conn.execute('DELETE FROM file_tags WHERE file_id = ?', (file_id,))
        for tag_name in [t.strip().lstrip('#') for t in data['tags'] if t.strip()]:
            conn.execute('INSERT OR IGNORE INTO tags (name) VALUES (?)', (tag_name,))
            tag_row = conn.execute('SELECT id FROM tags WHERE name = ?', (tag_name,)).fetchone()
            conn.execute('INSERT OR IGNORE INTO file_tags (file_id, tag_id) VALUES (?,?)',
                         (file_id, tag_row['id']))

    if 'original_name' in data:
        conn.execute('UPDATE files SET original_name = ? WHERE id = ?',
                     (data['original_name'], file_id))

    conn.commit()
    conn.execute('DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM file_tags)')
    conn.commit()

    tags = get_file_tags(conn, file_id)
    row  = conn.execute(
        '''SELECT f.*, folders.name as folder_name FROM files f
           LEFT JOIN folders ON f.folder_id = folders.id WHERE f.id = ?''',
        (file_id,)
    ).fetchone()
    conn.close()
    return jsonify(file_to_dict(row, tags))


@app.route('/api/files/batch', methods=['PUT'])
def batch_update_files():
    """
    Apply the same change to multiple files at once.
    Accepted body keys:
      - ids       (required) list of file IDs
      - folder_id           move all to this folder (null = unclassified)
      - add_tags            list of tag names to add
      - remove_tags         list of tag names to remove
    """
    data     = request.json or {}
    ids      = data.get('ids', [])
    if not ids:
        return jsonify({'error': 'No file IDs provided'}), 400

    conn = get_db()

    # Move
    if 'folder_id' in data:
        new_folder_id   = data['folder_id']
        new_folder_name = None
        if new_folder_id:
            frow = conn.execute('SELECT name FROM folders WHERE id = ?', (new_folder_id,)).fetchone()
            if frow:
                new_folder_name = frow['name']
        for fid in ids:
            row = conn.execute(
                '''SELECT f.filename, folders.name as folder_name FROM files f
                   LEFT JOIN folders ON f.folder_id = folders.id WHERE f.id = ?''',
                (fid,)
            ).fetchone()
            if row:
                move_file_on_disk(row['filename'], row['folder_name'], new_folder_name)
        placeholders = ','.join('?' * len(ids))
        conn.execute(f'UPDATE files SET folder_id = ? WHERE id IN ({placeholders})',
                     [new_folder_id] + ids)

    # Add tags
    for tag_name in [t.strip().lstrip('#') for t in data.get('add_tags', []) if t.strip()]:
        conn.execute('INSERT OR IGNORE INTO tags (name) VALUES (?)', (tag_name,))
        tag_row = conn.execute('SELECT id FROM tags WHERE name = ?', (tag_name,)).fetchone()
        for fid in ids:
            conn.execute('INSERT OR IGNORE INTO file_tags (file_id, tag_id) VALUES (?,?)',
                         (fid, tag_row['id']))

    # Remove tags
    for tag_name in [t.strip().lstrip('#') for t in data.get('remove_tags', []) if t.strip()]:
        tag_row = conn.execute('SELECT id FROM tags WHERE name = ?', (tag_name,)).fetchone()
        if tag_row:
            placeholders = ','.join('?' * len(ids))
            conn.execute(
                f'DELETE FROM file_tags WHERE tag_id = ? AND file_id IN ({placeholders})',
                [tag_row['id']] + ids
            )

    conn.commit()
    conn.execute('DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM file_tags)')
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'updated': len(ids)})


@app.route('/api/files/batch', methods=['DELETE'])
def batch_delete_files():
    """Delete multiple files at once."""
    data = request.json or {}
    ids  = data.get('ids', [])
    if not ids:
        return jsonify({'error': 'No file IDs provided'}), 400

    conn = get_db()
    deleted = 0
    for fid in ids:
        row = conn.execute(
            '''SELECT f.filename, folders.name as folder_name FROM files f
               LEFT JOIN folders ON f.folder_id = folders.id WHERE f.id = ?''',
            (fid,)
        ).fetchone()
        if not row:
            continue
        filepath = file_disk_path(row['filename'], row['folder_name'])
        if os.path.exists(filepath):
            os.remove(filepath)
        delete_thumbnail(row['filename'])
        conn.execute('DELETE FROM files WHERE id = ?', (fid,))
        deleted += 1

    conn.execute('DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM file_tags)')
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'deleted': deleted})


@app.route('/api/files/<int:file_id>', methods=['DELETE'])
def delete_file(file_id):
    conn = get_db()
    row = conn.execute(
        '''SELECT f.filename, folders.name as folder_name FROM files f
           LEFT JOIN folders ON f.folder_id = folders.id WHERE f.id = ?''',
        (file_id,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'File not found'}), 404

    filepath = file_disk_path(row['filename'], row['folder_name'])
    if os.path.exists(filepath):
        os.remove(filepath)
    delete_thumbnail(row['filename'])

    conn.execute('DELETE FROM files WHERE id = ?', (file_id,))
    conn.execute('DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM file_tags)')
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/files/<int:file_id>/download')
def download_file(file_id):
    conn = get_db()
    row = conn.execute(
        '''SELECT f.filename, f.original_name, folders.name as folder_name
           FROM files f LEFT JOIN folders ON f.folder_id = folders.id
           WHERE f.id = ?''',
        (file_id,)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    directory = folder_disk_path(row['folder_name']) if row['folder_name'] else UPLOAD_FOLDER
    return send_from_directory(directory, row['filename'],
                               as_attachment=True,
                               download_name=row['original_name'])


@app.route('/api/files/<int:file_id>/preview')
def preview_file(file_id):
    """
    Serve a thumbnail for images (auto-generated and cached as WebP).
    Pass ?full=1 to bypass the thumbnail and serve the original file.
    """
    conn = get_db()
    row = conn.execute(
        '''SELECT f.filename, f.mimetype, folders.name as folder_name
           FROM files f LEFT JOIN folders ON f.folder_id = folders.id
           WHERE f.id = ?''',
        (file_id,)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404

    full_path  = file_disk_path(row['filename'], row['folder_name'])
    full_mode  = request.args.get('full', '0') == '1'
    is_image   = row['mimetype'] in IMAGE_MIMETYPES

    if is_image and not full_mode and PIL_AVAILABLE:
        thumb_bytes = get_thumbnail_bytes(full_path, row['filename'])
        if thumb_bytes:
            response = send_file(
                io.BytesIO(thumb_bytes),
                mimetype='image/webp',
                max_age=0,           # browser revalidates; server answers from RAM
            )
            response.headers['Cache-Control'] = 'private, max-age=3600'
            response.headers['Vary'] = 'Accept'
            return response

    directory = folder_disk_path(row['folder_name']) if row['folder_name'] else UPLOAD_FOLDER
    return send_from_directory(directory, row['filename'], as_attachment=False)


@app.route('/api/thumbnails/cache-info', methods=['GET'])
def thumbnail_cache_info():
    """Return current thumbnail LRU cache statistics (entries, memory usage)."""
    return jsonify(_thumb_cache.info)


@app.route('/api/thumbnails/cache', methods=['DELETE'])
def clear_thumbnail_cache():
    """Flush the entire thumbnail cache (useful after bulk operations)."""
    with _thumb_cache._lock:
        _thumb_cache._data.clear()
    return jsonify({'ok': True, 'message': 'Thumbnail cache cleared'})


@app.route('/api/tags', methods=['GET'])
def get_all_tags():
    conn = get_db()
    rows = conn.execute(
        '''SELECT name, COUNT(ft.file_id) as count
           FROM tags t LEFT JOIN file_tags ft ON t.id = ft.tag_id
           GROUP BY t.id ORDER BY count DESC'''
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/duplicates', methods=['GET'])
def get_duplicates():
    """
    Return groups of files that are exact (sha256) or visually similar (phash).
    Query param ?type=exact|similar (default: exact).
    """
    dup_type = request.args.get('type', 'exact')
    conn = get_db()

    if dup_type == 'exact':
        rows = conn.execute('''
            SELECT f.*, folders.name as folder_name
            FROM files f
            LEFT JOIN folders ON f.folder_id = folders.id
            WHERE f.sha256 IN (
                SELECT sha256 FROM files
                WHERE sha256 IS NOT NULL
                GROUP BY sha256 HAVING COUNT(*) > 1
            )
            ORDER BY f.sha256, f.uploaded_at
        ''').fetchall()
        file_ids = [r['id'] for r in rows]
        tags_map = get_tags_for_ids(conn, file_ids)
        conn.close()
        # Group by sha256
        groups = defaultdict(list)
        for r in rows:
            groups[r['sha256']].append(file_to_dict(r, tags_map.get(r['id'], [])))
        return jsonify({'type': 'exact', 'groups': list(groups.values())})

    # Similar (perceptual) — O(n²) in Python; fine for personal libraries.
    rows = conn.execute(
        '''SELECT f.id, f.original_name, f.phash, f.uploaded_at,
                  f.size, f.mimetype, f.filename, f.folder_id, f.sha256,
                  folders.name as folder_name
           FROM files f
           LEFT JOIN folders ON f.folder_id = folders.id
           WHERE f.phash IS NOT NULL'''
    ).fetchall()
    conn.close()

    seen    = set()
    groups  = []
    entries = list(rows)
    for i, a in enumerate(entries):
        if a['id'] in seen:
            continue
        group = [a]
        for b in entries[i + 1:]:
            if b['id'] in seen:
                continue
            dist = phash_distance(a['phash'], b['phash'])
            if dist is not None and dist <= 10:
                group.append(b)
                seen.add(b['id'])
        if len(group) > 1:
            seen.add(a['id'])
            groups.append([file_to_dict(r, []) for r in group])

    return jsonify({'type': 'similar', 'groups': groups})

# ─── Run ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()

    import socket
    try:
        import qrcode
        _qr_available = True
    except ImportError:
        _qr_available = False

    try:
        _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _s.connect(('8.8.8.8', 80))
        local_ip = _s.getsockname()[0]
        _s.close()
    except Exception:
        local_ip = '127.0.0.1'

    url = f'http://{local_ip}:5000'

    if _qr_available:
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    else:
        print("\n  (install 'qrcode' via pip to display the QR code here)")

    print(f'\n✅  LocalFileHub is running')
    print(f'📱  Scan the QR or open: {url}')
    print(f'💻  From this PC: http://localhost:5000\n')

    # Print optional-dependency status

    print(f'🖼️   Thumbnails (Pillow):      {"✅ active – LRU in-memory cache (" + str(THUMB_CACHE_MAX_ENTRIES) + " entries)" if PIL_AVAILABLE else "❌ install Pillow"}')
    print(f'🔍  Perceptual hash (imagehash): {"✅ active" if IMAGEHASH_AVAILABLE else "❌ install imagehash"}')
    print(f'🚦  Rate limiter (flask-limiter):{"✅ active" if LIMITER_AVAILABLE else "⚠️  using built-in fallback"}\n')

    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
