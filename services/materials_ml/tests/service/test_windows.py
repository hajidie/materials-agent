import ctypes as C
from ctypes import wintypes as W
import json
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from materials_ml_service.windows import JobProcess, RunnerError, SingleWorker, kernel, recover_job

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows process-tree acceptance")


def job_name():
    return "Local\\MaterialsML-" + uuid4().hex + "-" + uuid4().hex


def process_handle(pid):
    k = kernel()
    k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    k.OpenProcess.restype = W.HANDLE
    handle = k.OpenProcess(0x100000, False, pid)
    assert handle
    return handle


def assert_exited(handle, timeout=10000):
    k = kernel()
    k.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
    k.WaitForSingleObject.restype = W.DWORD
    try:
        assert k.WaitForSingleObject(handle, timeout) == 0
    finally:
        k.CloseHandle(handle)


def active_job_handles(name):
    k = kernel()
    handle = k.OpenJobObjectW(0x1F003F, False, name)
    if not handle:
        return []
    class PIDs(C.Structure):
        _fields_ = [("assigned", W.DWORD), ("count", W.DWORD), ("ids", C.c_size_t * 64)]
    try:
        info = PIDs()
        assert k.QueryInformationJobObject(handle, 3, C.byref(info), C.sizeof(info), None)
        return [process_handle(pid) for pid in info.ids[:info.count]]
    finally:
        k.CloseHandle(handle)


def test_job_gate_does_not_train_before_release_and_stop_leaves_no_process(tmp_path):
    process = JobProcess(job_name(), tmp_path)
    handle = process_handle(process.process.pid)
    try:
        time.sleep(.2)
        assert process.poll() is None
        assert list(tmp_path.iterdir()) == []
        process.stop()
        assert_exited(handle)
        assert process.poll() is not None
    finally:
        process.close()


def test_worker_parent_exit_kills_managed_child(tmp_path):
    program = """
from materials_ml_service.windows import JobProcess
import time, sys
p = JobProcess(sys.argv[1], sys.argv[2])
print(p.process.pid, flush=True)
time.sleep(60)
"""
    parent = subprocess.Popen([sys.executable, "-I", "-c", program, job_name(), str(tmp_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        line = parent.stdout.readline()
        assert line, parent.stderr.read().decode(errors="replace")
        handle = process_handle(int(line))
        parent.kill(); parent.wait(timeout=5)
        assert_exited(handle)
    finally:
        if parent.poll() is None:
            parent.kill(); parent.wait(timeout=5)


def test_eof_before_job_assignment_exits_without_training(tmp_path):
    child = subprocess.Popen([sys.executable, "-I", "-m", "materials_ml_service.child", str(tmp_path)],
                              stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child.stdin.close()
    assert child.wait(timeout=10) == 2
    assert list(tmp_path.iterdir()) == []


def test_single_worker_lock_and_recovery(tmp_path):
    identity = uuid4().hex
    with SingleWorker(identity):
        with pytest.raises(RunnerError, match="WORKER_ALREADY_RUNNING"):
            SingleWorker(identity)
    name = job_name()
    process = JobProcess(name, tmp_path)
    try:
        recover_job(name)
        assert process.process.wait(timeout=5) is not None
    finally:
        process.close()


def test_job_assignment_failure_stops_unassigned_gated_child(tmp_path, monkeypatch):
    import materials_ml_service.windows as windows
    actual_kernel, actual_popen = kernel(), subprocess.Popen
    handles = []
    class FailedAssignment:
        def __getattr__(self, key):
            return getattr(actual_kernel, key)
        def AssignProcessToJobObject(self, *args):
            return False
    def capture(*args, **kwargs):
        process = actual_popen(*args, **kwargs)
        handles.append(process_handle(process.pid))
        assert not any(key.startswith("ML_") for key in kwargs["env"])
        return process
    monkeypatch.setattr(windows, "kernel", FailedAssignment)
    monkeypatch.setattr(windows.subprocess, "Popen", capture)
    with pytest.raises(RunnerError, match="JOB_SETUP_FAILED"):
        JobProcess(job_name(), tmp_path)
    assert len(handles) == 1
    assert_exited(handles[0])
    assert list(tmp_path.iterdir()) == []
