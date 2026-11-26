#!/usr/bin/env python3
"""Sylp provenance recorder. Python stdlib only; never runs model/tool commands."""
import argparse
import datetime
import difflib
import hashlib
import html
import json
import os
import re
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
    CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY, kind TEXT, target TEXT, body TEXT, at REAL);
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

def default_db_for(root):
    """Per-repository capture database, so one install can record across repos."""
    digest = hashlib.sha1(str(root).encode()).hexdigest()[:12]
    return Path.home() / '.sylp-provenance' / 'repos' / (Path(str(root)).name + '-' + digest + '.sqlite3')

def resolve_db(given, hint):
    """Precedence: --db flag, SYLP_PROVENANCE_DB env, per-repo path from a repo hint, legacy default."""
    if given:
        return Path(given).expanduser()
    env = os.environ.get('SYLP_PROVENANCE_DB')
    if env:
        return Path(env).expanduser()
    if hint:
        try:
            return default_db_for(gitroot(hint))
        except Exception:
            pass
    return Path.home() / '.sylp-provenance' / 'history.sqlite3'


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
            elif body['tool_name'] in {'Edit','Write','StrReplace','ApplyPatch','apply_patch','write_to_file','replace_file_content','multi_replace_file_content'} and start['path']:
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
        log = subprocess.run(['git', '-C', str(root), 'log', '--all', '--pretty=format:%H%x00%aI%x00%D%x00%s%x00%an'],
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
                at = datetime.datetime.fromisoformat(parts[1].replace('Z', '+00:00')).timestamp()
            except ValueError:
                continue
            commits.append({'hash': parts[0], 'at': at, 'refs': parts[2], 'subject': parts[3],
                            'author': parts[4] if len(parts) > 4 else ''})
    return commits, sorted(b for b in os.fsdecode(refs.stdout).splitlines() if b)


def thread_ordinals(db):
    ords = {}
    for r in db.execute("SELECT id,prompt FROM events WHERE kind='UserPromptSubmit' ORDER BY at"):
        key = r['prompt'] or r['id']
        if key not in ords:
            ords[key] = len(ords)
    return ords


def usage_block(db, total_commits):
    """What the hooks can honestly count. Token and cost data are not in hook payloads."""
    esc = lambda s: html.escape(str(s or ''))
    sessions = db.execute("SELECT count(DISTINCT session) FROM events WHERE kind='SessionStart'").fetchone()[0]
    prompts = db.execute("SELECT count(*) FROM events WHERE kind='UserPromptSubmit'").fetchone()[0]
    tools = db.execute("SELECT json_extract(body,'$.tool_name') AS n, count(*) AS c FROM events "
                       "WHERE kind='PreToolUse' GROUP BY n ORDER BY c DESC").fetchall()
    changes = db.execute('SELECT count(*) FROM changes').fetchone()[0]
    span = db.execute('SELECT min(at), max(at) FROM events').fetchone()
    tool_text = ', '.join((str(t['n'] or 'unknown') + ' ' + str(t['c'])) for t in tools) or 'none'
    when = ''
    if span and span[0]:
        fmt = '%d %b %Y %H:%M'
        when = ('; ' + datetime.datetime.fromtimestamp(span[0]).strftime(fmt)
                + ' to ' + datetime.datetime.fromtimestamp(span[1]).strftime(fmt))
    return ('<p class="summary">Recorded usage: ' + str(sessions) + ' session' + ('s' if sessions != 1 else '')
            + ', ' + str(prompts) + ' prompt' + ('s' if prompts != 1 else '')
            + ', tool calls: ' + esc(tool_text)
            + ', ' + str(changes) + ' recorded change' + ('s' if changes != 1 else '')
            + ', ' + str(total_commits) + ' commit' + ('s' if total_commits != 1 else '') + ' at render time'
            + esc(when) + '. Token counts and cost are not exposed by host hooks, so they are not shown.</p>')

def evolution(db):
    """SVG graph: prompts, tool windows and changes on the conversation lane, commits alongside."""
    esc = lambda s: html.escape(str(s or ''))
    root = meta(db, 'root')
    commits, branches = git_history(root) if root else ([], [])
    rows = db.execute('SELECT id,at,kind,session,prompt,tool,body FROM events ORDER BY at').fetchall()
    commits.sort(key=lambda c: c['at'])
    hidden = 0
    MAX_COMMITS = 25
    if len(commits) > MAX_COMMITS:
        if rows:
            t0, t1 = rows[0]['at'] - 3600, rows[-1]['at'] + 3600
            core = [c for c in commits if t0 <= c['at'] <= t1]
            before = [c for c in commits if c['at'] < t0][-5:]
            after = [c for c in commits if c['at'] > t1][:5]
            keep = before + core + after
        else:
            keep = commits[-15:]
        hidden = len(commits) - len(keep)
        commits = keep
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
                        'label': c['hash'][:8] + ' ' + c['subject'] + ((' - ' + c['author']) if c.get('author') else '')
                                 + ((' [' + c['refs'] + ']') if c['refs'] else ''),
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
            + 'Commits are read from git when this page was rendered, not from hook records, with their git author. '
            'A commit inside a tool window is possibly agent-made, not proven; a commit by anyone whose session was '
            'not recorded here is outside recorded sessions, whatever its author. '
            + ((str(hidden) + ' older commits not shown. ') if hidden else '')
            + 'Click a prompt or change node to jump to its thread below.</p>')
    empty = ''
    if not has_conversation:
        empty = ('<div class="notice"><strong>No recorded agent activity yet.</strong> The hooks did not fire in a live '
                 'session (or no session ran), so only git commits appear above. After a recorded session, prompts, '
                 'tool windows and changes appear here connected to the commits they produced.</div>')
    return '<h2 id="graph">How this repository evolved</h2>' + note + legend + svg + empty


