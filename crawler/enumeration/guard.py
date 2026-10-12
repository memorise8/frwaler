"""Thread-local network guard for reused crawler methods.

While a reused crawler method runs inside ``guarded()``, any socket connect,
process spawn (curl, browsers) or ``os.system`` in *that thread* raises
``AdapterUngatedNetwork``. Other threads (e.g. other sites' gated fetchers)
are unaffected. Patches are installed once per process and are inert
outside a guarded block.

Not covered: native code that opens sockets without Python's socket module
(e.g. curl_cffi/libcurl in-process); see docs/COLLECTION_FRAMEWORK.md.
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

        socket.socket.connect = connect
        socket.socket.connect_ex = connect_ex
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
