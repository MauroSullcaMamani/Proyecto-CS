"""Job Object de Windows para limitar memoria y cerrar el árbol de procesos."""
import ctypes
from ctypes import wintypes


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", ctypes.c_uint64 * 6),  # IO_COUNTERS
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


_kernel = ctypes.WinDLL("kernel32", use_last_error=True)


def _api(name, result, *args):
    func = getattr(_kernel, name)
    func.restype, func.argtypes = result, args
    return func


_create = _api("CreateJobObjectW", wintypes.HANDLE, ctypes.c_void_p, wintypes.LPCWSTR)
_configure = _api("SetInformationJobObject", wintypes.BOOL, wintypes.HANDLE,
                  ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
_open = _api("OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_assign = _api("AssignProcessToJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE)
_close = _api("CloseHandle", wintypes.BOOL, wintypes.HANDLE)
_snapshot = _api("CreateToolhelp32Snapshot", wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD)
_thread_first = _api("Thread32First", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(_ThreadEntry))
_thread_next = _api("Thread32Next", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(_ThreadEntry))
_open_thread = _api("OpenThread", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_resume = _api("ResumeThread", wintypes.DWORD, wintypes.HANDLE)

CREATE_SUSPENDED = 0x00000004
_EXTENDED_LIMIT_INFORMATION = 9
_JOB_MEMORY = 0x00000200
_KILL_ON_JOB_CLOSE = 0x00002000
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_SNAP_THREADS = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_INVALID_DWORD = 0xFFFFFFFF


def resume_suspended_process(pid):
    # Popen conserva el handle del proceso, pero cierra el del hilo primario.
    snapshot = _snapshot(_SNAP_THREADS, 0)
    if snapshot == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = _ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        valid = _thread_first(snapshot, ctypes.byref(entry))
        while valid:
            if entry.th32OwnerProcessID == pid:
                thread = _open_thread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if not thread:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    if _resume(thread) == _INVALID_DWORD:
                        raise ctypes.WinError(ctypes.get_last_error())
                    return
                finally:
                    _close(thread)
            entry.dwSize = ctypes.sizeof(entry)
            valid = _thread_next(snapshot, ctypes.byref(entry))
        raise OSError(f"No se encontró el hilo inicial del proceso {pid}")
    finally:
        _close(snapshot)


class WindowsJob:
    def __init__(self, memory_bytes):
        self.handle = _create(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _JOB_MEMORY | _KILL_ON_JOB_CLOSE
        limits.JobMemoryLimit = memory_bytes
        if not _configure(self.handle, _EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits),
                          ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def attach(self, pid):
        process = _open(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not _assign(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            _close(process)

    def close(self):
        if self.handle:
            if not _close(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = None
