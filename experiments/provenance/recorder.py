#!/usr/bin/env python3
"""Private provenance spike. Python stdlib only; never runs model/tool commands."""
import argparse
import datetime
"""Private provenance spike. Python stdlib only; never runs model/tool commands."""
import argparse
import datetime
import difflib
import hashlib
import html
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

MAX_FILE = 128 * 1024
MAX_TOTAL = 8 * 1024 * 1024
DENY_NAMES = {'.env', '.netrc', '.npmrc', '.pypirc', 'credentials', 'auth.json', 'id_rsa', 'id_ed25519'}
DENY_PARTS = {'.git', '.sylp', '.claude', '.ssh', '.aws', '.gnupg', 'node_modules', '__pycache__'}
DENY_SUFFIX = {'.pem', '.key', '.p12', '.pfx', '.sqlite', '.sqlite3', '.db'}


def digest(value):
    return hashlib.sha256(value).hexdigest()


def connect(path):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    old = os.umask(0o077)
    try:
        db = sqlite3.connect(path, timeout=10, isolation_level=None)
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    db.executescript('''
    PRAGMA journal_mode=WAL;
    PRAGMA synchronous=FULL;
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, at REAL, kind TEXT, session TEXT, prompt TEXT, tool TEXT, body TEXT);
    CREATE TABLE IF NOT EXISTS blobs(hash TEXT PRIMARY KEY, content TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY, event TEXT, files TEXT, gaps TEXT);
    CREATE TABLE IF NOT EXISTS pending(tool TEXT PRIMARY KEY, event TEXT, path TEXT, overlap INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS changes(id INTEGER PRIMARY KEY, event TEXT, path TEXT, before_hash TEXT, after_hash TEXT, evidence TEXT, patch TEXT);
    ''')
    return db

"""Private provenance spike. Python stdlib only; never runs model/tool commands."""
import argparse
import datetime
import difflib
import hashlib
import html
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

MAX_FILE = 128 * 1024
MAX_TOTAL = 8 * 1024 * 1024
DENY_NAMES = {'.env', '.netrc', '.npmrc', '.pypirc', 'credentials', 'auth.json', 'id_rsa', 'id_ed25519'}
DENY_PARTS = {'.git', '.sylp', '.claude', '.ssh', '.aws', '.gnupg', 'node_modules', '__pycache__'}
DENY_SUFFIX = {'.pem', '.key', '.p12', '.pfx', '.sqlite', '.sqlite3', '.db'}


def digest(value):
    return hashlib.sha256(value).hexdigest()


def connect(path):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    old = os.umask(0o077)
    try:
        db = sqlite3.connect(path, timeout=10, isolation_level=None)
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    db.executescript('''
    PRAGMA journal_mode=WAL;
    PRAGMA synchronous=FULL;
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, at REAL, kind TEXT, session TEXT, prompt TEXT, tool TEXT, body TEXT);
    CREATE TABLE IF NOT EXISTS blobs(hash TEXT PRIMARY KEY, content TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY, event TEXT, files TEXT, gaps TEXT);
    CREATE TABLE IF NOT EXISTS pending(tool TEXT PRIMARY KEY, event TEXT, path TEXT, overlap INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS changes(id INTEGER PRIMARY KEY, event TEXT, path TEXT, before_hash TEXT, after_hash TEXT, evidence TEXT, patch TEXT);
    ''')
    return db


def meta(db, key, default=None):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def setmeta(db, key, value):
    db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, str(value)))


def gitroot(cwd):
    result = subprocess.run(['git', '-C', str(cwd), 'rev-parse', '--show-toplevel'], capture_output=True, check=True)
    return Path(os.fsdecode(result.stdout).strip()).resolve()


def allowed(relative):
    p = Path(relative)
    return not (set(p.parts) & DENY_PARTS or p.name.lower() in DENY_NAMES or p.name.lower().startswith('.env.') or p.suffix.lower() in DENY_SUFFIX or 'secret' in p.name.lower() or 'credential' in p.name.lower())


