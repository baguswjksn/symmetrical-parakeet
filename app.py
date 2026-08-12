import os
import sqlite3
import uuid
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, send_file, abort
from ebooklib import epub

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'epub_drafts.db')
COVER_DIR = os.path.join(BASE_DIR, 'static', 'covers')
EXPORT_DIR = os.path.join(BASE_DIR, 'exports')
ALLOWED_COVER_EXT = {'png', 'jpg', 'jpeg'}

os.makedirs(COVER_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = 'dev-secret-change-me'

DEFAULT_CSS = """body { font-family: serif; line-height: 1.6; margin: 1em; }
h1, h2, h3 { font-family: sans-serif; }
p { text-align: justify; margin: 0 0 1em 0; }
"""


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS drafts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL DEFAULT 'Draf Tanpa Judul',
        author TEXT NOT NULL DEFAULT '',
        language TEXT NOT NULL DEFAULT 'id',
        cover_filename TEXT,
        css TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS pages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        draft_id INTEGER NOT NULL,
        position INTEGER NOT NULL,
        title TEXT NOT NULL DEFAULT 'Halaman Baru',
        content TEXT NOT NULL DEFAULT '',
        FOREIGN KEY (draft_id) REFERENCES drafts(id) ON DELETE CASCADE
    );
    """)
    conn.commit()
    conn.close()


def now():
    return datetime.utcnow().isoformat()


def touch_draft(conn, draft_id):
    conn.execute("UPDATE drafts SET updated_at = ? WHERE id = ?", (now(), draft_id))


def allowed_cover(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_COVER_EXT


# ---------- List & create ----------

@app.route('/')
def index():
    conn = get_db()
    drafts = conn.execute("SELECT * FROM drafts ORDER BY updated_at DESC").fetchall()
    conn.close()
    return render_template('index.html', drafts=drafts)


@app.route('/draft/new', methods=['POST'])
def new_draft():
    conn = get_db()
    t = now()
    cur = conn.execute(
        "INSERT INTO drafts (title, author, css, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ('Draf Tanpa Judul', '', DEFAULT_CSS, t, t)
    )
    draft_id = cur.lastrowid
    conn.execute(
        "INSERT INTO pages (draft_id, position, title, content) VALUES (?, ?, ?, ?)",
        (draft_id, 0, 'Bab 1', '<p>Mulai tulis di sini...</p>')
    )
    conn.commit()
    conn.close()
    return redirect(url_for('edit_draft', draft_id=draft_id))


@app.route('/draft/<int:draft_id>/delete', methods=['POST'])
def delete_draft(draft_id):
    conn = get_db()
    draft = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if draft and draft['cover_filename']:
        cover_path = os.path.join(COVER_DIR, draft['cover_filename'])
        if os.path.exists(cover_path):
            os.remove(cover_path)
    conn.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))


# ---------- Edit draft ----------

@app.route('/draft/<int:draft_id>')
def edit_draft(draft_id):
    conn = get_db()
    draft = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if not draft:
        conn.close()
        abort(404)
    pages = conn.execute(
        "SELECT * FROM pages WHERE draft_id = ? ORDER BY position ASC", (draft_id,)
    ).fetchall()
    conn.close()
    return render_template('edit.html', draft=draft, pages=pages)


@app.route('/draft/<int:draft_id>/meta', methods=['POST'])
def update_meta(draft_id):
    title = request.form.get('title', '').strip() or 'Draf Tanpa Judul'
    author = request.form.get('author', '').strip()
    language = request.form.get('language', 'id').strip() or 'id'
    conn = get_db()
    conn.execute(
        "UPDATE drafts SET title = ?, author = ?, language = ?, updated_at = ? WHERE id = ?",
        (title, author, language, now(), draft_id)
    )
    conn.commit()
    conn.close()
    return redirect(url_for('edit_draft', draft_id=draft_id))


@app.route('/draft/<int:draft_id>/cover', methods=['POST'])
def update_cover(draft_id):
    file = request.files.get('cover')
    conn = get_db()
    if file and file.filename and allowed_cover(file.filename):
        ext = file.filename.rsplit('.', 1)[1].lower()
        filename = f"cover_{draft_id}_{uuid.uuid4().hex[:8]}.{ext}"

        draft = conn.execute("SELECT cover_filename FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if draft and draft['cover_filename']:
            old_path = os.path.join(COVER_DIR, draft['cover_filename'])
            if os.path.exists(old_path):
                os.remove(old_path)

        file.save(os.path.join(COVER_DIR, filename))
        conn.execute(
            "UPDATE drafts SET cover_filename = ?, updated_at = ? WHERE id = ?",
            (filename, now(), draft_id)
        )
        conn.commit()
    conn.close()
    return redirect(url_for('edit_draft', draft_id=draft_id))


@app.route('/draft/<int:draft_id>/css', methods=['POST'])
def update_css(draft_id):
    css = request.form.get('css', '')
    conn = get_db()
    conn.execute("UPDATE drafts SET css = ?, updated_at = ? WHERE id = ?", (css, now(), draft_id))
    conn.commit()
    conn.close()
    return redirect(url_for('edit_draft', draft_id=draft_id))


# ---------- Pages ----------

@app.route('/draft/<int:draft_id>/page/new', methods=['POST'])
def new_page(draft_id):
    conn = get_db()
    max_pos = conn.execute(
        "SELECT COALESCE(MAX(position), -1) AS m FROM pages WHERE draft_id = ?", (draft_id,)
    ).fetchone()['m']
    conn.execute(
        "INSERT INTO pages (draft_id, position, title, content) VALUES (?, ?, ?, ?)",
        (draft_id, max_pos + 1, 'Halaman Baru', '<p></p>')
    )
    touch_draft(conn, draft_id)
    conn.commit()
    conn.close()
    return redirect(url_for('edit_draft', draft_id=draft_id))


@app.route('/draft/<int:draft_id>/page/<int:page_id>/update', methods=['POST'])
def update_page(draft_id, page_id):
    title = request.form.get('title', '').strip() or 'Tanpa Judul'
    content = request.form.get('content', '')
    conn = get_db()
    conn.execute(
        "UPDATE pages SET title = ?, content = ? WHERE id = ? AND draft_id = ?",
        (title, content, page_id, draft_id)
    )
    touch_draft(conn, draft_id)
    conn.commit()
    conn.close()
    return redirect(url_for('edit_draft', draft_id=draft_id))


@app.route('/draft/<int:draft_id>/page/<int:page_id>/delete', methods=['POST'])
def delete_page(draft_id, page_id):
    conn = get_db()
    conn.execute("DELETE FROM pages WHERE id = ? AND draft_id = ?", (page_id, draft_id))
    touch_draft(conn, draft_id)
    conn.commit()
    conn.close()
    return redirect(url_for('edit_draft', draft_id=draft_id))


@app.route('/draft/<int:draft_id>/page/<int:page_id>/move/<direction>', methods=['POST'])
def move_page(draft_id, page_id, direction):
    conn = get_db()
    pages = conn.execute(
        "SELECT id, position FROM pages WHERE draft_id = ? ORDER BY position ASC", (draft_id,)
    ).fetchall()
    ids = [p['id'] for p in pages]
    if page_id in ids:
        idx = ids.index(page_id)
        swap_idx = idx - 1 if direction == 'up' else idx + 1
        if 0 <= swap_idx < len(ids):
            a_id, b_id = ids[idx], ids[swap_idx]
            a_pos, b_pos = pages[idx]['position'], pages[swap_idx]['position']
            conn.execute("UPDATE pages SET position = ? WHERE id = ?", (b_pos, a_id))
            conn.execute("UPDATE pages SET position = ? WHERE id = ?", (a_pos, b_id))
            touch_draft(conn, draft_id)
            conn.commit()
    conn.close()
    return redirect(url_for('edit_draft', draft_id=draft_id))


# ---------- Export ----------

@app.route('/draft/<int:draft_id>/export')
def export_epub(draft_id):
    conn = get_db()
    draft = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if not draft:
        conn.close()
        abort(404)
    pages = conn.execute(
        "SELECT * FROM pages WHERE draft_id = ? ORDER BY position ASC", (draft_id,)
    ).fetchall()
    conn.close()

    book = epub.EpubBook()
    book.set_identifier(f'draft-{draft_id}-{uuid.uuid4().hex[:8]}')
    book.set_title(draft['title'])
    book.set_language(draft['language'] or 'id')
    if draft['author']:
        book.add_author(draft['author'])

    if draft['cover_filename']:
        cover_path = os.path.join(COVER_DIR, draft['cover_filename'])
        if os.path.exists(cover_path):
            with open(cover_path, 'rb') as f:
                ext = draft['cover_filename'].rsplit('.', 1)[1].lower()
                book.set_cover(f"cover.{ext}", f.read())

    css_content = draft['css'] or DEFAULT_CSS
    nav_css = epub.EpubItem(
        uid="style_nav", file_name="style/style.css", media_type="text/css", content=css_content
    )
    book.add_item(nav_css)

    chapters = []
    for i, page in enumerate(pages):
        c = epub.EpubHtml(
            title=page['title'], file_name=f'page_{i + 1}.xhtml', lang=draft['language'] or 'id'
        )
        c.content = f"<h1>{page['title']}</h1>{page['content']}"
        c.add_item(nav_css)
        book.add_item(c)
        chapters.append(c)

    book.toc = tuple(chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ['nav'] + chapters

    safe_title = "".join(c for c in draft['title'] if c.isalnum() or c in (' ', '_', '-')).strip() or 'draft'
    out_path = os.path.join(EXPORT_DIR, f"{safe_title}_{draft_id}.epub")
    epub.write_epub(out_path, book)

    return send_file(out_path, as_attachment=True, download_name=f"{safe_title}.epub")


if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000)
