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

import itertools
import logging
from collections import defaultdict, deque
from typing import Dict, List, Tuple, Type, Union

from ....config import Config
from ....core import ChunkGraph, ChunkType, enter_mode
from ....core.operand import (
    Fetch,
    LogicKeyGenerator,
    MapReduceOperand,
    OperandStage,
    ShuffleFetchType,
    ShuffleProxy,
    VirtualOperand,
)
from ....lib.ordered_set import OrderedSet
from ....resource import Resource
from ....typing import BandType, OperandType
from ....utils import build_fetch, build_fetch_shuffle, tokenize
from ...subtask import Subtask, SubtaskGraph
from ..core import MapReduceInfo, Task, new_task_id
from .assigner import AbstractGraphAssigner, GraphAssigner
from .fusion import Coloring

logger = logging.getLogger(__name__)


def need_reassign_worker(op: OperandType) -> bool:
    # NOTE(qinxuye): special process for reducer
    # We'd better set reducer op's stage to reduce, however,
    # in many case, we copy a reducer op from tileable op,
    # then set stage as reducer one,
    # it would be quite nasty to take over the __setattr__ and
    # make reassign_worker True etc.
    return op.reassign_worker or (
        isinstance(op, MapReduceOperand) and op.stage == OperandStage.reduce
    )


