# Copyright 2022-2023 XProbe Inc.
# derived from copyright 1999-2021 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import atexit
import concurrent.futures
import queue
import threading
from typing import List
from weakref import WeakKeyDictionary, ref

from ...lib.aio import get_isolation
from ...typing import SessionType, TileableType
from ..mode import enter_mode


class DecrefRunner:
    def __init__(self):
        self._decref_thread = None
        self._queue = queue.Queue()

    def start(self):
        self._decref_thread = threading.Thread(
            target=self._thread_body, name="DecrefThread"
        )
        self._decref_thread.daemon = True
        self._decref_thread.start()

    def _thread_body(self):
        from ...deploy.oscar.session import SyncSession
        from ...oscar.errors import ActorNotExist

        while True:
            key, session_ref, fut = self._queue.get()
            if key is None:
                break

            session = session_ref()
            if session is None:
                fut.set_result(None)
                continue
            try:
                s = SyncSession.from_isolated_session(session)
                s.decref(key)
                fut.set_result(None)
            except (RuntimeError, ConnectionError, KeyError, ActorNotExist):
                fut.set_result(None)
            except (
                Exception
            ) as ex:  # pragma: no cover  # noqa: E722  # nosec  # pylint: disable=bare-except
                fut.set_exception(ex)
            finally:
                del session

    def stop(self):
        if self._decref_thread:  # pragma: no branch
            self._queue.put_nowait((None, None, None))
            self._decref_thread.join(1)

    def put(self, key: str, session_ref: ref):
        if self._decref_thread is None:
            self.start()

        fut = concurrent.futures.Future()
        self._queue.put_nowait((key, session_ref, fut))
        return fut


_decref_runner = DecrefRunner()
atexit.register(_decref_runner.stop)


class _TileableSession:
    def __init__(self, tileable: TileableType, session: SessionType):
        self._sess_id = id(session)
        key = tileable.key

        def cb(_, sess=ref(session)):
            try:
                cur_thread_ident = threading.current_thread().ident
                decref_in_isolation = get_isolation().thread_ident == cur_thread_ident
            except KeyError:
                # isolation destroyed, no need to decref
                return

            fut = _decref_runner.put(key, sess)
            if not decref_in_isolation:
                # if decref in isolation, means that this tileable
                # is not required for main thread, thus we do not need
                # to wait for decref, otherwise, wait a bit
                try:
                    fut.result(0.5)
                except concurrent.futures.TimeoutError:
                    # ignore timeout
                    pass

        self.tileable = ref(tileable, cb)

    def __eq__(self, other: "_TileableSession"):
        return self._sess_id == other._sess_id


class _TileableDataCleaner:
    def __init__(self):
        self._tileable_to_sessions = WeakKeyDictionary()

    @enter_mode(build=True)
    def register(self, tileable: TileableType, session: SessionType):
        if tileable in self._tileable_to_sessions:
            self._tileable_to_sessions[tileable].append(
                _TileableSession(tileable, session)
            )
        else:
            self._tileable_to_sessions[tileable] = [_TileableSession(tileable, session)]


# we don't use __del__ to avoid potential Circular reference
_cleaner = _TileableDataCleaner()


def _get_session(executable: "_ExecutableMixin", session: SessionType = None):
    from ...deploy.oscar.session import get_default_session

    # if session is not specified, use default session
    if session is None:
        session = get_default_session()

    return session


class _ExecutableMixin:
    __slots__ = ()
    _executed_sessions: List[SessionType]

    def execute(self, session: SessionType = None, **kw):
        from ...deploy.oscar.session import execute

        session = _get_session(self, session)
        return execute(self, session=session, **kw)

    def _check_session(self, session: SessionType, action: str):
        if session is None:
            if isinstance(self, tuple):
                key = self[0].key
            else:
                key = self.key
            raise ValueError(
                f"Tileable object {key} must be executed first before {action}"
            )

    def _fetch(self, session: SessionType = None, **kw):
        from ...deploy.oscar.session import fetch

        session = _get_session(self, session)
        self._check_session(session, "fetch")
        return fetch(self, session=session, **kw)

    def fetch(self, session: SessionType = None, **kw):
        return self._fetch(session=session, **kw)

    def fetch_log(
        self,
        session: SessionType = None,
        offsets: List[int] = None,
        sizes: List[int] = None,
    ):
        from ...deploy.oscar.session import fetch_log

        session = _get_session(self, session)
        self._check_session(session, "fetch_log")
        return fetch_log(self, session=session, offsets=offsets, sizes=sizes)[0]

    def _fetch_infos(self, fields=None, session=None, **kw):
        from ...deploy.oscar.session import fetch_infos

        session = _get_session(self, session)
        self._check_session(session, "fetch_infos")
        return fetch_infos(self, fields=fields, session=session, **kw)

    def _attach_session(self, session: SessionType):
        if session not in self._executed_sessions:
            _cleaner.register(self, session)
            self._executed_sessions.append(session)

    def _detach_session(self, session: SessionType):
        if session in self._executed_sessions:
            sessions = _cleaner._tileable_to_sessions.get(self, [])
            if sessions:
                sessions.remove(_TileableSession(self, session))
            if len(sessions) == 0:
                del _cleaner._tileable_to_sessions[self]
            self._executed_sessions.remove(session)