def scan(db, root, include=None):
    result = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], capture_output=True, check=True)
    files, gaps, total = {}, [], 0
    names = sorted(set(os.fsdecode(result.stdout).split('\0')) - {''})
    if len(names) > 2000:
        gaps.append('Repository has more than 2000 candidate files; remaining files not captured.')
    wanted = names[:2000]
    # Declared tool files and anything captured before are always diffed, even past the cap.
    extras = sorted((set(include or ()) | set(previous(db))) - set(wanted))
    extra_only = set(extras)
    for name in wanted + extras:
        rel = Path(name)
        if not allowed(name):
            gaps.append(name + ': excluded by capture policy')
            continue
        p = root / rel
        if rel.is_absolute() or '..' in rel.parts or any(parent.is_symlink() for parent in [p, *list(p.parents)[:len(rel.parts)-1]]):
            gaps.append(name + ': symlink or unsafe path excluded')
            continue
        try:
            if not p.exists():
                continue
            if not p.is_file() or p.stat().st_size > MAX_FILE:
                gaps.append(name + ': non-file or file-size limit')
                continue
            raw = p.read_bytes()
            if len(raw) > MAX_FILE or b'\0' in raw:
                gaps.append(name + ': binary or file-size limit')
                continue
            text = raw.decode('utf-8')
            total += len(raw)
            if total > MAX_TOTAL:
                gaps.append('Total snapshot byte limit reached; remaining files not captured.')
                break
            sha = digest(raw)
            db.execute('INSERT OR IGNORE INTO blobs VALUES(?,?)', (sha, text))
            files[name] = sha
        except (OSError, UnicodeError) as exc:
            gaps.append(name + ': unreadable or non-UTF8 (' + type(exc).__name__ + ')')
    return files, gaps, extra_only & set(files)


def previous(db):
    row = db.execute('SELECT files FROM snapshots ORDER BY id DESC LIMIT 1').fetchone()
    return json.loads(row[0]) if row else {}


def textfor(db, sha):
    return db.execute('SELECT content FROM blobs WHERE hash=?', (sha,)).fetchone()[0] if sha else ''


def snapshot(db, event, evidence='unattributed', target=None, include=None):
    root = Path(meta(db, 'root'))
    before = previous(db)
    after, gaps, entered = scan(db, root, include=include)
    for name in sorted(before.keys() | after.keys()):
        old, new = before.get(name), after.get(name)
        if old == new:
            continue
        if old is None and name in entered:
            label = 'capture gap: file entered the capture set at this event; before-content not recorded'
        else:
            label = evidence if target is None or name in target else 'ambiguous: changed outside named file during tool window'
        # An omitted file is not a verified deletion.
        if new is None and (root / name).exists():
            label = 'capture gap: file no longer eligible, not a verified deletion'
        patch = ''.join(difflib.unified_diff(textfor(db, old).splitlines(True), textfor(db, new).splitlines(True), fromfile='before/' + name, tofile='after/' + name))
        db.execute('INSERT INTO changes(event,path,before_hash,after_hash,evidence,patch) VALUES(?,?,?,?,?,?)', (event, name, old, new, label, patch))
    db.execute('INSERT INTO snapshots(event,files,gaps) VALUES(?,?,?)', (event, json.dumps(after), json.dumps(gaps)))


def initialize(db, cwd):
    root = gitroot(cwd)
    if meta(db, 'root'):
        if meta(db, 'root') != str(root):
            raise ValueError('Database already belongs to another repository')
        return
    with transaction(db):
        setmeta(db, 'root', root)
        setmeta(db, 'enabled', '1')
        files, gaps, _ = scan(db, root)
        db.execute('INSERT INTO snapshots(event,files,gaps) VALUES(?,?,?)', ('baseline', json.dumps(files), json.dumps(gaps)))


class transaction:
    def __init__(self, db): self.db = db
    def __enter__(self): self.db.execute('BEGIN IMMEDIATE')
import difflib
import hashlib
import html
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

MAX_FILE = 128 * 1024
MAX_TOTAL = 8 * 1024 * 1024
DENY_NAMES = {'.env', '.netrc', '.npmrc', '.pypirc', 'credentials', 'auth.json', 'id_rsa', 'id_ed25519'}
DENY_PARTS = {'.git', '.sylp', '.claude', '.ssh', '.aws', '.gnupg', 'node_modules', '__pycache__'}
DENY_SUFFIX = {'.pem', '.key', '.p12', '.pfx', '.sqlite', '.sqlite3', '.db'}


def digest(value):
    return hashlib.sha256(value).hexdigest()


