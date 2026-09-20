"""Small Windows-only process-tree adapter; no training or service dependencies."""
import ctypes as C
from ctypes import wintypes as W
import os
import re
import subprocess
import sys
import threading
import time


class RunnerError(RuntimeError):
    pass


class BasicLimit(C.Structure):
    _fields_ = [("process_time", C.c_int64), ("job_time", C.c_int64), ("flags", W.DWORD),
                ("min_working", C.c_size_t), ("max_working", C.c_size_t), ("active_limit", W.DWORD),
                ("affinity", C.c_size_t), ("priority", W.DWORD), ("scheduling", W.DWORD)]


class IOCounters(C.Structure):
    _fields_ = [(name, C.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]


class ExtendedLimit(C.Structure):
    _fields_ = [("basic", BasicLimit), ("io", IOCounters), ("process_memory", C.c_size_t),
                ("job_memory", C.c_size_t), ("peak_process", C.c_size_t), ("peak_job", C.c_size_t)]


class Accounting(C.Structure):
    _fields_ = [(name, C.c_int64) for name in ("user", "kernel", "period_user", "period_kernel")] + [
        (name, W.DWORD) for name in ("faults", "total", "active", "terminated")]


def kernel():
    if sys.platform != "win32":
        raise RunnerError("WINDOWS_REQUIRED")
    k = C.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([C.c_void_p, W.LPCWSTR], W.HANDLE),
        "OpenJobObjectW": ([W.DWORD, W.BOOL, W.LPCWSTR], W.HANDLE),
        "SetInformationJobObject": ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
        "QueryInformationJobObject": ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.c_void_p], W.BOOL),
        "AssignProcessToJobObject": ([W.HANDLE, W.HANDLE], W.BOOL),
        "IsProcessInJob": ([W.HANDLE, W.HANDLE, C.POINTER(W.BOOL)], W.BOOL),
        "TerminateJobObject": ([W.HANDLE, W.UINT], W.BOOL),
        "CloseHandle": ([W.HANDLE], W.BOOL),
        "CreateMutexW": ([C.c_void_p, W.BOOL, W.LPCWSTR], W.HANDLE),
        "ReleaseMutex": ([W.HANDLE], W.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(k, name)
        function.argtypes, function.restype = args, result
    return k


def _valid_job(name):
    if not re.fullmatch(r"Local\\MaterialsML(?:Prediction)?-[0-9a-f]{32}-[0-9a-f]{32}", name):
        raise RunnerError("INVALID_JOB_IDENTITY")


def _empty(k, handle, timeout=5):
    deadline = time.monotonic() + timeout
    while True:
        info = Accounting()
        if not k.QueryInformationJobObject(handle, 1, C.byref(info), C.sizeof(info), None):
            raise RunnerError("JOB_STATE_UNKNOWN")
        if info.active == 0:
            return
        if time.monotonic() >= deadline:
            raise RunnerError("PROCESS_STOP_UNCONFIRMED")
        time.sleep(.05)


def recover_job(name):
    """Stop only the exact service-issued named job; absence means its handle/tree is gone."""
    _valid_job(name)
    k = kernel()
    handle = k.OpenJobObjectW(0x1F003F, False, name)
    if not handle:
        if C.get_last_error() == 2:
            return
        raise RunnerError("JOB_STATE_UNKNOWN")
    try:
        if not k.TerminateJobObject(handle, 1):
            raise RunnerError("PROCESS_STOP_UNCONFIRMED")
        _empty(k, handle)
    finally:
        k.CloseHandle(handle)


class SingleWorker:
    def __init__(self, identity, *, service=False):
        self.k = kernel()
        prefix = "Local\\MaterialsMLService-" if service else "Local\\MaterialsMLWorker-"
        self.handle = self.k.CreateMutexW(None, True, prefix + identity)
        error = C.get_last_error()
        if not self.handle or error == 183:
            if self.handle:
                self.k.CloseHandle(self.handle)
            raise RunnerError("WORKER_ALREADY_RUNNING")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.k.ReleaseMutex(self.handle)
        self.k.CloseHandle(self.handle)


class JobProcess:
    def __init__(self, name, folder, *, prediction=False):
        _valid_job(name)
        self.k, self.handle, self.process = kernel(), None, None
        self.lock = threading.RLock()
        try:
            self.handle = self.k.CreateJobObjectW(None, name)
            if self.handle and C.get_last_error() == 183:
                self.close()  # Never adopt or stop an existing job owned by another execution.
                raise RunnerError("JOB_SETUP_FAILED")
            if not self.handle:
                raise RunnerError("JOB_SETUP_FAILED")
            limits = ExtendedLimit()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.k.SetInformationJobObject(self.handle, 9, C.byref(limits), C.sizeof(limits)):
                raise RunnerError("JOB_SETUP_FAILED")
            env = {k: v for k, v in os.environ.items() if k.upper() in
                   {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "PATH"}}
            module = "materials_ml_service.prediction_child" if prediction else "materials_ml_service.child"
            self.process = subprocess.Popen([sys.executable, "-I", "-m", module, str(folder)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW, env=env)
            # CPython 3.11 owns this handle throughout Popen's lifetime; never close it here.
            handle = int(self.process._handle)
            if not self.k.AssignProcessToJobObject(self.handle, handle):
                raise RunnerError("JOB_SETUP_FAILED")
            member = W.BOOL()
            if not self.k.IsProcessInJob(handle, self.handle, C.byref(member)) or not member.value:
                raise RunnerError("JOB_SETUP_FAILED")
        except Exception:
            self.stop()
            self.close()
            raise RunnerError("JOB_SETUP_FAILED") from None

    def release(self):
        with self.lock:
            if self.process.poll() is not None:
                raise RunnerError("CHILD_EXITED_BEFORE_START")
            self.process.stdin.write(b"GO\n")
            self.process.stdin.flush()
            self.process.stdin.close()

    def poll(self):
        return self.process.poll()

    def stop(self):
        with self.lock:
            if self.process is not None:
                if self.process.stdin and not self.process.stdin.closed:
                    self.process.stdin.close()  # EOF exits a not-yet-managed child's startup gate.
                if self.handle:
                    if not self.k.TerminateJobObject(self.handle, 1):
                        raise RunnerError("PROCESS_STOP_UNCONFIRMED")
                if self.process.poll() is None:
                    self.process.kill()  # Also covers failure before job assignment.
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    raise RunnerError("PROCESS_STOP_UNCONFIRMED") from None
            if self.handle:
                _empty(self.k, self.handle)

    def close(self):
        with self.lock:
            if self.handle:
                self.k.CloseHandle(self.handle)
                self.handle = None
