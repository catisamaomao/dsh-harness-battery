"""Stop only birth-identity-bound workers verified as children of this run."""
import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    ppid: int
    pgid: int
    uid: int
    birth: tuple


class _BsdInfo(ctypes.Structure):
    # Darwin sys/proc_info.h, struct proc_bsdinfo (PROC_PIDTBSDINFO=3).
    _fields_ = [(name, ctypes.c_uint32) for name in (
        'flags','status','xstatus','pid','ppid','uid','gid','ruid','rgid','svuid','svgid','reserved')]
    _fields_ += [('comm',ctypes.c_char*16),('name',ctypes.c_char*32)]
    _fields_ += [(name,ctypes.c_uint32) for name in ('nfiles','pgid','jobc','tdev','tpgid')]
    _fields_ += [('nice',ctypes.c_int32),('start_sec',ctypes.c_uint64),('start_usec',ctypes.c_uint64)]


def process_identity(pid):
    if type(pid) is not int or pid <= 1:
        return None
    if sys.platform == 'darwin':
        lib=ctypes.CDLL('/usr/lib/libproc.dylib',use_errno=True)
        function=lib.proc_pidinfo
        function.argtypes=[ctypes.c_int,ctypes.c_int,ctypes.c_uint64,ctypes.c_void_p,ctypes.c_int]
        function.restype=ctypes.c_int
        info=_BsdInfo()
        size=function(pid,3,0,ctypes.byref(info),ctypes.sizeof(info))
        if size != ctypes.sizeof(info) or info.pid != pid or not info.start_sec:
            return None
        return ProcessIdentity(pid,info.ppid,info.pgid,info.uid,(info.start_sec,info.start_usec))
    if sys.platform.startswith('linux'):
        try:
            folder=Path('/proc')/str(pid)
            fields=(folder/'stat').read_text(encoding='ascii').rsplit(')',1)[1].split()
            return ProcessIdentity(pid,int(fields[1]),int(fields[2]),folder.stat().st_uid,(int(fields[19]),))
        except (OSError,ValueError,IndexError):
            return None
    return None


def same_process(expected, actual):
    return expected is not None and actual is not None and (
        expected.pid,expected.uid,expected.birth)==(actual.pid,actual.uid,actual.birth)


def process_pids(pid, *, group=False):
    if sys.platform == 'darwin':
        library=ctypes.CDLL('/usr/lib/libproc.dylib',use_errno=True)
        function=library.proc_listpgrppids if group else library.proc_listchildpids
        function.argtypes=[ctypes.c_int,ctypes.c_void_p,ctypes.c_int]
        function.restype=ctypes.c_int
        buffer=(ctypes.c_int*16384)()
        count=function(pid,buffer,ctypes.sizeof(buffer))
        if count < 0 or count >= len(buffer):
            raise RuntimeError('cannot enumerate bounded DSH worker descendants')
        return [int(value) for value in buffer[:count] if value > 0]
    if sys.platform.startswith('linux'):
        result=[]
        for entry in Path('/proc').iterdir():
            if entry.name.isdigit():
                identity=process_identity(int(entry.name))
                if identity and (identity.pgid if group else identity.ppid)==pid:
                    result.append(identity.pid)
        return result
    return []


def verified_descendant(root, candidate):
    """Verify kernel ancestry and both endpoints; a control-frame PID is not authority."""
    if not same_process(root,process_identity(root.pid)):
        return False
    current=candidate
    seen=set()
    while current and current.pid not in seen and current.pid > 1:
        if same_process(root,current):
            return same_process(candidate,process_identity(candidate.pid))
        seen.add(current.pid)
        current=process_identity(current.ppid)
    return False


def signal_bound(identity, kind):
    # Never follow a recycled PID, even if its new owner has the same UID/PGID.
    if identity.pid == os.getpid() or not same_process(identity,process_identity(identity.pid)):
        return False
    try:
        os.kill(identity.pid,kind)
        return True
    except ProcessLookupError:
        return False


def kill_bound_tree(root):
    stopped=[]
    seen=set()
    def freeze(identity):
        key=(identity.pid,identity.birth)
        if key in seen or not signal_bound(identity,signal.SIGSTOP):
            return
        seen.add(key)
        stopped.append(identity)
        if not same_process(identity,process_identity(identity.pid)):
            return
        for child_pid in process_pids(identity.pid):
            child=process_identity(child_pid)
            if child and child.ppid==identity.pid and child.uid==identity.uid:
                freeze(child)
    try:
        freeze(root)
    finally:
        failure=None
        for identity in reversed(stopped):
            try:
                signal_bound(identity,signal.SIGKILL)
            except OSError as error:
                # Do not strand our verified process in SIGSTOP on cleanup error.
                signal_bound(identity,signal.SIGCONT)
                failure=error
        if failure is not None:raise failure
    return len(stopped)


