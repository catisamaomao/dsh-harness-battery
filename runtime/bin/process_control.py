"""Stop worker descendants, including DSH tools that create separate sessions."""
import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def process_pids(pid, *, group=False):
    if sys.platform == 'darwin':
        # libproc returns a PID count (not byte count), including other sessions.
        library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        function = library.proc_listpgrppids if group else library.proc_listchildpids
        function.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        function.restype = ctypes.c_int
        buffer = (ctypes.c_int * 16384)()
        count = function(pid, buffer, ctypes.sizeof(buffer))
        if count < 0 or count >= len(buffer):
            raise RuntimeError('cannot enumerate bounded DSH worker descendants')
        return [int(value) for value in buffer[:count] if value > 0]
    if sys.platform.startswith('linux'):
        result = []
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry/'stat').read_text().rsplit(')', 1)[1].split()
                if int(fields[2 if group else 1]) == pid:
                    result.append(int(entry.name))
            except (OSError, ValueError, IndexError):
                pass
        return result
    return []


def kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS can return EPERM for an already-empty detached group. Only
        # accept that outcome after independently proving it has no members.
        for attempt in range(25):
            if not process_pids(pid, group=True):
                return
            time.sleep(0.02)
        raise


def kill_tree(process):
    if os.name != 'posix':
        if process.poll() is None:
            process.kill()
        return
    stopped = []
    seen = set()
    def freeze(pid):
        if pid in seen or pid <= 1 or pid == os.getpid():
            return
        seen.add(pid)
        try:
            os.kill(pid, signal.SIGSTOP)
        except ProcessLookupError:
            return
        stopped.append(pid)
        # Freeze each ancestor before discovering children so active tool workers
        # cannot keep forking while the controller walks down the tree.
        for child in process_pids(pid):
            freeze(child)
    try:
        if process.poll() is None:
            freeze(process.pid)
    finally:
        for pid in reversed(stopped):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        kill_group(process.pid)



def kill_registered_worker(pid, detached=False):
    class Worker:
        def __init__(self, ident): self.pid=ident
        def poll(self):
            try: os.kill(self.pid,0)
            except ProcessLookupError: return 0
            return None
    # If its launcher has already exited, a detached group can still contain
    # background jobs. killpg works without its original group leader.
    worker=Worker(pid)
    if worker.poll() is None:
        kill_tree(worker)
    if detached:
        kill_group(pid)


def run_child(command, *, capture_output=False, timeout=None, check=False, input=None, **kwargs):
    if capture_output:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if input is not None:
        kwargs['stdin'] = subprocess.PIPE
    process = subprocess.Popen(command, start_new_session=True, **kwargs)
    try:
        stdout, stderr = process.communicate(input, timeout=timeout)
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result
    finally:
        kill_tree(process)
        if process.poll() is None:
            process.communicate(timeout=5)