class _ExecuteAndFetchMixin:
    __slots__ = ()

    def _execute_and_fetch(self, session: SessionType = None, **kw):
        from ...deploy.oscar.session import ExecutionInfo, SyncSession, fetch

        session = _get_session(self, session)
        fetch_kwargs = kw.pop("fetch_kwargs", dict())
        if session in self._executed_sessions:
            # if has been executed, fetch directly.
            return self.fetch(session=session, **fetch_kwargs)
        ret = self.execute(session=session, **kw)
        if isinstance(ret, ExecutionInfo):
            # wait=False
            aio_task = ret.aio_task

            async def _wait():
                await aio_task

            def run():
                asyncio.run_coroutine_threadsafe(_wait(), loop=ret.loop).result()
                return fetch(self, session=session, **fetch_kwargs)

            return SyncSession._execution_pool.submit(run)
        else:
            # wait=True
            return self.fetch(session=session, **fetch_kwargs)


class _ToObjectMixin(_ExecuteAndFetchMixin):
    __slots__ = ()

    def to_object(self, session: SessionType = None, **kw):
        return self._execute_and_fetch(session=session, **kw)


class ExecutableTuple(tuple, _ExecutableMixin, _ToObjectMixin):
    def __init__(self, *args):
        tuple.__init__(*args)

        self._fields_to_idx = None
        self._fields = None
        self._raw_type = None

        if len(args) == 1 and isinstance(args[0], tuple):
            self._fields = getattr(args[0], "_fields", None)
            if self._fields is not None:
                self._raw_type = type(args[0])
                self._fields_to_idx = {f: idx for idx, f in enumerate(self._fields)}

        self._executed_sessions = []

    def __getattr__(self, item):
        if self._fields_to_idx is None or item not in self._fields_to_idx:
            raise AttributeError(item)
        return self[self._fields_to_idx[item]]

    def __dir__(self):
        result = list(super().__dir__()) + list(self._fields or [])
        return sorted(result)

    def __repr__(self):
        if not self._fields:
            return super().__repr__()
        items = []
        for k, v in zip(self._fields, self):
            items.append(f"{k}={v!r}")
        return "%s(%s)" % (self._raw_type.__name__, ", ".join(items))

    def execute(self, session: SessionType = None, **kw):
        from ...deploy.oscar.session import execute

        if len(self) == 0:
            return self

        session = _get_session(self, session)
        ret = execute(*self, session=session, **kw)

        if session not in self._executed_sessions:
            self._executed_sessions.append(session)

        if kw.get("wait", True):
            return self
        else:
            return ret

    def _fetch(self, session: SessionType = None, **kw):
        from ...deploy.oscar.session import fetch

        session = _get_session(self, session)
        self._check_session(session, "fetch")
        return fetch(*self, session=session, **kw)

    def _fetch_infos(self, fields=None, session=None, **kw):
        from ...deploy.oscar.session import fetch_infos

        session = _get_session(self, session)
        self._check_session(session, "fetch_infos")
        return fetch_infos(*self, fields=fields, session=session, **kw)

    def fetch(self, session: SessionType = None, **kw):
        if len(self) == 0:
            return tuple()

        session = _get_session(self, session)
        ret = super().fetch(session=session, **kw)
        if self._raw_type is not None:
            ret = self._raw_type(*ret)
        if len(self) == 1:
            return (ret,)
        return ret

    def fetch_log(
        self,
        session: SessionType = None,
        offsets: List[int] = None,
        sizes: List[int] = None,
    ):
        from ...deploy.oscar.session import fetch_log

        if len(self) == 0:
            return []
        session = self._get_session(session=session)
        return fetch_log(*self, session=session, offsets=offsets, sizes=sizes)

    def _get_session(self, session: SessionType = None):
        if session is None:
            for item in self:
                session = _get_session(item, session)
                if session is not None:
                    return session
        return session