NOTE_LIMIT = 2000
EXTRA_CSS = ('.turn{background:white;border:1px solid #d5ded8;border-radius:10px;margin:0 0 18px;padding:16px 20px}'
             '.turn h3{font:600 16px system-ui,sans-serif;margin:0 0 6px}.turn .ord{font:12px monospace;color:#486a60}'
             '.since{margin-top:8px}.note{background:#fff8e9;border-left:3px solid #b67d30;padding:8px 12px;margin:8px 0;white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px}'
             '.noteform{display:flex;gap:8px;margin:8px 0}.noteform input[type=text]{margin:0;flex:1}'
             '.noteform button{border:1px solid #486a60;background:#eaf0eb;border-radius:7px;padding:0 14px;cursor:pointer}'
             '.grp{border:1px solid #d5ded8;border-radius:8px;margin:12px 0;background:white}.grp header{padding:8px 12px;background:#f5f7f4;font-size:13px;border-bottom:1px solid #e1e7e3}'
             '.grp.none header{background:#f4f3ef;color:#7d8a83}.grp pre{margin:0;border-radius:0;background:white}'
             '.diff{display:block;min-height:1.6em;white-space:pre-wrap}.add{color:#1d6b3a;background:#eaf6ee}.del{color:#a1302b;background:#fbeceb}')


def diff_html(patch):
    esc = html.escape
    out = []
    for line in patch.splitlines():
        cls = 'add' if line.startswith('+') and not line.startswith('+++') else 'del' if line.startswith('-') and not line.startswith('---') else ''
        out.append('<span class="diff ' + cls + '">' + esc(line) + '</span>')
    return '<pre>' + ''.join(out) + '</pre>'


def note_form(ui, kind, target, label='Add a note'):
    if not ui: return ''
    return ('<form class="noteform" method="post" action="' + ui['action'] + '"><input type="hidden" name="token" value="' + ui['token'] + '">'
            '<input type="hidden" name="kind" value="' + kind + '"><input type="hidden" name="target" value="' + html.escape(str(target)) + '">'
            '<input type="text" name="body" maxlength="' + str(NOTE_LIMIT) + '" placeholder="' + label + ' (stays in your local database)" required>'
            '<button type="submit">Add note</button></form>')


def notes_html(db, kind, target):
    return ''.join('<div class="note">' + html.escape(b) + '</div>' for b in notes_for(db, kind, target))


def turns_html(db, ui=None):
    esc = html.escape
    turns = turn_diffs(db)
    if not turns:
        return '<p class="muted">No recorded file changes yet.</p>'
    out = []
    for t in turns:
        title = t['prompt'] if t['prompt'] else '(no recorded prompt for this turn)'
        block = ['<section class="turn" id="turn-' + str(t['turn']) + '"><span class="ord">TURN ' + str(t['turn']) + '</span>'
                 '<h3>' + esc(title.replace('\n', ' ')[:300]) + '</h3>', notes_html(db, 'turn', t['key'])]
        for path, f in t['files'].items():
            patch = net_diff(db, path, f['before'], f['after'])
            block.append('<details open><summary>' + esc(path) + ' <span class="badge">' + esc('; '.join(f['evidence'])) + '</span></summary>'
                         + (diff_html(patch) if patch else '<p class="muted">No net change: edits cancelled out.</p>')
                         + ''.join(notes_html(db, 'change', c) for c in f['change_ids'])
                         + note_form(ui, 'change', f['change_ids'][-1], 'Add a note on this file change') + '</details>')
        block.append(note_form(ui, 'turn', t['turn'], 'Add a note on this turn'))
        files = since_turn(db, turns, t['turn'])
        if t['turn'] < len(turns) or len(files) > len(t['files']):
            parts = []
            for path, f in files.items():
                patch = net_diff(db, path, f['before'], f['after'])
                parts.append('<h4>' + esc(path) + ' <span class="muted">(turns ' + ', '.join(map(str, f['turns'])) + ')</span></h4>'
                             + (diff_html(patch) if patch else '<p class="muted">No net change.</p>'))
            block.append('<details class="since"><summary>Everything changed since this turn (through turn ' + str(len(turns)) + ')</summary>' + ''.join(parts) + '</details>')
        block.append('</section>')
        out.append(''.join(block))
    return ''.join(out)


def blame_html(db, root, turns):
    esc = html.escape
    if not root: return '<p class="muted">No repository root recorded.</p>'
    owner = {}
    for t in turns:
        for f in t['files'].values():
            for c in f['change_ids']: owner[c] = t['turn']
    paths = []
    for t in turns:
        for path in t['files']:
            if path not in paths: paths.append(path)
    out = []
    for path in paths[:20]:
        if git_text(root, ['ls-files', '--error-unmatch', '--', path]) is None: continue
        try:
            result = blame_file(db, root, path)
        except SystemExit:
            continue
        groups = ''
        for g in result['groups'][:400]:
            span = 'L' + str(g['start']) + ('-' + str(g['end']) if g['end'] != g['start'] else '')
            subject = esc((g.get('commit_info') or {}).get('subject', ''))
            if g['matched'] == 'exact':
                turn = owner.get(g.get('change_id'))
                who = ('<a href="#turn-' + str(turn) + '">Turn ' + str(turn) + '</a> ' if turn else '') + esc((g.get('prompt') or '(no prompt association)').replace('\n', ' ')[:140]) \
                    + ' <span class="badge">' + esc(g.get('evidence')) + '</span>'
                cls = 'grp'
            else:
                who = esc(g.get('reason', 'no recorded match')); cls = 'grp none'
            numbered = ''.join('<span class="diff">' + str(g['start'] + i).rjust(4) + '  ' + esc(line) + '</span>' for i, line in enumerate(g['lines'][:200]))
            groups += ('<div class="' + cls + '"><header>' + span + ' &middot; ' + esc(g['commit'][:8]) + ' ' + subject + ' &middot; ' + who + '</header><pre>' + numbered + '</pre></div>')
        out.append('<details' + (' open' if not out else '') + '><summary>' + esc(path) + '</summary>' + groups + '</details>')
    if not out: return '<p class="muted">No recorded file is tracked in git yet, so there is nothing to blame.</p>'
    return ''.join(out) + '<p class="muted">A line is linked only when its commit holds content exactly matching a recorded snapshot and a recorded change added that line. Blank lines are shown with the group above. Repeated identical lines can be attributed to the wrong recorded change.</p>'


