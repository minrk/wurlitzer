"""Capture C-level FD output on pipes

Use `wurlitzer.pipes` or `wurlitzer.sys_pipes` as context managers.
"""

from __future__ import print_function

__version__ = '3.0.4.dev'

__all__ = [
    'pipes',
    'sys_pipes',
    'sys_pipes_forever',
    'stop_sys_pipes',
    'PIPE',
    'STDOUT',
    'Wurlitzer',
]

import ctypes
import errno
import io
import os
import platform
import selectors
import sys
import threading
import time
import warnings
from contextlib import contextmanager
from functools import lru_cache, partial
from queue import Empty, Queue

if os.name != 'nt':
    from fcntl import F_GETFL, F_SETFL, fcntl

    try:
        from fcntl import F_GETPIPE_SZ, F_SETPIPE_SZ
    except ImportError:
        # ref: linux uapi/linux/fcntl.h
        F_SETPIPE_SZ = 1024 + 7
        F_GETPIPE_SZ = 1024 + 8


# Windows support adapted from
# https://github.com/chrisjbillington/zprocess@89b464c3a2d10cb3282bea625670f807430adf03
# MIT License


def _get_streams_windows():
    """Get file pointer for C stream"""

    class FILE(ctypes.Structure):
        """FILE struct for Windows"""

        _fields_ = [
            ("_ptr", ctypes.c_char_p),
            ("_cnt", ctypes.c_int),
            ("_base", ctypes.c_char_p),
            ("_flag", ctypes.c_int),
            ("_file", ctypes.c_int),
            ("_charbuf", ctypes.c_int),
            ("_bufsize", ctypes.c_int),
            ("_tmpfname", ctypes.c_char_p),
        ]

    iob_func = libc.__iob_func
    iob_func.restype = ctypes.POINTER(FILE)
    iob_func.argtypes = []
    streams = iob_func()
    return (ctypes.c_void_p(ctypes.addressof(streams[i])) for i in (1, 2))


def _get_streams_cffi():
    """Use CFFI to lookup stdout/stderr pointers

    Should work ~everywhere, but requires compilation
    """
    try:
        import cffi
    except ImportError:
        raise ImportError(
            "Failed to lookup stdout symbols in libc. Fallback requires cffi."
        )

    try:
        _ffi = cffi.FFI()
        _ffi.cdef("const size_t c_stdout_p();")
        _ffi.cdef("const size_t c_stderr_p();")
        _lib = _ffi.verify(
            '\n'.join(
                [
                    "#include <stdio.h>",
                    "const size_t c_stdout_p() { return (size_t) (void*) stdout; }",
                    "const size_t c_stderr_p() { return (size_t) (void*) stderr; }",
                ]
            )
        )
        c_stdout_p = ctypes.c_void_p(_lib.c_stdout_p())
        c_stderr_p = ctypes.c_void_p(_lib.c_stderr_p())
    except Exception as e:
        warnings.warn(
            "Failed to lookup stdout with cffi: {}.\nStreams may not be flushed.".format(
                e
            )
        )
        return (None, None)
    else:
        return c_stdout_p, c_stderr_p


c_stdout_p = c_stderr_p = None
if os.name == "nt":
    libc = ctypes.cdll.msvcrt
    c_stdout_p, c_stderr_p = _get_streams_windows()
else:
    libc = ctypes.CDLL(None)
    try:
        c_stdout_p = ctypes.c_void_p.in_dll(libc, 'stdout')
        c_stderr_p = ctypes.c_void_p.in_dll(libc, 'stderr')
    except ValueError:
        # libc.stdout has a funny name on macOS
        try:
            c_stdout_p = ctypes.c_void_p.in_dll(libc, '__stdoutp')
            c_stderr_p = ctypes.c_void_p.in_dll(libc, '__stderrp')
        except ValueError:
            c_stdout_p, c_stderr_p = _get_streams_cffi()

