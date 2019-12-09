"""
(*)~---------------------------------------------------------------------------
Pupil - eye tracking platform
Copyright (C) 2012-2019 Pupil Labs

Distributed under the terms of the GNU
Lesser General Public License (LGPL v3.0).
See COPYING and COPYING.LESSER for license details.
---------------------------------------------------------------------------~(*)
"""

import abc
import math
import logging
import multiprocessing as mp
import os
import typing as T
from fractions import Fraction

import av
import numpy as np
from av.packet import Packet

import audio_utils
from video_capture.utils import Video

logger = logging.getLogger(__name__)


"""
notes on time_bases and presentation timestamps:

Time_base (fraction) is the conversion factor to multipy the uint64 pts value to seconds

two time bases that we care about exsist:
    time_base of the stream (AVStream) this is used for the packet pts/dts
    time_base fo the codec (AVCodecContext) this is used for the frame

going from packet pts to frame pts when decoding:
frame.pts  = av_rescale_q ( packet. pts , packetTimeBase , frameTimeBase )

..when encoding:
packet.pts  = av_rescale_q ( frame. pts , frameTimeBase,packetTimeBase  )


Setting the time_base:
The timebase of the codec is settable (and only settable at the beginnig):
currently in PyAV this is done container.add_stream(codec,codec_timebase)

The timebase of the stream is not user settable. It is determined by ffmpeg.
The streamtimebase uses the codec timebase as a hint to find a good value.
The stream timebase in influenced by the contraints/rules of the container as well.
Only when the header  of the stream is written stream.time_base is garanteed
to be valid and should only now be accesed.
"""


def write_timestamps(file_loc, timestamps, output_format="npy"):
    """
    Attritbutes:
        output_format (str): Output file format. Available values:
        - "npy": numpy array format (default)
        - "csv": csv file export
        - "all": Exports in all of the above formats
    """
    directory, video_file = os.path.split(file_loc)
    name, ext = os.path.splitext(video_file)
    ts_file = "{}_timestamps".format(name)
    ts_loc = os.path.join(directory, ts_file)
    ts = np.array(timestamps)
    if output_format not in ("npy", "csv", "all"):
        raise ValueError("Unknown timestamp output format `{}`".format(output_format))
    if output_format in ("npy", "all"):
        np.save(ts_loc + ".npy", ts)
    if output_format in ("csv", "all"):
        output_video = Video(file_loc)
        if not output_video.is_valid:
            logger.error(f"Failed to extract PTS frome exported video {file_loc}")
            return
        pts = output_video.pts
        ts_pts = np.vstack((ts, pts)).T
        np.savetxt(
            ts_loc + ".csv",
            ts_pts,
            fmt=["%f", "%i"],
            delimiter=",",
            header="timestamps [seconds],pts",
        )


