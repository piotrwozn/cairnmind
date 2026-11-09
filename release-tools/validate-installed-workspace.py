"""Open the installed local inspection workspace and inspect its real tmux panes.

No client work request is issued. Pane contents are hashed, never exported.
The workspace remains available for the user's next `devbrain` invocation.
"""
import hashlib
import importlib.metadata
import json
import time
from pathlib import Path

from devbrain.config import Settings
from devbrain.workspace import WorkspaceManager


def main():
    settings = Settings.load()
    manager = WorkspaceManager(settings)
    manager.broker.ensure()
    manager.create()
    frames = []
    failures = []
    panels = {}
    for _ in range(5):
        time.sleep(1)
        manager.refresh()
        panels = manager.panes()
        for role, pane in panels.items():
            dead = manager.tmux('display-message', '-p', '-t', pane, '#{pane_dead}').stdout.strip()
            if dead == '1':
                failures.append(f'{role}: pane exited')
        graph = panels.get('graph')
        if graph:
            rendered = manager.tmux('capture-pane', '-p', '-t', graph).stdout
            frames.append(hashlib.sha256(rendered.encode()).hexdigest())
            if 'Traceback (most recent call last)' in rendered:
                failures.append('graph: traceback rendered')
    state = manager.status()
    report = {
        'version': importlib.metadata.version('devbrain'),
        'workspace_available': state.get('available'),
        'panels': sorted(panels),
        'all_five_panels_present': set(panels) == {'sidebar', 'client1', 'client2', 'inspector', 'graph'},
        'unique_graph_frames': len(set(frames)),
        'graph_frame_samples': len(frames),
        'failures': sorted(set(failures)),
        'client_work_requested_by_test': False,
        'pane_contents_exported': False,
    }
    report['status'] = ('passed' if report['workspace_available'] and report['all_five_panels_present']
                        and report['unique_graph_frames'] > 1 and not failures else 'failed')
    target = Path(__file__).resolve().parents[1] / 'evidence/installed-workspace-validation-20261107.json'
    target.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report['status'] == 'passed' else 1)


if __name__ == '__main__':
    main()
