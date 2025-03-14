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

import sys
from collections.abc import Iterable

import cloudpickle
import numpy as np
from pandas.core.groupby import DataFrameGroupBy, SeriesGroupBy

from ..utils import estimate_pandas_size, no_default, pd_release_version

_HAS_SQUEEZE = pd_release_version < (1, 1, 0)
_HAS_DROPNA = pd_release_version >= (1, 1, 0)
_GROUP_KEYS_NO_DEFAULT = pd_release_version >= (1, 5, 0)

_default_group_keys = no_default if _GROUP_KEYS_NO_DEFAULT else True


class GroupByWrapper:
    def __init__(
        self,
        obj,
        groupby_obj=None,
        keys=None,
        axis=0,
        level=None,
        grouper=None,
        exclusions=None,
        selection=None,
        as_index=True,
        sort=True,
        group_keys=_default_group_keys,
        squeeze=False,
        observed=False,
        dropna=True,
        mutated=False,
        grouper_cache=None,
    ):
        def fill_value(v, key):
            return (
                v if v is not None or groupby_obj is None else getattr(groupby_obj, key)
            )

        self.obj = obj
        self.keys = fill_value(keys, "keys")
        self.axis = fill_value(axis, "axis")
        self.level = fill_value(level, "level")
        self.exclusions = fill_value(exclusions, "exclusions")
        self.selection = selection
        self.as_index = fill_value(as_index, "as_index")
        self.sort = fill_value(sort, "sort")
        self.group_keys = fill_value(group_keys, "group_keys")
        self.squeeze = fill_value(squeeze, "squeeze")
        self.observed = fill_value(observed, "observed")
        self.mutated = fill_value(mutated, "mutated")
        self.dropna = fill_value(dropna, "dropna")

        if groupby_obj is None:
            groupby_kw = dict(
                keys=keys,
                axis=axis,
                level=level,
                grouper=grouper,
                exclusions=exclusions,
                as_index=as_index,
                group_keys=group_keys,
                squeeze=squeeze,
                observed=observed,
                mutated=mutated,
                dropna=dropna,
            )
            if not _HAS_SQUEEZE:  # pragma: no branch
                groupby_kw.pop("squeeze")
            if not _HAS_DROPNA:  # pragma: no branch
                groupby_kw.pop("dropna")

            if obj.ndim == 2:
                self.groupby_obj = DataFrameGroupBy(obj, **groupby_kw)
            else:
                self.groupby_obj = SeriesGroupBy(obj, **groupby_kw)
        else:
            self.groupby_obj = groupby_obj

        if grouper_cache:
            self.groupby_obj.grouper._cache = grouper_cache
        if selection:
            self.groupby_obj = self.groupby_obj[selection]

        self.is_frame = isinstance(self.groupby_obj, DataFrameGroupBy)

    def __getitem__(self, item):
        return GroupByWrapper(
            self.obj,
            keys=self.keys,
            axis=self.axis,
            level=self.level,
            grouper=self.groupby_obj.grouper,
            exclusions=self.exclusions,
            selection=item,
            as_index=self.as_index,
            sort=self.sort,
            group_keys=self.group_keys,
            squeeze=self.squeeze,
            observed=self.observed,
            dropna=self.dropna,
            mutated=self.mutated,
        )

    def __getattr__(self, item):
        if item.startswith("_"):  # pragma: no cover
            return object.__getattribute__(self, item)
        if item in getattr(self.obj, "columns", ()):
            return self.__getitem__(item)
        return getattr(self.groupby_obj, item)

    def __iter__(self):
        return self.groupby_obj.__iter__()

    def __sizeof__(self):
        return sys.getsizeof(self.obj) + sys.getsizeof(
            getattr(self.groupby_obj.grouper, "_cache", None)
        )

    def estimate_size(self):
        return estimate_pandas_size(self.obj) + estimate_pandas_size(self.obj.index)

    def __reduce__(self):
        return (
            type(self).from_tuple,
            (self.to_tuple(pickle_function=True, truncate=True),),
        )

    def __bool__(self):
        return bool(np.prod(self.shape))

    @property
    def empty(self):
        return self.obj.empty

    @property
    def shape(self):
        shape = list(self.groupby_obj.obj.shape)
        if self.is_frame and self.selection:
            shape[1] = len(self.selection)
        return tuple(shape)

    @property
    def _selected_obj(self):
        return getattr(self.groupby_obj, "_selected_obj")

    def to_tuple(self, truncate=False, pickle_function=False):
        if self.selection and truncate:
            if isinstance(self.selection, Iterable) and not isinstance(
                self.selection, str
            ):
                item_list = list(self.selection)
            else:
                item_list = [self.selection]
            item_set = set(item_list)

            if isinstance(self.keys, list):
                sel_keys = self.keys
            elif self.keys in self.obj.columns:
                sel_keys = [self.keys]
            else:
                sel_keys = []

            all_items = item_list + [k for k in sel_keys or () if k not in item_set]
            if set(all_items) == set(self.obj.columns):
                obj = self.obj
            else:
                obj = self.obj[all_items]
        else:
            obj = self.obj

        if pickle_function and callable(self.keys):
            keys = cloudpickle.dumps(self.keys)
        else:
            keys = self.keys

        return (
            obj,
            keys,
            self.axis,
            self.level,
            self.exclusions,
            self.selection,
            self.as_index,
            self.sort,
            self.group_keys,
            self.squeeze,
            self.observed,
            self.dropna,
            self.mutated,
            getattr(self.groupby_obj.grouper, "_cache", dict()),
        )

    @classmethod
    def from_tuple(cls, tp):
        (
            obj,
            keys,
            axis,
            level,
            exclusions,
            selection,
            as_index,
            sort,
            group_keys,
            squeeze,
            observed,
            dropna,
            mutated,
            grouper_cache,
        ) = tp

        if isinstance(keys, (bytes, bytearray)):
            keys = cloudpickle.loads(keys)

        return cls(
            obj,
            keys=keys,
            axis=axis,
            level=level,
            exclusions=exclusions,
            selection=selection,
            as_index=as_index,
            sort=sort,
            group_keys=group_keys,
            squeeze=squeeze,
            observed=observed,
            dropna=dropna,
            mutated=mutated,
            grouper_cache=grouper_cache,
        )


def wrapped_groupby(
    obj,
    by=None,
    axis=0,
    level=None,
    as_index=True,
    sort=True,
    group_keys=_default_group_keys,
    squeeze=False,
    observed=False,
    dropna=True,
):
    groupby_kw = dict(
        by=by,
        axis=axis,
        level=level,
        as_index=as_index,
        sort=sort,
        group_keys=group_keys,
        squeeze=squeeze,
        observed=observed,
        dropna=dropna,
    )
    if not _HAS_SQUEEZE:  # pragma: no branch
        groupby_kw.pop("squeeze")
    if not _HAS_DROPNA:  # pragma: no branch
        groupby_kw.pop("dropna")

    groupby_obj = obj.groupby(**groupby_kw)
    return GroupByWrapper(obj, groupby_obj=groupby_obj, as_index=as_index)
