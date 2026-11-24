#!/usr/bin/env python3
"""Private provenance spike. Python stdlib only; never runs model/tool commands."""
import argparse
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


def scan(db, root):
    result = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], capture_output=True, check=True)
    files, gaps, total = {}, [], 0
    names = sorted(set(os.fsdecode(result.stdout).split('\0')) - {''})
    if len(names) > 2000:
        gaps.append('Repository has more than 2000 candidate files; remaining files not captured.')
    for name in names[:2000]:
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
    return files, gaps


def previous(db):
    row = db.execute('SELECT files FROM snapshots ORDER BY id DESC LIMIT 1').fetchone()
    return json.loads(row[0]) if row else {}


def textfor(db, sha):
    return db.execute('SELECT content FROM blobs WHERE hash=?', (sha,)).fetchone()[0] if sha else ''


def snapshot(db, event, evidence='unattributed', target=None):
    root = Path(meta(db, 'root'))
    before = previous(db)
    after, gaps = scan(db, root)
    for name in sorted(before.keys() | after.keys()):
        old, new = before.get(name), after.get(name)
        if old == new:
            continue
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
        files, gaps = scan(db, root)
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
            snapshot(db, event, 'unattributed: observed before tool started')
            targets = []
            for value in body['file_paths']:
                try:
                    candidate=Path(value)
                    if not candidate.is_absolute(): candidate=Path(data['cwd'])/candidate
                    targets.append(str(candidate.resolve().relative_to(Path(meta(db,'root')))))
                except ValueError: pass
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
            snapshot(db,event,label,target)
            db.execute('DELETE FROM pending WHERE tool=?', (tool,))
        else:
            pending = bool(db.execute('SELECT 1 FROM pending').fetchone())
            snapshot(db,event,'ambiguous: tool still pending or recorder interrupted' if pending else 'unattributed: observed between tool boundaries')
    return True