class AV_Writer(abc.ABC):
    def __init__(self, output_file_path: str, start_time_synced: int):
        """
        A generic writer for frames to a file using pyAV.

        output_file_path: File to write frames to.
        start_time_synced: Start time of the recording.
            Will be used to calculate positions of frames (pts).
        """

        self.timestamps = []
        self.start_time = start_time_synced
        self.last_video_pts = float("-inf")

        # always write with highest resolution for mp4
        # NOTE: libav might lower the resolution on saving, if possible
        self.time_base = Fraction(1, 65535)

        self.output_file_path = output_file_path
        directory, video_file = os.path.split(output_file_path)
        name, ext = os.path.splitext(video_file)

        if ext not in self.supported_extensions:
            logger.warning(
                f"Opening media file writer for {ext}. "
                f"Only {self.supported_extensions} are supported! "
                "Using a different container is risky!"
            )

        self.container = av.open(output_file_path, "w")
        logger.debug("Opened '{}' for writing.".format(output_file_path))

        self.configured = False
        self.video_stream = self.container.add_stream(
            codec_name=self.codec, rate=1 / self.time_base
        )

        # TODO: Where does this bit-rate come from? Seems like an unreasonable
        # value. Also 10e3 == 1e4, which makes this even weirder!
        BIT_RATE = 15000 * 10e3
        self.video_stream.bit_rate = BIT_RATE
        self.video_stream.bit_rate_tolerance = BIT_RATE / 20
        self.video_stream.thread_count = max(1, mp.cpu_count() - 1)

        self.closed = False

    def write_video_frame(self, input_frame):
        """
        Write a frame to the video_stream.

        For subclasses, implement self.encode_frame().
        """
        if self.closed:
            logger.warning("Container was closed already!")
            return

        if not self.configured:
            self.video_stream.height = input_frame.height
            self.video_stream.width = input_frame.width
            self.configured = True
            self.on_first_frame(input_frame)

        ts = input_frame.timestamp

        if ts < self.start_time:
            # This can happen, because we might have a frame already in the
            # pipeline when starting the recording. We should skip this frame
            # then, as the processes are not yet synced.
            logger.debug("Skipping frame that arrived before sync time.")
            return

        if self.timestamps:
            last_ts = self.timestamps[-1]
            if ts < last_ts:
                self.release()
                raise ValueError(
                    "Non-monotonic timestamps!"
                    f"Last timestamp: {last_ts}. Given timestamp: {ts}"
                )

        pts = int((input_frame.timestamp - self.start_time) / self.time_base)

        # ensure strong monotonic pts
        pts = max(pts, self.last_video_pts + 1)

        # TODO: Use custom Frame wrapper class, that wraps backend-specific frames.
        # This way we could just attach the pts here to the frame.
        # Currently this will fail e.g. for av.VideoFrame.
        video_packed_encoded = False
        for packet in self.encode_frame(input_frame, pts):
            if packet.stream is self.video_stream:
                if video_packed_encoded:
                    # NOTE: Assumption: Each frame is encoded into a single packet!
                    # This is required for the frame.pts == packet.pts assumption below.
                    logger.warning("Single frame yielded more than one packet")
                video_packed_encoded = True
            self.container.mux(packet)

        if not video_packed_encoded:
            logger.warning(f"Encoding frame {input_frame.index} failed!")
            return

        self.last_video_pts = pts
        self.timestamps.append(ts)

    def close(self, timestamp_export_format="npy"):
        """Close writer, triggering stream and timestamp save."""

        if self.closed:
            logger.warning("Trying to close container multiple times!")
            return

        if self.configured:
            # at least one frame has been written, flush stream
            for packet in self.video_stream.encode(None):
                self.container.mux(packet)

        self.container.close()
        self.closed = True

        if self.configured and timestamp_export_format is not None:
            # Requires self.container to be closed since we extract pts
            # from the exported video file.
            write_timestamps(
                self.output_file_path, self.timestamps, timestamp_export_format
            )

    def release(self):
        """Close writer, triggering stream and timestamp save."""
        self.close()

    def on_first_frame(self, input_frame) -> None:
        """
        Will be called once for the first frame.

        Overwrite to do additional setup.
        """
        pass

    @abc.abstractmethod
    def encode_frame(self, input_frame, pts: int) -> T.Iterator[Packet]:
        """Encode a frame into one or multiple av packets with given pts."""

    @property
    @abc.abstractmethod
    def supported_extensions(self) -> T.Tuple[str]:
        """Supported file extensions (starting with '.')."""

    @property
    @abc.abstractmethod
    def codec(self) -> str:
        """Desired video stream codec."""


class MPEG_Writer(AV_Writer):
    """AV_Writer with MPEG4 encoding."""

    @property
    def supported_extensions(self):
        return (".mp4", ".mov", ".mkv")

    @property
    def codec(self):
        return "mpeg4"

    def on_first_frame(self, input_frame) -> None:
        # setup av frame once to use as buffer throughout the process
        if input_frame.yuv_buffer is not None:
            pix_format = "yuv422p"
        else:
            pix_format = "bgr24"
        self.frame = av.VideoFrame(input_frame.width, input_frame.height, pix_format)
        self.frame.time_base = self.time_base

    def encode_frame(self, input_frame, pts: int) -> T.Iterator[Packet]:
        if input_frame.yuv_buffer is not None:
            y, u, v = input_frame.yuv422
            self.frame.planes[0].update(y)
            self.frame.planes[1].update(u)
            self.frame.planes[2].update(v)
        else:
            self.frame.planes[0].update(input_frame.img)

        self.frame.pts = pts

        yield from self.video_stream.encode(self.frame)


class JPEG_Writer(AV_Writer):
    """AV_Writer with MJPEG encoding."""

    @property
    def supported_extensions(self):
        return (".mp4",)

    @property
    def codec(self):
        return "mjpeg"

    def on_first_frame(self, input_frame) -> None:
        self.video_stream.pix_fmt = "yuvj422p"

    def encode_frame(self, input_frame, pts: int) -> T.Iterator[Packet]:
        # for JPEG we only get a single packet per frame
        packet = Packet()
        packet.stream = self.video_stream
        packet.payload = input_frame.jpeg_buffer
        packet.time_base = self.time_base
        packet.pts = pts
        # TODO: check if we still need dts here, as they were removed from MPEG_Writer
        packet.dts = pts
        yield packet


class MPEG_Audio_Writer(MPEG_Writer):
    """Extension of MPEG_Writer with audio support."""

    @staticmethod
    def _add_stream(container, template):
        stream = container.add_stream(
            codec_name=template.codec.name,
            rate=template.rate,
        )
        stream.layout = template.layout
        return stream

    def __init__(self, *args, audio_dir: str, **kwargs):
        super().__init__(*args, **kwargs)

        try:
            self.audio_parts = audio_utils.load_audio(audio_dir)
            self.audio_export_stream = type(self)._add_stream(
                container=self.container,
                template=self.audio_parts[0].stream,
            )
        except audio_utils.NoAudioLoadedError:
            logger.debug("Could not mux audio. File not found.")
            self.audio_parts = None
            return

        # setup stateful packet iterator
        self.audio_packet_iterator = self.iterate_audio_packets()

    def encode_frame(self, input_frame, pts: int) -> T.Iterator[Packet]:
        # encode video packets from AV_Writer base first
        yield from super().encode_frame(input_frame, pts)

        if self.audio_parts is None:
            return

        # encode all audio packets up to current frame timestamp
        frame_ts = self.frame.pts * self.time_base
        for audio_packet in self.audio_packet_iterator:
            audio_ts = audio_packet.pts * self.audio_export_stream.time_base

            yield audio_packet

            if audio_ts > frame_ts:
                # done for this frame, pause iteration
                return

    def iterate_audio_packets(self):
        """Yields all audio packets from start_time to end."""
        yield from _AudioPacketIterator(
            start_time=self.start_time,
            audio_export_stream=self.audio_export_stream,
            audio_parts=self.audio_parts,
            fill_gaps=True
        ).iterate_audio_packets()