def connect(path):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    old = os.umask(0o077)
    try:
        db = sqlite3.connect(path, timeout=10, isolation_level=None)
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    db.executescript('''
    PRAGMA journal_mode=WAL;
    PRAGMA synchronous=FULL;
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, at REAL, kind TEXT, session TEXT, prompt TEXT, tool TEXT, body TEXT);
    CREATE TABLE IF NOT EXISTS blobs(hash TEXT PRIMARY KEY, content TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY, event TEXT, files TEXT, gaps TEXT);
    CREATE TABLE IF NOT EXISTS pending(tool TEXT PRIMARY KEY, event TEXT, path TEXT, overlap INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS changes(id INTEGER PRIMARY KEY, event TEXT, path TEXT, before_hash TEXT, after_hash TEXT, evidence TEXT, patch TEXT);
    ''')
    return db


def meta(db, key, default=None):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def setmeta(db, key, value):
    db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, str(value)))


def gitroot(cwd):
    result = subprocess.run(['git', '-C', str(cwd), 'rev-parse', '--show-toplevel'], capture_output=True, check=True)
    return Path(os.fsdecode(result.stdout).strip()).resolve()


def allowed(relative):
    p = Path(relative)
    return not (set(p.parts) & DENY_PARTS or p.name.lower() in DENY_NAMES or p.name.lower().startswith('.env.') or p.suffix.lower() in DENY_SUFFIX or 'secret' in p.name.lower() or 'credential' in p.name.lower())


def scan(db, root, include=None):
    result = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], capture_output=True, check=True)
    files, gaps, total = {}, [], 0
    names = sorted(set(os.fsdecode(result.stdout).split('\0')) - {''})
    if len(names) > 2000:
        gaps.append('Repository has more than 2000 candidate files; remaining files not captured.')
    wanted = names[:2000]
    # Declared tool files and anything captured before are always diffed, even past the cap.
    extras = sorted((set(include or ()) | set(previous(db))) - set(wanted))
    extra_only = set(extras)
    for name in wanted + extras:
        rel = Path(name)
        if not allowed(name):
            gaps.append(name + ': excluded by capture policy')
            continue
        p = root / rel
        if rel.is_absolute() or '..' in rel.parts or any(parent.is_symlink() for parent in [p, *list(p.parents)[:len(rel.parts)-1]]):
            gaps.append(name + ': symlink or unsafe path excluded')
            continue
        try:
            if not p.exists():
                continue
            if not p.is_file() or p.stat().st_size > MAX_FILE:
                gaps.append(name + ': non-file or file-size limit')
                continue
            raw = p.read_bytes()
            if len(raw) > MAX_FILE or b'\0' in raw:
                gaps.append(name + ': binary or file-size limit')
                continue
            text = raw.decode('utf-8')
            total += len(raw)
            if total > MAX_TOTAL:
                gaps.append('Total snapshot byte limit reached; remaining files not captured.')
                break
            sha = digest(raw)
            db.execute('INSERT OR IGNORE INTO blobs VALUES(?,?)', (sha, text))
            files[name] = sha
        except (OSError, UnicodeError) as exc:
            gaps.append(name + ': unreadable or non-UTF8 (' + type(exc).__name__ + ')')
    return files, gaps, extra_only & set(files)


def previous(db):
    row = db.execute('SELECT files FROM snapshots ORDER BY id DESC LIMIT 1').fetchone()
    return json.loads(row[0]) if row else {}


def textfor(db, sha):
    return db.execute('SELECT content FROM blobs WHERE hash=?', (sha,)).fetchone()[0] if sha else ''


def snapshot(db, event, evidence='unattributed', target=None, include=None):
    root = Path(meta(db, 'root'))
    before = previous(db)
    after, gaps, entered = scan(db, root, include=include)
    for name in sorted(before.keys() | after.keys()):
        old, new = before.get(name), after.get(name)
        if old == new:
            continue
        if old is None and name in entered:
            label = 'capture gap: file entered the capture set at this event; before-content not recorded'
        else:
            label = evidence if target is None or name in target else 'ambiguous: changed outside named file during tool window'
        # An omitted file is not a verified deletion.
        if new is None and (root / name).exists():
            label = 'capture gap: file no longer eligible, not a verified deletion'
        patch = ''.join(difflib.unified_diff(textfor(db, old).splitlines(True), textfor(db, new).splitlines(True), fromfile='before/' + name, tofile='after/' + name))
        db.execute('INSERT INTO changes(event,path,before_hash,after_hash,evidence,patch) VALUES(?,?,?,?,?,?)', (event, name, old, new, label, patch))
    db.execute('INSERT INTO snapshots(event,files,gaps) VALUES(?,?,?)', (event, json.dumps(after), json.dumps(gaps)))


