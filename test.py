# coding: utf-8
from __future__ import print_function

import io
import os
import platform
import sys
import time
from functools import partial
from tempfile import TemporaryFile
from unittest import mock

import pytest

import wurlitzer
from wurlitzer import (
    PIPE,
    STDOUT,
    Wurlitzer,
    _default_encoding,
    c_stderr_p,
    c_stdout_p,
    libc,
    pipes,
    stop_sys_pipes,
    sys_pipes,
    sys_pipes_forever,
)

StringIO = partial(io.StringIO, newline=None)  # universal newlines by default


def printf(msg):
    """Call C printf"""
    libc.printf((msg + '\n').encode(_default_encoding))


def printf_err(msg):
    """Cal C fprintf on stderr"""
    libc.fprintf(c_stderr_p, (msg + '\n').encode(_default_encoding))


def test_stdout_only():
    # stdout-only so stderr can still be used for debug
    with pipes(stdout=PIPE, stderr=None) as (stdout, _):
        printf(u"Hellø")

    assert stdout.read() == u"Hellø\n"


def test_pipes():
    with pipes(stdout=PIPE, stderr=PIPE) as (stdout, stderr):
        printf(u"Hellø")
        printf_err(u"Hi, stdérr")

    assert stdout.read() == u"Hellø\n"
    assert stderr.read() == u"Hi, stdérr\n"


def test_pipe_bytes():
    with pipes(encoding=None) as (stdout, stderr):
        printf(u"Hellø")
        printf_err(u"Hi, stdérr")

    LF = '\nnewline=None'

    assert stdout.read() == f"Hellø{os.linesep}".encode(_default_encoding)
    assert stderr.read() == f"Hi, stdérr{os.linesep}".encode(_default_encoding)


def test_forward():
    stdout = StringIO()
    stderr = StringIO()
    with pipes(stdout=stdout, stderr=stderr) as (_stdout, _stderr):
        printf(u"Hellø")
        printf_err(u"Hi, stdérr")
        assert _stdout is stdout
        assert _stderr is stderr

    assert stdout.getvalue() == u"Hellø\n"
    assert stderr.getvalue() == u"Hi, stdérr\n"


def test_pipes_stderr():
    stdout = StringIO()
    with pipes(stdout=stdout, stderr=STDOUT) as (_stdout, _stderr):
        printf(u"Hellø")
        libc.fflush(c_stdout_p)
        time.sleep(0.1)
        printf_err(u"Hi, stdérr")
        assert _stdout is stdout
        assert _stderr is None

    assert stdout.getvalue() == u"Hellø\nHi, stdérr\n"


def test_flush():
    stdout = StringIO()
    w = Wurlitzer(stdout=stdout, stderr=STDOUT)
    with w:
        printf_err(u"Hellø")
        time.sleep(0.5)
        assert stdout.getvalue().strip() == u"Hellø"


def test_sys_pipes():
    stdout = StringIO()
    stderr = StringIO()
    with mock.patch('sys.stdout', stdout), mock.patch(
        'sys.stderr', stderr
    ), sys_pipes():
        printf(u"Hellø")
        printf_err(u"Hi, stdérr")

    assert stdout.getvalue() == u"Hellø\n"
    assert stderr.getvalue() == u"Hi, stdérr\n"


def test_redirect_everything():
    stdout = StringIO()
    stderr = StringIO()
    with mock.patch('sys.stdout', stdout), mock.patch('sys.stderr', stderr):
        sys_pipes_forever()
        printf(u"Hellø")
        printf_err(u"Hi, stdérr")
        stop_sys_pipes()
    assert stdout.getvalue() == u"Hellø\n"
    assert stderr.getvalue() == u"Hi, stdérr\n"


def count_fds():
    """utility for counting file descriptors"""
    proc_fds = '/proc/{}/fd'.format(os.getpid())
    if os.path.isdir(proc_fds):
        return len(proc_fds)
    else:
        # this is an approximate count,
        # but it should at least be stable if we aren't leaking
        with TemporaryFile() as tf:
            return tf.fileno()


def test_fd_leak():
    base_count = count_fds()
    with pipes():
        print('ok')
    assert count_fds() == base_count
    for i in range(10):
        with pipes():
            print('ok')
        assert count_fds() == base_count


def test_buffer_full():
    with pipes(stdout=None, stderr=StringIO()) as (stdout, stderr):
        long_string = "x" * 100000  # create a long string (longer than 65536)
        printf_err(long_string)

    # Test never reaches here as the process hangs.
    assert stderr.getvalue() == long_string + "\n"


def test_buffer_full_default():
    with pipes() as (stdout, stderr):
        long_string = "x" * 100000  # create a long string (longer than 65536)
        printf(long_string)

    # Test never reaches here as the process hangs.
    assert stdout.read() == long_string + "\n"


def test_pipe_max_size():
    max_pipe_size = wurlitzer._get_max_pipe_size()
    if platform.system() == 'Linux':
        assert 65535 <= max_pipe_size <= 1024 * 1024
    else:
        assert max_pipe_size is None


@pytest.mark.skipif(
    wurlitzer._get_max_pipe_size() is None, reason="requires _get_max_pipe_size"
)
def test_bufsize():
    from fcntl import fcntl

    default_bufsize = wurlitzer._get_max_pipe_size()
    with wurlitzer.pipes() as (stdout, stderr):
        assert fcntl(sys.__stdout__, wurlitzer.F_GETPIPE_SZ) == default_bufsize
        assert fcntl(sys.__stderr__, wurlitzer.F_GETPIPE_SZ) == default_bufsize

    bufsize = 2**18  # seems to only accept powers of two?
    with wurlitzer.pipes(bufsize=bufsize) as (stdout, stderr):
        assert fcntl(sys.__stdout__, wurlitzer.F_GETPIPE_SZ) == bufsize
        assert fcntl(sys.__stderr__, wurlitzer.F_GETPIPE_SZ) == bufsize
