"""Small OS boundary for locks, shell arguments and owned child processes."""
import base64
import errno
import os
from pathlib import Path
import shlex
import re
import shutil
import signal
import subprocess
import sys
import time

WINDOWS = os.name == 'nt'
SIGNALS = tuple(getattr(signal, name) for name in ('SIGTERM', 'SIGINT', 'SIGHUP') if hasattr(signal, name))
if WINDOWS:
    import ctypes as ct
    from ctypes import wintypes as wt
    import msvcrt
    LOCK_EX, LOCK_NB = 2, 4
    os.environ['PYTHONUTF8'] = '1'
else:
    from fcntl import flock, LOCK_EX, LOCK_NB


if WINDOWS:
    def flock(stream, operation):
        deadline = time.monotonic() + 7
        while True:
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if operation & LOCK_NB:
                    raise BlockingIOError('Guard state is already locked') from exc
                if time.monotonic() >= deadline:
                    raise TimeoutError('Guard state lock exceeded 7 seconds') from exc
                time.sleep(0.05)


def lexer(command, punctuation=False):
    class ShellLexer(shlex.shlex):
        def get_token(self):
            token = super().get_token()
            return token.replace('\ue000', "'") if WINDOWS and isinstance(token, str) else token
    if WINDOWS:
        if '\ue000' in command:
            raise ValueError('Unsupported shell character')
        text, state, index = [], None, 0
        while index < len(command):
            char = command[index]
            if state == "'" and command[index:index + 2] == "''":
                text.append('\ue000')
                index += 2
                continue
            text.append(char)
            if state != "'" and char == '`' and index + 1 < len(command):
                index += 1
                text.append(command[index])
            elif char == state:
                state = None
            elif state is None and char in ("'", '"'):
                state = char
            index += 1
        command = ''.join(text)
    result = ShellLexer(command, posix=True, punctuation_chars=punctuation)
    result.whitespace_split = True
    if WINDOWS:
        result.escape = '`'  # PowerShell preserves backslashes, including C:\ paths.
    return result


def split(command):
    if not WINDOWS:
        return shlex.split(command)
    words = list(lexer(command))
    return words[1:] if words[:1] == ['&'] else words


def quote(value):
    return "'" + str(value).replace("'", "''") + "'" if WINDOWS else shlex.quote(str(value))


def join(words):
    return ('& ' + ' '.join(quote(word) for word in words)) if WINDOWS else shlex.join(words)


def python_command(script, *arguments):
    python = [sys.executable, '-X', 'utf8'] if WINDOWS else ['python3']
    return join([*python, str(script), *map(str, arguments)])


def basename(value):
    name = Path(value).name.lower() if WINDOWS else Path(value).name
    return re.sub(r'\.(?:exe|cmd|bat|ps1)$', '', name) if WINDOWS else name


def powershell(command):
    command = ("$ProgressPreference = 'SilentlyContinue'; "
               "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
               "$OutputEncoding = [Console]::OutputEncoding\n" + command)
    encoded = base64.b64encode(command.encode('utf-16-le')).decode('ascii')
    return [str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'),
            '-NoLogo', '-NoProfile', '-NonInteractive', '-OutputFormat', 'Text', '-EncodedCommand', encoded]


def portable_command(command):
    # EncodedCommand survives both cmd.exe and PowerShell, including spaces/Unicode.
    return subprocess.list2cmdline(powershell(command)) if WINDOWS else command


def agy_binary():
    config = Path(__file__).with_name('runtime.json')
    if config.exists():
        import json
        return Path(json.loads(config.read_text(encoding='utf-8'))['agy'])
    candidate = Path.home() / '.local/bin/agy'
    return Path(shutil.which('agy.exe') or candidate) if WINDOWS else candidate