class _AudioPacketIterator:

    audio_part_end_marker = Ellipsis

    def __init__(self, start_time, audio_parts, audio_export_stream, fill_gaps=True):
        self.start_time = start_time
        self.audio_parts = audio_parts
        self.audio_export_stream = audio_export_stream
        self.fill_gaps = fill_gaps

    def iterate_audio_packets(self):

        last_audio_pts = float("-inf")

        if self.fill_gaps:
            packet_with_ts_iterator = self._iterate_audio_packets_filling_gaps(self.audio_parts)
        else:
            packet_with_ts_iterator = self._iterate_audio_packets(self.audio_parts)

        for packet, raw_ts in packet_with_ts_iterator:
            audio_ts = raw_ts - self.start_time
            audio_pts = int(audio_ts / self.audio_export_stream.time_base)

            # ensure strong monotonic pts
            audio_pts = max(audio_pts, last_audio_pts + 1)
            last_audio_pts = audio_pts

            packet.pts = audio_pts
            packet.dts = audio_pts
            packet.stream = self.audio_export_stream

            if audio_ts < 0:
                logger.debug(f"Seeking audio: {audio_ts} -> {self.start_time}")
                # discard all packets before start time
                return None

            yield packet

    def _iterate_audio_packets(self, audio_parts):
        for packet, raw_ts in self._iterate_audio_packets_with_part_marker(audio_parts):
            if packet is not self.audio_part_end_marker:
                yield packet, raw_ts

    def _iterate_audio_packets_filling_gaps(self, audio_parts):

        history = [(None, None)]

        for curr_packet, curr_ts in self._iterate_audio_packets_with_part_marker(audio_parts):

            prev_packet, prev_ts = history.pop()

            if prev_packet is self.audio_part_end_marker:
                prev_packet, prev_ts = history.pop()

                start_ts = prev_ts + prev_packet.duration
                duration = curr_ts - start_ts
                silent_packets = self._generate_silence_packets(
                    stream=self.audio_export_stream,
                    start_ts=start_ts,
                    duration=duration,
                )
                yield from silent_packets
            else:
                history.append((prev_packet, prev_ts))

            if curr_packet is not self.audio_part_end_marker:
                yield curr_packet, curr_ts

            history.append((curr_packet, curr_ts))

    def _iterate_audio_packets_with_part_marker(self, audio_parts):
        if audio_parts is None:
            return

        for part_idx, audio_part in enumerate(audio_parts):
            frames = audio_part.container.decode(audio=0)
            for frame, timestamp in zip(frames, audio_part.timestamps):
                frame.pts = None
                for packet in self.audio_export_stream.encode(frame):
                    if packet:
                        yield packet, timestamp
            yield self.audio_part_end_marker, 0

    @staticmethod
    def _generate_silence_packets(stream, start_ts, duration):
        sample_rate = stream.codec_context.sample_rate
        frame_size = stream.codec_context.frame_size
        av_format = stream.codec_context.format.name
        av_layout = stream.codec_context.layout.name
        dtype = np.dtype(_AudioPacketIterator._format_dtypes[av_format])

        sample_count = sample_rate * duration

        sample_count = int(sample_count)

        frame_count = math.ceil(sample_count / frame_size)

        frame_timestamp = start_ts

        for frame_idx in range(frame_count):
            remaining_count = sample_count - (frame_idx * frame_size)
            frame_samples = min(remaining_count, frame_size)
            audio_frame = av.AudioFrame(samples=frame_samples, format=av_format, layout=av_layout)
            audio_frame.pts = None
            audio_frame.sample_rate = sample_rate

            for plane in audio_frame.planes:
                buffer = np.frombuffer(plane, dtype=dtype)
                buffer[:] = 0
            for packet in stream.encode(audio_frame):
                if packet:
                    yield packet, frame_timestamp
            frame_timestamp += 1.0 / sample_rate

    # https://github.com/mikeboers/PyAV/blob/develop/av/audio/frame.pyx
    _format_dtypes = {
        'dbl': '<f8',
        'dblp': '<f8',
        'flt': '<f4',
        'fltp': '<f4',
        's16': '<i2',
        's16p': '<i2',
        's32': '<i4',
        's32p': '<i4',
        'u8': 'u1',
        'u8p': 'u1',
    }
