from __future__ import annotations

import queue
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


ProgressCallback = Callable[[dict[str, Any]], None]


class MediaProcessingError(RuntimeError):
    pass


class MediaProcessingInterrupted(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MediaInfo:
    video_codec: str
    pixel_format: str
    audio_codec: str | None
    audio_profile: str | None
    duration: float


def probe_media(
    ffmpeg: Path,
    source: Path,
    on_progress: ProgressCallback | None = None,
    progress: float = 0.0,
) -> MediaInfo:
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            [str(ffmpeg), "-hide_banner", "-i", str(source)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert process.stdout is not None
        lines: queue.Queue[str | None] = queue.Queue()
        reader = _start_reader(process.stdout, lines)
        output: deque[str] = deque(maxlen=4000)
        started = time.monotonic()
        next_heartbeat = started
        stream_finished = False
        while not stream_finished or process.poll() is None:
            now = time.monotonic()
            if now - started >= 30:
                raise MediaProcessingError("FFmpeg 媒体探测超时")
            if on_progress is not None and now >= next_heartbeat:
                on_progress({"status": "transcoding", "progress": progress})
                next_heartbeat = now + 0.25
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                stream_finished = True
            else:
                output.append(line)
        process.wait()
        reader.join(timeout=1)
        details = "\n".join(output)
    except MediaProcessingInterrupted:
        if process is not None:
            _terminate(process)
        raise
    except MediaProcessingError:
        if process is not None:
            _terminate(process)
        raise
    except Exception as exc:
        if process is not None:
            _terminate(process)
        raise MediaProcessingError(str(exc)) from exc
    except BaseException:
        if process is not None:
            _terminate(process)
        raise

    video_line = next((line for line in details.splitlines() if "Video:" in line), None)
    if video_line is None:
        raise MediaProcessingError("FFmpeg 未识别到视频流")

    video_description = video_line.split("Video:", 1)[1].strip()
    video_parts = [part.strip() for part in video_description.split(",")]
    video_codec = video_parts[0].split()[0].lower()
    pixel_format = video_parts[1].split("(", 1)[0].strip().lower() if len(video_parts) > 1 else ""

    audio_line = next((line for line in details.splitlines() if "Audio:" in line), None)
    audio_codec: str | None = None
    audio_profile: str | None = None
    if audio_line is not None:
        audio_description = audio_line.split("Audio:", 1)[1].strip()
        audio_header = audio_description.split(",", 1)[0]
        audio_codec = audio_header.split()[0].lower()
        profile_match = re.search(r"\(([^)/]+)\)", audio_header)
        if profile_match:
            audio_profile = profile_match.group(1).strip()

    duration = 0.0
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", details)
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    return MediaInfo(
        video_codec=video_codec,
        pixel_format=pixel_format,
        audio_codec=audio_codec,
        audio_profile=audio_profile,
        duration=duration,
    )


def _start_reader(stream, lines: queue.Queue[str | None]) -> threading.Thread:
    def read_output() -> None:
        try:
            for line in stream:
                lines.put(line.strip())
        finally:
            lines.put(None)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    return reader


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
    except OSError:
        return


def _remove_partial(output: Path) -> None:
    try:
        output.unlink(missing_ok=True)
    except OSError:
        pass


class FFmpegMediaProcessor:
    def __init__(self, ffmpeg: Path) -> None:
        self.ffmpeg = Path(ffmpeg)

    def ensure_compatible(
        self,
        source: Path,
        output: Path,
        on_progress: ProgressCallback,
    ) -> Path:
        source_info = probe_media(self.ffmpeg, source, on_progress, 0.0)
        output.parent.mkdir(parents=True, exist_ok=True)
        _remove_partial(output)
        command = self._build_command(source, output, source_info)

        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            on_progress({"status": "transcoding", "progress": 0.0})
            assert process.stdout is not None
            lines: queue.Queue[str | None] = queue.Queue()
            reader = _start_reader(process.stdout, lines)
            error_tail: deque[str] = deque(maxlen=200)
            progress_finished = False
            last_progress = 0.0
            next_heartbeat = time.monotonic() + 0.5
            while not progress_finished or process.poll() is None:
                try:
                    line = lines.get(timeout=0.1)
                except queue.Empty:
                    now = time.monotonic()
                    if now >= next_heartbeat:
                        on_progress(
                            {"status": "transcoding", "progress": last_progress}
                        )
                        next_heartbeat = now + 0.5
                    continue
                if line is None:
                    progress_finished = True
                    continue
                if line.startswith("out_time_us=") and source_info.duration > 0:
                    elapsed_us = int(line.split("=", 1)[1] or 0)
                    progress = min(99.0, elapsed_us / 1_000_000 / source_info.duration * 100)
                    last_progress = round(progress, 2)
                    on_progress({"status": "transcoding", "progress": last_progress})
                else:
                    error_tail.append(line)

            return_code = process.wait()
            reader.join(timeout=1)
            if return_code != 0:
                error_output = "\n".join(error_tail).strip()
                raise MediaProcessingError(error_output or f"FFmpeg 退出码 {return_code}")

            output_info = probe_media(self.ffmpeg, output, on_progress, 99.0)
            if not self._is_compatible(output_info):
                raise MediaProcessingError("转换后的文件仍不是 H.264/yuv420p/AAC-LC MP4")
            on_progress({"status": "transcoding", "progress": 100.0})
            return output
        except MediaProcessingInterrupted:
            if process is not None and process.poll() is None:
                _terminate(process)
            _remove_partial(output)
            raise
        except MediaProcessingError:
            if process is not None and process.poll() is None:
                _terminate(process)
            _remove_partial(output)
            raise
        except Exception as exc:
            if process is not None and process.poll() is None:
                _terminate(process)
            _remove_partial(output)
            raise MediaProcessingError(str(exc)) from exc
        except BaseException:
            if process is not None and process.poll() is None:
                _terminate(process)
            _remove_partial(output)
            raise

    def _build_command(self, source: Path, output: Path, media: MediaInfo) -> list[str]:
        video_compatible = self._video_is_compatible(media)
        audio_compatible = self._audio_is_compatible(media)
        command = [
            str(self.ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
        ]
        if video_compatible:
            command.extend(["-c:v", "copy"])
        else:
            command.extend(
                [
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "20",
                    "-pix_fmt",
                    "yuv420p",
                ]
            )
        if audio_compatible:
            command.extend(["-c:a", "copy"])
        else:
            command.extend(
                [
                    "-c:a",
                    "aac",
                    "-profile:a",
                    "aac_low",
                    "-b:a",
                    "160k",
                ]
            )
        command.extend(
            [
                "-movflags",
                "+faststart",
                "-progress",
                "pipe:1",
                "-nostats",
                "-f",
                "mp4",
                str(output),
            ]
        )
        return command

    @staticmethod
    def _video_is_compatible(media: MediaInfo) -> bool:
        return media.video_codec in {"h264", "avc1"} and media.pixel_format == "yuv420p"

    @staticmethod
    def _audio_is_compatible(media: MediaInfo) -> bool:
        return media.audio_codec is None or (
            media.audio_codec == "aac" and media.audio_profile == "LC"
        )

    @classmethod
    def _is_compatible(cls, media: MediaInfo) -> bool:
        return cls._video_is_compatible(media) and cls._audio_is_compatible(media)
