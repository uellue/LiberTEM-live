from contextlib import contextmanager
import logging
from typing import Callable, Generator, Iterator, Tuple, Optional
import warnings
import numpy as np
from libertem.common.math import prod
from libertem.common import Shape, Slice
from libertem.common.executor import (
    TaskProtocol, WorkerQueue, TaskCommHandler, WorkerContext,
)
from libertem.io.dataset.base import (
    DataTile, DataSetMeta, BasePartition, Partition, DataSet, TilingScheme,
)
from sparseconverter import ArrayBackend, NUMPY, CUDA

from libertem_live.detectors.base.acquisition import AcquisitionMixin
from .data import MerlinRawFrames, MerlinRawSocket, validate_get_sig_shape

from opentelemetry import trace


tracer = trace.get_tracer(__name__)
logger = logging.getLogger(__name__)


class MerlinAcquisition(AcquisitionMixin, DataSet):
    '''
    Acquisition from a Quantum Detectors Merlin camera

    Parameters
    ----------

    trigger : function()
        See :meth:`~libertem_live.api.LiveContext.prepare_acquisition`
        and :ref:`trigger` for details!
    nav_shape : tuple(int)
    sig_shape : tuple(int)
    host : str
        Hostname of the Merlin data server, default '127.0.0.1'
    port : int
        Data port of the Merlin data server, default 6342
    drain : bool
        Drain the socket before triggering. Disable this when using internal
        start trigger!
    frames_per_partition : int
        Number of frames to process before performing a merge operation. Decreasing this number
        increases the update rate, but can decrease performance.
    '''
    def __init__(
        self,
        trigger: Callable,
        nav_shape: Tuple[int, int],
        sig_shape: Tuple[int, int] = (256, 256),
        host: str = '127.0.0.1',
        port: int = 6342,
        drain: bool = True,
        frames_per_partition: int = 256,
        pool_size: Optional[int] = None,
        timeout: float = 5,
    ):
        # This will also call the DataSet constructor, additional arguments
        # could be passed -- currently not necessary
        super().__init__(trigger=trigger)
        self._socket = MerlinRawSocket(host, port, timeout=timeout)
        self._drain = drain
        self._nav_shape = nav_shape
        self._sig_shape = sig_shape
        self._frames_per_partition = min(frames_per_partition, prod(nav_shape))
        self._timeout = timeout
        if pool_size is not None:
            warnings.warn('`pool_size` parameter is no longer used and ignored', UserWarning)

    def initialize(self, executor):
        # FIXME: possibly need to have an "acquisition plan" object
        # so we know all relevant parameters beforehand
        dtype = np.uint8  # FIXME: don't know the dtype yet
        self._meta = DataSetMeta(
            shape=Shape(self._nav_shape + self._sig_shape, sig_dims=2),
            raw_dtype=dtype,
            dtype=dtype,
        )
        return self

    @property
    def raw_socket(self):
        return self._socket

    @property
    def dtype(self):
        return self._meta.dtype

    @property
    def raw_dtype(self):
        return self._meta.raw_dtype

    @property
    def shape(self):
        return self._meta.shape

    @property
    def meta(self):
        return self._meta

    @contextmanager
    def acquire(self):
        with self.raw_socket:
            if self._drain:
                with tracer.start_as_current_span("drain") as span:
                    drained_bytes = self.raw_socket.drain()
                    span.set_attributes({
                        "libertem_live.drained_bytes": drained_bytes,
                    })
                if drained_bytes > 0:
                    logger.info(f"drained {drained_bytes} bytes of garbage")
            with tracer.start_as_current_span("MerlinAcquisition.trigger"):
                self.trigger()
            self.raw_socket.read_headers(cancel_timeout=self._timeout)

            frame_header = self.raw_socket.get_first_frame_header()
            validate_get_sig_shape(frame_header, self._sig_shape)
            yield

    def check_valid(self):
        pass

    def need_decode(self, read_dtype, roi, corrections):
        return True  # FIXME: we just do this to get a large tile size

    def adjust_tileshape(self, tileshape, roi):
        depth = 24
        return (depth, *self.meta.shape.sig)
        # return Shape((self._end_idx - self._start_idx, 256, 256), sig_dims=2)

    def get_max_io_size(self):
        # return 12*256*256*8
        # FIXME magic numbers?
        return 24*np.prod(self.meta.shape.sig)*8

    def get_base_shape(self, roi):
        return (1, 1, self.meta.shape.sig[-1])

    def get_partitions(self):
        num_frames = np.prod(self._nav_shape, dtype=np.uint64)
        num_partitions = int(num_frames // self._frames_per_partition)

        header = self.raw_socket.get_acquisition_header()

        slices = BasePartition.make_slices(self.shape, num_partitions)
        for part_slice, start, stop in slices:
            yield MerlinLivePartition(
                start_idx=start,
                end_idx=stop,
                meta=self._meta,
                partition_slice=part_slice,
                acq_header=header,
            )

    def get_task_comm_handler(self) -> TaskCommHandler:
        return MerlinCommHandler(
            socket=self.raw_socket,
            tiling_depth=24,  # FIXME!
        )


def get_frames_from_queue(
    queue: WorkerQueue,
    tiling_scheme: TilingScheme,
    sig_shape: Tuple[int, ...],
    dtype
) -> Generator[Tuple[np.ndarray, int], None, None]:
    out = np.zeros((tiling_scheme.depth,) + sig_shape, dtype=dtype)
    out_flat = out.reshape((tiling_scheme.depth, -1,))
    while True:
        with queue.get() as msg:
            header, payload = msg
            header_type = header["type"]
            if header_type == "FRAME":
                raw_frames = MerlinRawFrames(
                    buffer=payload,
                    start_idx=header['start_idx'],
                    end_idx=header['end_idx'],
                    first_frame_header=header['first_frame_header'],
                )
                with tracer.start_as_current_span("MerlinRawFrames.decode") as span:
                    span.set_attributes({
                        "libertem_live.header.start_idx": header["start_idx"],
                        "libertem_live.header.end_idx": header["end_idx"],
                        "libertem_live.buffer.len": len(payload),
                    })
                    raw_frames.decode(
                        out_flat=out_flat,
                    )
                assert raw_frames is not None
                yield out[:raw_frames.num_frames], raw_frames.start_idx
            elif header_type == "END_PARTITION":
                # print(f"partition {partition} done")
                return
            else:
                raise RuntimeError(
                    f"invalid header type {header['type']}; FRAME or END_PARTITION expected"
                )


def _accum_partition(
    frames_iter: Iterator[Tuple[np.ndarray, int]],
    tiling_scheme: TilingScheme,
    sig_shape: Tuple[int, ...],
    dtype
) -> np.ndarray:
    """
    Accumulate frame stacks for a partition
    """
    out = np.zeros((tiling_scheme.depth,) + sig_shape, dtype=dtype)
    offset = 0
    for frame_stack, _ in frames_iter:
        out[offset:offset+frame_stack.shape[0]] = frame_stack
        offset += frame_stack.shape[0]
    assert offset == tiling_scheme.depth
    return out


class MerlinCommHandler(TaskCommHandler):
    def __init__(self, socket: MerlinRawSocket, tiling_depth: int):
        self._socket = socket
        self._tiling_depth = tiling_depth

    def handle_task(self, task: TaskProtocol, queue: WorkerQueue):
        with tracer.start_as_current_span("MerlinCommHandler.handle_task") as span:
            slice_ = task.get_partition().slice
            start_idx = slice_.origin[0]
            end_idx = slice_.origin[0] + slice_.shape[0]
            span.set_attributes({
                "libertem.partition.start_idx": start_idx,
                "libertem.partition.end_idx": end_idx,
            })
            frame_chunk_size = min(32, self._tiling_depth)
            frames_read = 0
            num_frames_in_partition = end_idx - start_idx
            while frames_read < num_frames_in_partition:
                next_read_size = min(
                    num_frames_in_partition - frames_read,
                    frame_chunk_size,
                )
                # FIXME: can't properly reuse this buffer currently...
                input_buffer = self._socket.get_input_buffer(next_read_size)
                res = self._socket.read_multi_frames(
                    input_buffer=input_buffer,
                    num_frames=next_read_size,
                    read_upto_frame=end_idx,
                )
                if res is False:
                    raise RuntimeError("timeout while handling task")
                if res is True:
                    raise RuntimeError("expected more data, didn't get any")
                frames_read += res.num_frames
                queue.put({
                    "type": "FRAME",
                    "start_idx": res.start_idx,
                    "end_idx": res.end_idx,
                    "first_frame_header": res.first_frame_header,
                }, payload=np.frombuffer(res.buffer, dtype=np.uint8))
            # FIXME: END_PARTITION in finally block?
            queue.put({
                "type": "END_PARTITION",
            })

    def start(self):
        pass

    def done(self):
        pass


class MerlinLivePartition(Partition):
    def __init__(
        self, start_idx, end_idx, partition_slice,
        meta, acq_header,
    ):
        super().__init__(meta=meta, partition_slice=partition_slice, io_backend=None, decoder=None)
        self._start_idx = start_idx
        self._end_idx = end_idx
        self._acq_header = acq_header

    def shape_for_roi(self, roi):
        return self.slice.adjust_for_roi(roi).shape

    @property
    def shape(self):
        return self.slice.shape

    @property
    def dtype(self):
        return self.meta.raw_dtype

    def set_corrections(self, corrections):
        self._corrections = corrections

    def set_worker_context(self, worker_context: WorkerContext):
        self._worker_context = worker_context

    def _get_tiles_fullframe(self, tiling_scheme: TilingScheme, dest_dtype="float32", roi=None,
            array_backend: Optional["ArrayBackend"] = None):
        assert array_backend in (None, NUMPY, CUDA)
        # assert len(tiling_scheme) == 1
        tiling_scheme = tiling_scheme.adjust_for_partition(self)
        logger.debug("reading up to frame idx %d for this partition", self._end_idx)

        queue = self._worker_context.get_worker_queue()
        frames = get_frames_from_queue(
            queue,
            tiling_scheme,
            self.shape.sig.to_tuple(),
            dtype=dest_dtype
        )

        # special case: copy from the decode buffer into a larger partition buffer
        # can be further optimized if needed (by directly decoding into said buffer)
        if tiling_scheme.intent == "partition":
            frame_stack = _accum_partition(
                frames,
                tiling_scheme,
                self.shape.sig.to_tuple(),
                dtype=dest_dtype
            )
            tile_shape = Shape(
                frame_stack.shape,
                sig_dims=2
            )
            tile_slice = Slice(
                origin=(self._start_idx,) + (0, 0),
                shape=tile_shape,
            )
            yield DataTile(
                frame_stack,
                tile_slice=tile_slice,
                scheme_idx=0,
            )
            return

        for frame_stack, start_idx in frames:
            tile_shape = Shape(
                frame_stack.shape,
                sig_dims=2
            )
            tile_slice = Slice(
                origin=(start_idx,) + (0, 0),
                shape=tile_shape,
            )
            yield DataTile(
                frame_stack,
                tile_slice=tile_slice,
                scheme_idx=0,
            )

    def get_tiles(self, tiling_scheme, dest_dtype="float32", roi=None,
            array_backend: Optional["ArrayBackend"] = None):
        yield from self._get_tiles_fullframe(
            tiling_scheme, dest_dtype, roi, array_backend=array_backend
        )

    def __repr__(self):
        return f"<MerlinLivePartition {self._start_idx}:{self._end_idx}>"