libc.setvbuf.argtypes = [
    ctypes.c_void_p,
    ctypes.c_char_p,
    ctypes.c_int,
    ctypes.c_size_t,
]

STDOUT = 2
PIPE = 3

_default_encoding = getattr(sys.stdin, 'encoding', None) or 'utf8'
if _default_encoding.lower() == 'ascii':
    # don't respect ascii
    _default_encoding = 'utf8'  # pragma: no cover


def dup2(a, b, timeout=3):
    """Like os.dup2, but retry on EBUSY"""
    dup_err = None
    # give FDs 3 seconds to not be busy anymore
    for i in range(int(10 * timeout)):
        try:
            return os.dup2(a, b)
        except OSError as e:
            dup_err = e
            if e.errno == errno.EBUSY:
                time.sleep(0.1)
            else:
                raise
    if dup_err:
        raise dup_err


def _unbuffer(c_stream_p):
    """Set C output streams to unbuffered

    From chrisjbillington/zprocess (MIT License)
    """
    if os.name == 'nt':
        _IONBF = 4
    else:
        _IONBF = 2
    libc.setvbuf(c_stream_p, None, _IONBF, 0)


def _nonblocking(fd):
    # Windows impl adapted from https://stackoverflow.com/questions/34504970/non-blocking-read-on-os-pipe-on-windows
    if os.name == 'nt':
        import msvcrt
        from ctypes import POINTER, WinError, byref, windll
        from ctypes.wintypes import BOOL, DWORD, HANDLE

        LPDWORD = POINTER(DWORD)

        PIPE_NOWAIT = DWORD(1)
        SetNamedPipeHandleState = windll.kernel32.SetNamedPipeHandleState
        SetNamedPipeHandleState.argtypes = [HANDLE, LPDWORD, LPDWORD, LPDWORD]
        SetNamedPipeHandleState.restype = BOOL

        h = msvcrt.get_osfhandle(fd)

        res = windll.kernel32.SetNamedPipeHandleState(h, byref(PIPE_NOWAIT), None, None)
        if res == 0:
            print(WinError())
    else:
        flags = fcntl(fd, F_GETFL)
        fcntl(fd, F_SETFL, flags | os.O_NONBLOCK)


_WIN_ERROR_NO_DATA = 232


@lru_cache()
def _get_max_pipe_size():
    """Get max pipe size

    Reads /proc/sys/fs/pipe-max-size on Linux.
    Always returns None elsewhere.

    Returns integer (up to 1MB),
    or None if no value can be determined.

    Adapted from wal-e, (c) 2018, WAL-E Contributors
    used under BSD-3-clause
    """
    if platform.system() != 'Linux':
        return

    # If Linux procfs (or something that looks like it) exposes its
    # maximum F_SETPIPE_SZ, adjust the default buffer sizes.
    try:
        with open('/proc/sys/fs/pipe-max-size', 'r') as f:
            # Figure out OS max pipe size
            pipe_max_size = int(f.read())
    except Exception:
        pass
    else:
        if pipe_max_size > 1024 * 1024:
            # avoid unusually large values, limit to 1MB
            return 1024 * 1024
        elif pipe_max_size <= 65536:
            # smaller than default, don't do anything
            return None
        else:
            return pipe_max_size


