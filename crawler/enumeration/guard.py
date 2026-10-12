"""Thread-local network *tripwire* for reused crawler methods.

This is NOT a security boundary. It turns the common ways a reused crawler
method could bypass the request gate into an explicit ``AdapterUngatedNetwork``
stop: a new socket connect, a process spawn (curl, browsers) or ``os.system``
in the guarded thread, and in threads started from it. Other threads (other
sites' gated fetchers) are unaffected.

Not covered (documented limits): sockets already connected before the guarded
call (keep-alive reuse via send/recv), native code that opens sockets itself
(curl_cffi/libcurl, playwright drivers already running), threads that existed
before the guarded block (e.g. pre-created executor workers), and code paths
run outside ``guarded()`` such as crawler constructors and ``_save_paper``.
"""
from __future__ import annotations

import os
import socket
import subprocess
import threading
from contextlib import contextmanager

from .http import EnumerationStop


class AdapterUngatedNetwork(EnumerationStop):
    pass


_state = threading.local()
_install_lock = threading.Lock()
_installed = False


def active():
    return getattr(_state, "depth", 0) > 0


def _deny(what):
    raise AdapterUngatedNetwork(f"adapter_ungated_network:{what}")


def _install():
    global _installed
    with _install_lock:
        if _installed:
            return
        orig_connect = socket.socket.connect
        orig_connect_ex = socket.socket.connect_ex
        orig_popen_init = subprocess.Popen.__init__
        orig_system = os.system

        def connect(self, address):
            if active() and self.family in (socket.AF_INET, socket.AF_INET6):
                _deny("socket.connect")
            return orig_connect(self, address)

        def connect_ex(self, address):
            if active() and self.family in (socket.AF_INET, socket.AF_INET6):
                _deny("socket.connect_ex")
            return orig_connect_ex(self, address)

        def popen_init(self, *args, **kwargs):
            if active():
                _deny("subprocess")
            return orig_popen_init(self, *args, **kwargs)

        def system(command):
            if active():
                _deny("os.system")
            return orig_system(command)

        orig_thread_start = threading.Thread.start

        def thread_start(self):
            # Threads started from a guarded block inherit the guard.
            if active():
                inner = self.run

                def guarded_run():
                    with guarded():
                        inner()
                self.run = guarded_run
            return orig_thread_start(self)

        socket.socket.connect = connect
        socket.socket.connect_ex = connect_ex
        threading.Thread.start = thread_start
        subprocess.Popen.__init__ = popen_init
        os.system = system
        _installed = True


@contextmanager
def guarded():
    _install()
    _state.depth = getattr(_state, "depth", 0) + 1
    try:
        yield
    finally:
        _state.depth -= 1
