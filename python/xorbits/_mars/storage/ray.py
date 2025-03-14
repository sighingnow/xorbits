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

import inspect
from typing import Any, Dict, List, Tuple

from ..lib import sparse
from ..metrics import Metrics, Percentile, record_time_cost_percentile
from ..oscar.debug import debug_async_timeout
from ..utils import implements, lazy_import, register_ray_serializer
from .base import ObjectInfo, StorageBackend, StorageLevel, register_storage_backend
from .core import BufferWrappedFileObject, StorageFileObject

ray = lazy_import("ray")


# TODO(fyrestone): make the SparseMatrix pickleable.


def _mars_sparse_matrix_serializer(value):
    return [value.shape, value.spmatrix]


def _mars_sparse_matrix_deserializer(obj) -> sparse.SparseNDArray:
    shape, spmatrix = obj
    return sparse.matrix.SparseMatrix(spmatrix, shape=shape)


def _register_sparse_matrix_serializer():
    # register a custom serializer for Mars SparseMatrix
    register_ray_serializer(
        sparse.matrix.SparseMatrix,
        serializer=_mars_sparse_matrix_serializer,
        deserializer=_mars_sparse_matrix_deserializer,
    )


class RayFileLikeObject:
    def __init__(self):
        self._buffers = []
        self._size = 0

    def write(self, content: bytes):
        self._buffers.append(content)
        self._size += len(content)

    def readinto(self, buffer):
        read_bytes = 0
        for b in self._buffers:
            read_pos = read_bytes + len(b)
            buffer[read_bytes:read_pos] = b
            read_bytes = read_pos
        return read_bytes

    def close(self):
        self._buffers.clear()
        self._size = 0

    def tell(self):
        return self._size


class RayFileObject(BufferWrappedFileObject):
    def __init__(self, object_id: Any, mode: str):
        super().__init__(object_id, mode, size=0)

    def _write_init(self):
        self._buffer = RayFileLikeObject()

    def _read_init(self):
        self._buffer = ray.get(self._object_id)
        self._mv = memoryview(self._buffer)
        self._size = len(self._buffer)

    def write(self, content: bytes):
        if not self._initialized:
            self._write_init()
            self._initialized = True

        return self._buffer.write(content)

    def _write_close(self):
        worker = ray.worker.global_worker
        metadata = ray.ray_constants.OBJECT_METADATA_TYPE_RAW
        args = [metadata, self._buffer.tell(), self._buffer, self._object_id]
        try:
            worker.core_worker.put_file_like_object(*args)
        except TypeError:
            args.append(None)  # owner_address for ray >= 1.3.0
            worker.core_worker.put_file_like_object(*args)

    def _read_close(self):
        pass


_support_specify_owner = None


def support_specify_owner():
    global _support_specify_owner
    if _support_specify_owner is None:
        sig = inspect.signature(ray.put)
        _support_specify_owner = "_owner" in sig.parameters
    return _support_specify_owner


@register_storage_backend
class RayStorage(StorageBackend):
    name = "ray"
    is_seekable = False

    def __init__(self, *args, **kwargs):
        self._owner_address = kwargs.get("owner")
        self._owner = None  # A ray actor which will own the objects put by workers.
        self._storage_get_metrics = [
            (
                Percentile.PercentileType.P99,
                Metrics.gauge(
                    "mars.storage.ray.get_cost_time_p99_seconds",
                    "P99 time consuming in seconds to get object, every 1000 times report once.",
                ).record,
                1000,
            ),
            (
                Percentile.PercentileType.P95,
                Metrics.gauge(
                    "mars.storage.ray.get_cost_time_p95_seconds",
                    "P95 time consuming in seconds to get object, every 1000 times report once.",
                ).record,
                1000,
            ),
            (
                Percentile.PercentileType.P90,
                Metrics.gauge(
                    "mars.storage.ray.get_cost_time_p90_seconds",
                    "P90 time consuming in seconds to get object, every 1000 times report once.",
                ).record,
                1000,
            ),
        ]

        self._storage_put_metrics = [
            (
                Percentile.PercentileType.P99,
                Metrics.gauge(
                    "mars.storage.ray.put_cost_time_p99_seconds",
                    "P99 time consuming in seconds to put object, every 1000 times report once.",
                ).record,
                1000,
            ),
            (
                Percentile.PercentileType.P95,
                Metrics.gauge(
                    "mars.storage.ray.put_cost_time_p95_seconds",
                    "P95 time consuming in seconds to put object, every 1000 times report once.",
                ).record,
                1000,
            ),
            (
                Percentile.PercentileType.P90,
                Metrics.gauge(
                    "mars.storage.ray.put_cost_time_p90_seconds",
                    "P90 time consuming in seconds to put object, every 1000 times report once.",
                ).record,
                1000,
            ),
        ]

    @classmethod
    @implements(StorageBackend.setup)
    async def setup(cls, **kwargs) -> Tuple[Dict, Dict]:
        _register_sparse_matrix_serializer()
        return kwargs, dict()

    @staticmethod
    @implements(StorageBackend.teardown)
    async def teardown(**kwargs):
        pass

    @property
    @implements(StorageBackend.level)
    def level(self) -> StorageLevel:
        # TODO(fyrestone): return StorageLevel.MEMORY & StorageLevel.DISK
        # if object spilling is available.
        return StorageLevel.MEMORY | StorageLevel.REMOTE

    @implements(StorageBackend.get)
    async def get(self, object_id, **kwargs) -> object:
        if kwargs:  # pragma: no cover
            raise NotImplementedError(f'Got unsupported args: {",".join(kwargs)}')
        with debug_async_timeout(
            "ray_object_retrieval_timeout",
            "Storage get object timeout, ObjectRef: %s",
            object_id,
        ):
            with record_time_cost_percentile(self._storage_get_metrics):
                return await object_id

    @implements(StorageBackend.put)
    async def put(self, obj, importance=0) -> ObjectInfo:
        with record_time_cost_percentile(self._storage_put_metrics):
            if support_specify_owner() and self._owner_address:
                if not self._owner:
                    self._owner = ray.get_actor(self._owner_address)
                object_id = ray.put(obj, _owner=self._owner)
            else:
                object_id = ray.put(obj)
        # We can't get the serialized bytes length from ray.put
        return ObjectInfo(object_id=object_id)

    @implements(StorageBackend.delete)
    async def delete(self, object_id):
        ray.internal.free(object_id)

    @implements(StorageBackend.object_info)
    async def object_info(self, object_id) -> ObjectInfo:
        # The performance of obtaining the object size is poor.
        return ObjectInfo(object_id=object_id)

    @implements(StorageBackend.open_writer)
    async def open_writer(self, size=None) -> StorageFileObject:
        new_id = ray.ObjectRef.from_random()
        ray_writer = RayFileObject(new_id, mode="w")
        return StorageFileObject(ray_writer, object_id=new_id)

    @implements(StorageBackend.open_reader)
    async def open_reader(self, object_id) -> StorageFileObject:
        ray_reader = RayFileObject(object_id, mode="r")
        return StorageFileObject(ray_reader, object_id=object_id)

    @implements(StorageBackend.list)
    async def list(self) -> List:
        raise NotImplementedError("Ray storage does not support list")

    @implements(StorageBackend.fetch)
    async def fetch(self, object_id):
        pass
