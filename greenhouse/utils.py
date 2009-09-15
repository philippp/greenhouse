from __future__ import with_statement

import bisect
import collections
import functools
import time

from greenhouse._state import state
from greenhouse.compat import greenlet
from greenhouse import scheduler


__all__ = ["Event", "Lock", "RLock", "Condition", "Semaphore",
           "BoundedSemaphore", "Timer", "Local", "Queue"]

class Event(object):
    """an event for which greenlets can wait

    mirrors the standard library threading.Event API"""
    def __init__(self):
        self._is_set = False
        self._guid = id(self)
        self._timeout_callbacks = collections.defaultdict(list)
        self._active_timeout_runners = set()

    def is_set(self):
        "returns True if waiting on this event will block, False if not"
        return self._is_set
    isSet = is_set

    def set(self):
        """set the event to triggered

        after calling this method, all greenlets waiting on this event will be
        woken up, and calling wait() will not block until the clear() method
        has been called"""
        self._is_set = True
        self._active_timeout_runners.clear()
        state.awoken_from_events.update(state.paused_on_events[self._guid])
        del state.paused_on_events[self._guid]

    def clear(self):
        """clear the event from being triggered

        after calling this method, waiting on this event will block until the
        set() method has been called"""
        self._is_set = False

    def _add_timeout_callback(self, func, for_glet=None):
        if for_glet is None:
            for_glet = greenlet.getcurrent()
        self._timeout_callbacks[for_glet].append(func)

    def wait(self, timeout=None):
        """pause the current coroutine until this event is set

        if the set() method has been called, this method will not block at
        all. otherwise it will block until the set() method is called"""
        if not self._is_set:
            current = greenlet.getcurrent() # the waiting greenlet
            state.paused_on_events[self._guid].append(current)
            if timeout is not None:
                @scheduler.schedule_in(timeout)
                def hit_timeout():
                    if hit_timeout not in self._active_timeout_runners:
                        return
                    state.paused_on_events[self._guid].remove(current)
                    state.awoken_from_events.add(current)
                    error = None
                    for cback in self._timeout_callbacks[current]:
                        try:
                            cback()
                        except Exception, err:
                            if error is None:
                                error = err
                    if error is not None:
                        raise error
                self._active_timeout_runners.add(hit_timeout)
            scheduler.go_to_next()

class Lock(object):
    """an object that can only be 'owned' by one greenlet at a time

    mirrors the standard library threading.Lock API"""
    def __init__(self):
        self._locked = False
        self._event = Event()

    def locked(self):
        "returns true if the lock is already 'locked' or 'owned'"
        return self._locked

    def acquire(self, blocking=True):
        "lock the lock, or block until it is available"
        if not blocking:
            locked_already = self._locked
            self._locked = True
            return not locked_already
        while self._locked:
            self._event.wait()
        self._locked = True
        return True

    def release(self):
        "open the lock back up to wake up greenlets waiting on this lock"
        if not self._locked:
            raise RuntimeError("cannot release un-acquired lock")
        self._locked = False
        self._event.set()
        self._event.clear()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, type, value, traceback):
        return self.release()

class RLock(Lock):
    """a lock which may be acquired more than once by the same greenlet

    mirrors the standard library threading.RLock API"""
    def __init__(self):
        super(RLock, self).__init__()
        self._owner = None
        self._count = 0

    def _is_owned(self):
        return self._owner is greenlet.getcurrent()

    def acquire(self, blocking=True):
        """if the lock is owned by a different greenlet, block until it is
        fully released. then increment the acquired count by one"""
        current = greenlet.getcurrent()
        if self._owner is current:
            self._count += 1
            return True
        if self._locked and not blocking:
            return False
        while self._locked:
            self._event.wait()
        self._owner = current
        self._locked = True
        self._count = 1
        return True

    def release(self):
        """decrement the owned count by one. if it reaches zero, fully release
        the lock, waking up a waiting greenlet"""
        if not self._locked or self._owner is not greenlet.getcurrent():
            raise RuntimeError("cannot release un-acquired lock")
        self._count -= 1
        if self._count == 0:
            self._locked = False
            self._owner = None
            self._event.set()
            self._event.clear()