def build_page(db, ui=None):
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
                '<p>Before ' + esc(r['before_hash'] or 'absent') + '<br>After ' + esc(r['after_hash'] or 'absent') + '</p></details>'
                + notes_html(db, 'change', r['id']) + note_form(ui, 'change', r['id']) + '</article>')

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
    page = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"><title>REPOTITLE</title><style>*{box-sizing:border-box}body{margin:0;background:#f4f3ef;color:#1d2826;font:16px system-ui,sans-serif}main{max-width:960px;margin:auto;padding:44px 24px}.eyebrow{font:12px monospace;letter-spacing:.12em;color:#486a60}h1{font-size:40px;letter-spacing:-.04em;margin:12px 0}h2{margin:28px 0 8px;font-size:24px}h3{margin:10px 0 4px;font:600 17px monospace;overflow-wrap:anywhere}p{line-height:1.6}.repo{font-size:18px;margin:0 0 14px}.repo strong{font-family:monospace}.summary{color:#5a6b65}.notice{border-left:3px solid #b67d30;padding:12px 18px;background:#fff8e9;margin:24px 0}.legend{font-size:13px;color:#5a6b65}svg{display:block;max-width:100%;height:auto;background:white;border:1px solid #d5ded8;border-radius:10px;margin:8px 0 16px}a{text-decoration:none}.thread{background:white;border:1px solid #d5ded8;border-radius:10px;margin:0 0 22px;padding:18px 20px}.bubble{max-width:82%;padding:10px 14px;border-radius:12px;margin:0 0 10px;white-space:pre-wrap;overflow-wrap:anywhere}.bubble.user{background:#e3ecf7}.bubble.agent{background:#e7efe9}.muted{color:#7d8a83;font-size:14px}.change{border-top:1px solid #e1e7e3;margin-top:10px;padding-top:8px}article.change header small{color:#5a6b65;font:11px monospace}.badge{font:11px monospace;background:#eaf0eb;padding:5px 7px;display:inline-block;max-width:100%;overflow-wrap:anywhere}details{border-top:1px solid #e1e7e3;padding:10px 0}summary{cursor:pointer;font-weight:600;font-size:14px}pre{font:13px/1.6 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7f4;padding:16px;border-radius:6px}details p{font:11px monospace;overflow-wrap:anywhere}input{padding:12px;width:100%;border:1px solid #a4b5ad;background:white;border-radius:7px;margin:6px 0 14px}footer{margin-top:40px;font-size:13px;color:#52695e}@media(max-width:700px){main{padding:24px 16px}h1{font-size:30px}.bubble{max-width:100%}}</style><main><div class="eyebrow">SYLP / PROVENANCE</div><h1>How this code came to be.</h1><p class="repo">Repository: <strong>REPONAME</strong> <span class="muted">REPOPATH</span></p><p>Every recorded change, connected to the prompt and tool action behind it. Evidence first. Unknowns stay unknown.</p>SUMMARYUSAGE<div class="notice">Tool-linked means temporal and named-file evidence, not guaranteed authorship. Nothing here is uploaded.</div>EVOLUTION<h2 id="turns">What each prompt changed</h2><p>Net change per file for each prompt, and everything changed since any turn. Evidence labels describe association, not proven authorship.</p>TURNS<h2 id="blame">Who wrote each line</h2><p>git blame joined to recorded prompts. Lines are linked only on an exact content match.</p>BLAME<h2 id="threads">The conversations behind the changes</h2><label for="filter">Find a file</label><input id="filter" placeholder="e.g. retry.py">THREADSUNLINKED<h2>Recorded conversation boundaries</h2><p>Only submitted prompts, selected hook fields and final assistant text supplied to Stop are captured. Full transcript import and inline message-to-hunk navigation are not implemented.</p>TIMELINE<details><summary>Capture policy gaps</summary><pre>GAPS</pre></details><footer>Local SQLite + content-addressed snapshots. No model calls, telemetry, remote assets or repository commits. This exported HTML contains captured code and prompt text; treat it as private.</footer></main><script>document.getElementById('filter').addEventListener('input',e=>{for(const a of document.querySelectorAll('.change'))a.hidden=!a.dataset.path.toLowerCase().includes(e.target.value.toLowerCase())})</script></html>'''
    root = meta(db, 'root') or ''
    repo_name = Path(root).name if root else 'unknown repository'
    values = {'REPOTITLE': esc('Sylp / ' + repo_name + ' / How this code came to be'), 'REPONAME': esc(repo_name), 'REPOPATH': esc(root),
              'SUMMARY': summary, 'USAGE': usage_block(db, len(commits)), 'EVOLUTION': evolution(db),
              'TURNS': turns_html(db, ui), 'BLAME': blame_html(db, root, turn_diffs(db)),
              'THREADS': ''.join(threads_html) or '<p class="muted">No recorded conversations yet.</p>',
              'UNLINKED': ('<h3>Changes without a recorded prompt</h3>' + unlinked) if unlinked else '',
              'TIMELINE': timeline, 'GAPS': esc(gaps_text or 'No policy gaps reported in last snapshot.')}
    # One pass, so recorded text that happens to contain a placeholder word is never substituted.
    page = re.sub('|'.join(values), lambda m: values[m.group(0)], page)
    return page.replace('</style>', EXTRA_CSS + '</style>', 1)

def render(db, output):
    target = Path(output)
    target.write_text(build_page(db))
    target.chmod(0o600)

