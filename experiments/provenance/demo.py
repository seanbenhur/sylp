#!/usr/bin/env python3
"""Deterministic real-filesystem demo with SYNTHETIC Claude hook payloads."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parent

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('destination'); parser.add_argument('--host',choices=['claude','codex','cursor'],default='claude'); args=parser.parse_args()
    out=Path(args.destination).resolve()
    if out.exists(): raise SystemExit('Use a new empty destination path')
    repo=out/'fixture'; repo.mkdir(parents=True)
    subprocess.run(['git','init','-q',str(repo)],check=True)
    source=repo/'retry.py'; source.write_text('def should_retry(status):\n    return status >= 400\n')
    db=out/'history.sqlite3'
    cli=[sys.executable,str(ROOT/'recorder.py'),'--db',str(db)]
    subprocess.run(cli+['init',str(repo),'--accept-local-capture'],check=True)
    log=[]
    def hook(kind,prompt_id='prompt-1',tool='',name='',**fields):
        payload={'hook_event_name':kind,'session_id':'synthetic-fixture-session','prompt_id':prompt_id,'cwd':str(repo),'tool_use_id':tool,'tool_name':name,'tool_input':{'file_path':str(source)}}
        payload.update(fields)
        if args.host=='codex':
            payload['turn_id']=payload.pop('prompt_id')
            if name=='Edit':
                payload['tool_name']='apply_patch'
                payload['tool_input']={'command':'*** Update File: retry.py'}
            if kind=='PostToolUseFailure': payload['hook_event_name']='PostToolUse'
        elif args.host=='cursor':
            mapping={'SessionStart':'sessionStart','UserPromptSubmit':'beforeSubmitPrompt','PreToolUse':'preToolUse','PostToolUse':'postToolUse','PostToolUseFailure':'postToolUseFailure','Stop':'afterAgentResponse'}
            payload['hook_event_name']=mapping[kind]
            payload['conversation_id']=payload.pop('session_id')
            payload['generation_id']=payload.pop('prompt_id')
            if 'last_assistant_message' in payload: payload['text']=payload.pop('last_assistant_message')
            if name=='Edit': payload['tool_name']='StrReplace'
        log.append(payload)
        # Fresh process per hook demonstrates durable state between invocations.
        subprocess.run(cli+['hook','--host',args.host],input=json.dumps(payload),text=True,check=True)
    hook('SessionStart')
    hook('UserPromptSubmit',**{'prompt':'Retry server errors, but do not retry every client error.'})
    hook('PreToolUse',tool='edit-1',name='Edit')
    source.write_text('def should_retry(status):\n    return status >= 500\n')
    hook('PostToolUse',tool='edit-1',name='Edit')
    hook('Stop',last_assistant_message='Changed the retry predicate to server errors. This fixture has not run behavioral tests.')
    hook('UserPromptSubmit',prompt_id='prompt-2',**{'prompt':'Also retry HTTP 429. Preserve the server-error rule.'})
    hook('PreToolUse',prompt_id='prompt-2',tool='edit-2',name='Edit')
    source.write_text('def should_retry(status):\n    return status == 429 or status >= 500\n')
    hook('PostToolUse',prompt_id='prompt-2',tool='edit-2',name='Edit')
    hook('Stop',prompt_id='prompt-2',last_assistant_message='Included 429 as an explicit exception to the client-error exclusion.')
    # Outside any captured tool window: this must NOT be credited to the agent.
    source.write_text(source.read_text()+'\n# Manual note added outside a captured tool call.\n')
    hook('Stop',prompt_id='manual-boundary',last_assistant_message='Manual fixture edit observed. Author not established by hooks.')
    hook('UserPromptSubmit',prompt_id='prompt-3',**{'prompt':'Write a generated marker, then simulate a failed command.'})
    hook('PreToolUse',prompt_id='prompt-3',tool='bash-1',name='Bash')
    (repo/'generated.txt').write_text('File produced before the command failed.\n')
    hook('PostToolUseFailure',prompt_id='prompt-3',tool='bash-1',name='Bash')
    hook('Stop',prompt_id='prompt-3',last_assistant_message='Failure does not imply that no files were changed.')
    (out/'synthetic-hooks.json').write_text(json.dumps(log,indent=2))
    subprocess.run(cli+['render',str(out/'history.html')],check=True)
    print(out/'history.html')

if __name__=='__main__': main()