class Wurlitzer:
    """Class for Capturing Process-level FD output via dup2

    Typically used via `wurlitzer.pipes`
    """

    flush_interval = 0.2

    def __init__(
        self,
        stdout=None,
        stderr=None,
        encoding=_default_encoding,
        bufsize=_get_max_pipe_size(),
    ):
        """
        Parameters
        ----------
        stdout: stream or None
            The stream for forwarding stdout.
        stderr = stream or None
            The stream for forwarding stderr.
        encoding: str or None
            The encoding to use, if streams should be interpreted as text.
        bufsize: int or None
            Set pipe buffer size using fcntl F_SETPIPE_SZ (linux only)
            default: use /proc/sys/fs/pipe-max-size up to a max of 1MB
            if 0, will do nothing.
        """
        self._stdout = stdout
        if stderr == STDOUT:
            self._stderr = self._stdout
        else:
            self._stderr = stderr
        self.encoding = encoding
        if bufsize is None:
            bufsize = _get_max_pipe_size()
        self._bufsize = bufsize
        self._save_fds = {}
        self._real_fds = {}
        self._handlers = {}
        self._handlers['stderr'] = self._handle_stderr
        self._handlers['stdout'] = self._handle_stdout

    def _setup_pipe(self, name):
        real_fd = getattr(sys, '__%s__' % name).fileno()
        save_fd = os.dup(real_fd)
        self._save_fds[name] = save_fd
        c_stream_p = globals()["c_{}_p".format(name)]
        if c_stream_p is not None:
            _unbuffer(c_stream_p)

        pipe_out, pipe_in = os.pipe()
        # set max pipe buffer size (linux only)
        if os.name != 'nt' and self._bufsize:
            try:
                fcntl(pipe_in, F_SETPIPE_SZ, self._bufsize)
            except OSError as error:
                warnings.warn(
                    "Failed to set pipe buffer size: " + str(error), RuntimeWarning
                )

        dup2(pipe_in, real_fd)
        os.close(pipe_in)
        self._real_fds[name] = real_fd

        # make pipe_out non-blocking
        _nonblocking(pipe_out)
        return pipe_out

    def _decode(self, data):
        """Decode data, if any

        Called before passing to stdout/stderr streams
        """
        if self.encoding:
            data = data.decode(self.encoding, 'replace')
        return data

    def _handle_stdout(self, data):
        if self._stdout:
            self._stdout.write(self._decode(data))

    def _handle_stderr(self, data):
        if self._stderr:
            self._stderr.write(self._decode(data))

    def _setup_handle(self):
        """Setup handle for output, if any"""
        self.handle = (self._stdout, self._stderr)

    def _finish_handle(self):
        """Finish handle, if anything should be done when it's all wrapped up."""
        pass

    def _flush(self):
        """flush sys.stdout/err and low-level FDs"""
        if self._stdout and sys.stdout:
            sys.stdout.flush()
        if self._stderr and sys.stderr:
            sys.stderr.flush()

        if os.name == "nt":
            # In windows we flush all output streams
            # by calling flush on a NULL pointer
            libc.fflush(ctypes.c_void_p())
        else:
            if c_stdout_p is not None:
                libc.fflush(c_stdout_p)

            if c_stderr_p is not None:
                libc.fflush(c_stderr_p)

    def _set_thread_priority(self):
        """Set thread priority

        no-op except on Windows

        Adapted from zprocess (MIT License)
        """
        if os.name == 'nt':
            w32 = ctypes.windll.kernel32
            THREAD_SET_INFORMATION = 0x20
            THREAD_PRIORITY_ABOVE_NORMAL = 1
            handle = w32.OpenThread(
                THREAD_SET_INFORMATION, False, threading.current_thread().ident
            )
            result = w32.SetThreadPriority(handle, THREAD_PRIORITY_ABOVE_NORMAL)
            w32.CloseHandle(handle)
            if not result:
                print(
                    "Failed to set priority of thread:",
                    w32.GetLastError(),
                    file=sys.__stderr__,
                )

    def __enter__(self):
        return self.start()

    def start(self):
        """Start capturing output

        Typically used via context manager
        """
        # flush anything out before starting
        self._flush()
        # setup handle
        self._setup_handle()
        self._control_r, self._control_w = os.pipe()

        # create pipe for stdout
        draining = False
        names = {}
        pipes = []
        if self._stdout:
            pipe = self._setup_pipe('stdout')
            pipes.append(pipe)
            names[pipe] = 'stdout'
        if self._stderr:
            pipe = self._setup_pipe('stderr')
            pipes.append(pipe)
            names[pipe] = 'stderr'

        # flush pipes in a background thread to avoid blocking
        # the reader thread when the buffer is full
        flush_queue = Queue()

        def flush_main():
            self._set_thread_priority()
            msg = ''
            while True:
                try:
                    msg = flush_queue.get(timeout=self.flush_interval)
                except Empty:
                    # flush every flush_interval,
                    # even if we get no input
                    pass
                self._flush()
                if msg == 'stop':
                    return

        flush_thread = threading.Thread(target=flush_main)
        flush_thread.daemon = True
        flush_thread.start()

        # run one thread per pipe
        # because Windows can only do blocking reads on pipes
        # Windows can't select on pipes,
        # and even ProactorEventLoop.connect_read_pipe doesn't actually support pipes.

        def pipe_forwarder(fd, name, handler):
            """Forward bytes on a pipe to stream messages"""
            self._set_thread_priority()
            if os.name == 'nt':
                # can't poll on Windows, sleep instead
                # this shouldn't come up because reads are always blocking
                def poll():
                    time.sleep(self.flush_interval)

            else:
                poller = selectors.DefaultSelector()
                poller.register(fd, selectors.EVENT_READ)

                def poll():
                    poller.select(self.flush_interval)

            while True:
                try:
                    data = os.read(fd, 1024)
                except OSError as e:
                    if os.name == 'nt':
                        w32 = ctypes.windll.kernel32
                        no_data = (
                            e.errno == errno.EINVAL
                            and w32.GetLastError() == _WIN_ERROR_NO_DATA
                        )
                    else:
                        no_data = e.errno == errno.EAGAIN
                    if no_data:
                        if draining:
                            break
                        else:
                            poll()
                            continue
                    else:
                        raise
                if not data:
                    break
                else:
                    try:
                        handler(data)
                    except Exception as e:
                        # FIXME: this would produce an infinite loop on stderr
                        if name != 'stderr':
                            print(
                                "Error handling pipe bytes: {}".format(e),
                                file=sys.__stderr__,
                            )
                        break
            if os.name != 'nt':
                poller.close()
            # done reading, close pipe
            os.close(fd)

        def forwarder_control():
            self._set_thread_priority()
            nonlocal draining
            pipe_threads = []
            for fd in pipes:
                name = names[fd]
                handler = handler = getattr(self, '_handle_%s' % name)
                pipe_thread = threading.Thread(
                    target=pipe_forwarder, args=(fd, name, handler)
                )
                pipe_thread.daemon = True
                pipe_thread.start()
                pipe_threads.append(pipe_thread)

            msg = os.read(self._control_r, 1)
            os.close(self._control_r)

            # stop flush thread
            flush_queue.put('stop')
            flush_thread.join(timeout=10)
            draining = True

            # stop pipe threads
            for pipe_thread in pipe_threads:
                pipe_thread.join(timeout=10)
                if pipe_thread.is_alive():
                    print("Pipe still alive!", pipe_thread, file=sys.__stderr__)

        self.thread = threading.Thread(target=forwarder_control)
        self.thread.daemon = True
        self.thread.start()

        return self.handle

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()

    def stop(self):
        """Stop capturing output"""
        # flush before exiting
        self._flush()

        # restore original state, close pipes
        # forwarder thread will finish when it reaches EOF

        # signal output is complete on control channel
        os.write(self._control_w, b'\1')
        os.close(self._control_w)

        # finally, wait for thread to finish
        self.thread.join(timeout=10)

        for name, real_fd in self._real_fds.items():
            save_fd = self._save_fds[name]
            dup2(save_fd, real_fd)
            os.close(save_fd)

        if self.thread.is_alive():
            print("Wurlitzer thread failed to complete!", file=sys.__stderr__)

        # finalize handle
        self._finish_handle()