class Condition(object):
    """a synchronization object capable of waking all or one of its waiters

    mirrors the standard library threading.Condition API"""
    def __init__(self, lock=None):
        if lock is None:
            lock = RLock()
        self._lock = lock
        self._waiters = collections.deque()
        self.acquire = lock.acquire
        self.release = lock.release
        self.__enter__ = lock.__enter__
        self.__exit__ = lock.__exit__
        if hasattr(lock, '_is_owned'):
            self._is_owned = lock._is_owned

    def _is_owned(self):
        owned = not self._lock.acquire(False)
        self._lock.release()
        return owned

    def wait(self, timeout=None):
        """wait to be woken up by the condition

        you must have acquired the underlying lock first"""
        if not self._is_owned():
            raise RuntimeError("cannot wait on un-acquired lock")
        self._lock.release()
        event = Event()
        self._waiters.append(event)
        def timeout_cback():
            self._waiters.remove(event)
        event._add_timeout_callback(timeout_cback)
        event.wait(timeout)
        self._lock.acquire()

    def notify(self, num=1):
        """wake up a set number (default 1) of the waiting greenlets

        you must have acquired the underlying lock first"""
        if not self._is_owned():
            raise RuntimeError("cannot wait on un-acquired lock")
        for i in xrange(min(num, len(self._waiters))):
            self._waiters.popleft().set()

    def notify_all(self):
        """wake up all the greenlets waiting on the condition

        you must have acquired the underlying lock first"""
        if not self._is_owned():
            raise RuntimeError("cannot wait on un-acquired lock")
        self.notify(len(self._waiters))
    notifyAll = notify_all

class Semaphore(object):
    """a synchronization object with a counter that blocks when it reaches 0

    mirrors the api of threading.Semaphore"""
    def __init__(self, value=1):
        assert value >= 0, "semaphore value cannot be negative"
        self._value = value
        self._waiters = collections.deque()

    def acquire(self, blocking=True):
        "lock or decrement the semaphore"
        if self._value:
            self._value -= 1
            return True
        elif not blocking:
            return False
        event = Event()
        self._waiters.append(event)
        event.wait()
        return True

    def release(self):
        "release or increment the semaphore"
        if self._value or not self._waiters:
            self._value += 1
        else:
            self._waiters.popleft().set()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, type, value, traceback):
        return self.release()

class BoundedSemaphore(Semaphore):
    """a semaphore with an upper limit to the counter"""
    def __init__(self, value=1):
        super(BoundedSemaphore, self).__init__(value)
        self._initial_value = value

    def release(self):
        if self._value >= self._initial_value:
            raise ValueError("BoundedSemaphore released too many times")
        return super(BoundedSemaphore, self).release()
    release.__doc__ = Semaphore.release.__doc__

class Timer(object):
    """creates a greenlet from *func* and schedules it to run in *secs* seconds

    mirrors the standard library threading.Timer API"""
    def __init__(self, secs, func, args=(), kwargs=None):
        self.func = func
        self.args = args
        self.kwargs = kwargs or {}
        self._glet = glet = greenlet(self._run)
        self.waketime = waketime = time.time() + secs
        bisect.insort(state.timed_paused, (waketime, glet))

    def cancel(self):
        "if called before the greenlet runs, stop it from ever starting"
        tp = state.timed_paused
        if not tp:
            return
        index = bisect.bisect(tp, (self.waketime, self._glet)) - 1
        if tp[index][1] is self._glet:
            tp[index:index + 1] = []

    def _run(self):
        return self.func(*self.args, **self.kwargs)