def serve(directory, port):
    """Read-only, localhost-only live view over every capture database in a directory."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    directory = Path(directory).expanduser()

    def load():
        found = []
        for path in sorted(directory.rglob('*.sqlite3')):
            try:
                db = sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)
                db.row_factory = sqlite3.Row
                found.append((path, meta(db, 'root'), db))
            except Exception:
                continue
        return found

    esc = lambda s: html.escape(str(s or ''))
    import secrets, hmac
    from urllib.parse import parse_qs
    token = secrets.token_hex(16)
    allowed_hosts = {'127.0.0.1:' + str(port), 'localhost:' + str(port)}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            m = re.match(r'^/repo/(\d+)/note$', self.path)
            dbs = load()
            def fail(code, msg):
                self.send_response(code); self.send_header('Content-Type', 'text/plain'); self.end_headers(); self.wfile.write(msg.encode())
            origin = self.headers.get('Origin')
            if self.headers.get('Host') not in allowed_hosts or (origin and origin not in {'http://' + h for h in allowed_hosts}):
                return fail(403, 'forbidden')
            if not m or int(m.group(1)) >= len(dbs): return fail(404, 'not found')
            length = int(self.headers.get('Content-Length') or 0)
            if length > 8192: return fail(413, 'note too large')
            form = parse_qs(self.rfile.read(length).decode('utf-8', 'replace'))
            get = lambda k: (form.get(k) or [''])[0]
            if not hmac.compare_digest(get('token'), token): return fail(403, 'bad token; reload the page')
            body, kind, target = get('body').strip(), get('kind'), get('target')
            if not body or len(body) > NOTE_LIMIT or kind not in ('turn', 'change') or not target.isdigit():
                return fail(400, 'invalid note')
            path = dbs[int(m.group(1))][0]
            writable = connect(str(path))
            try:
                add_note(writable, kind, target, body)
                anchor = ('turn-' + target) if kind == 'turn' else 'change-' + target
            except SystemExit as exc:
                return fail(400, str(exc))
            finally:
                writable.close()
            self.send_response(303); self.send_header('Location', '/repo/' + m.group(1) + '#' + anchor); self.end_headers()

        def send_html(self, text):
            body = text.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            dbs = load()
            if self.path == '/':
                items = []
                for i, (path, root, db) in enumerate(dbs):
                    try:
                        prompts = db.execute("SELECT count(*) FROM events WHERE kind='UserPromptSubmit'").fetchone()[0]
                        changes = db.execute('SELECT count(*) FROM changes').fetchone()[0]
                    except Exception:
                        prompts = changes = 0
                    items.append('<li><a href="/repo/' + str(i) + '">' + esc(root or path.name) + '</a> '
                                 '<span class="muted">' + str(prompts) + ' prompts, ' + str(changes) + ' changes</span></li>')
                listing = ''.join(items) or ('<li class="muted">No capture databases in ' + esc(str(directory))
                                             + ' yet. Run install.py or recorder.py init first.</li>')
                self.send_html('<!doctype html><html lang="en"><meta charset="utf-8"><meta http-equiv="refresh" content="10">'
                               '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Sylp serve</title>'
                               '<style>body{margin:0;background:#f4f3ef;color:#1d2826;font:16px system-ui,sans-serif}'
                               'main{max-width:760px;margin:auto;padding:44px 24px}h1{font-size:32px;letter-spacing:-.03em}'
                               'li{margin:10px 0}a{color:#1d5158}.muted{color:#7d8a83;font-size:14px}</style>'
                               '<main><div style="font:12px monospace;letter-spacing:.12em;color:#486a60">SYLP / PROVENANCE</div>'
                               '<h1>Recorded repositories</h1>'
                               '<p class="muted">Read-only local view, auto-refreshes every 10s. Nothing leaves this machine.</p>'
                               '<ul>' + listing + '</ul></main></html>')
                return
            m = re.match(r'^/repo/(\d+)$', self.path)
            if m and int(m.group(1)) < len(dbs):
                path, _root, db = dbs[int(m.group(1))]
                page = build_page(db, {'token': token, 'action': '/repo/' + m.group(1) + '/note'})
                page = page.replace("script-src 'unsafe-inline'", "script-src 'unsafe-inline'; form-action 'self'", 1)
                # Refresh to show live captures, but never while a note is being typed.
                page = page.replace('</script>', "setInterval(()=>{if(![...document.querySelectorAll('.noteform input[type=text]')].some(i=>i.value||i===document.activeElement))location.reload()},10000)</script>", 1)
                page = page.replace('</main>', '<p class="muted"><a href="/">All recorded repositories</a> &middot; served from '
                                    + esc(str(path)) + '. Read-only except for notes you add here.</p></main>', 1)
                self.send_html(page)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(('127.0.0.1', port), Handler)
    url = 'http://127.0.0.1:' + str(port) + '/'
    print('Serving provenance view (read-only except for notes you add) at ' + url + ' (Ctrl+C to stop)')
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def turn_key(session, prompt):
    return json.dumps([session or '', prompt or ''])


def turn_diffs(db):
    """Net change per recorded turn (one prompt in one session), in order of first change.
    Each file's diff runs from its content before the turn's first recorded change to its content after the last.
    Evidence labels are kept per file; a turn with no recorded prompt is shown as such, never guessed."""
    rows = db.execute('SELECT c.id,c.path,c.before_hash,c.after_hash,c.evidence,e.session,e.prompt FROM changes c '
                      'JOIN events e ON c.event=e.id ORDER BY c.id').fetchall()
    turns, index = [], {}
    for r in rows:
        key = turn_key(r['session'], r['prompt'])
        if key not in index:
            index[key] = {'turn': len(turns) + 1, 'key': key, 'session': r['session'], 'prompt_id': r['prompt'] or '',
                          'prompt': prompt_text_for(db, r['prompt']) if r['prompt'] else None, 'files': {}}
            turns.append(index[key])
        f = index[key]['files'].setdefault(r['path'], {'before': r['before_hash'], 'after': r['after_hash'],
                                                       'evidence': [], 'change_ids': []})
        f['after'] = r['after_hash']
        f['change_ids'].append(r['id'])
        if r['evidence'] not in f['evidence']: f['evidence'].append(r['evidence'])
    return turns


def net_diff(db, path, before, after):
    if before == after: return ''
    return ''.join(difflib.unified_diff(textfor(db, before).splitlines(True), textfor(db, after).splitlines(True),
                                        fromfile='before/' + path, tofile='after/' + path))


def since_turn(db, turns, n):
    """Combined change from before turn n's first change to the latest recorded state."""
    files = {}
    for t in turns:
        if t['turn'] < n: continue
        for path, f in t['files'].items():
            g = files.setdefault(path, {'before': f['before'], 'after': f['after'], 'evidence': [], 'turns': []})
            g['after'] = f['after']
            g['turns'].append(t['turn'])
            for e in f['evidence']:
                if e not in g['evidence']: g['evidence'].append(e)
    return files


def notes_for(db, kind, target):
    return [r['body'] for r in db.execute('SELECT body FROM notes WHERE kind=? AND target=? ORDER BY id', (kind, str(target)))]