@contextmanager
def pipes(stdout=PIPE, stderr=PIPE, encoding=_default_encoding, bufsize=None):
    """Capture C-level stdout/stderr in a context manager.

    The return value for the context manager is (stdout, stderr).

    .. versionchanged:: 3.0

        when using `PIPE` (default), the type of captured output
        is `io.StringIO/BytesIO` instead of an OS pipe.
        This eliminates max buffer size issues (and hang when output exceeds 65536 bytes),
        but also means the buffer cannot be read with `.read()` methods
        until after the context exits.

    Examples
    --------

    >>> with pipes() as (stdout, stderr):
    ...     printf("C-level stdout")
    ... output = stdout.read()
    """
    stdout_pipe = stderr_pipe = False
    if encoding:
        PipeIO = partial(io.StringIO, newline=None)
    else:
        PipeIO = io.BytesIO
    # setup stdout
    if stdout == PIPE:
        stdout_r = stdout_w = PipeIO()
        stdout_pipe = True
    else:
        stdout_r = stdout_w = stdout
    # setup stderr
    if stderr == STDOUT:
        stderr_r = None
        stderr_w = stdout_w
    elif stderr == PIPE:
        stderr_r = stderr_w = PipeIO()
        stderr_pipe = True
    else:
        stderr_r = stderr_w = stderr
    w = Wurlitzer(stdout=stdout_w, stderr=stderr_w, encoding=encoding, bufsize=bufsize)
    try:
        with w:
            yield stdout_r, stderr_r
    finally:
        # close pipes
        if stdout_pipe:
            # seek to 0 so that it can be read after exit
            stdout_r.seek(0)
        if stderr_pipe:
            # seek to 0 so that it can be read after exit
            stderr_r.seek(0)


