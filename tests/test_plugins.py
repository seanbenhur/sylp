"""Guard the committed plugin dirs: no drift from the shared code, valid manifests/hooks."""
import json
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

EVENTS = {
    'claude': ['SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'PostToolUseFailure', 'Stop', 'SessionEnd'],
    'codex': ['SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop', 'SessionEnd'],
    'cursor': ['sessionStart', 'beforeSubmitPrompt', 'preToolUse', 'postToolUse', 'postToolUseFailure', 'afterAgentResponse', 'stop', 'sessionEnd'],
}
MANIFEST_DIR = {'claude': '.claude-plugin', 'codex': '.codex-plugin', 'cursor': '.cursor-plugin'}
VARIABLE = {'claude': 'CLAUDE_PLUGIN_ROOT', 'codex': 'PLUGIN_ROOT', 'cursor': 'CURSOR_PLUGIN_ROOT'}


class TestPluginDirs(unittest.TestCase):
    def test_shared_code_copies_are_identical(self):
        for host in list(EVENTS) + ['antigravity']:
            folder = ROOT / 'plugins' / host
            for name in ('recorder.py', 'adapters.py', 'demo.py'):
                self.assertEqual((ROOT / name).read_bytes(), (folder / name).read_bytes(),
                                 host + '/' + name + ' drifted from the shared ' + name)

    def test_manifests(self):
        for host in EVENTS:
            manifest = json.loads((ROOT / 'plugins' / host / MANIFEST_DIR[host] / 'plugin.json').read_text())
            self.assertEqual(manifest['name'], 'sylp-provenance')
            self.assertTrue(manifest['name'].islower() and ' ' not in manifest['name'])
            self.assertEqual(manifest['hooks'], './hooks/hooks.json')
        portable = json.loads((ROOT / 'plugins' / 'codex' / 'plugin.json').read_text())
        self.assertEqual(portable['name'], 'sylp-provenance')

    def test_hooks_cover_events_and_reference_recorder(self):
        for host, events in EVENTS.items():
            config = json.loads((ROOT / 'plugins' / host / 'hooks' / 'hooks.json').read_text())
            self.assertEqual(sorted(config['hooks'].keys()), sorted(events), host)
            if host == 'cursor':
                self.assertEqual(config['version'], 1)
                commands = [d['command'] for entries in config['hooks'].values() for d in entries]
            else:
                commands = [h['command'] for entries in config['hooks'].values()
                            for group in entries for h in group['hooks']]
            for command in commands:
                self.assertIn('${' + VARIABLE[host] + '}/recorder.py', command)
                self.assertIn('--host ' + host, command)

    def test_hook_commands_execute_against_fixture(self):
        """Run the exact plugin hook command shape with the variable substituted."""
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'repo'
            repo.mkdir()
            subprocess.run(['git', '-C', str(repo), 'init'], check=True, capture_output=True)
            (repo / 'a.py').write_text('x=1\n')
            subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t',
                            'commit', '-m', 'i'], check=True, capture_output=True)
            db = Path(tmp) / 'h.db'
            for host in EVENTS:
                recorder = ROOT / 'plugins' / host / 'recorder.py'
                subprocess.run(['python3', str(recorder), '--db', str(db), 'init', str(repo),
                                '--accept-local-capture'], check=True, capture_output=True)
                payload = self._payload(host, repo)
                command = 'python3 "${%s}/recorder.py" hook --host %s' % (VARIABLE[host], host)
                result = subprocess.run(['bash', '-c', command], input=json.dumps(payload).encode(),
                                        capture_output=True, env={'PATH': '/usr/bin:/bin',
                                                                  VARIABLE[host]: str(recorder.parent),
                                                                  'SYLP_PROVENANCE_DB': str(db)})
                self.assertEqual(result.returncode, 0, host + ': ' + result.stderr.decode())
                self.assertEqual(result.stdout.decode().strip(), '{}', host)

    def test_render_includes_evolution_graph(self):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'repo'
            repo.mkdir()
            subprocess.run(['git', '-C', str(repo), 'init'], check=True, capture_output=True)
            (repo / 'a.py').write_text('x=1\n')
            subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t',
                            'commit', '-m', 'initial'], check=True, capture_output=True)
            time.sleep(1.1)  # commit times have 1s resolution; keep the first commit outside the tool window
            db = Path(tmp) / 'h.db'
            recorder = ROOT / 'plugins' / 'cursor' / 'recorder.py'
            env = {'PATH': '/usr/bin:/bin', 'SYLP_PROVENANCE_DB': str(db)}
            subprocess.run(['python3', str(recorder), '--db', str(db), 'init', str(repo),
                            '--accept-local-capture'], check=True, capture_output=True)
            def hook(payload):
                subprocess.run(['python3', str(recorder), 'hook', '--host', 'cursor'],
                               input=json.dumps(payload).encode(), check=True, capture_output=True, env=env)
            hook({'hook_event_name': 'sessionStart', 'conversation_id': 'c1', 'generation_id': 'g1',
                  'workspace_roots': [str(repo)]})
            hook({'hook_event_name': 'preToolUse', 'conversation_id': 'c1', 'generation_id': 'g1',
                  'cwd': str(repo), 'tool_name': 'Shell', 'tool_use_id': 't1', 'tool_input': {}})
            (repo / 'a.py').write_text('x=2\n')
            subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t',
                            'commit', '-m', 'during tool window'], check=True, capture_output=True)
            hook({'hook_event_name': 'postToolUse', 'conversation_id': 'c1', 'generation_id': 'g1',
                  'cwd': str(repo), 'tool_name': 'Shell', 'tool_use_id': 't1', 'tool_input': {}})
            out = Path(tmp) / 'history.html'
            subprocess.run(['python3', str(recorder), '--db', str(db), 'render', str(out)],
                           check=True, capture_output=True)
            page = out.read_text()
            self.assertIn('How this repository evolved', page)
            self.assertIn('during tool window', page)
            self.assertIn('possibly agent-made, not proven', page)
            self.assertIn('initial', page)
            self.assertIn('outside recorded agent sessions', page)
            self.assertIn('#change-', page)

    @staticmethod
    def _payload(host, repo):
        base = {'hook_event_name': {'claude': 'SessionStart', 'codex': 'SessionStart',
                                    'cursor': 'sessionStart'}[host]}
        if host == 'cursor':
            base.update({'conversation_id': 'conv-fixture', 'generation_id': 'gen-1',
                         'workspace_roots': [str(repo)]})
        else:
            base.update({'session_id': 'sess-fixture', 'cwd': str(repo)})
            if host == 'codex':
                base['turn_id'] = 'turn-1'
        return base


