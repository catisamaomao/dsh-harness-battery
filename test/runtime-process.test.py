"""Birth identity and ancestry gates prevent cleanup from following recycled PIDs."""
import os
from pathlib import Path
import signal
import sys
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime/bin'))
import process_control as pc
from dsh_adapter import ProviderBridge


def identity(pid,ppid=1,birth=1,uid=501,pgid=None):
    return pc.ProcessIdentity(pid,ppid,pid if pgid is None else pgid,uid,(birth,0))


@unittest.skipUnless(os.name=='posix','POSIX process identity and signal behavior')
class ProcessIdentityTests(unittest.TestCase):
    def setUp(self):
        self.root=identity(10001,9001,10)
        self.child=identity(10002,10001,11)

    def test_worker_registration_must_be_an_actual_descendant(self):
        unrelated=identity(10003,9002,12)
        table={self.root.pid:self.root,unrelated.pid:unrelated}
        registry=pc.WorkerRegistry(self.root)
        with patch.object(pc,'process_identity',side_effect=table.get),patch.object(pc.os,'kill') as kill:
            self.assertFalse(registry.register(unrelated.pid,True))
            self.assertNotIn(unrelated.pid,registry.identities)
            kill.assert_not_called()

    def test_foreign_uid_registration_is_rejected(self):
        foreign=identity(self.child.pid,self.root.pid,11,uid=777)
        table={self.root.pid:self.root,foreign.pid:foreign}
        registry=pc.WorkerRegistry(self.root)
        with patch.object(pc,'process_identity',side_effect=table.get),patch.object(pc.os,'kill') as kill:
            self.assertFalse(registry.register(foreign.pid,True))
            kill.assert_not_called()

    def test_registration_rejects_a_reused_dsh_root(self):
        changed_root=identity(self.root.pid,self.root.ppid,99)
        table={self.root.pid:changed_root,self.child.pid:self.child}
        registry=pc.WorkerRegistry(self.root)
        with patch.object(pc,'process_identity',side_effect=table.get),patch.object(pc.os,'kill') as kill:
            self.assertFalse(registry.register(self.child.pid,True))
            kill.assert_not_called()

    def test_cleanup_skips_a_reused_worker_pid_even_with_same_uid_and_group(self):
        replacement=identity(self.child.pid,self.child.ppid,99)
        registry=pc.WorkerRegistry(self.root)
        registry.identities[self.child.pid]=self.child
        registry.detached.add(self.child.pid)
        table={self.child.pid:replacement}  # Original DSH leader has exited.
        with patch.object(pc,'process_identity',side_effect=table.get),patch.object(pc.os,'kill') as kill, \
             patch.object(pc.os,'killpg') as killpg:
            result=registry.cleanup()
            self.assertEqual(result['identity_changes_skipped'],1)
            kill.assert_not_called()
            killpg.assert_not_called()

    def test_exited_group_leader_does_not_authorize_unknown_group_members(self):
        registry=pc.WorkerRegistry(self.root)
        registry.identities[self.child.pid]=self.child
        registry.detached.add(self.child.pid)
        with patch.object(pc,'process_identity',return_value=None), \
             patch.object(pc,'process_pids',return_value=[77777]) as listing, \
             patch.object(pc.os,'kill') as kill,patch.object(pc.os,'killpg') as killpg:
            registry.cleanup()
            listing.assert_not_called()
            kill.assert_not_called()
            killpg.assert_not_called()

    def test_signal_rechecks_birth_immediately_before_use(self):
        replacement=identity(self.child.pid,self.child.ppid,22)
        with patch.object(pc,'process_identity',return_value=replacement),patch.object(pc.os,'kill') as kill:
            self.assertFalse(pc.signal_bound(self.child,signal.SIGSTOP))
            self.assertFalse(pc.signal_bound(self.child,signal.SIGKILL))
            kill.assert_not_called()

    def test_popen_cleanup_never_rebinds_a_reaped_pid(self):
        process=Mock(pid=self.child.pid,_harness_identity=self.child)
        process.poll.return_value=0
        with patch.object(pc,'process_identity',return_value=identity(self.child.pid,birth=222)), \
             patch.object(pc.os,'kill') as kill,patch.object(pc.os,'killpg') as killpg:
            pc.kill_tree(process)
            kill.assert_not_called()
            killpg.assert_not_called()

    def test_public_control_frame_is_not_accepted_as_process_authority(self):
        bridge=ProviderBridge('mock',None,'deepseek-flash',2,256,5)
        bridge.workers=pc.WorkerRegistry(self.root)
        unrelated=identity(10003,9002,12)
        table={self.root.pid:self.root,unrelated.pid:unrelated}
        with patch.object(pc,'process_identity',side_effect=table.get),patch.object(pc.os,'kill') as kill:
            bridge.register_worker({'pid':unrelated.pid,'detached':True})
            self.assertNotIn(unrelated.pid,bridge.workers.identities)
            kill.assert_not_called()

    @unittest.skipUnless(sys.platform=='darwin' or sys.platform.startswith('linux'),'supported kernel identity API')
    def test_kernel_identity_matches_the_test_process(self):
        actual=pc.process_identity(os.getpid())
        self.assertIsNotNone(actual)
        self.assertEqual(actual.ppid,os.getppid())
        self.assertEqual(actual.uid,os.getuid())
        self.assertEqual(actual.pgid,os.getpgid(0))
        self.assertTrue(pc.same_process(actual,pc.process_identity(os.getpid())))


if __name__=='__main__':unittest.main()
