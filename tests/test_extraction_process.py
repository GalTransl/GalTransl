"""Process cleanup preserves real permission failures after cancellation."""
import errno
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from GalTransl.Extraction import process


@pytest.mark.parametrize('exited', [True, False], ids=['exited', 'running'])
def test_process_group_permission_error_depends_on_parent_state(monkeypatch, exited):
    def denied(pid, signum):
        # macOS can return EPERM when a process group contains only zombies.
        raise PermissionError(errno.EPERM, 'Operation not permitted')

    monkeypatch.setattr(process, 'os', SimpleNamespace(name='posix', killpg=denied))
    monkeypatch.setattr(signal, 'SIGKILL', 9, raising=False)
    with subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                          stdin=subprocess.PIPE) as child:
        try:
            if exited:
                child.communicate(timeout=5)
                process.terminate_tree(child)
                return
            with pytest.raises(PermissionError, match='Operation not permitted'):
                process.terminate_tree(child)
        finally:
            child.kill()
            child.communicate(timeout=5)