class TestAntigravityPlugin(unittest.TestCase):
    """Antigravity differs: manifest, hooks.json shape, relative commands, --event, no hook-event field."""
    folder = ROOT / 'plugins' / 'antigravity'

    def test_manifest(self):
        for path in (self.folder / 'plugin.json', self.folder / '.antigravity-plugin' / 'plugin.json'):
            manifest = json.loads(path.read_text())
            self.assertEqual(manifest['name'], 'sylp-provenance')
            self.assertEqual(manifest['$schema'], 'https://antigravity.google/schemas/v1/plugin.json')
            self.assertRegex(manifest['version'], r'^\d+\.\d+\.\d+$')

    def test_hooks_shape_and_commands(self):
        group = json.loads((self.folder / 'hooks.json').read_text())['sylp-provenance']
        self.assertEqual(set(group), {'SessionStart', 'PreInvocation', 'PreToolUse', 'PostToolUse', 'Stop'})
        for event, entries in group.items():
            for entry in entries:
                if event in ('PreToolUse', 'PostToolUse'):
                    self.assertIn('matcher', entry)
                    handlers = entry['hooks']
                else:
                    handlers = [entry]
                for handler in handlers:
                    self.assertEqual(handler['type'], 'command')
                    self.assertEqual(handler['command'], 'python3 ./recorder.py hook --host antigravity --event ' + event)
                    self.assertNotIn('PLUGIN_ROOT', handler['command'])

    def test_mcp_config(self):
        config = json.loads((self.folder / 'mcp_config.json').read_text())
        self.assertEqual(config['mcpServers']['provenance']['args'], ['${PLUGIN_ROOT}/recorder.py', 'mcp'])

    def test_fixture_session_end_to_end(self):
        """Fixture payloads in the documented camelCase shape, run through the plugin's own hook commands."""
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'repo'
            repo.mkdir()
            subprocess.run(['git', '-C', str(repo), 'init'], check=True, capture_output=True)
            (repo / 'a.py').write_text('x=1\n')
            subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-m', 'i'],
                           check=True, capture_output=True)
            time.sleep(1.1)
            db = Path(tmp) / 'h.db'
            env = {'PATH': '/usr/bin:/bin', 'SYLP_PROVENANCE_DB': str(db)}
            group = json.loads((self.folder / 'hooks.json').read_text())['sylp-provenance']
            def command(event):
                entry = group[event][0]
                return (entry['hooks'][0] if 'hooks' in entry else entry)['command']
            def fire(event, **fields):
                payload = {'conversationId': 'conv1', 'workspacePaths': [str(repo)], 'modelName': 'm'}
                payload.update(fields)
                result = subprocess.run(['bash', '-c', command(event)], cwd=self.folder, input=json.dumps(payload).encode(),
                                        capture_output=True, env=env)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                return json.loads(result.stdout)
            self.assertEqual(fire('SessionStart'), {})
            fire('PreInvocation', lastUserInput='Change x to 2')
            fire('PreInvocation', lastUserInput='Change x to 2')  # repeated per model call: one prompt event
            call = {'name': 'replace_file_content', 'args': {'TargetFile': str(repo / 'a.py')}}
            self.assertEqual(fire('PreToolUse', lastUserInput='Change x to 2', toolCall=call, stepIdx=3), {'decision': 'allow'})
            (repo / 'a.py').write_text('x=2\n')
            fire('PostToolUse', lastUserInput='Change x to 2', toolCall=call, stepIdx=3)
            self.assertEqual(fire('Stop'), {})
            import sqlite3
            con = sqlite3.connect(db)
            self.assertEqual(con.execute("SELECT count(*) FROM events WHERE kind='UserPromptSubmit'").fetchone()[0], 1)
            evidence = con.execute('SELECT evidence FROM changes').fetchall()
            self.assertEqual(len(evidence), 1)
            self.assertTrue(evidence[0][0].startswith('tool-linked'))
            subprocess.run(['python3', str(self.folder / 'recorder.py'), '--db', str(db), 'render', str(Path(tmp) / 'o.html')],
                           check=True, capture_output=True, env=env)
            self.assertIn('Change x to 2', (Path(tmp) / 'o.html').read_text())


if __name__ == '__main__':
    unittest.main()
