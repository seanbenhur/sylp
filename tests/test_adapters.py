import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import adapters
import recorder as r

class HostAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'repo'; self.root.mkdir()
        subprocess.run(['git','init','-q',str(self.root)],check=True)
        self.a=self.root/'a.py'; self.a.write_text('x = 1\n')
        self.b=self.root/'b.py'; self.b.write_text('y = 1\n')
        self.db=r.connect(Path(self.tmp.name)/'history.sqlite3'); self.addCleanup(self.db.close)
        r.initialize(self.db,self.root)
    def send(self,host,kind,**fields):
        base={'hook_event_name':kind,'cwd':str(self.root),'session_id':'s1','prompt_id':'p1','turn_id':'p1','conversation_id':'s1','generation_id':'p1','workspace_roots':[str(self.root)]}
        base.update(fields); value=adapters.normalize(host,base); r.record(self.db,value); return value
    def changes(self): return self.db.execute('SELECT * FROM changes').fetchall()
    def test_codex_multifile_patch_and_turn(self):
        self.send('codex','UserPromptSubmit',prompt='Change two files')
        tool={'tool_use_id':'patch1','tool_name':'apply_patch','tool_input':{'command':'*** Begin Patch\n*** Update File: a.py\n@@\n-x = 1\n+x = 2\n*** Update File: b.py\n@@\n-y = 1\n+y = 2\n*** End Patch'}}
        self.send('codex','PreToolUse',**tool); self.a.write_text('x = 2\n'); self.b.write_text('y = 2\n'); self.send('codex','PostToolUse',**tool)
        self.send('codex','Stop',last_assistant_message='Updated both files')
        self.assertEqual(len(self.changes()),2)
        self.assertTrue(all(c['evidence'].startswith('tool-linked') for c in self.changes()))
        out=Path(self.tmp.name)/'view.html'; r.render(self.db,out)
        self.assertIn('Change two files',out.read_text()); self.assertIn('Updated both files',out.read_text())
    def test_codex_failed_bash_still_post(self):
        kw={'tool_use_id':'exec1','tool_name':'Bash','tool_input':{'command':'create then fail'}}
        self.send('codex','PreToolUse',**kw); self.a.write_text('partially changed\n'); self.send('codex','PostToolUse',tool_response={'exit_code':1},**kw)
        self.assertTrue(self.changes()[0]['evidence'].startswith('associated'))
    def test_cursor_native_prompt_edit_response(self):
        self.send('cursor','beforeSubmitPrompt',prompt='Change a')
        kw={'tool_use_id':'edit1','tool_name':'StrReplace','tool_input':{'file_path':str(self.a)}}
        self.send('cursor','preToolUse',**kw); self.a.write_text('x = 3\n'); self.send('cursor','postToolUse',**kw)
        self.send('cursor','afterAgentResponse',text='Changed a')
        self.send('cursor','stop')
        out=Path(self.tmp.name)/'view.html'; r.render(self.db,out)
        self.assertTrue(self.changes()[0]['evidence'].startswith('tool-linked'))
        self.assertIn('Changed a',out.read_text()); self.assertIn('Change a',out.read_text())
    def test_cursor_failure(self):
        kw={'tool_use_id':'shell1','tool_name':'Shell'}
        self.send('cursor','preToolUse',**kw); self.a.write_text('failed but wrote\n'); self.send('cursor','postToolUseFailure',**kw)
        self.assertTrue(self.changes()[0]['evidence'].startswith('associated'))
    def test_no_cross_host_session_collision(self):
        self.send('codex','UserPromptSubmit',prompt='codex prompt')
        self.send('cursor','beforeSubmitPrompt',prompt='cursor prompt')
        self.assertEqual(self.db.execute('SELECT count(DISTINCT session) FROM events').fetchone()[0],2)
    def test_missing_tool_id_rejected(self):
        with self.assertRaises(ValueError): self.send('cursor','preToolUse')
    def test_missing_turn_not_guessed(self):
        value=self.send('codex','Stop',turn_id=None,last_assistant_message='No ID')
        self.assertEqual(value['prompt_id'],'')
    def test_cursor_multiroot_needs_cwd(self):
        with self.assertRaises(ValueError):
            adapters.normalize('cursor',dict(hook_event_name='stop',conversation_id='s',generation_id='p',workspace_roots=['/a','/b']))
    def test_patch_path_outside_repo_does_not_get_linked(self):
        kw={'tool_use_id':'patch1','tool_name':'apply_patch','tool_input':{'command':'*** Update File: ../outside.py'}}
        self.send('codex','PreToolUse',**kw); self.a.write_text('changed\n'); self.send('codex','PostToolUse',**kw)
        self.assertFalse(self.changes()[0]['evidence'].startswith('tool-linked'))
    def test_cli_response_contract_all_hosts(self):
        for host,kind in [('claude','Stop'),('codex','Stop'),('cursor','stop')]:
            payload=dict(hook_event_name=kind,cwd=str(self.root),session_id='s',prompt_id='p',turn_id='p',conversation_id='s',generation_id='p')
            result=subprocess.run([sys.executable,str(ROOT/'recorder.py'),'--db',str(Path(self.tmp.name)/'history.sqlite3'),'hook','--host',host],input=json.dumps(payload),text=True,capture_output=True,check=True)
            self.assertEqual(json.loads(result.stdout),{})
    def test_codex_delayed_exec_window(self):
        kw={'tool_use_id':'delayed','tool_name':'Bash'}
        self.send('codex','PreToolUse',**kw)
        self.send('codex','Stop',last_assistant_message='Command is still running')
        self.a.write_text('late output\n'); self.send('codex','PostToolUse',**kw)
        self.assertTrue(self.changes()[0]['evidence'].startswith('associated'))
    def test_codex_duplicate_post(self):
        kw={'tool_use_id':'unique','tool_name':'Bash'}
        self.send('codex','PreToolUse',**kw); self.a.write_text('once\n'); self.send('codex','PostToolUse',**kw); self.send('codex','PostToolUse',**kw)
        self.assertEqual(len(self.changes()),1)

if __name__=='__main__': unittest.main()