def render_turns(db, since=None, include_prompts=True):
    turns = turn_diffs(db)
    if not turns: return 'No recorded changes yet.\n'
    out = []
    if since is not None:
        if since < 1 or since > len(turns): raise SystemExit('turn must be between 1 and ' + str(len(turns)))
        files = since_turn(db, turns, since)
        out.append('# Changes since turn ' + str(since) + ' (net, through turn ' + str(len(turns)) + ')')
        for path, f in files.items():
            patch = net_diff(db, path, f['before'], f['after'])
            out.append('\n## ' + path + ' (turns ' + ', '.join(map(str, f['turns'])) + '; evidence: ' + '; '.join(f['evidence']) + ')')
            out.append(patch if patch else 'no net change (edits cancelled out)')
        return '\n'.join(out) + '\n'
    out.append('# Per-turn changes (net per file; evidence labels describe association, not proven authorship)')
    for t in turns:
        text = t['prompt'] if t['prompt'] else '(no recorded prompt for this turn)'
        if t['prompt'] and not include_prompts: text = '[redacted]'
        out.append('\n## Turn ' + str(t['turn']) + ': ' + text.replace('\n', ' ')[:200])
        if include_prompts:
            for note in notes_for(db, 'turn', t['key']): out.append('note: ' + note)
        for path, f in t['files'].items():
            out.append('\n### ' + path + ' (evidence: ' + '; '.join(f['evidence']) + ')')
            if include_prompts:
                for cid in f['change_ids']:
                    for note in notes_for(db, 'change', cid): out.append('note (change ' + str(cid) + '): ' + note)
            patch = net_diff(db, path, f['before'], f['after'])
            out.append(patch if patch else 'no net change (edits cancelled out)')
    return '\n'.join(out) + '\n'


def blame_file(db, root, path):
    """Line-level join: git blame each line, then match the blamed commit's file content to a recorded snapshot.
    Only exact content matches are reported as linked; everything else says what is missing."""
    root = Path(root).resolve()
    rel = os.path.relpath(str(Path(path).resolve()), str(root)) if os.path.isabs(str(path)) else str(path)
    out = git_text(root, ['blame', '--porcelain', '--', rel])
    if out is None: raise SystemExit('git blame failed for ' + rel + '; is it tracked?')
    lines, commit, final = [], None, 0
    for raw in out.splitlines():
        if raw.startswith('\t'):
            lines.append({'line': final, 'commit': commit, 'text': raw[1:]})
        else:
            parts = raw.split(' ')
            if len(parts[0]) == 40 and all(c in '0123456789abcdef' for c in parts[0]) and len(parts) >= 3:
                commit, final = parts[0], int(parts[2])
    cache = {}
    def commit_link(sha):
        if sha in cache: return cache[sha]
        info = {'matched': 'none'}
        if set(sha) == {'0'}:
            info['reason'] = 'uncommitted line'
        else:
            info['commit_info'] = commit_subject(root, sha)
            blob = git_text(root, ['rev-parse', sha + ':' + rel])
            row = match_change_to_blob(db, rel, blob.strip() if blob else None)
            if row: info['row'] = row
            else: info['reason'] = 'no recorded session produced this exact content'
        cache[sha] = info
        return info
    added = {}
    def adder(row, text):
        """Latest recorded change on this path, up to the commit's matching change, whose patch added this exact line."""
        if not text.strip(): return None
        for c in reversed([r for r in changes_for_path(db, rel) if r['id'] <= row['id']]):
            if c['id'] not in added:
                patch = db.execute('SELECT patch FROM changes WHERE id=?', (c['id'],)).fetchone()[0] or ''
                added[c['id']] = {l[1:] for l in patch.splitlines(True) if l.startswith('+') and not l.startswith('+++')}
            if text + '\n' in added[c['id']] or text in added[c['id']]: return c
        return None
    groups = []
    for l in lines:
        base = commit_link(l['commit'])
        info = {k: v for k, v in base.items() if k != 'row'}
        if 'row' in base:
            hit = adder(base['row'], l['text'])
            if hit:
                info = dict(describe_change(db, hit), matched='exact', commit_info=base.get('commit_info'))
            else:
                info = {'matched': 'none', 'commit_info': base.get('commit_info'),
                        'reason': 'commit content matches a recorded snapshot, but no recorded change added this line (or it is repeated text)'}
        if 'row' in base and not l['text'].strip() and groups and groups[-1]['commit'] == l['commit'] \
                and groups[-1]['matched'] == 'exact' and groups[-1]['end'] + 1 == l['line']:
            groups[-1]['end'] = l['line']; groups[-1]['lines'].append(l['text'])  # blank line: shown with the group above
            continue
        key = (l['commit'], info.get('change_id'), info['matched'], info.get('reason'))
        if groups and groups[-1]['key'] == key and groups[-1]['end'] + 1 == l['line']:
            groups[-1]['end'] = l['line']; groups[-1]['lines'].append(l['text'])
        else:
            groups.append(dict(info, commit=l['commit'], key=key, start=l['line'], end=l['line'], lines=[l['text']]))
    for g in groups: g.pop('key')
    return {'path': rel, 'groups': groups, 'caveats': ['Evidence labels describe recorded association, not proven authorship.',
            'A line is linked only when its commit holds content exactly matching a recorded snapshot, and the line text was added by a recorded change. Blank lines are shown with the group above. Repeated identical lines can be attributed to the wrong recorded change.']}


def render_blame(result, include_prompts=True):
    out = ['# Blame with provenance: ' + result['path']]
    for g in result['groups']:
        span = str(g['start']) + ('-' + str(g['end']) if g['end'] != g['start'] else '')
        subject = (g.get('commit_info') or {}).get('subject', '')
        head = 'lines ' + span + ': ' + g['commit'][:8]
        if g['matched'] == 'exact':
            prompt = g.get('prompt') or '(no prompt association)'
            if g.get('prompt') and not include_prompts: prompt = '[redacted]'
            out.append(head + ' ' + str(subject) + ' | prompt: ' + prompt.replace('\n', ' ')[:160] + ' | evidence: ' + str(g.get('evidence')))
        else:
            out.append(head + ' ' + str(subject) + ' | ' + g.get('reason', 'no recorded match'))
    out.extend('note: ' + c for c in result['caveats'])
    return '\n'.join(out) + '\n'


def add_note(db, kind, target, body):
    if kind == 'turn':
        turns = turn_diffs(db)
        if not 1 <= int(target) <= len(turns): raise SystemExit('turn must be between 1 and ' + str(len(turns)))
        target = turns[int(target) - 1]['key']
    elif kind == 'change':
        if not db.execute('SELECT 1 FROM changes WHERE id=?', (int(target),)).fetchone(): raise SystemExit('no such change id')
    db.execute('INSERT INTO notes(kind,target,body,at) VALUES(?,?,?,?)', (kind, str(target), body, time.time()))


