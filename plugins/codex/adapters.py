"""Normalize documented host events. No inferred IDs or transcript scraping."""
import hashlib
from pathlib import Path

CURSOR_EVENTS = {
    'sessionStart':'SessionStart', 'sessionEnd':'SessionEnd',
    'beforeSubmitPrompt':'UserPromptSubmit', 'preToolUse':'PreToolUse',
    'postToolUse':'PostToolUse', 'postToolUseFailure':'PostToolUseFailure',
    'afterAgentResponse':'AssistantResponse', 'stop':'Stop',
}


ANTIGRAVITY_EVENTS = {'SessionStart', 'PreToolUse', 'PostToolUse', 'PreInvocation', 'PostInvocation', 'Stop'}
ANTIGRAVITY_EDIT_TOOLS = {'write_to_file', 'replace_file_content', 'multi_replace_file_content'}


def _antigravity(data, event):
    """Antigravity payloads carry no event name, so the hook command passes it with --event.
    No prompt event exists: the prompt is lastUserInput, recorded once per distinct text at PreInvocation.
    Tool calls have no ID: conversationId plus stepIdx names the step. Returns None for events not recorded."""
    if event not in ANTIGRAVITY_EVENTS: raise ValueError('Unsupported Antigravity event')
    session = data.get('conversationId')
    roots = data.get('workspacePaths') or []
    if not isinstance(roots, list) or len(roots) != 1 or not isinstance(roots[0], str) or not roots[0]:
        raise ValueError('Antigravity hook needs exactly one workspace path')
    if not isinstance(session, str) or not session: raise ValueError('Missing explicit conversation ID')
    cwd = roots[0]
    last = data.get('lastUserInput')
    last = last if isinstance(last, str) and last else None
    prompt = 'ag-' + hashlib.sha256(last.encode()).hexdigest()[:16] if last else ''
    kind, name, tool, paths, text = event, '', '', [], None
    if event == 'PostInvocation': return None
    if event == 'PreInvocation':
        if not last: return None
        kind, text = 'UserPromptSubmit', last
    elif event in {'PreToolUse', 'PostToolUse'}:
        call = data.get('toolCall')
        step = data.get('stepIdx')
        if not isinstance(call, dict) or not isinstance(call.get('name'), str) or not call['name']:
            raise ValueError('Tool boundary lacks a tool name')
        if isinstance(step, bool) or not isinstance(step, int):
            raise ValueError('Tool boundary lacks stepIdx; refusing guessed correlation')
        name, tool = call['name'], 'step' + str(step)
        args = call.get('args') if isinstance(call.get('args'), dict) else {}
        if name in ANTIGRAVITY_EDIT_TOOLS and isinstance(args.get('TargetFile'), str) and args['TargetFile']:
            paths = [args['TargetFile']]
        if event == 'PostToolUse' and data.get('error'): kind = 'PostToolUseFailure'
    return {
        'host': 'antigravity', 'raw_kind': event, 'hook_event_name': kind,
        'session_id': 'antigravity:' + session, 'prompt_id': prompt, 'cwd': cwd,
        'tool_use_id': 'antigravity:' + session + ':' + tool if tool else '',
        'tool_name': name, 'tool_input': {'file_path': paths[0] if paths else None},
        'file_paths': paths, 'prompt': text, 'last_assistant_message': None,
        'capture_note': 'Tool path association only. Host-declared paths and time windows do not prove sole authorship.',
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


def normalize(host, data, event=None):
    if not isinstance(data,dict): raise ValueError('Hook payload must be an object')
    if host not in {'claude','codex','cursor','antigravity'}: raise ValueError('Unknown host')
    if host=='antigravity': return _antigravity(data,event)
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
