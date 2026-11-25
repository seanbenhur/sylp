#!/usr/bin/env python3
"""One-command local install of the sylp provenance plugin (Cursor, Codex, Claude).

What it does, per host:
  cursor: copies plugins/cursor into ~/.cursor/plugins/local/sylp-provenance
          (official local-plugin location, loads in the Cursor editor and CLI),
          initializes the provenance database for the target repo.
  codex:  writes a project .codex/hooks.json into the target repo pointing at the
          committed plugins/codex recorder by absolute path (documented project
          hooks), or --marketplace copies plugins/codex into ~/.codex/plugins/
          and registers it in ~/.agents/plugins/marketplace.json (personal
          marketplace flow; not verified against a live Codex install).
  claude: initializes the database and prints the local marketplace install
          commands (claude plugin marketplace add + install, using the committed
          .claude-plugin/marketplace.json), plus the `claude --plugin-dir`
          session-scoped alternative.

Nothing here contacts a marketplace, publishes, or touches any remote service.
Python 3.10+, stdlib only.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = Path.home() / '.sylp-provenance' / 'history.sqlite3'


def default_db_for(root):
    """Same per-repository database path the recorder derives."""
    digest = hashlib.sha1(str(root).encode()).hexdigest()[:12]
    return Path.home() / '.sylp-provenance' / 'repos' / (Path(str(root)).name + '-' + digest + '.sqlite3')
CODEX_EVENTS = ['SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop', 'SessionEnd']


def run(cmd):
    subprocess.run(cmd, check=True, capture_output=True)


def ensure_repo(path):
    """Create path as a throwaway git repo if needed; verify it otherwise."""
    repo = Path(path).expanduser().resolve()
    if not repo.exists():
        repo.mkdir(parents=True)
        run(['git', '-C', str(repo), 'init'])
        (repo / 'a.py').write_text("print('hi')\n")
        run(['git', '-C', str(repo), 'add', '.'])
        # -c identity so this works on a fresh Mac without global git config
        run(['git', '-C', str(repo), '-c', 'user.name=provenance-trial',
             '-c', 'user.email=provenance-trial@localhost', 'commit', '-m', 'init'])
        return repo, True
    result = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--is-inside-work-tree'],
                            capture_output=True)
    if result.returncode != 0:
        raise SystemExit(str(repo) + ' exists but is not a git repository')
    return repo, False


def init_db(host, repo, db):
    recorder = ROOT / 'plugins' / host / 'recorder.py'
    run([sys.executable, str(recorder), '--db', str(db), 'init', str(repo), '--accept-local-capture'])


def copy_plugin(host, dest):
    dest = Path(dest).expanduser()
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(ROOT / 'plugins' / host, dest, ignore=shutil.ignore_patterns('__pycache__'))
    return dest


def codex_project_hooks(repo, force=False):
    """Write <repo>/.codex/hooks.json calling the committed recorder by absolute path."""
    target = Path(repo) / '.codex' / 'hooks.json'
    if target.exists() and not force:
        raise SystemExit(str(target) + ' already exists; refusing to overwrite (use --force)')
    recorder = ROOT / 'plugins' / 'codex' / 'recorder.py'
    command = 'python3 "{}" hook --host codex'.format(recorder)
    config = {
        'description': 'sylp provenance local capture (opt-in, disposable repo only)',
        'hooks': {event: [{'hooks': [{'type': 'command', 'command': command,
                                      'timeout': 3 if event == 'SessionEnd' else 15}]}]
                  for event in CODEX_EVENTS},
    }
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(config, indent=2) + '\n')
    return target


def codex_marketplace():
    """Personal marketplace install per developers.openai.com/plugins/build/plugins.

    Not verified against a live Codex install; the docs verify via the ChatGPT
    desktop app. CLI pickup through /plugins is expected but unconfirmed.
    """
    dest = copy_plugin('codex', Path.home() / '.codex' / 'plugins' / 'sylp-provenance')
    market = Path.home() / '.agents' / 'plugins' / 'marketplace.json'
    if market.exists():
        data = json.loads(market.read_text())
    else:
        data = {'name': 'personal-local',
                'interface': {'displayName': 'Personal local plugins'},
                'plugins': []}
    data['plugins'] = [p for p in data.get('plugins', []) if p.get('name') != 'sylp-provenance']
    data['plugins'].append({
        'name': 'sylp-provenance',
        'source': {'source': 'local', 'path': './.codex/plugins/sylp-provenance'},
        'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_INSTALL'},
        'category': 'Productivity',
    })
    market.parent.mkdir(parents=True, exist_ok=True)
    market.write_text(json.dumps(data, indent=2) + '\n')
    return dest, market


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--host', required=True, choices=['cursor', 'codex', 'claude'])
    parser.add_argument('--repo', required=True,
                        help='throwaway repo to capture; created with one commit if missing')
    parser.add_argument('--db', default=None,
                        help='capture database path (default: one per repository under ~/.sylp-provenance/repos/, '
                             'so repos can be selected separately in the viewer)')
    parser.add_argument('--marketplace', action='store_true',
                        help='codex only: personal-marketplace plugin install instead of project hooks')
    parser.add_argument('--force', action='store_true',
                        help='codex only: overwrite an existing .codex/hooks.json')
    args = parser.parse_args()

    if sys.version_info < (3, 10):
        raise SystemExit('Python 3.10+ required')
    repo, created = ensure_repo(args.repo)
    db = Path(args.db).expanduser() if args.db else default_db_for(repo)
    init_db(args.host, repo, db)
    print(('created throwaway repo: ' if created else 'using repo: ') + str(repo))
    print('initialized capture db: ' + str(db))

    if args.host == 'cursor':
        dest = copy_plugin('cursor', Path.home() / '.cursor' / 'plugins' / 'local' / 'sylp-provenance')
        print('installed plugin files to: ' + str(dest))
        print()
        print('Next:')
        print('  cd ' + str(repo) + ' && agent')
        print('  (the plugin auto-loads from ~/.cursor/plugins/local; if your build blocks')
        print('   local plugin imports, run instead: agent --plugin-dir '
              + str(ROOT / 'plugins' / 'cursor') + ')')
    elif args.host == 'codex':
        if args.marketplace:
            dest, market = codex_marketplace()
            print('installed plugin files to: ' + str(dest))
            print('registered in marketplace: ' + str(market))
            print()
            print('Next (docs-based, not live-verified):')
            print('  cd ' + str(repo) + ' && codex')
            print('  open /plugins, enable sylp-provenance, then /hooks to trust its hooks')
        else:
            target = codex_project_hooks(repo, force=args.force)
            print('wrote project hooks: ' + str(target))
            print()
            print('Next:')
            print('  cd ' + str(repo) + ' && codex')
            print('  run /hooks and trust the sylp provenance hooks (Codex skips untrusted hooks)')
        print('Optional "why" MCP server (if the plugin .mcp.json is not picked up):')
        print('  codex mcp add sylp-provenance -- python3 ' + str(ROOT / 'plugins' / 'codex' / 'recorder.py') + ' mcp')
    else:
        print('Next (local marketplace install, persistent):')
        print('  claude plugin marketplace add ' + str(ROOT))
        print('  claude plugin install sylp-provenance@sylp-provenance')
        print('Or for one session only:')
        print('  cd ' + str(repo) + ' && claude --plugin-dir ' + str(ROOT / 'plugins' / 'claude'))
        print('Then open the dashboard any time with /sylp-provenance:serve')

    print()
    print('After one small edit prompt, render the captured history:')
    print('  python3 ' + str(ROOT / 'recorder.py') + ' render ~/history.html')
    print('Uninstall: remove the plugin folder above (and .codex/hooks.json for codex project mode).')


if __name__ == '__main__':
    main()
