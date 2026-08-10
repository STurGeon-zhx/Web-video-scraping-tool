from __future__ import annotations

import io
import subprocess
import time
from pathlib import Path

import pytest
from imageio_ffmpeg import get_ffmpeg_exe

from douyin_downloader.downloader import DownloadCancelled
from douyin_downloader import media as media_module
from douyin_downloader.media import FFmpegMediaProcessor, MediaInfo, probe_media


FFMPEG = Path(get_ffmpeg_exe())


def run_ffmpeg(*args: str) -> None:
    subprocess.run(
        [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        capture_output=True,
        text=True,
    )


def make_video(path: Path, *, video_codec: str, audio_codec: str = "aac") -> None:
    video_args = ["-c:v", video_codec, "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if video_codec == "libx265":
        video_args.extend(["-x265-params", "log-level=error"])
    audio_args = ["-c:a", audio_codec]
    if audio_codec == "aac":
        audio_args.extend(["-profile:a", "aac_low", "-b:a", "64k"])
    run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=160x90:rate=10",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=1000:sample_rate=44100",
        "-t",
        "1",
        *video_args,
        *audio_args,
        "-shortest",
        str(path),
    )


def video_stream_md5(path: Path) -> str:
    completed = subprocess.run(
        [
            str(FFMPEG),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-c",
            "copy",
            "-f",
            "md5",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_probe_media_identifies_hevc_and_aac_lc(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    make_video(source, video_codec="libx265")

    media = probe_media(FFMPEG, source)

    assert media.video_codec == "hevc"
    assert media.pixel_format == "yuv420p"
    assert media.audio_codec == "aac"
    assert media.audio_profile == "LC"
    assert media.duration == pytest.approx(1.0, abs=0.2)


def test_hevc_is_transcoded_to_windows_compatible_mp4(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"
    make_video(source, video_codec="libx265")
    events: list[dict] = []

    result = FFmpegMediaProcessor(FFMPEG).ensure_compatible(source, output, events.append)

    media = probe_media(FFMPEG, result)
    assert result == output
    assert media.video_codec == "h264"
    assert media.pixel_format == "yuv420p"
    assert media.audio_codec == "aac"
    assert media.audio_profile == "LC"
    assert events[0] == {"status": "transcoding", "progress": 0.0}
    assert events[-1] == {"status": "transcoding", "progress": 100.0}


def test_incompatible_audio_is_converted_without_reencoding_video(tmp_path: Path) -> None:
    source = tmp_path / "source.mkv"
    output = tmp_path / "output.mp4"
    make_video(source, video_codec="libx264", audio_codec="pcm_s16le")
    source_video_md5 = video_stream_md5(source)

    FFmpegMediaProcessor(FFMPEG).ensure_compatible(source, output, lambda _event: None)

    media = probe_media(FFMPEG, output)
    assert media.video_codec == "h264"
    assert media.audio_codec == "aac"
    assert media.audio_profile == "LC"
    assert video_stream_md5(output) == source_video_md5


def test_compatible_video_is_remuxed_without_reencoding_streams(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"
    make_video(source, video_codec="libx264")
    source_video_md5 = video_stream_md5(source)

    FFmpegMediaProcessor(FFMPEG).ensure_compatible(source, output, lambda _event: None)

    assert video_stream_md5(output) == source_video_md5


def test_cancellation_terminates_compatibility_processing_and_removes_output(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"
    make_video(source, video_codec="libx265")

    def cancel(event: dict) -> None:
        if event["status"] == "transcoding":
            raise DownloadCancelled()

    with pytest.raises(DownloadCancelled):
        FFmpegMediaProcessor(FFMPEG).ensure_compatible(source, output, cancel)

    assert not output.exists()


def test_probe_can_be_cancelled_after_ffmpeg_starts(monkeypatch, tmp_path: Path) -> None:
    class HangingProbe:
        def __init__(self) -> None:
            self.stdout = io.StringIO("")
            self.returncode = None
            self.terminated = False

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = -15 if self.terminated else 1
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def kill(self):
            self.terminate()

    process = HangingProbe()
    monkeypatch.setattr(media_module.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(DownloadCancelled):
        probe_media(
            FFMPEG,
            tmp_path / "source.mp4",
            lambda _event: (_ for _ in ()).throw(DownloadCancelled()),
        )

    assert process.terminated is True


def test_conversion_checks_cancellation_even_without_ffmpeg_progress(monkeypatch, tmp_path: Path) -> None:
    class HangingConversion:
        def __init__(self) -> None:
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("encoder stalled")
            self.started = time.monotonic()
            self.returncode = None
            self.terminated = False

        def poll(self):
            if self.returncode is None and time.monotonic() - self.started > 0.8:
                self.returncode = 1
            return self.returncode

        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = -15 if self.terminated else 1
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def kill(self):
            self.terminate()

    compatible = MediaInfo("h264", "yuv420p", "aac", "LC", 10.0)
    monkeypatch.setattr(media_module, "probe_media", lambda *args, **kwargs: compatible)
    process = HangingConversion()
    monkeypatch.setattr(media_module.subprocess, "Popen", lambda *args, **kwargs: process)
    events = 0

    def cancel_on_heartbeat(_event: dict) -> None:
        nonlocal events
        events += 1
        if events == 2:
            raise DownloadCancelled()

    with pytest.raises(DownloadCancelled):
        FFmpegMediaProcessor(FFMPEG).ensure_compatible(
            tmp_path / "source.mp4",
            tmp_path / "output.mp4",
            cancel_on_heartbeat,
        )

    assert process.terminated is True
