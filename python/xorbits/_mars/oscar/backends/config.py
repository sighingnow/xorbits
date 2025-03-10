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

from typing import Any, Dict, List, Union


class ActorPoolConfig:
    __slots__ = ("_conf",)

    def __init__(self, conf: Dict = None):
        if conf is None:
            conf = dict()
        self._conf = conf
        if "pools" not in self._conf:
            self._conf["pools"] = dict()
        if "mapping" not in self._conf:
            self._conf["mapping"] = dict()
        if "metrics" not in self._conf:
            self._conf["metrics"] = dict()
        if "comm" not in self._conf:
            self._conf["comm"] = dict()

    @property
    def n_pool(self):
        return len(self._conf["pools"])

    def add_pool_conf(
        self,
        process_index: int,
        label: str,
        internal_address: str,
        external_address: Union[str, List[str]],
        env: Dict = None,
        modules: List[str] = None,
        suspend_sigint: bool = False,
        use_uvloop: bool = False,
        logging_conf: Dict = None,
        kwargs: Dict = None,
    ):
        pools: Dict = self._conf["pools"]
        if not isinstance(external_address, list):
            external_address = [external_address]
        pools[process_index] = {
            "label": label,
            "internal_address": internal_address,
            "external_address": external_address,
            "env": env,
            "modules": modules,
            "suspend_sigint": suspend_sigint,
            "use_uvloop": use_uvloop,
            "logging_conf": logging_conf,
            "kwargs": kwargs or {},
        }

        mapping: Dict = self._conf["mapping"]
        for addr in external_address:
            mapping[addr] = internal_address

    def get_pool_config(self, process_index: int):
        return self._conf["pools"][process_index]

    def get_external_address(self, process_index: int) -> str:
        return self._conf["pools"][process_index]["external_address"][0]

    def get_process_indexes(self):
        return list(self._conf["pools"])

    def get_process_index(self, external_address: str):
        for process_index, conf in self._conf["pools"].items():
            if external_address in conf["external_address"]:
                return process_index
        raise ValueError(
            f"Cannot get process_index for {external_address}"
        )  # pragma: no cover

    def reset_pool_external_address(
        self,
        process_index: int,
        external_address: Union[str, List[str]],
    ):
        if not isinstance(external_address, list):
            external_address = [external_address]
        cur_pool_config = self._conf["pools"][process_index]
        internal_address = cur_pool_config["internal_address"]

        mapping: Dict = self._conf["mapping"]
        for addr in cur_pool_config["external_address"]:
            if internal_address == addr:
                # internal address may be the same as external address in Windows
                internal_address = external_address[0]
            mapping.pop(addr, None)

        cur_pool_config["external_address"] = external_address
        for addr in external_address:
            mapping[addr] = internal_address

    def get_external_addresses(self, label=None) -> List[str]:
        result = []
        for c in self._conf["pools"].values():
            if label is not None:
                if label == c["label"]:
                    result.append(c["external_address"][0])
            else:
                result.append(c["external_address"][0])
        return result

    @property
    def external_to_internal_address_map(self) -> Dict[str, str]:
        return self._conf["mapping"]

    def as_dict(self):
        return self._conf

    def add_metric_configs(self, metrics: Dict[str, Any]):
        if metrics:
            self._conf["metrics"].update(metrics)

    def get_metric_configs(self):
        return self._conf["metrics"]

    def add_comm_config(self, comm_config: Dict[str, Any]):
        if comm_config:
            self._conf["comm"].update(comm_config)

    def get_comm_config(self) -> dict:
        return self._conf["comm"]