def render(db, output):
    esc = lambda s: html.escape(str(s or ''))
    rows = db.execute('SELECT c.*,e.kind,e.session,e.prompt,e.body FROM changes c LEFT JOIN events e ON c.event=e.id ORDER BY c.id').fetchall()
    cards = []
    for r in rows:
        body = json.loads(r['body'] or '{}')
        prompt = db.execute("SELECT body FROM events WHERE session=? AND prompt=? AND kind='UserPromptSubmit' ORDER BY at DESC LIMIT 1", (r['session'], r['prompt'])).fetchone() if r['prompt'] else None
        prompt_text = json.loads(prompt[0]).get('prompt') if prompt else None
        answer = db.execute("SELECT body FROM events WHERE session=? AND prompt=? AND kind IN ('Stop','AssistantResponse') AND json_extract(body,'$.last_assistant_message') IS NOT NULL ORDER BY at DESC LIMIT 1", (r['session'],r['prompt'])).fetchone() if r['prompt'] else None
        answer_text = json.loads(answer[0]).get('last_assistant_message') if answer else None
        cards.append('<article data-path="'+esc(r['path'])+'"><header><span class="badge">'+esc(r['evidence'])+'</span><h2>'+esc(r['path'])+'</h2><small>CHANGE '+str(r['id'])+' / '+esc(body.get('host','claude'))+' / '+esc(r['kind'])+' / prompt '+esc((r['prompt'] or 'unknown')[:12])+'</small></header><p class="prompt">'+esc(prompt_text or 'No prompt captured for this change.')+'</p><details><summary>Recorded assistant response for this prompt</summary><p>'+esc(answer_text or 'No final assistant response captured.')+'</p></details><details open><summary>Recorded code change</summary><pre>'+esc(r['patch'])+'</pre></details><details><summary>Recorded boundary evidence</summary><pre>'+esc(json.dumps(body,indent=2))+'</pre><p>Before '+esc(r['before_hash'] or 'absent')+'<br>After '+esc(r['after_hash'] or 'absent')+'</p></details></article>')
    events = db.execute('SELECT kind,body FROM events ORDER BY at').fetchall()
    timeline = ''.join('<details><summary>'+esc(r['kind'])+'</summary><pre>'+esc(json.dumps(json.loads(r['body']),indent=2))+'</pre></details>' for r in events)
    gaps = db.execute('SELECT gaps FROM snapshots ORDER BY id DESC LIMIT 1').fetchone()
    gaps_text = '\n'.join(json.loads(gaps[0])) if gaps else ''
    page = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"><title>Sylp / Recorded code history</title><style>*{box-sizing:border-box}body{margin:0;background:#f4f3ef;color:#1d2826;font:16px system-ui,sans-serif}main{max-width:1120px;margin:auto;padding:44px 24px}.eyebrow{font:12px monospace;letter-spacing:.12em;color:#486a60}h1{font-size:40px;letter-spacing:-.04em;margin:12px 0}p{line-height:1.6}.notice{border-left:3px solid #b67d30;padding:12px 18px;background:#fff8e9;margin:24px 0}.layout{display:grid;grid-template-columns:240px 1fr;gap:24px}aside{position:sticky;top:20px;align-self:start}input{padding:12px;width:100%;border:1px solid #a4b5ad;background:white;border-radius:7px}article{background:white;border:1px solid #d5ded8;border-radius:10px;margin:0 0 24px;overflow:hidden}article header{padding:20px 24px 12px}h2{margin:12px 0 4px;font:600 20px monospace;overflow-wrap:anywhere}small{color:#5a6b65;font:11px monospace}.badge{font:11px monospace;background:#eaf0eb;padding:5px 7px;display:inline-block;max-width:100%;overflow-wrap:anywhere}.prompt{padding:0 24px;color:#204f43}details{border-top:1px solid #e1e7e3;padding:14px 24px}summary{cursor:pointer;font-weight:600;font-size:14px}pre{font:13px/1.6 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7f4;padding:16px;border-radius:6px}details p{font:11px monospace;overflow-wrap:anywhere}aside p{font-size:13px;color:#5a6b65}footer{margin-top:40px;font-size:13px;color:#52695e}@media(max-width:700px){main{padding:24px 16px}h1{font-size:30px}.layout{display:block}aside{position:static;margin-bottom:20px}article header,details{padding:16px}.prompt{padding:0 16px}}</style><main><div class="eyebrow">SYLP / PRIVATE PROVENANCE SPIKE</div><h1>From code change to conversation.</h1><p>Inspect the prompt and tool boundary recorded around a file change. Evidence first. Unknowns stay unknown.</p><div class="notice">Experimental local recording. This is not a verified live agent session. Claude, Codex and Cursor hook simulations drive real file changes in the included fixture. Tool-linked means temporal and named-file evidence, not guaranteed authorship.</div><div class="layout"><aside><label for="filter">Find a file</label><p><input id="filter" placeholder="e.g. retry.py"></p><p>Changes are immutable historical diffs. They do not follow today's line numbers after rewrites or rebases.</p><p>Nothing is uploaded by the recorder. This exported HTML contains captured code and prompt text. Treat it as private.</p><details><summary>Capture policy gaps</summary><pre>GAPS</pre></details></aside><section>CARDS</section></div><h2>Recorded conversation boundaries</h2><p>Only submitted prompts, selected hook fields and final assistant text supplied to Stop are captured in this spike. Full transcript import and inline message-to-hunk navigation are not implemented.</p>TIMELINE<footer>Local SQLite + content-addressed snapshots. No model calls, telemetry, remote assets or repository commits.</footer></main><script>document.getElementById('filter').addEventListener('input',e=>{for(const a of document.querySelectorAll('article'))a.hidden=!a.dataset.path.toLowerCase().includes(e.target.value.toLowerCase())})</script></html>'''
    page = page.replace('GAPS',esc(gaps_text or 'No policy gaps reported in last snapshot.')).replace('CARDS',''.join(cards) or '<p>No file changes captured.</p>').replace('TIMELINE',timeline)
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
