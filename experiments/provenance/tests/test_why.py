import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('recorder', HERE / 'recorder.py')
r = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(r)


class WhyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'repo'; self.root.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        self.file = self.root / 'retry.py'; self.file.write_text('attempts = 1\n')
        self.commit(self.file, 'initial')
        self.dbpath = Path(self.tmp.name) / 'state' / 'history.sqlite3'
        self.db = r.connect(str(self.dbpath)); self.addCleanup(self.db.close)
        r.initialize(self.db, self.root)

    def commit(self, path, message):
        subprocess.run(['git', '-C', str(self.root), 'add', str(path)], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=t', '-c', 'user.email=t@t',
                        'commit', '-q', '-m', message], check=True)

    def hook(self, kind, tool='t1', prompt_id='p1', name='Edit', **kw):
        data = dict(cwd=str(self.root), session_id='session1', prompt_id=prompt_id, hook_event_name=kind,
                    tool_use_id=tool, tool_name=name, tool_input={'file_path': str(self.file)})
        data.update(kw); return r.record(self.db, data)

    def record_edit(self, text, prompt_text='Use three attempts'):
        self.hook('UserPromptSubmit', **{'prompt': prompt_text})
        self.hook('PreToolUse'); self.file.write_text(text); self.hook('PostToolUse')

    def test_exact_match_after_commit(self):
        self.record_edit('attempts = 3\n'); self.commit(self.file, 'three attempts')
        found = r.why_lookup(self.db, self.root, 'retry.py', 1)
        self.assertEqual(found['matched'], 'exact')
        self.assertEqual(found['prompt'], 'Use three attempts')
        self.assertEqual(found['tool'], 'Edit')
        self.assertTrue(found['evidence'].startswith('tool-linked'))
        self.assertEqual(found['commit']['subject'], 'three attempts')

    def test_exact_match_without_line(self):
        self.record_edit('attempts = 3\n'); self.commit(self.file, 'three attempts')
        found = r.why_lookup(self.db, self.root, 'retry.py')
        self.assertEqual(found['matched'], 'exact')

    def test_no_recorded_session(self):
        self.file.write_text('attempts = 9\n'); self.commit(self.file, 'manual')
        found = r.why_lookup(self.db, self.root, 'retry.py', 1)
        self.assertEqual(found['matched'], 'none')
        self.assertIn('no recorded session', found['reason'])

    def test_nearest_not_exact(self):
        self.record_edit('attempts = 3\n'); self.commit(self.file, 'three attempts')
        self.file.write_text('attempts = 4\n'); self.commit(self.file, 'four attempts, unrecorded')
        found = r.why_lookup(self.db, self.root, 'retry.py', 1)
        self.assertEqual(found['matched'], 'nearest')
        self.assertEqual(found['prompt'], 'Use three attempts')
        self.assertTrue(any('nearest earlier' in c for c in found['caveats']))

    def test_pr_summary_labels_and_flags(self):
        self.record_edit('attempts = 3\n'); self.commit(self.file, 'three attempts')
        (self.root / 'other.py').write_text('x = 1\n'); self.commit(self.root / 'other.py', 'other file')
        out = r.pr_summary(self.db, self.root, 'HEAD~2', 'HEAD')
        self.assertIn('retry.py', out)
        self.assertIn('tool-linked', out)
        self.assertIn('Use three attempts', out)
        self.assertIn('NO RECORDED PROMPT', out)
        self.assertIn('other.py', out)
        self.assertIn('private capture', out)

    def test_pr_summary_redacts(self):
        self.record_edit('attempts = 3\n'); self.commit(self.file, 'three attempts')
        out = r.pr_summary(self.db, self.root, 'HEAD~1', 'HEAD', include_prompts=False)
        self.assertNotIn('Use three attempts', out)
        self.assertIn('[redacted', out)


class McpTests(unittest.TestCase):
    def test_handshake_list_and_call(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / 'repo'; root.mkdir()
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        f = root / 'a.py'; f.write_text('a = 1\n')
        subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(root), '-c', 'user.name=t', '-c', 'user.email=t@t',
                        'commit', '-q', '-m', 'init'], check=True)
        db = r.connect(str(Path(tmp.name) / 'history.sqlite3'))
        r.initialize(db, root)
        r.record(db, dict(cwd=str(root), session_id='s', prompt_id='p1', hook_event_name='UserPromptSubmit', prompt='Make it two'))
        r.record(db, dict(cwd=str(root), session_id='s', prompt_id='p1', hook_event_name='PreToolUse',
                          tool_use_id='t1', tool_name='Edit', tool_input={'file_path': str(f)}))
        f.write_text('a = 2\n')
        r.record(db, dict(cwd=str(root), session_id='s', prompt_id='p1', hook_event_name='PostToolUse',
                          tool_use_id='t1', tool_name='Edit'))
        db.close()
        subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(root), '-c', 'user.name=t', '-c', 'user.email=t@t',
                        'commit', '-q', '-m', 'two'], check=True)
        env = dict(os.environ, SYLP_PROVENANCE_DB=str(Path(tmp.name) / 'history.sqlite3'))
        rpc = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
             'params': {'name': 'why', 'arguments': {'path': 'a.py', 'line': 1, 'repo': str(root)}}},
        ]
        proc = subprocess.run([sys.executable, str(HERE / 'recorder.py'), 'mcp'], env=env,
                              input='\n'.join(json.dumps(m) for m in rpc), text=True, capture_output=True, check=True)
        replies = [json.loads(x) for x in proc.stdout.strip().splitlines()]
        self.assertEqual(len(replies), 3)
        self.assertEqual(replies[0]['result']['serverInfo']['name'], 'sylp-provenance')
        names = [t['name'] for t in replies[1]['result']['tools']]
        self.assertEqual(names, ['why', 'pr_summary'])
        payload = json.loads(replies[2]['result']['content'][0]['text'])
        self.assertEqual(payload['matched'], 'exact')
        self.assertEqual(payload['prompt'], 'Make it two')

    def test_mcp_error_for_unknown_tool(self):
        proc = subprocess.run([sys.executable, str(HERE / 'recorder.py'), 'mcp'],
                              input=json.dumps({'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
                                                'params': {'name': 'nope', 'arguments': {}}}),
                              text=True, capture_output=True, check=True)
        reply = json.loads(proc.stdout.strip())
        self.assertEqual(reply['error']['code'], -32602)


if __name__ == '__main__':
    unittest.main()