def initialize(db, cwd):
    root = gitroot(cwd)
    if meta(db, 'root'):
        if meta(db, 'root') != str(root):
            raise ValueError('Database already belongs to another repository')
        return
    with transaction(db):
        setmeta(db, 'root', root)
        setmeta(db, 'enabled', '1')
        files, gaps, _ = scan(db, root)
        db.execute('INSERT INTO snapshots(event,files,gaps) VALUES(?,?,?)', ('baseline', json.dumps(files), json.dumps(gaps)))


class transaction:
    def __init__(self, db): self.db = db
    def __enter__(self): self.db.execute('BEGIN IMMEDIATE')
    def __exit__(self, kind, value, tb): self.db.execute('ROLLBACK' if kind else 'COMMIT')


def record(db, data):
    if not meta(db, 'root') or meta(db, 'enabled') != '1':
        return False
    if gitroot(data.get('cwd', '.')) != Path(meta(db, 'root')):
        raise ValueError('Hook cwd does not match opted-in repository')
    kind = data.get('hook_event_name', '')
    session = str(data.get('session_id', ''))
    prompt = str(data.get('prompt_id', ''))
    tool = str(data.get('tool_use_id', ''))
    if not session or kind not in {'SessionStart','UserPromptSubmit','PreToolUse','PostToolUse','PostToolUseFailure','Stop','SessionEnd','AssistantResponse'}:
        raise ValueError('Unsupported or incomplete hook payload')
    # Only persist required fields, not environment, transcript files or arbitrary payloads.
    body = {'host':data.get('host','claude'), 'raw_event':data.get('raw_kind'), 'tool_name':data.get('tool_name'), 'prompt':data.get('prompt'), 'last_assistant_message':data.get('last_assistant_message')}
    ti = data.get('tool_input') or {}
    body['file_path'] = ti.get('file_path') if isinstance(ti, dict) else None
    body['file_paths'] = data.get('file_paths') or ([body['file_path']] if body['file_path'] else [])
    identity = [session, kind, tool or prompt or digest(json.dumps(body, sort_keys=True).encode())]
    if kind in {'Stop','AssistantResponse'}: identity.append(digest(json.dumps(body, sort_keys=True).encode()))
    if kind in {'SessionStart', 'SessionEnd'}:
        identity.append(str(time.time_ns()))  # lifecycle hooks may legitimately repeat on resume
    event = digest(json.dumps(identity).encode())
    with transaction(db):
        if db.execute('SELECT 1 FROM events WHERE id=?', (event,)).fetchone(): return False
        db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?)', (event, time.time(), kind, session, prompt, tool, json.dumps(body)))
        if kind == 'PreToolUse':
            targets = []
            for value in body['file_paths']:
                try:
                    candidate=Path(value)
                    if not candidate.is_absolute(): candidate=Path(data['cwd'])/candidate
                    targets.append(str(candidate.resolve().relative_to(Path(meta(db,'root')))))
                except ValueError: pass
            snapshot(db, event, 'unattributed: observed before tool started', include=targets)
            target = json.dumps(targets) if targets else None
            overlap = bool(db.execute('SELECT 1 FROM pending').fetchone())
            if overlap: db.execute('UPDATE pending SET overlap=1')
            db.execute('INSERT OR REPLACE INTO pending VALUES(?,?,?,?)', (tool or event,event,target,int(overlap)))
        elif kind in {'PostToolUse','PostToolUseFailure'}:
            start = db.execute('SELECT * FROM pending WHERE tool=?', (tool,)).fetchone()
            if not start:
                label, target = 'unattributed: no recorded tool-start boundary', None
            elif start['overlap']:
                label, target = 'ambiguous: overlapping tool windows', None
            elif body['tool_name'] in {'Edit','Write','StrReplace','ApplyPatch','apply_patch'} and start['path']:
                target = json.loads(start['path'])
                label = 'tool-linked: declared file changed in observed tool window'
            else:
                label, target = 'associated: changed during tool window, causality unproven', None
            snapshot(db,event,label,target, include=target)
            db.execute('DELETE FROM pending WHERE tool=?', (tool,))
        else:
            pending = bool(db.execute('SELECT 1 FROM pending').fetchone())
            snapshot(db,event,'ambiguous: tool still pending or recorder interrupted' if pending else 'unattributed: observed between tool boundaries')
    return True


