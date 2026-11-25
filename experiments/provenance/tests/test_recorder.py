import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('recorder',Path(__file__).resolve().parents[1]/'recorder.py')
r = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(r)

class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'repo'; self.root.mkdir()
        subprocess.run(['git','init','-q',str(self.root)],check=True)
        self.file=self.root/'retry.py'; self.file.write_text('attempts = 1\n')
        self.dbpath=Path(self.tmp.name)/'state'/'history.sqlite3'
        self.db=r.connect(self.dbpath); self.addCleanup(self.db.close)
        r.initialize(self.db,self.root)
    def hook(self,kind,tool='t1',prompt_id='p1',name='Edit',**kw):
        data=dict(cwd=str(self.root),session_id='session1',prompt_id=prompt_id,hook_event_name=kind,tool_use_id=tool,tool_name=name,tool_input={'file_path':str(self.file)})
        data.update(kw); return r.record(self.db,data)
    def labels(self): return [x[0] for x in self.db.execute('SELECT evidence FROM changes')]
    def test_named_tool_and_prompt(self):
        self.hook('UserPromptSubmit',prompt_id='p1',**{'prompt':'Use three attempts'})
        self.hook('PreToolUse'); self.file.write_text('attempts = 3\n'); self.hook('PostToolUse')
        self.assertTrue(self.labels()[0].startswith('tool-linked'))
    def test_manual_change_unknown(self):
        self.file.write_text('attempts = 2\n'); self.hook('Stop')
        self.assertTrue(self.labels()[0].startswith('unattributed'))
    def test_failed_tool_can_write(self):
        self.hook('PreToolUse',name='Bash'); self.file.write_text('attempts = 4\n'); self.hook('PostToolUseFailure',name='Bash')
        self.assertTrue(self.labels()[0].startswith('associated'))
    def test_overlap_ambiguous(self):
        self.hook('PreToolUse',tool='a'); self.hook('PreToolUse',tool='b')
        self.file.write_text('attempts = 5\n'); self.hook('PostToolUse',tool='a')
        self.assertTrue(self.labels()[0].startswith('ambiguous'))
    def test_duplicate_hook(self):
        self.hook('PreToolUse'); self.file.write_text('attempts = 3\n'); self.hook('PostToolUse')
        self.assertFalse(self.hook('PostToolUse')); self.assertEqual(len(self.labels()),1)
    def test_restart_keeps_pending(self):
        self.hook('PreToolUse'); self.file.write_text('attempts = 6\n')
        other=r.connect(self.dbpath)
        try:
            self.assertEqual(other.execute('SELECT count(*) FROM pending').fetchone()[0],1)
            r.record(other,dict(cwd=str(self.root),session_id='session1',prompt_id='p1',hook_event_name='SessionStart'))
        finally: other.close()
        self.assertTrue(self.labels()[0].startswith('ambiguous'))
    def test_revert_retained_between_tools(self):
        self.hook('PreToolUse'); self.file.write_text('attempts = 2\n'); self.hook('PostToolUse')
        self.hook('PreToolUse',tool='t2'); self.file.write_text('attempts = 1\n'); self.hook('PostToolUse',tool='t2')
        self.assertEqual(len(self.labels()),2)
    def test_secret_symlink_binary_and_ignored(self):
        (self.root/'.env').write_text('never store me')
        (self.root/'link').symlink_to('/etc/passwd')
        (self.root/'binary').write_bytes(b'a\0b')
        (self.root/'.gitignore').write_text('ignored.txt\n')
        (self.root/'ignored.txt').write_text('ignored')
        self.hook('Stop')
        contents=''.join(x[0] for x in self.db.execute('SELECT content FROM blobs'))
        self.assertNotIn('never store me',contents); self.assertNotIn('root:x:',contents)
        names=json.loads(self.db.execute('SELECT files FROM snapshots ORDER BY id DESC LIMIT 1').fetchone()[0])
        self.assertNotIn('binary',names); self.assertNotIn('ignored.txt',names)
    def test_html_escapes_and_does_not_load_assets(self):
        self.file.write_text('<script>alert(1)</script>\n'); self.hook('Stop')
        out=Path(self.tmp.name)/'view.html'; r.render(self.db,out)
        page=out.read_text(); self.assertIn('&lt;script&gt;',page); self.assertNotIn('src="http',page)
    def test_pause_does_not_record(self):
        r.setmeta(self.db,'enabled','0'); self.file.write_text('changed\n')
        self.assertFalse(self.hook('Stop')); self.assertEqual(self.labels(),[])
    def test_rename_is_delete_add_not_fabricated_lineage(self):
        self.hook('PreToolUse',name='Bash'); self.file.rename(self.root/'policy.py'); self.hook('PostToolUse',name='Bash')
        self.assertEqual(len(self.labels()),2)
    def test_oversized_file_gap(self):
        self.file.write_text('a'*(r.MAX_FILE+1)); self.hook('Stop')
        self.assertIn('capture gap',self.labels()[0])
    def make_crowd(self, n=2100):
        for i in range(n): (self.root/('f%04d.py' % i)).write_text('x = %d\n' % i)
    def rebaseline(self):
        self.db.close(); self.dbpath.unlink()
        self.db=r.connect(self.dbpath); r.initialize(self.db,self.root)
    def rows(self): return list(self.db.execute('SELECT path,evidence,before_hash,after_hash FROM changes'))
    def test_declared_file_captured_beyond_scan_cap(self):
        self.make_crowd(); self.rebaseline()
        self.hook('UserPromptSubmit',prompt_id='p1',**{'prompt':'Bump attempts'})
        self.hook('PreToolUse'); self.file.write_text('attempts = 3\n'); self.hook('PostToolUse')
        linked=[x for x in self.rows() if x[1].startswith('tool-linked')]
        self.assertEqual(len(linked),1); self.assertEqual(linked[0][0],'retry.py')
    def test_cap_still_hides_undeclared_beyond_cap(self):
        hidden=self.root/'zzz_hidden.py'; hidden.write_text('v = 1\n')
        self.make_crowd(); self.rebaseline()
        self.hook('Stop'); hidden.write_text('v = 2\n'); self.hook('Stop')
        self.assertNotIn('zzz_hidden.py',[x[0] for x in self.rows()])
    def test_no_gap_churn_after_declared_capture(self):
        self.make_crowd(); self.rebaseline()
        self.hook('PreToolUse'); self.file.write_text('attempts = 3\n'); self.hook('PostToolUse')
        n=len(self.rows())
        self.hook('Stop'); self.hook('Stop')
        rows=self.rows(); self.assertEqual(len(rows),n)
        self.assertFalse([x for x in rows if 'no longer eligible' in x[1]])
    def test_declared_delete_beyond_cap_is_verified(self):
        self.make_crowd(); self.rebaseline()
        self.hook('PreToolUse',name='Bash'); self.file.unlink(); self.hook('PostToolUse',name='Bash')
        deletes=[x for x in self.rows() if x[0]=='retry.py' and x[3] is None]
        self.assertEqual(len(deletes),1); self.assertIsNotNone(deletes[0][2])
    def test_different_repo_rejected(self):
        other=Path(self.tmp.name)/'other'; other.mkdir(); subprocess.run(['git','init','-q',str(other)],check=True)
        with self.assertRaises(ValueError): self.hook('Stop',cwd=str(other))

if __name__=='__main__': unittest.main()
