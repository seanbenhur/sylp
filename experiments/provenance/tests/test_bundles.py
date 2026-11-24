import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import build_bundles

class BundleTests(unittest.TestCase):
    def test_manifest_hook_commands_run_without_host_cli(self):
        build_bundles.build()
        with tempfile.TemporaryDirectory(prefix='sylp bundle spaces ') as temp:
            repo=Path(temp)/'repo'; repo.mkdir()
            subprocess.run(['git','init','-q',str(repo)],check=True)
            for host in ['claude','codex','cursor']:
                folder=ROOT/'dist'/host
                config=json.loads((folder/'hooks/hooks.json').read_text())
                event='Stop' if host!='cursor' else 'stop'
                entry=config['hooks'][event][0]
                command=entry['command'] if host=='cursor' else entry['hooks'][0]['command']
                env=dict(os.environ)
                env.update(CLAUDE_PLUGIN_ROOT=str(folder),PLUGIN_ROOT=str(folder),CURSOR_PLUGIN_ROOT=str(folder),SYLP_PROVENANCE_DB=str(Path(temp)/(host+'.sqlite3')))
                subprocess.run([sys.executable,str(folder/'recorder.py'),'init',str(repo),'--accept-local-capture'],check=True,env=env)
                data={'hook_event_name':event,'cwd':str(repo),'session_id':'s','prompt_id':'p','turn_id':'p','conversation_id':'s','generation_id':'p'}
                # Exercise the actual generated hook command and cwd/env, not installed host loading.
                result=subprocess.run(command,shell=True,cwd=repo,env=env,input=json.dumps(data),text=True,capture_output=True,check=True)
                self.assertEqual(json.loads(result.stdout),{})
                self.assertEqual(result.stderr,'')
                manifest=folder/('.claude-plugin' if host=='claude' else '.'+host+'-plugin')/'plugin.json'
                self.assertIn('name',json.loads(manifest.read_text()))

if __name__=='__main__': unittest.main()