def git_history(root):
    """Commits and branches read from git at render time; never recorded by hooks."""
    try:
        log = subprocess.run(['git', '-C', str(root), 'log', '--all', '--pretty=format:%H%x00%aI%x00%D%x00%s'],
                             capture_output=True, check=True)
        refs = subprocess.run(['git', '-C', str(root), 'for-each-ref', 'refs/heads', '--format=%(refname:short)'],
                              capture_output=True, check=True)
    except (subprocess.CalledProcessError, OSError):
        return [], []
    commits = []
    for line in os.fsdecode(log.stdout).splitlines():
        parts = line.split('\x00')
        if len(parts) >= 4 and parts[0]:
            try:
                at = datetime.datetime.fromisoformat(parts[1]).timestamp()
            except ValueError:
                continue
            commits.append({'hash': parts[0], 'at': at, 'refs': parts[2], 'subject': parts[3]})
    return commits, sorted(b for b in os.fsdecode(refs.stdout).splitlines() if b)


def thread_ordinals(db):
    ords = {}
    for r in db.execute("SELECT id,prompt FROM events WHERE kind='UserPromptSubmit' ORDER BY at"):
        key = r['prompt'] or r['id']
        if key not in ords:
            ords[key] = len(ords)
    return ords


def evolution(db):
    """SVG graph: prompts, tool windows and changes on the conversation lane, commits alongside."""
    esc = lambda s: html.escape(str(s or ''))
    root = meta(db, 'root')
    commits, branches = git_history(root) if root else ([], [])
    rows = db.execute('SELECT id,at,kind,session,prompt,tool,body FROM events ORDER BY at').fetchall()
    ords = thread_ordinals(db)
    windows, open_windows, sessions = [], {}, {}
    for r in rows:
        if r['kind'] == 'PreToolUse':
            open_windows[r['tool'] or str(r['at'])] = (r['at'], r['tool'] or r['id'])
        elif r['kind'] in {'PostToolUse', 'PostToolUseFailure'} and r['tool'] in open_windows:
            start, tool = open_windows.pop(r['tool'])
            windows.append((start, r['at'], tool))
        elif r['kind'] == 'SessionStart':
            sessions.setdefault(r['session'], [r['at'], None])[0] = r['at']
        elif r['kind'] == 'SessionEnd':
            sessions.setdefault(r['session'], [None, r['at']])[1] = r['at']
    entries = []
    for r in rows:
        body = json.loads(r['body'] or '{}')
        if r['kind'] == 'UserPromptSubmit' and body.get('prompt'):
            entries.append({'at': r['at'], 'lane': 0, 'kind': 'prompt', 'key': ('prompt', r['prompt'] or r['id']),
                            'label': 'Prompt: ' + str(body['prompt']),
                            'href': '#thread-' + str(ords[r['prompt'] or r['id']])})
        elif r['kind'] == 'PreToolUse':
            target = ', '.join(body.get('file_paths') or []) or 'no declared file'
            entries.append({'at': r['at'] + 0.0001, 'lane': 0, 'kind': 'tool', 'key': ('tool', r['tool'] or r['id']),
                            'label': 'Tool window: ' + str(body.get('tool_name') or 'unknown tool') + ' (' + target + ')',
                            'parent': ('prompt', r['prompt']) if r['prompt'] else None})
    for r in db.execute('SELECT c.id,c.event,c.path,c.evidence,e.at,e.tool,e.prompt FROM changes c LEFT JOIN events e ON c.event=e.id ORDER BY c.id').fetchall():
        parent = ('tool', r['tool']) if r['tool'] else (('prompt', r['prompt']) if r['prompt'] else None)
        entries.append({'at': (r['at'] or 0) + 0.0002, 'lane': 0, 'kind': 'change', 'key': ('change', str(r['id'])),
                        'label': str(r['path']) + ' changed', 'href': '#change-' + str(r['id']),
                        'sub': r['evidence'], 'parent': parent})
    for c in commits:
        parent = None
        for start, end, tool in windows:
            if start <= c['at'] <= end:
                parent = ('tool', tool)
                break
        if parent:
            label = 'inside a recorded tool window: possibly agent-made, not proven'
        else:
            in_session = any((s[0] or 0) <= c['at'] <= (s[1] or float('inf')) for s in sessions.values())
            label = 'during a recorded session, outside tool windows' if in_session else 'outside recorded agent sessions'
        entries.append({'at': c['at'], 'lane': 1, 'kind': 'commit', 'key': ('commit', c['hash']),
                        'label': c['hash'][:8] + ' ' + c['subject'] + ((' [' + c['refs'] + ']') if c['refs'] else ''),
                        'sub': label, 'parent': parent})
    colors = {'prompt': '#3a6ea5', 'tool': '#8a938e', 'change': '#3f7d54', 'commit': '#6b4fa1'}
    lane_x = [70, 560]
    row_h = 38
    has_conversation = any(e['lane'] == 0 for e in entries)
    svg = ''
    if entries:
        entries.sort(key=lambda e: e['at'])
        pos = {e['key']: i for i, e in enumerate(entries)}
        height = 30 + row_h * len(entries)
        parts = ['<svg viewBox="0 0 900 ' + str(height) + '" role="img" aria-label="Repository evolution graph">']
        parts.append('<line x1="70" y1="14" x2="70" y2="' + str(height - 8) + '" stroke="#d5ded8" stroke-width="2"/>')
        parts.append('<line x1="560" y1="14" x2="560" y2="' + str(height - 8) + '" stroke="#d5ded8" stroke-width="2"/>')
        parts.append('<text x="70" y="12" font-size="11" fill="#5a6b65" text-anchor="middle">conversation</text>')
        parts.append('<text x="560" y="12" font-size="11" fill="#5a6b65" text-anchor="middle">commits</text>')
        for i, e in enumerate(entries):
            parent = e.get('parent')
            if parent and parent in pos:
                j = pos[parent]
                dash = ' stroke-dasharray="4 3"' if e['kind'] == 'commit' else ''
                parts.append('<line x1="' + str(lane_x[entries[j]['lane']]) + '" y1="' + str(26 + row_h * j)
                             + '" x2="' + str(lane_x[e['lane']]) + '" y2="' + str(26 + row_h * i)
                             + '" stroke="#b9c6be" stroke-width="1.5"' + dash + '/>')
        for i, e in enumerate(entries):
            x = lane_x[e['lane']]
            y = 26 + row_h * i
            limit = 62 if e['lane'] == 0 else 44
            label = e['label']
            label = esc(label[:limit]) + ('&hellip;' if len(label) > limit else '')
            node = '<circle cx="' + str(x) + '" cy="' + str(y) + '" r="7" fill="' + colors[e['kind']] + '"/>'
            text = '<text x="' + str(x + 16) + '" y="' + str(y - 2) + '" font-size="13" fill="#1d2826">' + label + '</text>'
            sub = ''
            if e.get('sub'):
                sublimit = 90 if e['lane'] == 0 else 55
                sub = ('<text x="' + str(x + 16) + '" y="' + str(y + 13) + '" font-size="10" fill="#5a6b65">'
                       + esc(str(e['sub'])[:sublimit]) + '</text>')
            inner = node + text + sub
            if e.get('href'):
                inner = '<a href="' + e['href'] + '">' + inner + '</a>'
            parts.append(inner)
        parts.append('</svg>')
        svg = ''.join(parts)
    legend = ('<p class="legend"><span style="color:#3a6ea5">&#9679;</span> prompt &nbsp;'
              '<span style="color:#8a938e">&#9679;</span> tool window &nbsp;'
              '<span style="color:#3f7d54">&#9679;</span> recorded change &nbsp;'
              '<span style="color:#6b4fa1">&#9679;</span> git commit</p>')
    note = ('<p>' + (('Branches at render time: ' + esc(', '.join(branches)) + '. ') if branches else '')
            + 'Commits are read from git when this page was rendered, not from hook records. '
            'A commit inside a tool window is possibly agent-made, not proven. '
            'Click a prompt or change node to jump to its thread below.</p>')
    empty = ''
    if not has_conversation:
        empty = ('<div class="notice"><strong>No recorded agent activity yet.</strong> The hooks did not fire in a live '
                 'session (or no session ran), so only git commits appear above. After a recorded session, prompts, '
                 'tool windows and changes appear here connected to the commits they produced.</div>')
    return '<h2 id="graph">How this repository evolved</h2>' + note + legend + svg + empty


