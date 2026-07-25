"""Portable physical and process memory measurements."""

from __future__ import annotations

import ctypes
import os
import sys


GiB = 1024**3


def physical_memory_bytes() -> int:
    """Return installed physical RAM without requiring psutil."""

    if os.name == "nt":

        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys)
        except (AttributeError, OSError):
            pass
    try:
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        if pages > 0 and page_size > 0:
            return pages * page_size
    except (AttributeError, OSError, ValueError):
        pass
    return 16 * GiB


def process_memory_bytes() -> tuple[int, int]:
    """Return current RSS and process peak RSS using the standard library."""

    if os.name == "nt":

        class _ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        try:
            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            psapi.GetProcessMemoryInfo.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_ProcessMemoryCounters),
                ctypes.c_ulong,
            ]
            psapi.GetProcessMemoryInfo.restype = ctypes.c_int
            handle = kernel32.GetCurrentProcess()
            if psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            ):
                return (
                    int(counters.WorkingSetSize),
                    int(counters.PeakWorkingSetSize),
                )
        except (AttributeError, OSError):
            pass
    try:
        status = {}
        with open("/proc/self/status", "r", encoding="ascii") as handle:
            for line in handle:
                name, _separator, value = line.partition(":")
                if name in {"VmRSS", "VmHWM"}:
                    status[name] = int(value.strip().split()[0]) * 1024
        if status:
            rss = int(status.get("VmRSS", 0))
            return rss, int(status.get("VmHWM", rss))
    except (OSError, ValueError):
        pass
    try:
        import resource

        maximum = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        maximum_bytes = maximum if sys.platform == "darwin" else maximum * 1024
        return 0, maximum_bytes
    except (ImportError, OSError, ValueError):
        pass
    return 0, 0