def bind_process(process):
    identity=process_identity(process.pid)
    if identity is None:
        raise RuntimeError('worker birth identity unavailable; cannot establish safe cleanup')
    process._harness_identity=identity
    return identity


def kill_tree(process):
    if os.name != 'posix':
        if process.poll() is None:
            process.kill()
        return
    # A live, unreaped Popen child cannot have its PID reused. Once reaped, only
    # the identity captured at spawn is acceptable; never look up a fresh owner.
    identity=getattr(process,'_harness_identity',None)
    if identity is None and process.poll() is None:
        identity=bind_process(process)
    if identity is not None:
        kill_bound_tree(identity)


class WorkerRegistry:
    def __init__(self, root):
        self.root=root
        self.identities={root.pid:root}
        self.detached={root.pid} if root.pgid==root.pid else set()
        self.gated=set()
        self.unverified=set()
        self.changed=set()
        self.lock=threading.RLock()

    def register(self, pid, detached, *, gated=False, resume=True):
        identity=process_identity(pid)
        with self.lock:
            if not identity or identity.uid != self.root.uid or not verified_descendant(self.root,identity):
                self.unverified.add(pid)
                return False
            if len(self.identities)>=2048:
                raise ValueError('child-worker registry budget exceeded')
            previous=self.identities.get(pid)
            if previous and not same_process(previous,identity):
                self.changed.add(pid)
                return False
            self.identities[pid]=identity
            if gated:self.gated.add(pid)
            if detached and identity.pgid==pid:
                self.detached.add(pid)
        self.refresh(resume=resume)
        return True

    def refresh(self, *, resume=True):
        # Discover children only through a still-matching ancestor. A historical
        # group number alone can never establish membership after its leader exits.
        with self.lock:
            queue=list(self.identities.values())
            visited=set()
            while queue:
                parent=queue.pop()
                if parent.pid in visited or not same_process(parent,process_identity(parent.pid)):
                    continue
                visited.add(parent.pid)
                candidates=process_pids(parent.pid)
                if parent.pid in self.detached:
                    try:
                        if os.getsid(parent.pid)==parent.pid:
                            candidates+=process_pids(parent.pid,group=True)
                    except ProcessLookupError:
                        pass
                for pid in candidates:
                    child=process_identity(pid)
                    if not child or child.uid!=self.root.uid or child.pid==self.root.pid:
                        continue
                    # A detached session can retain orphan members while its
                    # original, birth-bound session leader remains alive.
                    member=child.ppid==parent.pid
                    if not member and parent.pid in self.detached and child.pgid==parent.pid:
                        try:member=os.getsid(child.pid)==parent.pid
                        except ProcessLookupError:member=False
                    if not member or not same_process(parent,process_identity(parent.pid)):
                        continue
                    previous=self.identities.get(pid)
                    if previous and not same_process(previous,child):
                        self.changed.add(pid)
                        continue
                    if len(self.identities)>=2048:
                        raise ValueError('child-worker registry budget exceeded')
                    self.identities[pid]=child
                    queue.append(child)
            if resume:
                for pid in self.gated:
                    # The wrapper's exit gate keeps the session leader alive
                    # until this scan binds any otherwise orphaned background jobs.
                    signal_bound(self.identities[pid],signal.SIGCONT)

    def cleanup(self):
        self.refresh(resume=False)
        with self.lock:
            entries=list(self.identities.values())
        for identity in reversed(entries):
            actual=process_identity(identity.pid)
            if actual is not None and not same_process(identity,actual):
                self.changed.add(identity.pid)
                continue
            if actual is not None:
                kill_bound_tree(identity)
        # Deliberately no killpg(old_pid): that group may now belong to a new run.
        return {'registered_processes':len(entries),'identity_changes_skipped':len(self.changed),
                'unverified_registrations_skipped':len(self.unverified),
                'scope':'birth-bound workers and descendants; unknown or reused identities never signalled'}


def run_child(command, *, capture_output=False, timeout=None, check=False, input=None, **kwargs):
    if capture_output:
        kwargs.update(stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if input is not None:
        kwargs['stdin']=subprocess.PIPE
    process=subprocess.Popen(command,start_new_session=True,**kwargs)
    try:
        bind_process(process)
        stdout,stderr=process.communicate(input,timeout=timeout)
        result=subprocess.CompletedProcess(command,process.returncode,stdout,stderr)
        if check:result.check_returncode()
        return result
    finally:
        kill_tree(process)
        if process.poll() is None:process.communicate(timeout=5)