def git_blob_id(text):
    """The git SHA-1 blob id for file content, so recorded snapshots can be matched to commits exactly."""
    raw = text.encode('utf-8', 'replace')
    return hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\x00' + raw).hexdigest()


def git_text(root, args):
    result = subprocess.run(['git', '-C', str(root)] + args, capture_output=True)
    if result.returncode != 0:
        return None
    return os.fsdecode(result.stdout)


def commit_subject(root, commit):
    out = git_text(root, ['show', '-s', '--format=%ct%x00%an%x00%s', commit])
    if not out:
        return None
    stamp, author, subject = (out.strip().split('\x00') + ['', '', ''])[:3]
    return {'hash': commit, 'at': float(stamp or 0), 'author': author, 'subject': subject}


def prompt_text_for(db, prompt_id):
    row = db.execute("SELECT body FROM events WHERE kind='UserPromptSubmit' AND (prompt=? OR id=?) "
                     'ORDER BY at DESC LIMIT 1', (prompt_id, prompt_id)).fetchone()
    return json.loads(row[0]).get('prompt') if row else None


def tool_name_for(db, tool_id):
    row = db.execute("SELECT body FROM events WHERE kind='PreToolUse' AND tool=? LIMIT 1", (tool_id,)).fetchone()
    return json.loads(row[0]).get('tool_name') if row else None


def answer_text_for(db, session, prompt_id):
    row = db.execute("SELECT body FROM events WHERE session=? AND prompt=? AND kind IN ('Stop','AssistantResponse') "
                     "AND json_extract(body,'$.last_assistant_message') IS NOT NULL ORDER BY at DESC LIMIT 1",
                     (session, prompt_id)).fetchone()
    return json.loads(row[0]).get('last_assistant_message') if row else None


def describe_change(db, row):
    return {'change_id': row['id'], 'evidence': row['evidence'], 'path': row['path'],
            'prompt': prompt_text_for(db, row['prompt']) if row['prompt'] else None,
            'tool': tool_name_for(db, row['tool']) if row['tool'] else None,
            'assistant': answer_text_for(db, row['session'], row['prompt']) if row['prompt'] else None}


def changes_for_path(db, rel):
    return db.execute('SELECT c.id,c.event,c.path,c.after_hash,c.evidence,e.at,e.tool,e.prompt,e.session '
                      'FROM changes c LEFT JOIN events e ON c.event=e.id WHERE c.path=? ORDER BY c.id', (rel,)).fetchall()


def match_change_to_blob(db, rel, blob_id):
    """Exact content match between a recorded change's after-snapshot and a git blob. None when no exact match."""
    if not blob_id:
        return None
    for row in changes_for_path(db, rel):
        if row['after_hash'] and git_blob_id(textfor(db, row['after_hash'])) == blob_id:
            return row
    return None


def blame_commit(root, rel, line):
    out = git_text(root, ['blame', '--porcelain', '-L', str(line) + ',' + str(line), '--', rel])
    if not out or not out.strip():
        return None
    commit = out.split(' ', 1)[0]
    return None if set(commit) == {'0'} else commit


def why_lookup(db, root, path, line=None):
    """Answer 'why is this here' for a file or line: exact commit-to-prompt match when the content allows it."""
    root = Path(root).resolve()
    rel = os.path.relpath(str(Path(path).resolve()), str(root)) if os.path.isabs(str(path)) else str(path)
    caveats = ['Evidence labels describe recorded association, not proven authorship.',
               'Matching compares recorded snapshot content to git blobs (SHA-1 repos only).']
    result = {'path': rel, 'line': line, 'matched': 'none', 'caveats': caveats}
    if line is not None:
        commit = blame_commit(root, rel, line)
        if commit is None:
            result['reason'] = 'line is uncommitted or outside git history; nothing to attribute'
            return result
    else:
        out = git_text(root, ['log', '-1', '--format=%H', '--', rel])
        commit = out.strip() if out and out.strip() else None
        if commit is None:
            result['reason'] = 'path is not in git history; nothing to attribute'
            return result
    result['commit'] = commit_subject(root, commit)
    blob = git_text(root, ['rev-parse', commit + ':' + rel])
    blob = blob.strip() if blob else None
    if blob is None:
        caveats.append('the path was deleted in this commit, so no blob exists to match')
    hit = match_change_to_blob(db, rel, blob)
    if hit:
        result.update(describe_change(db, hit))
        result['matched'] = 'exact'
        result['notes'] = notes_for(db, 'change', hit['id'])
        return result
    # Git timestamps have 1-second resolution, so allow slack before calling a recorded change "earlier".
    prior = [r for r in changes_for_path(db, rel) if r['at'] and result['commit'] and r['at'] <= result['commit']['at'] + 120]
    if prior:
        result.update(describe_change(db, prior[-1]))
        result['matched'] = 'nearest'
        caveats.append('no recorded change produced this exact content; showing the nearest earlier recorded change on this path')
        return result
    result['reason'] = ('no recorded session produced this exact content'
                        + ('' if changes_for_path(db, rel) else ' and no recorded change exists for this path'))
    return result


