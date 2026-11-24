#!/usr/bin/env python3
"""Build separate host bundles. Does not install or register anything."""
import json
from pathlib import Path
import shutil

ROOT=Path(__file__).resolve().parent

def build():
    dist=ROOT/'dist'
    dist.mkdir(exist_ok=True)
    events={
        'claude':['SessionStart','UserPromptSubmit','PreToolUse','PostToolUse','PostToolUseFailure','Stop','SessionEnd'],
        'codex':['SessionStart','UserPromptSubmit','PreToolUse','PostToolUse','Stop','SessionEnd'],
        'cursor':['sessionStart','beforeSubmitPrompt','preToolUse','postToolUse','postToolUseFailure','afterAgentResponse','stop','sessionEnd'],
    }
    for host,names in events.items():
        folder=dist/host
        folder.mkdir(exist_ok=True)
        for filename in ['recorder.py','adapters.py']:
            shutil.copy2(ROOT/filename,folder/filename)
        manifest_dir=folder/('.claude-plugin' if host=='claude' else '.'+host+'-plugin')
        manifest_dir.mkdir(exist_ok=True)
        manifest={'name':'sylp-provenance-'+host,'version':'0.0.2','description':'Private local provenance experiment. Fixture-tested, not live-session verified.'}
        if host=='codex': manifest['hooks']='./hooks/hooks.json'
        if host=='cursor': manifest['hooks']='./hooks/hooks.json'
        (manifest_dir/'plugin.json').write_text(json.dumps(manifest,indent=2)+'\n')
        variable={'claude':'CLAUDE_PLUGIN_ROOT','codex':'PLUGIN_ROOT','cursor':'CURSOR_PLUGIN_ROOT'}[host]
        command='python3 "${'+variable+'}/recorder.py" hook --host '+host
        if host=='cursor':
            hooks={event:[{'command':command,'timeout':15}] for event in names}
            config={'version':1,'hooks':hooks}
        else:
            hooks={event:[{'hooks':[{'type':'command','command':command,'timeout':15}]}] for event in names}
            config={'hooks':hooks}
        (folder/'hooks').mkdir(exist_ok=True)
        (folder/'hooks/hooks.json').write_text(json.dumps(config,indent=2)+'\n')
    print(dist)

if __name__=='__main__': build()
