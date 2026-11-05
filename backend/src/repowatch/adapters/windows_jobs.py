"""受信任宿主命令的 Windows 进程树；仓库代码执行仍须使用容器适配。"""

import ctypes
import os
import subprocess
import time
from ctypes import wintypes


class _Limits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_longlong),
        ("job_time", ctypes.c_longlong),
        ("flags", wintypes.DWORD),
        ("min_ws", ctypes.c_size_t),
        ("max_ws", ctypes.c_size_t),
        ("active_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _IO(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "read_ops",
            "write_ops",
            "other_ops",
            "read_bytes",
            "write_bytes",
            "other_bytes",
        )
    ]


class _Extended(ctypes.Structure):
    _fields_ = [
        ("limits", _Limits),
        ("io", _IO),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process", ctypes.c_size_t),
        ("peak_job", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_longlong) for name in ("user", "kernel", "period_user", "period_kernel")
    ] + [(name, wintypes.DWORD) for name in ("faults", "total", "active", "terminated")]


def _kernel():
    if os.name != "nt":
        raise RuntimeError("此宿主命令适配需要 Windows 作业对象")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "OpenJobObjectW": ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": (
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
            wintypes.BOOL,
        ),
        "QueryInformationJobObject": (
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p],
            wintypes.BOOL,
        ),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "GetProcessTimes": (
            [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p],
            wintypes.BOOL,
        ),
        "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = arguments, result
    return api


def process_identity(pid: int) -> int | None:
    api = _kernel()
    handle = api.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:
            return None
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if api.WaitForSingleObject(handle, 0) == 0:
            return None
        values = [ctypes.c_ulonglong() for _ in range(4)]
        if not api.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
            raise ctypes.WinError(ctypes.get_last_error())
        return values[0].value
    finally:
        api.CloseHandle(handle)


def current_process() -> dict:
    if os.name != "nt":
        return {"platform": os.name, "pid": os.getpid(), "created": None}
    return {"platform": "windows", "pid": os.getpid(), "created": process_identity(os.getpid())}


class WindowsJob:
    def __init__(self, name: str, *, create: bool):
        self.api = _kernel()
        self.name = name
        if create:
            self.handle = self.api.CreateJobObjectW(None, name)
            existed = ctypes.get_last_error() == 183
        else:
            self.handle = self.api.OpenJobObjectW(0x0004 | 0x0008, False, name)
            existed = False
        if not self.handle:
            error = ctypes.get_last_error()
            if not create and error == 2:
                raise FileNotFoundError("受管作业已不存在")
            raise ctypes.WinError(error)
        if existed:
            self.close()
            raise FileExistsError("命令作业标识重复")
        if create:
            limits = _Extended()
            limits.limits.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.api.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                error = ctypes.get_last_error()
                self.close()
                raise ctypes.WinError(error)

    def assign(self, process: subprocess.Popen):
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def active(self) -> int:
        info = _Accounting()
        if not self.api.QueryInformationJobObject(
            self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.active

    def stop(self, timeout=5) -> None:
        if not self.api.TerminateJobObject(self.handle, 125):
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + timeout
        while self.active():
            if time.monotonic() >= deadline:
                raise TimeoutError("作业进程树尚未停止")
            time.sleep(0.01)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None
