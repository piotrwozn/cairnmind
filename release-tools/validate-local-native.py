"""Exercise real logged-in native clients using DevBrain's synthetic fixtures."""
import json
import os
from pathlib import Path
import tempfile
import time

from devbrain.config import Settings
from devbrain.engine import start_engine
from devbrain.evaluation import _clients, _fixture_settings, _git, _native_client, _seed
from devbrain.store import Store


def main():
    os.environ['PATH'] = str(Path.home() / '.local/bin') + os.pathsep + os.environ['PATH']
    settings = Settings.load()
    start_engine(settings)
    workspace = Path(__file__).resolve().parents[1]
    reports = workspace / 'evidence'
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    cache = Path.home() / '.cache/devbrain-validation-20261107'
    cache.mkdir(parents=True, exist_ok=True)
    progress = reports / f'local-native-{stamp}.log'

    def runner(client, *args, **kwargs):
        with progress.open('a') as log:
            log.write(json.dumps({'phase': 'starting', 'client': client}) + '\n')
        result = _native_client(client, *args, **kwargs)
        with progress.open('a') as log:
            log.write(json.dumps({'phase': 'finished', 'client': client,
                                  'status': result.get('status'), 'error_code': result.get('error_code')}) + '\n')
        return result

    with tempfile.TemporaryDirectory(prefix='native-eval-', dir=cache) as name:
        root = Path(name)
        work, other = root / 'worktree', root / 'other'
        work.mkdir()
        other.mkdir()
        _git(work, 'init', '--quiet')
        (work / 'README.md').write_text('Synthetic native client validation.\n')
        _git(work, 'add', 'README.md')
        _git(work, '-c', 'user.name=DevBrain Evaluation', '-c', 'user.email=eval@localhost',
             'commit', '--quiet', '-m', 'Synthetic fixture')
        isolated = _fixture_settings(settings, root / 'data')
        store = Store(isolated.data_dir, settings=isolated)
        try:
            project, _, canonical = _seed(store, work, other)
            results = _clients(store, isolated, project, canonical, runner)
            report = {'scope': 'real_native_clients_only', 'results': results,
                      'isolation': {'separate_database': True, 'user_projects_modified': False}}
            path = reports / f'local-native-{stamp}.json'
            path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
            print(json.dumps({'report': str(path), 'statuses': [r['status'] for r in results]}), flush=True)
        finally:
            store.client.server_command('drop database ' + isolated.arcade_database)


if __name__ == '__main__':
    main()
