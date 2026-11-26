"""Per-turn diffs, line-level blame join and anchored notes."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import recorder as r


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t'] + list(args),
                          check=True, capture_output=True, text=True).stdout


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'repo'; self.root.mkdir()
        git(self.root, 'init', '-q')
        self.a = self.root / 'a.py'; self.a.write_text('one\ntwo\nthree\n')
        git(self.root, 'add', '.'); git(self.root, 'commit', '-qm', 'initial')
        self.db = r.connect(Path(self.tmp.name) / 'h.db'); self.addCleanup(self.db.close)
        r.initialize(self.db, self.root)

    def edit(self, prompt_id, prompt, tool, content, name='Edit'):
        base = {'session_id': 's1', 'cwd': str(self.root), 'prompt_id': prompt_id}
        r.record(self.db, dict(base, hook_event_name='UserPromptSubmit', prompt=prompt))
        tl = dict(base, tool_use_id=tool, tool_name=name, tool_input={'file_path': str(self.a)})
        r.record(self.db, dict(tl, hook_event_name='PreToolUse'))
        self.a.write_text(content)
        r.record(self.db, dict(tl, hook_event_name='PostToolUse'))


class DeltaTests(Base):
    def test_turns_and_since(self):
        self.edit('p1', 'make two uppercase', 't1', 'one\nTWO\nthree\n')
        self.edit('p2', 'make three uppercase', 't2', 'one\nTWO\nTHREE\n')
        turns = r.turn_diffs(self.db)
        self.assertEqual([t['prompt'] for t in turns], ['make two uppercase', 'make three uppercase'])
        text = r.render_turns(self.db)
        self.assertIn('Turn 2: make three uppercase', text)
        self.assertIn('+THREE', text)
        self.assertNotIn('+TWO', text.split('## Turn 2')[1])
        since = r.render_turns(self.db, since=1)
        self.assertIn('+TWO', since); self.assertIn('+THREE', since)
        self.assertNotIn('+TWO', r.render_turns(self.db, since=2))
        self.assertIn('[redacted]', r.render_turns(self.db, include_prompts=False))
        with self.assertRaises(SystemExit): r.render_turns(self.db, since=3)

    def test_edits_that_cancel_say_so(self):
        self.edit('p1', 'break it', 't1', 'one\nbroken\nthree\n')
        self.edit('p1', 'break it', 't2', 'one\ntwo\nthree\n')
        self.assertIn('no net change', r.render_turns(self.db))

    def test_blame_join_exact_and_unmatched(self):
        self.edit('p1', 'make two uppercase', 't1', 'one\nTWO\nthree\n')
        git(self.root, 'commit', '-qam', 'uppercase two')
        result = r.blame_file(self.db, self.root, 'a.py')
        groups = {(g['start'], g['end']): g for g in result['groups']}
        self.assertEqual(groups[(1, 1)]['matched'], 'none')   # initial commit: no recorded session produced it
        self.assertEqual(groups[(2, 2)]['matched'], 'exact')
        self.assertEqual(groups[(2, 2)]['prompt'], 'make two uppercase')
        text = r.render_blame(result)
        self.assertIn('make two uppercase', text)
        self.assertIn('no recorded session produced this exact content', text)
        self.assertNotIn('make two uppercase', r.render_blame(result, include_prompts=False))

    def test_blame_attributes_each_line_to_its_own_turn(self):
        self.edit('p1', 'make two uppercase', 't1', 'one\nTWO\nthree\n')
        self.edit('p2', 'make three uppercase', 't2', 'one\nTWO\nTHREE\n')
        git(self.root, 'commit', '-qam', 'uppercase both')
        groups = {g['start']: g for g in r.blame_file(self.db, self.root, 'a.py')['groups']}
        self.assertEqual(groups[2]['prompt'], 'make two uppercase')
        self.assertEqual(groups[3]['prompt'], 'make three uppercase')
        self.assertEqual(groups[1]['matched'], 'none')

    def test_notes_anchor_and_redact(self):
        self.edit('p1', 'make two uppercase', 't1', 'one\nTWO\nthree\n')
        change_id = self.db.execute('SELECT id FROM changes').fetchone()[0]
        r.add_note(self.db, 'turn', 1, 'needs a test')
        r.add_note(self.db, 'change', change_id, 'check the casing rule')
        text = r.render_turns(self.db)
        self.assertIn('note: needs a test', text); self.assertIn('check the casing rule', text)
        redacted = r.render_turns(self.db, include_prompts=False)
        self.assertNotIn('needs a test', redacted); self.assertNotIn('check the casing rule', redacted)
        git(self.root, 'commit', '-qam', 'uppercase two')
        self.assertEqual(r.why_lookup(self.db, self.root, 'a.py', 2)['notes'], ['check the casing rule'])
        summary = r.pr_summary(self.db, self.root, 'HEAD~1')
        self.assertIn('note: check the casing rule', summary)
        self.assertNotIn('check the casing rule', r.pr_summary(self.db, self.root, 'HEAD~1', include_prompts=False))
        with self.assertRaises(SystemExit): r.add_note(self.db, 'turn', 9, 'x')
        with self.assertRaises(SystemExit): r.add_note(self.db, 'change', 999, 'x')

    def test_cli_and_mcp(self):
        self.edit('p1', 'make two uppercase', 't1', 'one\nTWO\nthree\n')
        git(self.root, 'commit', '-qam', 'uppercase two')
        db = str(Path(self.tmp.name) / 'h.db')
        run = lambda *a: subprocess.run([sys.executable, str(ROOT / 'recorder.py'), '--db', db] + list(a), cwd=self.root,
                                        capture_output=True, text=True, check=True).stdout
        self.assertIn('Turn 1', run('turns'))
        run('note', 'add', '--turn', '1', '--text', 'from the cli')
        self.assertIn('from the cli', run('note', 'list'))
        self.assertIn('make two uppercase', run('blame', 'a.py', '--repo', str(self.root)))
        names = [t['name'] for t in r.MCP_TOOLS]
        self.assertEqual(names, ['why', 'pr_summary', 'turn_diffs', 'blame'])


if __name__ == '__main__':
    unittest.main()


class PageTests(Base):
    """Same fixture session; checks the history page and the served dashboard."""
    def two_turns(self):
        self.edit('p1', 'make two uppercase', 't1', 'one\nTWO\nthree\n')
        self.edit('p2', 'make three uppercase THREADS BLAME', 't2', 'one\nTWO\nTHREE\n')
        git(self.root, 'commit', '-qam', 'uppercase both')

    def test_page_has_turns_blame_and_notes(self):
        self.two_turns()
        r.add_note(self.db, 'turn', 2, 'needs <b>review</b>')
        page = r.build_page(self.db)
        self.assertIn('What each prompt changed', page); self.assertIn('Who wrote each line', page)
        self.assertIn('id="turn-2"', page); self.assertIn('Everything changed since this turn', page)
        self.assertIn('needs &lt;b&gt;review&lt;/b&gt;', page); self.assertNotIn('<b>review</b>', page)
        self.assertIn('make three uppercase THREADS BLAME', page)  # recorded text containing placeholder words survives
        self.assertIn('<a href="#turn-1">Turn 1</a>', page)
        self.assertNotIn('<form', page)  # static export has no note form

    def test_served_dashboard_adds_note_with_token(self):
        import threading, socket, urllib.request, urllib.error, urllib.parse, re as _re
        from unittest import mock
        self.two_turns(); self.db.close()
        self.db = r.connect(Path(self.tmp.name) / 'h.db')
        folder = Path(self.tmp.name) / 'dbs'; folder.mkdir()
        (folder / 'x.sqlite3').write_bytes((Path(self.tmp.name) / 'h.db').read_bytes())
        sock = socket.socket(); sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]; sock.close()
        with mock.patch('webbrowser.open', lambda *a, **k: None):
            threading.Thread(target=r.serve, args=(str(folder), port), daemon=True).start()
            import time; time.sleep(0.8)
        base = 'http://127.0.0.1:' + str(port)
        page = urllib.request.urlopen(base + '/repo/0').read().decode()
        token = _re.search(r'name="token" value="([0-9a-f]+)"', page).group(1)
        post = lambda data, headers=None: urllib.request.urlopen(urllib.request.Request(
            base + '/repo/0/note', urllib.parse.urlencode(data).encode(), headers or {}))
        good = {'token': token, 'kind': 'turn', 'target': '2', 'body': 'from dashboard'}
        with self.assertRaises(urllib.error.HTTPError) as bad: post(dict(good, token='0' * 32))
        self.assertEqual(bad.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as bad: post(good, {'Origin': 'http://evil.example'})
        self.assertEqual(bad.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as bad: post(dict(good, kind='change', target='9999'))
        self.assertEqual(bad.exception.code, 400)
        post(good)
        self.assertIn('from dashboard', urllib.request.urlopen(base + '/repo/0').read().decode())
