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

from .... import oscar as mo
from ...core import AbstractService
from ..task_info_collector import TaskInfoCollectorActor
from .manager import TaskConfigurationActor, TaskManagerActor


class TaskSupervisorService(AbstractService):
    """
    Task service on supervisor.

    Service Configuration
    ---------------------
    {
        "task": {
            "default_config": {
                "optimize_tileable_graph": True,
                "optimize_chunk_graph": True,
                "fuse_enabled": True,
                "reserved_finish_tasks": 10
            },
            "execution_config": {
                "backend": "mars",
                "mars": {},
            }
        }
    }
    """

    async def start(self):
        task_config = self._config.get("task", dict())
        options = task_config.get("default_config", dict())
        execution_config = task_config.get("execution_config", dict())
        task_processor_cls = task_config.get("task_processor_cls")
        task_preprocessor_cls = task_config.get("task_preprocessor_cls")
        await mo.create_actor(
            TaskConfigurationActor,
            options,
            execution_config=execution_config,
            task_processor_cls=task_processor_cls,
            task_preprocessor_cls=task_preprocessor_cls,
            address=self._address,
            uid=TaskConfigurationActor.default_uid(),
        )

        profiling_config = self._config.get("profiling", dict())
        await mo.create_actor(
            TaskInfoCollectorActor,
            profiling_config,
            uid=TaskInfoCollectorActor.default_uid(),
            address=self._address,
        )

    async def stop(self):
        await mo.destroy_actor(
            mo.create_actor_ref(
                uid=TaskConfigurationActor.default_uid(), address=self._address
            )
        )
        await mo.destroy_actor(
            mo.create_actor_ref(
                uid=TaskInfoCollectorActor.default_uid(), address=self._address
            )
        )

    async def create_session(self, session_id: str):
        await mo.create_actor(
            TaskManagerActor,
            session_id,
            address=self._address,
            uid=TaskManagerActor.gen_uid(session_id),
        )

    async def destroy_session(self, session_id: str):
        task_manager_ref = await mo.actor_ref(
            self._address, TaskManagerActor.gen_uid(session_id)
        )
        return await mo.destroy_actor(task_manager_ref)
