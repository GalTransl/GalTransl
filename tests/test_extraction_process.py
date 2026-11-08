"""Process cleanup preserves real permission failures after cancellation."""
import errno
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from GalTransl.Extraction import process


@pytest.mark.parametrize('state', ['exited', 'running', 'unkillable'])
def test_process_group_permission_error_depends_on_parent_state(monkeypatch, state):
    def denied(pid, signum):
        # macOS can return EPERM when a process group contains zombies.
        raise PermissionError(errno.EPERM, 'Operation not permitted')

    monkeypatch.setattr(process, 'os', SimpleNamespace(name='posix', killpg=denied))
    monkeypatch.setattr(signal, 'SIGKILL', 9, raising=False)
    with subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                          stdin=subprocess.PIPE) as child:
        kill = child.kill
        try:
            if state == 'exited':
                child.communicate(timeout=5)
                process.terminate_tree(child)
                return
            if state == 'running':
                # The parent is still dying when the group reports EPERM; a direct kill finishes it.
                process.terminate_tree(child)
                assert child.poll() is not None
                return
            monkeypatch.setattr(child, 'kill', lambda: None)
            monkeypatch.setattr(child, 'wait', lambda timeout=None: None)
            with pytest.raises(PermissionError, match='Operation not permitted'):
                process.terminate_tree(child)
        finally:
            kill()
            child.communicate(timeout=5)
