# Licensed to Modin Development Team under one or more contributor license agreements.
# See the NOTICE file distributed with this work for additional information regarding
# copyright ownership.  The Modin Development Team licenses this file to you under the
# Apache License, Version 2.0 (the "License"); you may not use this file except in
# compliance with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under
# the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific language
# governing permissions and limitations under the License.

"""Module houses class that wraps data (block partition) and its metadata."""

import pandas

from modin.data_management.utils import length_fn_pandas, width_fn_pandas
from modin.engines.base.frame.partition import PandasFramePartition
from modin.pandas.indexing import compute_sliced_len

import ray
from ray.services import get_node_ip_address
from packaging import version

ObjectIDType = ray.ObjectRef
if version.parse(ray.__version__) >= version.parse("1.2.0"):
    from ray.util.client.common import ClientObjectRef

    ObjectIDType = (ray.ObjectRef, ClientObjectRef)

compute_sliced_len = ray.remote(compute_sliced_len)


class PandasOnRayFramePartition(PandasFramePartition):
    """
    The class implements the interface in ``PandasFramePartition``.

    Parameters
    ----------
    object_id : ray.ObjectRef
        A reference to ``pandas.DataFrame`` that need to be wrapped with this class.
    length : ray.ObjectRef or int, optional
        Length or reference to it of wrapped ``pandas.DataFrame``.
    width : ray.ObjectRef or int, optional
        Width or reference to it of wrapped ``pandas.DataFrame``.
    ip : ray.ObjectRef or str, optional
        Node IP address or reference to it that holds wrapped ``pandas.DataFrame``.
    call_queue : list
        Call queue that needs to be executed on wrapped ``pandas.DataFrame``.
    """

    def __init__(self, object_id, length=None, width=None, ip=None, call_queue=None):
        assert isinstance(object_id, ObjectIDType)

        self.oid = object_id
        if call_queue is None:
            call_queue = []
        self.call_queue = call_queue
        self._length_cache = length
        self._width_cache = width
        self._ip_cache = ip

    def get(self):
        """
        Get the object wrapped by this partition out of the Plasma store.

        Returns
        -------
        pandas.DataFrame
            The object from the Plasma store.
        """
        if len(self.call_queue):
            self.drain_call_queue()
        return ray.get(self.oid)

    def apply(self, func, **kwargs):
        """
        Apply a function to the object wrapped by this partition.

        Parameters
        ----------
        func : callable or ray.ObjectRef
            A function to apply.
        **kwargs
            Additional keyword arguments to be passed in `func`.

        Returns
        -------
        PandasOnRayFramePartition
            A new ``PandasOnRayFramePartition`` object.

        Notes
        -----
        It does not matter if `func` is callable or an ``ray.ObjectRef``. Ray will
        handle it correctly either way. The keyword arguments are sent as a dictionary.
        """
        oid = self.oid
        call_queue = self.call_queue + [(func, kwargs)]
        result, length, width, ip = deploy_ray_func.remote(call_queue, oid)
        return PandasOnRayFramePartition(result, length, width, ip)

    def add_to_apply_calls(self, func, **kwargs):
        """
        Add a function to the call queue.

        Parameters
        ----------
        func : callable or ray.ObjectRef
            Function to be added to the call queue.
        **kwargs : dict
            Additional keyword arguments to be passed in `func`.

        Returns
        -------
        PandasOnRayFramePartition
            A new ``PandasOnRayFramePartition`` object.

        Notes
        -----
        It does not matter if `func` is callable or an ``ray.ObjectRef``. Ray will
        handle it correctly either way. The keyword arguments are sent as a dictionary.
        """
        return PandasOnRayFramePartition(
            self.oid, call_queue=self.call_queue + [(func, kwargs)]
        )

    def drain_call_queue(self):
        """Execute all operations stored in the call queue on the object wrapped by this partition."""
        if len(self.call_queue) == 0:
            return
        oid = self.oid
        call_queue = self.call_queue
        (
            self.oid,
            self._length_cache,
            self._width_cache,
            self._ip_cache,
        ) = deploy_ray_func.remote(call_queue, oid)
        self.call_queue = []

    def wait(self):
        """Wait completing computations on the object wrapped by the partition."""
        self.drain_call_queue()
        ray.wait([self.oid])

    def __copy__(self):
        """
        Create a copy of this partition.

        Returns
        -------
        PandasOnRayFramePartition
            A copy of this partition.
        """
        return PandasOnRayFramePartition(
            self.oid,
            length=self._length_cache,
            width=self._width_cache,
            ip=self._ip_cache,
            call_queue=self.call_queue,
        )

    def to_pandas(self):
        """
        Convert the object wrapped by this partition to a ``pandas.DataFrame``.

        Returns
        -------
        pandas DataFrame.
        """
        dataframe = self.get()
        assert type(dataframe) is pandas.DataFrame or type(dataframe) is pandas.Series
        return dataframe

    def to_numpy(self, **kwargs):
        """
        Convert the object wrapped by this partition to a NumPy array.

        Parameters
        ----------
        **kwargs : dict
            Additional keyword arguments to be passed in ``to_numpy``.

        Returns
        -------
        np.ndarray
        """
        return self.apply(lambda df, **kwargs: df.to_numpy(**kwargs)).get()

    def mask(self, row_indices, col_indices):
        """
        Lazily create a mask that extracts the indices provided.

        Parameters
        ----------
        row_indices : list-like, slice or label
            The indices for the rows to extract.
        col_indices : list-like, slice or label
            The indices for the columns to extract.

        Returns
        -------
        PandasOnRayFramePartition
            A new ``PandasOnRayFramePartition`` object.
        """
        new_obj = super().mask(row_indices, col_indices)
        if isinstance(row_indices, slice) and isinstance(
            self._length_cache, ObjectIDType
        ):
            new_obj._length_cache = compute_sliced_len.remote(
                row_indices, self._length_cache
            )
        if isinstance(col_indices, slice) and isinstance(
            self._width_cache, ObjectIDType
        ):
            new_obj._width_cache = compute_sliced_len.remote(
                col_indices, self._width_cache
            )
        return new_obj

    @classmethod
    def put(cls, obj):
        """
        Put an object into Plasma store and wrap it with partition object.

        Parameters
        ----------
        obj : any
            An object to be put.

        Returns
        -------
        PandasOnRayFramePartition
            A new ``PandasOnRayFramePartition`` object.
        """
        return PandasOnRayFramePartition(ray.put(obj), len(obj.index), len(obj.columns))

    @classmethod
    def preprocess_func(cls, func):
        """
        Put a function into the Plasma store to use in ``apply``.

        Parameters
        ----------
        func : callable
            A function to preprocess.

        Returns
        -------
        ray.ObjectRef
            A reference to `func`.
        """
        return ray.put(func)

    def length(self):
        """
        Get the length of the object wrapped by this partition.

        Returns
        -------
        int
            The length of the object.
        """
        if self._length_cache is None:
            if len(self.call_queue):
                self.drain_call_queue()
            else:
                self._length_cache, self._width_cache = get_index_and_columns.remote(
                    self.oid
                )
        if isinstance(self._length_cache, ObjectIDType):
            self._length_cache = ray.get(self._length_cache)
        return self._length_cache

    def width(self):
        """
        Get the width of the object wrapped by the partition.

        Returns
        -------
        int
            The width of the object.
        """
        if self._width_cache is None:
            if len(self.call_queue):
                self.drain_call_queue()
            else:
                self._length_cache, self._width_cache = get_index_and_columns.remote(
                    self.oid
                )
        if isinstance(self._width_cache, ObjectIDType):
            self._width_cache = ray.get(self._width_cache)
        return self._width_cache

    def ip(self):
        """
        Get the node IP address of the object wrapped by this partition.

        Returns
        -------
        str
            IP address of the node that holds the data.
        """
        if self._ip_cache is None:
            if len(self.call_queue):
                self.drain_call_queue()
            else:
                self._ip_cache = self.apply(lambda df: df)._ip_cache
        if isinstance(self._ip_cache, ObjectIDType):
            self._ip_cache = ray.get(self._ip_cache)
        return self._ip_cache

    @classmethod
    def _length_extraction_fn(cls):
        """
        Return the function that computes the length of the object wrapped by this partition.

        Returns
        -------
        callable
            The function that computes the length of the object wrapped by this partition.
        """
        return length_fn_pandas

    @classmethod
    def _width_extraction_fn(cls):
        """
        Return the function that computes the width of the object wrapped by this partition.

        Returns
        -------
        callable
            The function that computes the width of the object wrapped by this partition.
        """
        return width_fn_pandas

    @classmethod
    def empty(cls):
        """
        Create a new partition that wraps an empty pandas DataFrame.

        Returns
        -------
        PandasOnRayFramePartition
            A new ``PandasOnRayFramePartition`` object.
        """
        return cls.put(pandas.DataFrame())


