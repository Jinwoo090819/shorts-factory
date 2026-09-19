import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output"
SITE = ROOT / "site"
VIDEO = OUT / "short.mp4"
VOICE = OUT / "narration.mp3"
FIXED = OUT / "short_with_audio.mp4"


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def probe_audio_stream(path: Path) -> str:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,channels,sample_rate",
            "-of", "default=noprint_wrappers=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def main():
    if not VIDEO.exists():
        raise RuntimeError(f"Missing rendered video: {VIDEO}")
    if not VOICE.exists():
        raise RuntimeError(f"Missing TTS narration: {VOICE}")

    duration = probe_duration(VOICE)
    if duration <= 0:
        raise RuntimeError("TTS narration has zero duration")

    # Keep the baked video/subtitles, discard any Pexels source audio, and build
    # a deterministic mix from TTS + locally generated ambient background music.
    cmd = [
        "ffmpeg", "-y",
        "-i", str(VIDEO),
        "-i", str(VOICE),
        "-f", "lavfi", "-i", f"sine=frequency=130.81:sample_rate=48000:duration={duration:.3f}",
        "-f", "lavfi", "-i", f"sine=frequency=196.00:sample_rate=48000:duration={duration:.3f}",
        "-filter_complex",
        (
            "[1:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            "volume=1.9,highpass=f=70,alimiter=limit=0.95[voice];"
            "[2:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            "volume=0.020,lowpass=f=700,tremolo=f=0.12:d=0.35[bg1];"
            "[3:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            "volume=0.014,lowpass=f=900,tremolo=f=0.09:d=0.30[bg2];"
            "[bg1][bg2]amix=inputs=2:normalize=0[bg];"
            "[voice][bg]amix=inputs=2:duration=first:normalize=0,"
            "alimiter=limit=0.95[aout]"
        ),
        "-map", "0:v:0",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-ac", "2",
        "-t", f"{duration:.3f}",
        "-movflags", "+faststart",
        str(FIXED),
    ]
    subprocess.run(cmd, check=True)

    audio_info = probe_audio_stream(FIXED)
    if not audio_info:
        raise RuntimeError("Final video has no audio stream after mixing")

    # Reject a suspiciously tiny output rather than silently publishing a broken file.
    if FIXED.stat().st_size < 500_000:
        raise RuntimeError("Final mixed video is unexpectedly small")

    FIXED.replace(VIDEO)
    SITE.mkdir(parents=True, exist_ok=True)
    shutil.copy2(VIDEO, SITE / "short.mp4")

    print("Audio mix complete")
    print(f"Narration duration: {duration:.2f}s")
    print(audio_info)


if __name__ == "__main__":
    main()