class Local(object):
    """class that represents greenlet-local data

    mirrors the standard library threading.local API"""
    def __init__(self):
        object.__setattr__(self, "data", {})

    def __getattr__(self, name):
        local = object.__getattr__(self, "data").setdefault(
                greenlet.getcurrent(), {})
        if name not in local:
            raise AttributeError, "Local object has no attribute %s" % name
        return local[name]

    def __setattr__(self, name, value):
        object.__getattr__(self, "data").setdefault(greenlet.getcurrent(),
                {})[name] = value

class Queue(object):
    """a producer-consumer queue

    mirrors the standard library Queue.Queue API"""
    class Empty(Exception):
        pass

    class Full(Exception):
        pass

    def __init__(self, maxsize=0):
        self.maxsize = maxsize
        self.queue = collections.deque()
        self.unfinished_tasks = 0
        self.not_empty = Condition()
        self.not_full = Condition()
        self.all_tasks_done = Event()
        self.all_tasks_done.set()

    def empty(self):
        "without blocking, returns True if the queue is empty"
        return not self.queue

    def full(self):
        """returns True if the queue is full without blocking

        if the queue has no *maxsize* this will always return False"""
        return self.maxsize > 0 and len(self.queue) == self.maxsize

    def _unsafe_get(self):
        with self.not_full:
            self.not_full.notify()
        return self.queue.popleft()

    def get(self, blocking=True, timeout=None):
        """get an item out of the queue

        if *blocking* is True (default), the method will block until an item is
        available, or until *timeout* seconds, whichever comes first. if it
        times out, it will raise a Queue.Empty exception

        if *blocking* is False, it will immediately either return an item or
        raise a Queue.Empty exception"""
        if not self.queue:
            if blocking:
                with self.not_empty:
                    self.not_empty.wait(timeout)
                if self.queue:
                    return self._unsafe_get()
            raise self.Empty()
        return self._unsafe_get()

    def get_nowait(self):
        "immediately return an item from the queue or raise Queue.Empty"
        return self.get(False)

    def join(self):
        "block until every put() call has had a corresponding task_done() call"
        self.all_tasks_done.wait()

    def _unsafe_put(self, item):
        with self.not_empty:
            self.not_empty.notify()
        self.queue.append(item)
        if not self.unfinished_tasks:
            self.all_tasks_done.clear()
        self.unfinished_tasks += 1

    def put(self, item, blocking=True, timeout=None):
        """put an item into the queue

        if *blocking* is True (default) and the queue has a maxsize, the method
        will block until a spot in the queue has been made available, or
        *timeout* seconds has passed, whichever comes first. if it times out,
        it will raise a Queue.Full exception

        if *blocking* is False, it will immediately either place the item in
        the queue or raise a Query.Full exception"""
        if self.maxsize and len(self.queue) >= self.maxsize:
            if blocking:
                with self.not_full:
                    self.not_full.wait(timeout)
                if len(self.queue) < self.maxsize:
                    self._unsafe_put(item)
            raise self.Full()
        self._unsafe_put(item)

    def put_nowait(self, item):
        "immediately place an item into the queue or raise Query.Full"
        self.put(item, False)

    def qsize(self):
        "return the number of items in the queue, without blocking"
        return len(self.queue)

    def task_done(self):
        "mark that a job (corresponding to a put() call) is finished"
        if not self.unfinished_tasks:
            raise ValueError('task_done() called too many times')
        self.unfinished_tasks -= 1
        if not self.unfinished_tasks:
            self.all_tasks_done.set()

def _debugger(cls):
    import types
    for name in dir(cls):
        attr = getattr(cls, name)
        if isinstance(attr, types.MethodType):
            def extrascope(attr):
                @functools.wraps(attr)
                def wrapper(*args, **kwargs):
                    print "%s.%s %s %s" % (cls.__name__, attr.__name__,
                            repr(args[1:]), repr(kwargs))
                    rc = attr(*args, **kwargs)
                    print "%s.%s --> %s" % (cls.__name__, attr.__name__,
                            repr(rc))
                    return rc
                return wrapper
            setattr(cls, name, extrascope(attr))
    return cls
