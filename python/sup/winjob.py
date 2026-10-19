"""Windows Job Object: closing the console kills every child instantly."""
from __future__ import annotations

import ctypes

from . import state
from .logx import log

JOB_OBJECT_EXTENDED_LIMIT = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


def setup_job_object():
    if state._K32 is None:
        return None
    try:
        import os

        if os.name != "nt":
            return None

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_size_t),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        k32 = state._K32
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k32.SetInformationJobObject.restype = ctypes.c_int
        k32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        k32.AssignProcessToJobObject.restype = ctypes.c_int
        k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        expected = ctypes.sizeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION)
        if expected != 144:
            log(f"job struct size {expected} unexpected (want 144) - skipping Job Object")
            return None

        job = k32.CreateJobObjectW(None, None)
        if not job:
            log(f"CreateJobObjectW failed (winerror {ctypes.get_last_error()}) "
                "- children stay alive after window close")
            return None

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT,
                                           ctypes.byref(info),
                                           ctypes.sizeof(info)):
            log(f"SetInformationJobObject failed (winerror {ctypes.get_last_error()}) "
                "- children stay alive after window close")
            k32.CloseHandle(job)
            return None

        log("Job Object active: closing this window shuts down all children")
        return job
    except Exception as exc:
        log(f"Job Object setup failed ({exc}) - continuing without it")
        return None


def assign_job(proc) -> None:
    if state.JOB is None or state._K32 is None or state._K32 is None:
        return
    try:
        if not state._K32.AssignProcessToJobObject(state.JOB, int(proc._handle)):
            log(f"AssignProcessToJobObject failed "
                f"(winerror {ctypes.get_last_error()})")
    except Exception as exc:
        log(f"job assign failed: {exc}")
