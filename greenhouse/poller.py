import collections
import select

from greenhouse._state import state


__all__ = ["Epoll", "Poll", "Select", "best", "set"]

SHORT_TIMEOUT = 0.0001

class Poll(object):
    "a greenhouse poller using the poll system call''"
    INMASK = getattr(select, 'POLLIN', None)
    OUTMASK = getattr(select, 'POLLOUT', None)
    ERRMASK = getattr(select, 'POLLERR', None)

    _POLLER = getattr(select, "poll")

    def __init__(self):
        self._poller = self._POLLER()
        self._registry = {}

    def register(self, fd, eventmask=None):
        fd = isinstance(fd, int) and fd or fd.fileno()
        if eventmask is None:
            eventmask = self.INMASK | self.OUTMASK | self.ERRMASK

        if fd in self._registry:
            reg = self._registry[fd]
            if reg & eventmask == eventmask:
                return

            eventmask = reg | eventmask
            self.unregister(fd)

        rc = self._poller.register(fd, eventmask)
        self._registry[fd] = eventmask
        return rc

    def unregister(self, fd):
        fd = isinstance(fd, int) and fd or fd.fileno()
        self._poller.unregister(fd)
        self._registry.pop(fd)

    def poll(self, timeout=SHORT_TIMEOUT):
        return self._poller.poll(timeout)

class Epoll(Poll):
    "a greenhouse poller utilizing the 2.6+ stdlib's epoll support"
    INMASK = getattr(select, 'EPOLLIN', None)
    OUTMASK = getattr(select, 'EPOLLOUT', None)
    ERRMASK = getattr(select, 'EPOLLERR', None)

    _POLLER = getattr(select, "epoll")

class Select(object):
    "a greenhouse poller using the select system call"
    INMASK = 1
    OUTMASK = 2
    ERRMASK = 4

    def __init__(self):
        self._registry = {}

    def register(self, fd, eventmask=None):
        fd = isinstance(fd, int) and fd or fd.fileno()
        if eventmask is None:
            eventmask = self.INMASK | self.OUTMASK | self.ERRMASK

        if fd in self._registry:
            reg = self._registry[fd]
            if reg & eventmask == eventmask:
                return

            eventmask = reg | eventmask

        isnew = fd not in self._registry
        self._registry[fd] = eventmask
        return isnew

    def unregister(self, fd):
        fd = isinstance(fd, int) and fd or fd.fileno()
        del self._registry[fd]

    def poll(self, timeout=SHORT_TIMEOUT):
        rlist, wlist, xlist = [], [], []
        for fd, eventmask in self._registry.iteritems():
            if eventmask & self.INMASK:
                rlist.append(fd)
            if eventmask & self.OUTMASK:
                wlist.append(fd)
            if eventmask & self.ERRMASK:
                xlist.append(fd)
        rlist, wlist, xlist = select.select(rlist, wlist, xlist, timeout)
        events = collections.defaultdict(int)
        for fd in rlist:
            events[fd] |= self.INMASK
        for fd in wlist:
            events[fd] |= self.OUTMASK
        for fd in xlist: #pragma: no cover
            events[fd] |= self.ERRMASK
        return events.items()

def best():
    if hasattr(select, 'epoll'):
        return Epoll()
    elif hasattr(select, 'poll'):
        return Poll()
    return Select()

def set(poller=None):
    state.poller = poller or best()
set()