def sys_pipes(encoding=_default_encoding, bufsize=None):
    """Redirect C-level stdout/stderr to sys.stdout/stderr

    This is useful of sys.sdout/stderr are already being forwarded somewhere.

    DO NOT USE THIS if sys.stdout and sys.stderr are not already being forwarded.
    """
    return pipes(sys.stdout, sys.stderr, encoding=encoding, bufsize=bufsize)


_mighty_wurlitzer = None
_mighty_lock = threading.Lock()


def sys_pipes_forever(encoding=_default_encoding, bufsize=None):
    """Redirect all C output to sys.stdout/err

    This is not a context manager; it turns on C-forwarding permanently.
    """
    global _mighty_wurlitzer
    with _mighty_lock:
        if _mighty_wurlitzer is None:
            _mighty_wurlitzer = sys_pipes(encoding, bufsize)
            _mighty_wurlitzer.__enter__()


def stop_sys_pipes():
    """Stop permanent redirection started by sys_pipes_forever"""
    global _mighty_wurlitzer
    with _mighty_lock:
        if _mighty_wurlitzer is not None:
            _mighty_wurlitzer.__exit__(None, None, None)
            _mighty_wurlitzer = None


_extension_enabled = False


def load_ipython_extension(ip):
    """Register me as an IPython extension

    Captures all C output during execution and forwards to sys.

    Does nothing on terminal IPython.

    Use: %load_ext wurlitzer
    """
    global _extension_enabled

    if not getattr(ip, 'kernel', None):
        warnings.warn("wurlitzer extension doesn't do anything in terminal IPython")
        return
    for name in ("__stdout__", "__stderr__"):
        if getattr(sys, name) is None:
            warnings.warn("sys.{} is None. Wurlitzer can't capture output without it.")
            return

    ip.events.register('pre_execute', sys_pipes_forever)
    ip.events.register('post_execute', stop_sys_pipes)
    _extension_enabled = True


def unload_ipython_extension(ip):
    """Unload me as an IPython extension

    Use: %unload_ext wurlitzer
    """
    global _extension_enabled
    if not _extension_enabled:
        return

    ip.events.unregister('pre_execute', sys_pipes_forever)
    ip.events.unregister('post_execute', stop_sys_pipes)
    # sys_pipes_forever was called in pre_execute
    # after unregister we need to call it explicitly:
    stop_sys_pipes()
    _extension_enabled = False
