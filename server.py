import os
import sqlite3
import shutil
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from datetime import datetime

app = Flask(__name__)

app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB max upload

@app.errorhandler(413)
def too_large(e):
    return jsonify({'error': 'File too large (500 MB limit per upload)'}), 413

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
DB_PATH = os.path.join(BASE_DIR, 'database.db')

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ─── Disk helpers ────────────────────────────────────────────────────────────

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

# ─── Database ────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            original_name TEXT NOT NULL,
            folder_id INTEGER,
            size INTEGER,
            mimetype TEXT,
            uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (folder_id) REFERENCES folders(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS file_tags (
            file_id INTEGER,
            tag_id INTEGER,
            PRIMARY KEY (file_id, tag_id),
            FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
            FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
        );
    ''')
    conn.commit()
    conn.close()

def file_to_dict(row, tags=[]):
    return {
        'id': row['id'],
        'filename': row['filename'],
        'original_name': row['original_name'],
        'folder_id': row['folder_id'],
        'folder_name': row['folder_name'] if 'folder_name' in row.keys() else None,
        'size': row['size'],
        'mimetype': row['mimetype'],
        'uploaded_at': row['uploaded_at'],
        'tags': tags
    }

# ─── Frontend ────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')

# ─── Folders API ─────────────────────────────────────────────────────────────

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

# ─── Files API ───────────────────────────────────────────────────────────────

def get_file_tags(conn, file_id):
    rows = conn.execute('''
        SELECT t.name FROM tags t
        JOIN file_tags ft ON ft.tag_id = t.id
        WHERE ft.file_id = ?
    ''', (file_id,)).fetchall()
    return [r['name'] for r in rows]


def build_files_query(search, folder_id, tag):
    """
    Returns (where_clause, params) for the files query.
    Shared by the paginated list and the count query.
    """
    conditions = []
    params = []

    if search:
        conditions.append('''(f.original_name LIKE ? OR f.id IN (
            SELECT ft.file_id FROM file_tags ft JOIN tags t ON ft.tag_id = t.id WHERE t.name LIKE ?
        ))''')
        params.extend([f'%{search}%', f'%{search}%'])

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

    # ── Pagination params ────────────────────────────────────────────────────
    # limit=-1 means "return everything" (used internally by folder-detail view)
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

    # ── Total count (for the frontend to know when all pages are loaded) ─────
    count_query = f'SELECT COUNT(*) as cnt FROM files f {where}'
    total = conn.execute(count_query, params).fetchone()['cnt']

    # ── Paginated data ───────────────────────────────────────────────────────
    data_query = f'''
        SELECT f.*, folders.name as folder_name
        FROM files f
        LEFT JOIN folders ON f.folder_id = folders.id
        {where}
        ORDER BY f.uploaded_at DESC
    '''

    if limit > 0:
        data_query += ' LIMIT ? OFFSET ?'
        rows = conn.execute(data_query, params + [limit, offset]).fetchall()
    else:
        # limit=-1: no pagination, return all (used by folder detail & move modal)
        rows = conn.execute(data_query, params).fetchall()

    result = [file_to_dict(r, get_file_tags(conn, r['id'])) for r in rows]
    conn.close()

    # ── Response format ──────────────────────────────────────────────────────
    # Always return { files, total } so the frontend can drive infinite scroll.
    # When limit=-1 total equals len(files), which is correct.
    return jsonify({'files': result, 'total': total})


@app.route('/api/files/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file found in request'}), 400
    file = request.files['file']
    folder_id = request.form.get('folder_id') or None
    tags_raw = request.form.get('tags', '')

    if file.filename == '':
        return jsonify({'error': 'Empty filename'}), 400

    original_name = file.filename
    safe_name = secure_filename(original_name)

    if not safe_name:
        ext_map = {
            'image/jpeg': '.jpg', 'image/png': '.png', 'image/gif': '.gif',
            'image/webp': '.webp', 'image/heic': '.heic', 'video/mp4': '.mp4',
            'application/pdf': '.pdf',
        }
        ext = ext_map.get(file.content_type or '', '')
    else:
        ext = os.path.splitext(safe_name)[1]

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    stored_name = f"{timestamp}{ext}"

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

    size = os.path.getsize(save_path)
    mimetype = file.content_type or 'application/octet-stream'

    conn = get_db()
    cur = conn.execute(
        'INSERT INTO files (filename, original_name, folder_id, size, mimetype) VALUES (?,?,?,?,?)',
        (stored_name, original_name, int(folder_id) if folder_id else None, size, mimetype)
    )
    file_id = cur.lastrowid
    for tag_name in [t.strip().lstrip('#') for t in tags_raw.split(',') if t.strip()]:
        conn.execute('INSERT OR IGNORE INTO tags (name) VALUES (?)', (tag_name,))
        tag_row = conn.execute('SELECT id FROM tags WHERE name = ?', (tag_name,)).fetchone()
        conn.execute('INSERT OR IGNORE INTO file_tags (file_id, tag_id) VALUES (?,?)', (file_id, tag_row['id']))
    conn.commit()
    tags = get_file_tags(conn, file_id)
    row = conn.execute('SELECT f.*, folders.name as folder_name FROM files f LEFT JOIN folders ON f.folder_id = folders.id WHERE f.id = ?', (file_id,)).fetchone()
    conn.close()
    return jsonify(file_to_dict(row, tags)), 201

@app.route('/api/files/<int:file_id>', methods=['PUT'])
def update_file(file_id):
    data = request.json
    conn = get_db()
    row = conn.execute('SELECT f.filename, f.folder_id, folders.name as folder_name FROM files f LEFT JOIN folders ON f.folder_id = folders.id WHERE f.id = ?', (file_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'File not found'}), 404

    stored_name = row['filename']
    old_folder_name = row['folder_name']

    if 'folder_id' in data:
        new_folder_id = data['folder_id']
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
            conn.execute('INSERT OR IGNORE INTO file_tags (file_id, tag_id) VALUES (?,?)', (file_id, tag_row['id']))

    if 'original_name' in data:
        conn.execute('UPDATE files SET original_name = ? WHERE id = ?', (data['original_name'], file_id))

    conn.commit()
    conn.execute('DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM file_tags)')
    conn.commit()

    tags = get_file_tags(conn, file_id)
    row = conn.execute('SELECT f.*, folders.name as folder_name FROM files f LEFT JOIN folders ON f.folder_id = folders.id WHERE f.id = ?', (file_id,)).fetchone()
    conn.close()
    return jsonify(file_to_dict(row, tags))

@app.route('/api/files/<int:file_id>', methods=['DELETE'])
def delete_file(file_id):
    conn = get_db()
    row = conn.execute('SELECT f.filename, folders.name as folder_name FROM files f LEFT JOIN folders ON f.folder_id = folders.id WHERE f.id = ?', (file_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'File not found'}), 404
    filepath = file_disk_path(row['filename'], row['folder_name'])
    if os.path.exists(filepath):
        os.remove(filepath)
    conn.execute('DELETE FROM files WHERE id = ?', (file_id,))
    conn.execute('DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM file_tags)')
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/files/<int:file_id>/download')
def download_file(file_id):
    conn = get_db()
    row = conn.execute('SELECT f.filename, f.original_name, folders.name as folder_name FROM files f LEFT JOIN folders ON f.folder_id = folders.id WHERE f.id = ?', (file_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    directory = folder_disk_path(row['folder_name']) if row['folder_name'] else UPLOAD_FOLDER
    return send_from_directory(directory, row['filename'], as_attachment=True, download_name=row['original_name'])

@app.route('/api/files/<int:file_id>/preview')
def preview_file(file_id):
    conn = get_db()
    row = conn.execute('SELECT f.filename, folders.name as folder_name FROM files f LEFT JOIN folders ON f.folder_id = folders.id WHERE f.id = ?', (file_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    directory = folder_disk_path(row['folder_name']) if row['folder_name'] else UPLOAD_FOLDER
    return send_from_directory(directory, row['filename'], as_attachment=False)

@app.route('/api/tags', methods=['GET'])
def get_all_tags():
    conn = get_db()
    rows = conn.execute('SELECT name, COUNT(ft.file_id) as count FROM tags t LEFT JOIN file_tags ft ON t.id = ft.tag_id GROUP BY t.id ORDER BY count DESC').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# ─── Run ─────────────────────────────────────────────────────────────────────

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

    print(f'\n✅ LocalFileHub is running')
    print(f'📱 Scan the QR or open: {url}')
    print(f'💻 From this PC: http://localhost:5000\n')
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