if WINDOWS:
    kernel = ct.WinDLL('kernel32', use_last_error=True)

    class BasicLimits(ct.Structure):
        _fields_ = [('ProcessTime', ct.c_int64), ('JobTime', ct.c_int64), ('LimitFlags', wt.DWORD),
                    ('MinWorkingSet', ct.c_size_t), ('MaxWorkingSet', ct.c_size_t),
                    ('ActiveProcessLimit', wt.DWORD), ('Affinity', ct.c_size_t),
                    ('PriorityClass', wt.DWORD), ('SchedulingClass', wt.DWORD)]

    class ExtendedLimits(ct.Structure):
        _fields_ = [('Basic', BasicLimits), ('IoCounters', ct.c_uint64 * 6),
                    ('ProcessMemory', ct.c_size_t), ('JobMemory', ct.c_size_t),
                    ('PeakProcessMemory', ct.c_size_t), ('PeakJobMemory', ct.c_size_t)]

    class ThreadEntry(ct.Structure):
        _fields_ = [('size', wt.DWORD), ('usage', wt.DWORD), ('thread', wt.DWORD),
                    ('owner', wt.DWORD), ('basePriority', wt.LONG), ('deltaPriority', wt.LONG),
                    ('flags', wt.DWORD)]

    class Accounting(ct.Structure):
        _fields_ = [('Times', ct.c_int64 * 4), ('PageFaults', wt.DWORD),
                    ('TotalProcesses', wt.DWORD), ('ActiveProcesses', wt.DWORD), ('Terminated', wt.DWORD)]

    for name, args, result in [
        ('CreateJobObjectW', [ct.c_void_p, wt.LPCWSTR], wt.HANDLE),
        ('SetInformationJobObject', [wt.HANDLE, ct.c_int, ct.c_void_p, wt.DWORD], wt.BOOL),
        ('AssignProcessToJobObject', [wt.HANDLE, wt.HANDLE], wt.BOOL),
        ('TerminateJobObject', [wt.HANDLE, wt.UINT], wt.BOOL),
        ('QueryInformationJobObject', [wt.HANDLE, ct.c_int, ct.c_void_p, wt.DWORD, ct.c_void_p], wt.BOOL),
        ('CloseHandle', [wt.HANDLE], wt.BOOL),
        ('CreateToolhelp32Snapshot', [wt.DWORD, wt.DWORD], wt.HANDLE),
        ('Thread32First', [wt.HANDLE, ct.POINTER(ThreadEntry)], wt.BOOL),
        ('Thread32Next', [wt.HANDLE, ct.POINTER(ThreadEntry)], wt.BOOL),
        ('OpenThread', [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE),
        ('ResumeThread', [wt.HANDLE], wt.DWORD),
    ]:
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result

    def checked(value):
        if not value:
            raise ct.WinError(ct.get_last_error())
        return value

    def resume(child):
        snapshot = kernel.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ct.c_void_p(-1).value:
            raise ct.WinError(ct.get_last_error())
        try:
            entry = ThreadEntry()
            entry.size = ct.sizeof(entry)
            more = kernel.Thread32First(snapshot, ct.byref(entry))
            while more:
                if entry.owner == child.pid:
                    thread = checked(kernel.OpenThread(2, False, entry.thread))
                    try:
                        if kernel.ResumeThread(thread) == 0xffffffff:
                            raise ct.WinError(ct.get_last_error())
                        return
                    finally:
                        kernel.CloseHandle(thread)
                more = kernel.Thread32Next(snapshot, ct.byref(entry))
            raise RuntimeError('Cannot resume owned child thread')
        finally:
            kernel.CloseHandle(snapshot)


def spawn(command, **kwargs):
    if not WINDOWS:
        return subprocess.Popen(command, start_new_session=True, **kwargs)
    job = checked(kernel.CreateJobObjectW(None, None))
    child = None
    try:
        limits = ExtendedLimits()
        limits.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        checked(kernel.SetInformationJobObject(job, 9, ct.byref(limits), ct.sizeof(limits)))
        # Suspend before assignment: even a fast shell cannot leave an unowned descendant.
        child = subprocess.Popen(command, creationflags=0x4 | subprocess.CREATE_NO_WINDOW, **kwargs)
        checked(kernel.AssignProcessToJobObject(job, int(child._handle)))
        child._guard_job = job
        resume(child)
        return child
    except BaseException:
        if child is not None:
            child.kill()
            child.wait(timeout=2)
        kernel.CloseHandle(job)
        raise


def cleanup(child, grace=0.5):
    if WINDOWS:
        job = getattr(child, '_guard_job', None)
        if job:
            try:
                checked(kernel.TerminateJobObject(job, 1))
                deadline = time.monotonic() + 2
                while True:
                    accounting = Accounting()
                    checked(kernel.QueryInformationJobObject(job, 1, ct.byref(accounting), ct.sizeof(accounting), None))
                    if not accounting.ActiveProcesses:
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Owned Windows job did not terminate within 2 seconds')
                    time.sleep(0.01)
            finally:
                kernel.CloseHandle(job)
                child._guard_job = None
        child.wait(timeout=2)
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            break
        if sig == signal.SIGTERM:
            time.sleep(grace)
    child.wait(timeout=2)
