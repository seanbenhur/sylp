"""Normalize documented host events. No inferred IDs or transcript scraping."""
from pathlib import Path

CURSOR_EVENTS = {
    'sessionStart':'SessionStart', 'sessionEnd':'SessionEnd',
    'beforeSubmitPrompt':'UserPromptSubmit', 'preToolUse':'PreToolUse',
    'postToolUse':'PostToolUse', 'postToolUseFailure':'PostToolUseFailure',
    'afterAgentResponse':'AssistantResponse', 'stop':'Stop',
}


def patch_paths(command, cwd):
    """Only explicit apply_patch headers; no shell evaluation or guessed renames."""
    if not isinstance(command,str): return []
    paths=[]
    for line in command.splitlines():
        for prefix in ('*** Add File: ', '*** Update File: ', '*** Delete File: ', '*** Move to: '):
            if line.startswith(prefix):
                value=line[len(prefix):]
                p=Path(value)
                if not p.is_absolute(): p=Path(cwd)/p
                paths.append(str(p.absolute()))
    return sorted(set(paths))


def normalize(host, data):
    if not isinstance(data,dict): raise ValueError('Hook payload must be an object')
    if host not in {'claude','codex','cursor'}: raise ValueError('Unknown host')
    raw_kind=data.get('hook_event_name','')
    tool_input=data.get('tool_input')
    if not isinstance(tool_input,dict): tool_input={}
    if host=='cursor':
        if raw_kind not in CURSOR_EVENTS: raise ValueError('Unsupported Cursor event')
        kind=CURSOR_EVENTS[raw_kind]
        session=data.get('conversation_id') or data.get('session_id')
        prompt=data.get('generation_id')
        cwd=data.get('cwd')
        if not cwd:
            roots=data.get('workspace_roots') or []
            if len(roots)!=1: raise ValueError('Cursor hook needs cwd or exactly one workspace root')
            cwd=roots[0]
        final=data.get('text') if kind=='AssistantResponse' else None
    else:
        kind=raw_kind; session=data.get('session_id'); cwd=data.get('cwd')
        prompt=data.get('turn_id') if host=='codex' else data.get('prompt_id')
        final=data.get('last_assistant_message')
    if not isinstance(session,str) or not session or not isinstance(cwd,str) or not cwd:
        raise ValueError('Missing explicit session or working directory')
    if prompt is not None and not isinstance(prompt,str): raise ValueError('Invalid turn ID')
    tool=data.get('tool_use_id') or ''
    if kind in {'PreToolUse','PostToolUse','PostToolUseFailure'} and not tool:
        raise ValueError('Tool boundary lacks tool_use_id; refusing guessed correlation')
    name=data.get('tool_name') or ''
    paths=[]
    if isinstance(tool_input.get('file_path'),str): paths=[tool_input['file_path']]
    if host=='codex' and name=='apply_patch':
        paths=patch_paths(tool_input.get('command'),cwd)
    return {
        'host':host,'raw_kind':raw_kind,'hook_event_name':kind,
        'session_id':host+':'+session, 'prompt_id':prompt or '', 'cwd':cwd,
        'tool_use_id':host+':'+session+':'+tool if tool else '',
        'tool_name':name,'tool_input':{'file_path':paths[0] if len(paths)==1 else None},
        'file_paths':paths, 'prompt':data.get('prompt'),
        'last_assistant_message':final,
        'capture_note':'Tool path association only. Host-declared paths and time windows do not prove sole authorship.',
    }