def pr_summary(db, root, base, head='HEAD', include_prompts=True):
    """Markdown provenance summary for a commit range: evidence per changed file, honest flags for gaps."""
    root = Path(root).resolve()
    span = base + '..' + head
    names = git_text(root, ['diff', '--name-status', base, head])
    if names is None:
        raise SystemExit('git diff failed for range ' + span + '; check the refs')
    lines = ['# PR provenance: ' + span, '',
             '> Recorded evidence from this machine only. Prompt text below is private capture: review it before '
             'posting, and re-run with --no-prompts to redact. Evidence labels describe recorded association, '
             'not proven authorship.', '']
    flagged, linked = [], 0
    for entry in names.strip().splitlines():
        if not entry.strip():
            continue
        parts = entry.split('\t')
        status, rel = parts[0], parts[-1]
        hits = []
        log = git_text(root, ['log', '--format=%H', span, '--', rel])
        for commit in (log.strip().splitlines() if log else []):
            blob = git_text(root, ['rev-parse', commit + ':' + rel])
            row = match_change_to_blob(db, rel, blob.strip() if blob else None)
            if row and row not in [h[1] for h in hits]:
                hits.append((commit, row))
        lines.append('## ' + rel + ' (' + status + ')')
        if hits:
            linked += 1
            for commit, row in hits:
                info = describe_change(db, row)
                lines.append('- ' + (commit_subject(root, commit) or {}).get('subject', commit)[:60]
                             + ' - evidence: ' + str(info['evidence']))
                if info.get('tool'):
                    lines.append('  - tool: ' + str(info['tool']))
                if include_prompts:
                    for note in notes_for(db, 'change', row['id']):
                        lines.append('  - note: ' + note.replace(chr(10), ' ')[:300])
                prompt = info.get('prompt')
                if prompt:
                    prompt = prompt if include_prompts else '[redacted: run without --no-prompts to include prompt text]'
                    lines.append('  - prompt: ' + prompt.replace(chr(10), ' ')[:300])
                elif row['prompt'] is None:
                    lines.append('  - recorded change without a prompt association: ' + str(info['evidence']))
        else:
            flagged.append(rel)
            lines.append('- NO RECORDED PROMPT for this file in ' + span
                         + '. It was written outside recorded sessions (or capture was off). Review it manually.')
        lines.append('')
    lines.append('---')
    lines.append(str(linked) + ' file(s) with recorded evidence, ' + str(len(flagged))
                 + ' file(s) without. Files without recorded evidence are not necessarily human-written; '
                   'capture may simply not have seen them.')
    return '\n'.join(lines) + '\n'


MCP_TOOLS = [
    {'name': 'why',
     'description': 'Why is this file or line here: the recorded prompt, tool action and evidence label behind it. '
                    'Matches recorded snapshots to git blobs exactly; reports "none" instead of guessing.',
     'inputSchema': {'type': 'object', 'properties': {
         'path': {'type': 'string', 'description': 'file path, absolute or relative to the repo'},
         'line': {'type': 'integer', 'description': 'optional 1-based line number'},
         'repo': {'type': 'string', 'description': 'repository root; defaults to the server working directory'}},
         'required': ['path']}},
    {'name': 'pr_summary',
     'description': 'Markdown provenance summary for a commit range: per changed file, the recorded prompt and '
                    'evidence label, with explicit flags for files with no recorded prompt. Contains private prompt '
                    'text unless include_prompts is false; review before posting anywhere.',
     'inputSchema': {'type': 'object', 'properties': {
         'base': {'type': 'string', 'description': 'base ref, e.g. main'},
         'head': {'type': 'string', 'description': 'head ref, default HEAD'},
         'repo': {'type': 'string', 'description': 'repository root; defaults to the server working directory'},
         'include_prompts': {'type': 'boolean', 'description': 'include recorded prompt text; default true'}},
         'required': ['base']}},
    {'name': 'turn_diffs',
     'description': 'Net file changes per recorded turn (prompt), or everything changed since a given turn. '
                    'Evidence labels per file; turns without a recorded prompt are shown as such. Contains private prompt text unless include_prompts is false.',
     'inputSchema': {'type': 'object', 'properties': {
         'since': {'type': 'integer', 'description': 'optional 1-based turn number: show the combined change since that turn'},
         'repo': {'type': 'string', 'description': 'repository root; defaults to the server working directory'},
         'include_prompts': {'type': 'boolean', 'description': 'include recorded prompt text and notes; default true'}}}},
    {'name': 'blame',
     'description': 'git blame for a file joined to recorded sessions: per line range, the commit and the recorded prompt '
                    'and evidence label when the commit content exactly matches a recorded snapshot; otherwise says why not.',
     'inputSchema': {'type': 'object', 'properties': {
         'path': {'type': 'string', 'description': 'file path, absolute or relative to the repo'},
         'repo': {'type': 'string', 'description': 'repository root; defaults to the server working directory'},
         'include_prompts': {'type': 'boolean', 'description': 'include recorded prompt text; default true'}},
         'required': ['path']}}]


def mcp_reply(msg_id, result=None, error=None):
    out = {'jsonrpc': '2.0', 'id': msg_id}
    if error:
        out['error'] = error
    else:
        out['result'] = result
    return out


def mcp_call(arguments, which):
    repo = arguments.get('repo') or os.getcwd()
    db_path = resolve_db(None, repo)
    if not db_path.exists():
        return {'content': [{'type': 'text', 'text': 'No sylp provenance database for ' + str(repo)
                            + ' (looked for ' + str(db_path) + '). Run recorder.py init for that repo first.'}],
                'isError': True}
    db = connect(str(db_path))
    try:
        if which == 'why':
            text = json.dumps(why_lookup(db, repo, arguments['path'], arguments.get('line')), indent=2)
        elif which == 'turn_diffs':
            text = render_turns(db, arguments.get('since'), arguments.get('include_prompts', True))
        elif which == 'blame':
            text = render_blame(blame_file(db, repo, arguments['path']), arguments.get('include_prompts', True))
        else:
            text = pr_summary(db, repo, arguments['base'], arguments.get('head') or 'HEAD',
                              arguments.get('include_prompts', True))
    except Exception as exc:
        return {'content': [{'type': 'text', 'text': 'sylp-provenance ' + which + ' failed: '
                            + type(exc).__name__ + ': ' + str(exc)}], 'isError': True}
    finally:
        db.close()
    return {'content': [{'type': 'text', 'text': text}]}


