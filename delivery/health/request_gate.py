"""Shared request-chain lock and host cooldowns for cooperating audit processes.

Chains are serialized globally, conservatively including automatic redirects.
This covers the probe's instrumented calls, not arbitrary sockets/browser assets.
No credentials, URLs, cookies or response bodies enter persistent state.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import math
from pathlib import Path
import threading
import time

from .diagnostics import host_only
from .results import atomic_json


class NetworkDeferred(BaseException):
    def __init__(self, host, retry_at):
        self.host = host
        self.retry_at = retry_at


class UngovernedRequest(BaseException):
    pass


class RequestGate:
    def __init__(self, root, *, delay=1.0):
        if not 0 <= delay <= 10:
            raise ValueError('invalid request gap')
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.delay = delay
        self._lock = threading.RLock()
        self._local = threading.local()

    def _read(self):
        path = self.root/'state.json'
        if not path.exists():
            return {'last_end': 0, 'cooldowns': {}}
        data = json.loads(path.read_text())
        if not isinstance(data.get('cooldowns'), dict):
            raise ValueError('invalid gate state')
        return data

    def _check(self, host, data):
        until = data['cooldowns'].get(host, 0)
        if until > time.time():
            raise NetworkDeferred(host, until)

    @contextmanager
    def enter(self, url):
        host = host_only(url)
        if not host:
            raise UngovernedRequest()
        with self._lock:
            # Some transports invoke another instrumented transport internally.
            # Do not acquire a second flock against our own open descriptor.
            if getattr(self._local, 'depth', 0):
                self._check(host, self._read())
                self._local.depth += 1
                try:
                    yield
                finally:
                    self._local.depth -= 1
                return
            with (self.root/'request.lock').open('a') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                self._local.depth = 1
                try:
                    data = self._read()
                    self._check(host, data)
                    wait = min(self.delay, max(0, data.get('last_end', 0) + self.delay - time.time()))
                    if wait:
                        time.sleep(wait)
                    yield
                finally:
                    self._local.depth = 0
                    data = self._read()
                    data['last_end'] = time.time()
                    atomic_json(self.root/'state.json', data)
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def defer(self, url, retry_at, final_url=None):
        """Call while holding enter(); persist before releasing the chain lock."""
        if not getattr(self._local, 'depth', 0):
            raise RuntimeError('defer requires an acquired request gate')
        if not isinstance(retry_at, (int, float)) or not math.isfinite(retry_at):
            raise ValueError('invalid cooldown')
        data = self._read()
        for host in {host_only(url), host_only(final_url)} - {None}:
            data['cooldowns'][host] = max(data['cooldowns'].get(host, 0), retry_at)
        atomic_json(self.root/'state.json', data)