def render(db, output):
    esc = lambda s: html.escape(str(s or ''))
    ords = thread_ordinals(db)
    rows = db.execute('SELECT c.*,e.kind,e.session,e.prompt,e.body FROM changes c LEFT JOIN events e ON c.event=e.id ORDER BY c.id').fetchall()

    def changeblock(r):
        body = json.loads(r['body'] or '{}')
        return ('<article class="change" id="change-' + str(r['id']) + '" data-path="' + esc(r['path']) + '">'
                '<header><span class="badge">' + esc(r['evidence']) + '</span><h3>' + esc(r['path']) + '</h3>'
                '<small>CHANGE ' + str(r['id']) + ' / ' + esc(body.get('host', 'claude')) + ' / ' + esc(r['kind']) + '</small></header>'
                '<details open><summary>Recorded code change</summary><pre>' + esc(r['patch']) + '</pre></details>'
                '<details><summary>Recorded boundary evidence</summary><pre>' + esc(json.dumps(body, indent=2)) + '</pre>'
                '<p>Before ' + esc(r['before_hash'] or 'absent') + '<br>After ' + esc(r['after_hash'] or 'absent') + '</p></details></article>')

    by_prompt = {}
    for r in rows:
        by_prompt.setdefault(r['prompt'] or '', []).append(r)
    threads_html = []
    prompt_rows = db.execute("SELECT id,session,prompt,body FROM events WHERE kind='UserPromptSubmit' ORDER BY at").fetchall()
    for pr in prompt_rows:
        body = json.loads(pr['body'] or '{}')
        text = body.get('prompt')
        if not text:
            continue
        pid = pr['prompt'] or pr['id']
        answer = db.execute("SELECT body FROM events WHERE session=? AND prompt=? AND kind IN ('Stop','AssistantResponse') "
                            "AND json_extract(body,'$.last_assistant_message') IS NOT NULL ORDER BY at DESC LIMIT 1",
                            (pr['session'], pr['prompt'])).fetchone() if pr['prompt'] else None
        answer_text = json.loads(answer[0]).get('last_assistant_message') if answer else None
        changes = by_prompt.pop(pr['prompt'] or '', [])
        blocks = ''.join(changeblock(r) for r in changes)
        threads_html.append('<section class="thread" id="thread-' + str(ords[pid]) + '">'
                            '<div class="bubble user">' + esc(text) + '</div>'
                            + ('<div class="bubble agent">' + esc(answer_text) + '</div>' if answer_text
                               else '<div class="bubble agent muted">No final assistant response recorded for this prompt.</div>')
                            + (blocks or '<p class="muted">No file changes recorded around this prompt.</p>')
                            + '</section>')
    unlinked = ''.join(changeblock(r) for leftovers in by_prompt.values() for r in leftovers)
    n_changes = len(rows)
    root = meta(db, 'root')
    commits, _branches = git_history(root) if root else ([], [])
    summary = ('<p class="summary">' + str(len(threads_html)) + ' recorded prompt' + ('s' if len(threads_html) != 1 else '')
               + ', ' + str(n_changes) + ' recorded change' + ('s' if n_changes != 1 else '')
               + ', ' + str(len(commits)) + ' commit' + ('s' if len(commits) != 1 else '') + ' at render time.</p>')
    events = db.execute('SELECT kind,body FROM events ORDER BY at').fetchall()
    timeline = ''.join('<details><summary>' + esc(r['kind']) + '</summary><pre>' + esc(json.dumps(json.loads(r['body']), indent=2)) + '</pre></details>' for r in events)
    gaps = db.execute('SELECT gaps FROM snapshots ORDER BY id DESC LIMIT 1').fetchone()
    gaps_text = '\n'.join(json.loads(gaps[0])) if gaps else ''
    page = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"><title>Sylp / How this code came to be</title><style>*{box-sizing:border-box}body{margin:0;background:#f4f3ef;color:#1d2826;font:16px system-ui,sans-serif}main{max-width:960px;margin:auto;padding:44px 24px}.eyebrow{font:12px monospace;letter-spacing:.12em;color:#486a60}h1{font-size:40px;letter-spacing:-.04em;margin:12px 0}h2{margin:28px 0 8px;font-size:24px}h3{margin:10px 0 4px;font:600 17px monospace;overflow-wrap:anywhere}p{line-height:1.6}.summary{color:#5a6b65}.notice{border-left:3px solid #b67d30;padding:12px 18px;background:#fff8e9;margin:24px 0}.legend{font-size:13px;color:#5a6b65}svg{display:block;max-width:100%;height:auto;background:white;border:1px solid #d5ded8;border-radius:10px;margin:8px 0 16px}a{text-decoration:none}.thread{background:white;border:1px solid #d5ded8;border-radius:10px;margin:0 0 22px;padding:18px 20px}.bubble{max-width:82%;padding:10px 14px;border-radius:12px;margin:0 0 10px;white-space:pre-wrap;overflow-wrap:anywhere}.bubble.user{background:#e3ecf7}.bubble.agent{background:#e7efe9}.muted{color:#7d8a83;font-size:14px}.change{border-top:1px solid #e1e7e3;margin-top:10px;padding-top:8px}article.change header small{color:#5a6b65;font:11px monospace}.badge{font:11px monospace;background:#eaf0eb;padding:5px 7px;display:inline-block;max-width:100%;overflow-wrap:anywhere}details{border-top:1px solid #e1e7e3;padding:10px 0}summary{cursor:pointer;font-weight:600;font-size:14px}pre{font:13px/1.6 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7f4;padding:16px;border-radius:6px}details p{font:11px monospace;overflow-wrap:anywhere}input{padding:12px;width:100%;border:1px solid #a4b5ad;background:white;border-radius:7px;margin:6px 0 14px}footer{margin-top:40px;font-size:13px;color:#52695e}@media(max-width:700px){main{padding:24px 16px}h1{font-size:30px}.bubble{max-width:100%}}</style><main><div class="eyebrow">SYLP / PRIVATE PROVENANCE SPIKE</div><h1>How this code came to be.</h1><p>Every recorded change, connected to the prompt and tool action behind it. Evidence first. Unknowns stay unknown.</p>SUMMARY<div class="notice">Experimental local recording. This is not a verified live agent session. Tool-linked means temporal and named-file evidence, not guaranteed authorship. Nothing here is uploaded.</div>EVOLUTION<h2 id="threads">The conversations behind the changes</h2><label for="filter">Find a file</label><input id="filter" placeholder="e.g. retry.py">THREADSUNLINKED<h2>Recorded conversation boundaries</h2><p>Only submitted prompts, selected hook fields and final assistant text supplied to Stop are captured in this spike. Full transcript import and inline message-to-hunk navigation are not implemented.</p>TIMELINE<details><summary>Capture policy gaps</summary><pre>GAPS</pre></details><footer>Local SQLite + content-addressed snapshots. No model calls, telemetry, remote assets or repository commits. This exported HTML contains captured code and prompt text; treat it as private.</footer></main><script>document.getElementById('filter').addEventListener('input',e=>{for(const a of document.querySelectorAll('.change'))a.hidden=!a.dataset.path.toLowerCase().includes(e.target.value.toLowerCase())})</script></html>'''
    page = (page.replace('SUMMARY', summary)
                .replace('EVOLUTION', evolution(db))
                .replace('THREADS', ''.join(threads_html) or '<p class="muted">No recorded conversations yet.</p>')
                .replace('UNLINKED', ('<h3>Changes without a recorded prompt</h3>' + unlinked) if unlinked else '')
                .replace('TIMELINE', timeline)
                .replace('GAPS', esc(gaps_text or 'No policy gaps reported in last snapshot.')))
    target = Path(output)
    target.write_text(page)
    target.chmod(0o600)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=os.environ.get('SYLP_PROVENANCE_DB', str(Path.home()/'.sylp-provenance'/'history.sqlite3')))
    sub = parser.add_subparsers(dest='action', required=True)
    init = sub.add_parser('init'); init.add_argument('repo'); init.add_argument('--accept-local-capture',action='store_true',required=True)
    hook = sub.add_parser('hook'); hook.add_argument('--host', choices=['claude','codex','cursor'], default='claude')
    show = sub.add_parser('render'); show.add_argument('output')
    sub.add_parser('pause'); sub.add_parser('enable')
    args = parser.parse_args()
    db = connect(args.db)
    try:
        if args.action == 'init': initialize(db,args.repo)
        elif args.action == 'hook':
            raw = sys.stdin.buffer.read(1024*1024+1)
            if len(raw)>1024*1024: raise ValueError('Hook payload too large')
            from adapters import normalize
            record(db,normalize(args.host,json.loads(raw)))
            print('{}')  # Valid no-op hook response, including Codex Stop
        elif args.action == 'render': render(db,args.output)
        else: setmeta(db,'enabled', '0' if args.action=='pause' else '1')
    finally: db.close()


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        # Observational hook must not block or inject text into a coding session.
        print('sylp-provenance capture failed: '+type(exc).__name__+': '+str(exc),file=sys.stderr)
        if 'hook' in sys.argv: print('{}')
        sys.exit(0 if 'hook' in sys.argv else 1)