def mcp_main():
    """Minimal MCP stdio server: newline-delimited JSON-RPC, stdlib only."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            sys.stdout.write(json.dumps(mcp_reply(None, error={'code': -32700, 'message': 'parse error'})) + '\n')
            sys.stdout.flush()
            continue
        method = msg.get('method', '')
        msg_id = msg.get('id')
        if msg_id is None:  # notification, e.g. notifications/initialized
            continue
        if method == 'initialize':
            resp = mcp_reply(msg_id, {'protocolVersion': '2024-11-05',
                                      'capabilities': {'tools': {'listChanged': False}},
                                      'serverInfo': {'name': 'sylp-provenance', 'version': '0.1.0'}})
        elif method == 'ping':
            resp = mcp_reply(msg_id, {})
        elif method == 'tools/list':
            resp = mcp_reply(msg_id, {'tools': MCP_TOOLS})
        elif method == 'tools/call':
            params = msg.get('params') or {}
            name = params.get('name', '')
            if name in ('why', 'pr_summary', 'turn_diffs', 'blame'):
                resp = mcp_reply(msg_id, mcp_call(params.get('arguments') or {}, name))
            else:
                resp = mcp_reply(msg_id, error={'code': -32602, 'message': 'unknown tool: ' + name})
        else:
            resp = mcp_reply(msg_id, error={'code': -32601, 'message': 'method not found: ' + method})
        sys.stdout.write(json.dumps(resp) + '\n')
        sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=None,
                        help='capture database path; default is a per-repository database under ~/.sylp-provenance/repos/')
    sub = parser.add_subparsers(dest='action', required=True)
    init = sub.add_parser('init'); init.add_argument('repo'); init.add_argument('--accept-local-capture',action='store_true',required=True)
    hook = sub.add_parser('hook'); hook.add_argument('--host', choices=['claude','codex','cursor','antigravity'], default='claude')
    hook.add_argument('--event', default=None, help='hook event name, for hosts whose payload does not carry it')
    show = sub.add_parser('render'); show.add_argument('output')
    srv = sub.add_parser('serve'); srv.add_argument('--dir', default=str(Path.home()/'.sylp-provenance')); srv.add_argument('--port', type=int, default=8777)
    sub.add_parser('pause'); sub.add_parser('enable')
    why = sub.add_parser('why'); why.add_argument('path'); why.add_argument('--line', type=int, default=None)
    why.add_argument('--repo', default='.'); why.add_argument('--json', action='store_true')
    pr = sub.add_parser('pr-summary'); pr.add_argument('base'); pr.add_argument('head', nargs='?', default='HEAD')
    pr.add_argument('--repo', default='.'); pr.add_argument('--no-prompts', action='store_true')
    tn = sub.add_parser('turns', help='net file changes per recorded turn'); tn.add_argument('--since', type=int, default=None)
    tn.add_argument('--no-prompts', action='store_true')
    bl = sub.add_parser('blame', help='git blame joined to recorded sessions'); bl.add_argument('path')
    bl.add_argument('--repo', default='.'); bl.add_argument('--no-prompts', action='store_true')
    nt = sub.add_parser('note', help='add or list your notes on a turn or change')
    nt.add_argument('verb', choices=['add', 'list']); nt.add_argument('--turn', type=int); nt.add_argument('--change', type=int)
    nt.add_argument('--text')
    sub.add_parser('mcp')
    args = parser.parse_args()
    if args.action == 'mcp':
        mcp_main()
        return
    if args.action == 'serve':
        serve(args.dir, args.port)
        return
    if args.action == 'hook':
        raw = sys.stdin.buffer.read(1024*1024+1)
        if len(raw)>1024*1024: raise ValueError('Hook payload too large')
        from adapters import normalize
        data = normalize(args.host, json.loads(raw), args.event)
        if data is not None:
            db = connect(str(resolve_db(args.db, data.get('cwd'))))
            try:
                if not meta(db, 'root'):
                    # Installing the plugin is the opt-in: first hook in a repo initializes
                    # that repo's own database automatically. `pause` stops capture per repo.
                    initialize(db, gitroot(data.get('cwd', '.')))
                record(db, data)
            finally:
                db.close()
        # Valid no-op hook response, including Codex Stop. Antigravity requires a decision on PreToolUse; allow defers to the default policy.
        print(json.dumps({'decision': 'allow'}) if args.host == 'antigravity' and args.event == 'PreToolUse' else '{}')
        return
    hint = args.repo if args.action in ('init', 'why', 'pr-summary', 'blame') else os.getcwd()
    db = connect(str(resolve_db(args.db, hint)))
    try:
        if args.action == 'init': initialize(db,args.repo)
        elif args.action == 'why':
            found = why_lookup(db, args.repo, args.path, args.line)
            if args.json:
                print(json.dumps(found, indent=2))
            else:
                print(found['matched'] + ': ' + found['path'] + ((' line ' + str(found['line'])) if found['line'] else ''))
                if found.get('commit'): print('commit ' + found['commit']['hash'][:8] + ' ' + found['commit']['subject'])
                if found.get('evidence'): print('evidence: ' + str(found['evidence']))
                if found.get('tool'): print('tool: ' + str(found['tool']))
                if found.get('prompt'): print('prompt: ' + str(found['prompt']))
                if found.get('reason'): print(found['reason'])
                for note in found['caveats']: print('note: ' + note)
        elif args.action == 'pr-summary':
            sys.stdout.write(pr_summary(db, args.repo, args.base, args.head, not args.no_prompts))
        elif args.action == 'turns':
            sys.stdout.write(render_turns(db, args.since, not args.no_prompts))
        elif args.action == 'blame':
            sys.stdout.write(render_blame(blame_file(db, args.repo, args.path), not args.no_prompts))
        elif args.action == 'note':
            if args.verb == 'list':
                for r in db.execute('SELECT * FROM notes ORDER BY id'): print(str(r['id']) + ' [' + r['kind'] + ' ' + r['target'] + '] ' + r['body'])
            else:
                text = args.text
                if not text or (args.turn is None) == (args.change is None): raise SystemExit('note add needs --text and exactly one of --turn N or --change ID')
                add_note(db, 'turn' if args.turn is not None else 'change', args.turn if args.turn is not None else args.change, text)
        elif args.action == 'render':
            if not meta(db, 'root'):
                repos_dir = Path.home()/'.sylp-provenance'/'repos'
                available = sorted(repos_dir.glob('*.sqlite3')) if repos_dir.exists() else []
                suffix = ('; available databases: ' + ', '.join(str(p) for p in available)) if available else ''
                raise SystemExit('This database has no recorded repository. Run render from inside the recorded repo, or pass --db' + suffix)
            render(db,args.output)
        else: setmeta(db,'enabled', '0' if args.action=='pause' else '1')
    finally: db.close()


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        # Observational hook must not block or inject text into a coding session.
        print('sylp-provenance capture failed: '+type(exc).__name__+': '+str(exc),file=sys.stderr)
        if 'hook' in sys.argv: print('{}')
        sys.exit(0 if 'hook' in sys.argv else 1)