@ray.remote(num_returns=2)
def get_index_and_columns(df):
    """
    Get the number of rows and columns of a pandas DataFrame.

    Parameters
    ----------
    df : pandas.DataFrame
        A pandas DataFrame which dimensions are needed.

    Returns
    -------
    int
        The number of rows.
    int
        The number of columns.
    """
    return len(df.index), len(df.columns)


@ray.remote(num_returns=4)
def deploy_ray_func(call_queue, partition):  # pragma: no cover
    """
    Execute all operations stored in the call queue on the partition in a worker process.

    Parameters
    ----------
    call_queue : list
        A call queue that needs to be executed on the partition.
    partition : pandas.DataFrame
        A pandas DataFrame the call queue needs to be executed on.

    Returns
    -------
    pandas.DataFrame
        The resulting pandas DataFrame.
    int
        The number of rows of the resulting pandas DataFrame.
    int
        The number of columns of the resulting pandas DataFrame.
    str
        The node IP address of the worker process.
    """

    def deserialize(obj):
        if isinstance(obj, ObjectIDType):
            return ray.get(obj)
        return obj

    if len(call_queue) > 1:
        for func, kwargs in call_queue[:-1]:
            func = deserialize(func)
            kwargs = deserialize(kwargs)
            try:
                partition = func(partition, **kwargs)
            except ValueError:
                partition = func(partition.copy(), **kwargs)
    func, kwargs = call_queue[-1]
    func = deserialize(func)
    kwargs = deserialize(kwargs)
    try:
        result = func(partition, **kwargs)
    # Sometimes Arrow forces us to make a copy of an object before we operate on it. We
    # don't want the error to propagate to the user, and we want to avoid copying unless
    # we absolutely have to.
    except ValueError:
        result = func(partition.copy(), **kwargs)
    return (
        result,
        len(result) if hasattr(result, "__len__") else 0,
        len(result.columns) if hasattr(result, "columns") else 0,
        get_node_ip_address(),
    )
