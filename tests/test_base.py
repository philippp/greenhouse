import contextlib
import os
import socket
import sys
import unittest
try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO

import greenhouse


port = lambda: 8000 + os.getpid() # because i want to run multiprocess nose

TESTING_TIMEOUT = 0.05

GTL = greenhouse.Lock()

class StateClearingTestCase(unittest.TestCase):
    def setUp(self):
        GTL.acquire()

        greenhouse.unmonkeypatch()

        state = greenhouse._state.state
        state.awoken_from_events.clear()
        state.timed_paused[:] = []
        state.paused[:] = []
        state.descriptormap.clear()
        state.to_run.clear()

        greenhouse.poller.set()

    def tearDown(self):
        GTL.release()

    @contextlib.contextmanager
    def socketpair(self):
        server = greenhouse.Socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("", port()))
        server.listen(5)

        client = greenhouse.Socket()
        client.connect(("", port()))

        handler, addr = server.accept()
        server.close()

        yield client, handler

        client.close()
        handler.close()
