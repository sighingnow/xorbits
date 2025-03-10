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

from ... import opcodes as OperandDef
from ...config import options
from .core import DataFrameCumReductionMixin, DataFrameCumReductionOperand


class DataFrameCumsum(DataFrameCumReductionOperand, DataFrameCumReductionMixin):
    _op_type_ = OperandDef.CUMSUM
    _func_name = "cumsum"


def cumsum(df, axis=None, skipna=True):
    use_inf_as_na = options.dataframe.mode.use_inf_as_na
    op = DataFrameCumsum(
        axis=axis,
        skipna=skipna,
        output_types=df.op.output_types,
        use_inf_as_na=use_inf_as_na,
    )
    return op(df)