class GraphAnalyzer:
    """
    An subtask graph builder which build subtask graph for chunk graph based on passed band_resource.

    If push shuffle is used, this builder will validate predecessors orders consistency of ShuffleProxy between
    chunk graph and generated subtask graph.
    """

    _map_reduce_id = itertools.count()

    def __init__(
        self,
        chunk_graph: ChunkGraph,
        band_resource: Dict[BandType, Resource],
        task: Task,
        config: Config,
        chunk_to_subtasks: Dict[ChunkType, Subtask],
        graph_assigner_cls: Type[AbstractGraphAssigner] = None,
        stage_id: str = None,
        map_reduce_id_to_infos: Dict[int, MapReduceInfo] = None,
        shuffle_fetch_type: ShuffleFetchType = ShuffleFetchType.FETCH_BY_KEY,
    ):
        self._chunk_graph = chunk_graph
        self._final_result_chunks_set = set(self._chunk_graph.result_chunks)
        self._band_resource = band_resource
        self._task = task
        self._stage_id = stage_id
        self._config = config
        self._shuffle_fetch_type = shuffle_fetch_type
        self._has_shuffle = any(
            isinstance(c.op, MapReduceOperand) for c in self._chunk_graph
        )
        self._fuse_enabled = task.fuse_enabled
        self._extra_config = task.extra_config
        self._chunk_to_subtasks = chunk_to_subtasks
        self._map_reduce_id_to_infos = map_reduce_id_to_infos
        if graph_assigner_cls is None:
            graph_assigner_cls = GraphAssigner
        self._graph_assigner_cls = graph_assigner_cls
        self._chunk_to_copied = dict()
        self._logic_key_generator = LogicKeyGenerator()

    @classmethod
    def next_map_reduce_id(cls) -> int:
        return next(cls._map_reduce_id)

    @classmethod
    def _iter_start_ops(cls, chunk_graph: ChunkGraph):
        visited = set()
        op_keys = set()
        start_chunks = deque(chunk_graph.iter_indep())
        stack = deque([start_chunks.popleft()])

        while stack:
            chunk = stack.popleft()
            if chunk not in visited:
                inp_chunks = chunk_graph.predecessors(chunk)
                if not inp_chunks or all(
                    inp_chunk in visited for inp_chunk in inp_chunks
                ):
                    if len(inp_chunks) == 0:
                        op_key = chunk.op.key
                        if op_key not in op_keys:
                            op_keys.add(op_key)
                            yield chunk.op
                    visited.add(chunk)
                    stack.extend(c for c in chunk_graph[chunk] if c not in visited)
                else:
                    stack.appendleft(chunk)
                    stack.extendleft(
                        reversed(
                            [
                                c
                                for c in chunk_graph.predecessors(chunk)
                                if c not in visited
                            ]
                        )
                    )
            if not stack and start_chunks:
                stack.appendleft(start_chunks.popleft())

    def _gen_input_chunks(
        self,
        inp_chunks: List[ChunkType],
        chunk_to_fetch_chunk: Dict[ChunkType, ChunkType],
    ) -> List[ChunkType]:
        # gen fetch chunks for input chunks
        inp_fetch_chunks = []
        for inp_chunk in inp_chunks:
            if inp_chunk in chunk_to_fetch_chunk:
                inp_fetch_chunks.append(chunk_to_fetch_chunk[inp_chunk])
            elif isinstance(inp_chunk.op, Fetch):
                chunk_to_fetch_chunk[inp_chunk] = inp_chunk
                inp_fetch_chunks.append(inp_chunk)
            elif isinstance(inp_chunk.op, ShuffleProxy):
                n_reducers = inp_chunk.op.n_reducers
                fetch_chunk = build_fetch_shuffle(
                    inp_chunk,
                    n_reducers=n_reducers,
                    shuffle_fetch_type=self._shuffle_fetch_type,
                ).data
                chunk_to_fetch_chunk[inp_chunk] = fetch_chunk
                inp_fetch_chunks.append(fetch_chunk)
            else:
                fetch_chunk = build_fetch(inp_chunk).data
                chunk_to_fetch_chunk[inp_chunk] = fetch_chunk
                inp_fetch_chunks.append(fetch_chunk)

        return inp_fetch_chunks

    @staticmethod
    def _to_band(band_or_worker: Union[BandType, str]) -> BandType:
        if isinstance(band_or_worker, tuple) and len(band_or_worker) == 2:
            # band already
            return band_or_worker
        else:
            return band_or_worker, "numa-0"

    @staticmethod
    def _get_expect_band(op: OperandType):
        if op.expect_band is not None:
            return op.expect_band
        elif op.expect_worker is not None:
            return GraphAnalyzer._to_band(op.expect_worker)

    def _gen_subtask_info(
        self,
        chunks: List[ChunkType],
        chunk_to_subtask: Dict[ChunkType, Subtask],
        chunk_to_bands: Dict[ChunkType, BandType],
        chunk_to_fetch_chunk: Dict[ChunkType, ChunkType],
    ) -> Tuple[Subtask, List[Subtask], bool]:
        # gen subtask and its input subtasks
        chunks_set = set(chunks)
        result_chunks = []
        result_chunks_set = set()
        chunk_graph = ChunkGraph(result_chunks)
        out_of_scope_chunks = []
        chunk_to_copied = self._chunk_to_copied
        update_meta_chunks = []
        # subtask properties
        band = None
        is_virtual = None
        retryable = True
        chunk_priority = None
        expect_band = None
        bands_specified = None
        processed = set()
        for chunk in chunks:
            if chunk in processed:
                continue
            if expect_band is None:
                expect_band = self._get_expect_band(chunk.op)
                bands_specified = expect_band is not None
            else:  # pragma: no cover
                curr_expect_band = self._get_expect_band(chunk.op)
                assert curr_expect_band is None or expect_band == curr_expect_band, (
                    f"expect_band {curr_expect_band} conflicts with chunks that have same color: "
                    f"{expect_band}"
                )
            # process band
            chunk_band = chunk_to_bands.get(chunk)
            if chunk_band is not None:
                assert (
                    band is None or band == chunk_band
                ), "band conflicts with chunks that have same color"
                band = chunk_band
            # process is_virtual
            if isinstance(chunk.op, VirtualOperand):
                assert is_virtual is None, "only 1 virtual operand can exist"
                is_virtual = True
            else:
                is_virtual = False
            # process retryable
            if not chunk.op.retryable:
                retryable = False
            # process priority
            if chunk.op.priority is not None:
                assert (
                    chunk_priority is None or chunk_priority == chunk.op.priority
                ), "priority conflicts with chunks that have same color"
                chunk_priority = chunk.op.priority
            # process input chunks
            inp_chunks = []
            build_fetch_index_to_chunks = dict()
            for i, inp_chunk in enumerate(chunk.inputs):
                if inp_chunk in chunks_set:
                    inp_chunks.append(chunk_to_copied[inp_chunk])
                else:
                    build_fetch_index_to_chunks[i] = inp_chunk
                    inp_chunks.append(None)
                    if not isinstance(inp_chunk.op, Fetch):
                        out_of_scope_chunks.append(inp_chunk)
            fetch_chunks = self._gen_input_chunks(
                list(build_fetch_index_to_chunks.values()), chunk_to_fetch_chunk
            )
            for i, fetch_chunk in zip(build_fetch_index_to_chunks, fetch_chunks):
                inp_chunks[i] = fetch_chunk
            copied_op = chunk.op.copy()
            copied_op._key = chunk.op.key
            out_chunks = [
                c.data
                for c in copied_op.new_chunks(
                    inp_chunks, kws=[c.params.copy() for c in chunk.op.outputs]
                )
            ]
            for src_chunk, out_chunk in zip(chunk.op.outputs, out_chunks):
                processed.add(src_chunk)
                out_chunk._key = src_chunk.key
                chunk_graph.add_node(out_chunk)
                # cannot be copied twice
                assert src_chunk not in chunk_to_copied
                chunk_to_copied[src_chunk] = out_chunk
                if src_chunk in self._final_result_chunks_set:
                    if out_chunk not in result_chunks_set:
                        # add to result chunks
                        result_chunks.append(out_chunk)
                        # chunk is in the result chunks of full chunk graph
                        # meta need to be updated
                        update_meta_chunks.append(out_chunk)
                        result_chunks_set.add(out_chunk)
                if not is_virtual:
                    # skip adding fetch chunk to chunk graph when op is virtual operand
                    for c in inp_chunks:
                        if c not in chunk_graph:
                            chunk_graph.add_node(c)
                        chunk_graph.add_edge(c, out_chunk)
        stage_n_outputs = len(result_chunks)
        # add chunks with no successors into result chunks
        result_chunks.extend(
            c
            for c in chunk_graph.iter_indep(reverse=True)
            if c not in result_chunks_set
        )
        expect_bands = (
            [expect_band] if bands_specified else ([band] if band is not None else None)
        )
        # calculate priority
        if out_of_scope_chunks:
            inp_subtasks = []
            for out_of_scope_chunk in out_of_scope_chunks:
                copied_out_of_scope_chunk = chunk_to_copied[out_of_scope_chunk]
                inp_subtask = chunk_to_subtask[out_of_scope_chunk]
                if (
                    copied_out_of_scope_chunk
                    not in inp_subtask.chunk_graph.result_chunks
                ):
                    # make sure the chunk that out of scope
                    # is in the input subtask's results,
                    # or the meta may be lost
                    inp_subtask.chunk_graph.result_chunks.append(
                        copied_out_of_scope_chunk
                    )
                inp_subtasks.append(inp_subtask)
            depth = max(st.priority[0] for st in inp_subtasks) + 1
        else:
            inp_subtasks = []
            depth = 0
        priority = (depth, chunk_priority or 0)

        subtask = Subtask(
            subtask_id=new_task_id(),
            stage_id=self._stage_id,
            logic_key=self._gen_logic_key(chunks),
            session_id=self._task.session_id,
            task_id=self._task.task_id,
            chunk_graph=chunk_graph,
            expect_bands=expect_bands,
            bands_specified=bands_specified,
            virtual=is_virtual,
            priority=priority,
            retryable=retryable,
            update_meta_chunks=update_meta_chunks,
            extra_config=self._extra_config,
            stage_n_outputs=stage_n_outputs,
        )

        is_shuffle_proxy = False
        if self._has_shuffle:
            proxy_chunks = [c for c in result_chunks if isinstance(c.op, ShuffleProxy)]
            if proxy_chunks:
                assert len(proxy_chunks) <= 1, proxy_chunks
                is_shuffle_proxy = True
        return subtask, inp_subtasks, is_shuffle_proxy

    def _gen_logic_key(self, chunks: List[ChunkType]):
        return tokenize(
            *[self._logic_key_generator.get_logic_key(chunk.op) for chunk in chunks]
        )

    def _gen_map_reduce_info(
        self, chunk: ChunkType, assign_results: Dict[ChunkType, BandType]
    ):
        reducer_ops = OrderedSet(
            [
                c.op
                for c in self._chunk_graph.successors(chunk)
                if c.op.stage == OperandStage.reduce
            ]
        )
        map_chunks = [
            c
            for c in self._chunk_graph.predecessors(chunk)
            if (c.op.stage == OperandStage.map) or c.is_mapper
        ]
        map_reduce_id = self.next_map_reduce_id()
        for map_chunk in map_chunks:
            # record analyzer map reduce id for mapper op
            # copied chunk exists because map chunk must have
            # been processed before shuffle proxy
            copied_map_chunk = self._chunk_to_copied[map_chunk]
            if not hasattr(copied_map_chunk, "extra_params"):  # pragma: no cover
                copied_map_chunk.extra_params = dict()
            copied_map_chunk.extra_params["analyzer_map_reduce_id"] = map_reduce_id
        reducer_bands = [assign_results[r.outputs[0]] for r in reducer_ops]
        map_reduce_info = MapReduceInfo(
            map_reduce_id=map_reduce_id,
            reducer_indexes=[reducer_op.reducer_index for reducer_op in reducer_ops],
            reducer_bands=reducer_bands,
        )
        self._map_reduce_id_to_infos[map_reduce_id] = map_reduce_info

    @enter_mode(build=True)
    def gen_subtask_graph(
        self, op_to_bands: Dict[str, BandType] = None
    ) -> SubtaskGraph:
        """
        Analyze chunk graph and generate subtask graph.

        Returns
        -------
        subtask_graph: SubtaskGraph
            Subtask graph.
        """
        # reassign worker when specified reassign_worker = True
        # or it's a reducer operands
        reassign_worker_ops = [
            chunk.op for chunk in self._chunk_graph if need_reassign_worker(chunk.op)
        ]
        start_ops = (
            list(self._iter_start_ops(self._chunk_graph))
            if len(self._chunk_graph) > 0
            else []
        )

        # assign start chunks
        to_assign_ops = start_ops + reassign_worker_ops
        assigner = self._graph_assigner_cls(
            self._chunk_graph, to_assign_ops, self._band_resource
        )
        # assign expect bands
        cur_assigns = {
            op.key: self._get_expect_band(op)
            for op in start_ops
            if op.expect_band is not None or op.expect_worker is not None
        }
        if op_to_bands:
            cur_assigns.update(op_to_bands)
        logger.debug(
            "Start to assign %s start chunks for task %s",
            len(start_ops),
            self._task.task_id,
        )
        chunk_to_bands = assigner.assign(cur_assigns=cur_assigns)
        logger.debug(
            "Assigned %s start chunks for task %s", len(start_ops), self._task.task_id
        )
        # assign expect workers for those specified with `expect_worker` or `expect_band`
        # skip `start_ops`, which have been assigned before
        start_ops_set = set(start_ops)
        for chunk in self._chunk_graph:
            if chunk not in start_ops_set:
                if chunk.op.expect_band is not None:
                    chunk_to_bands[chunk] = chunk.op.expect_band
                elif chunk.op.expect_worker is not None:
                    chunk_to_bands[chunk] = self._to_band(chunk.op.expect_worker)

        # color nodes
        if self._fuse_enabled:
            logger.debug("Start to fuse chunks for task %s", self._task.task_id)
            # sort start chunks in coloring as start_ops
            op_key_to_chunks = defaultdict(list)
            for chunk in self._chunk_graph:
                op_key_to_chunks[chunk.op.key].append(chunk)
            init_chunk_to_bands = dict()
            for start_op in start_ops:
                for start_chunk in op_key_to_chunks[start_op.key]:
                    init_chunk_to_bands[start_chunk] = chunk_to_bands[start_chunk]
            if (
                self._has_shuffle
                and self._shuffle_fetch_type == ShuffleFetchType.FETCH_BY_INDEX
            ):
                # ensure no shuffle mapper chunks fused into same subtask.
                initial_same_color_num = 1
            else:
                initial_same_color_num = getattr(
                    self._config, "initial_same_color_num", None
                )
            coloring = Coloring(
                self._chunk_graph,
                list(self._band_resource),
                init_chunk_to_bands,
                initial_same_color_num=initial_same_color_num,
                as_broadcaster_successor_num=getattr(
                    self._config, "as_broadcaster_successor_num", None
                ),
            )
            chunk_to_colors = coloring.color()
        else:
            # if not fuse enabled, color all chunks with different colors
            op_to_colors = dict()
            chunk_to_colors = dict()
            color_gen = itertools.count()
            for c in self._chunk_graph.topological_iter():
                if c.op not in op_to_colors:
                    chunk_to_colors[c] = op_to_colors[c.op] = next(color_gen)
                else:
                    chunk_to_colors[c] = op_to_colors[c.op]
        color_to_chunks = defaultdict(list)
        for chunk, color in chunk_to_colors.items():
            if not isinstance(chunk.op, Fetch):
                color_to_chunks[color].append(chunk)

        # gen subtask graph
        subtask_graph = SubtaskGraph()
        chunk_to_fetch_chunk = dict()
        chunk_to_subtask = self._chunk_to_subtasks
        # states
        visited = set()
        logic_key_to_subtasks = defaultdict(list)
        if self._shuffle_fetch_type == ShuffleFetchType.FETCH_BY_INDEX:
            for chunk in self._chunk_graph.topological_iter():
                if not isinstance(chunk.op, ShuffleProxy):
                    continue
                # Can't use `OperandStage.map` to find mappers directly, since `stage` of some operand
                # such as `DataFrameIndexAlign` are `OperandStage.map` but not a shuffle mapper sometimes.
                mapper_chunks = self._chunk_graph.predecessors(chunk)
                for mapper_chunk in mapper_chunks:
                    chunk_color = chunk_to_colors[mapper_chunk]
                    same_color_chunks = color_to_chunks[chunk_color]
                    mappers = [
                        c
                        for c in same_color_chunks
                        if c.op.stage == OperandStage.map
                        and any(
                            isinstance(succ.op, ShuffleProxy)
                            for succ in self._chunk_graph.iter_successors(c)
                        )
                    ]
                    if len(mappers) > 1:
                        # ensure every subtask contains only at most one mapper
                        for mapper in mappers:
                            same_color_chunks.remove(mapper)
                            mapper_color = coloring.next_color()
                            chunk_to_colors[mapper] = mapper_color
                            color_to_chunks[mapper_color] = [mapper]
        for chunk in self._chunk_graph.topological_iter():
            if chunk in visited or isinstance(chunk.op, Fetch):
                # skip fetch chunk
                continue

            color = chunk_to_colors[chunk]
            same_color_chunks = color_to_chunks[color]
            if all(isinstance(c.op, Fetch) for c in same_color_chunks):
                # all fetch ops, no need to gen subtask
                continue
            subtask, inp_subtasks, is_shuffle_proxy = self._gen_subtask_info(
                same_color_chunks,
                chunk_to_subtask,
                chunk_to_bands,
                chunk_to_fetch_chunk,
            )
            subtask_graph.add_node(subtask)
            if is_shuffle_proxy:
                subtask_graph.add_shuffle_proxy_subtask(subtask)
            logic_key_to_subtasks[subtask.logic_key].append(subtask)
            for inp_subtask in inp_subtasks:
                subtask_graph.add_edge(inp_subtask, subtask)

            for c in same_color_chunks:
                chunk_to_subtask[c] = subtask
            if self._map_reduce_id_to_infos is not None and isinstance(
                chunk.op, ShuffleProxy
            ):
                self._gen_map_reduce_info(chunk, chunk_to_bands)
            visited.update(same_color_chunks)

        for subtasks in logic_key_to_subtasks.values():
            for logic_index, subtask in enumerate(subtasks):
                subtask.logic_index = logic_index
                subtask.logic_parallelism = len(subtasks)
        return subtask_graph
