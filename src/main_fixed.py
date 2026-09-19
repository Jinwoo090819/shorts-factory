import json
import re
import subprocess

import main as factory


ORIGINAL_SELECT_READY_SCRIPT = factory.select_ready_script
CURRENT_TARGET_PUBLISH_TIME = "19:00"


def select_ready_script_with_slot():
    global CURRENT_TARGET_PUBLISH_TIME
    result = ORIGINAL_SELECT_READY_SCRIPT()
    content = result[2]
    target = str(content.get("target_publish_time") or "19:00").strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", target):
        raise RuntimeError(f"Invalid target_publish_time: {target}")
    CURRENT_TARGET_PUBLISH_TIME = target
    return result


def next_publish_time_for_slot():
    now = factory.datetime.now(factory.KST)
    hour, minute = [int(x) for x in CURRENT_TARGET_PUBLISH_TIME.split(":", 1)]
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # If rendering/Buffer work runs close to or after the slot, publish shortly
    # instead of postponing the video to the next day.
    if now >= target - factory.timedelta(minutes=10):
        target = now + factory.timedelta(minutes=5)
    return target


def _has_audio(path):
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,channels",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(probe.stdout or "{}")
    return bool(data.get("streams"))


def render_fixed(video, audio, srt, output):
    duration = factory.ffprobe_duration(audio)
    subtitle_path = str(srt).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")

    video_chain = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        "eq=brightness=-0.08:saturation=0.9,"
        f"subtitles='{subtitle_path}':force_style='FontName=Noto Sans CJK KR,FontSize=18,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=1,"
        "Alignment=2,MarginV=210'"
    )

    # Input 0 = Pexels video, input 1 = Edge TTS narration.
    # Never map Pexels source audio. Build a fresh stereo mix from TTS + local BGM.
    filter_complex = (
        f"[0:v:0]{video_chain}[vout];"
        "[1:a:0]aresample=48000,aformat=channel_layouts=stereo,"
        "volume=1.7,highpass=f=70,loudnorm=I=-15:LRA=7:TP=-1.5[voice];"
        f"sine=frequency=174.61:sample_rate=48000:duration={duration:.3f},volume=0.022[bg1];"
        f"sine=frequency=220:sample_rate=48000:duration={duration:.3f},volume=0.016[bg2];"
        f"sine=frequency=261.63:sample_rate=48000:duration={duration:.3f},volume=0.010[bg3];"
        "[bg1][bg2][bg3]amix=inputs=3:duration=longest:normalize=0,"
        "lowpass=f=1100,tremolo=f=0.16:d=0.22,volume=0.80[bgm];"
        "[voice][bgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
        "alimiter=limit=0.95[aout]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", str(video),
        "-i", str(audio),
        "-t", f"{duration:.3f}",
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-map", "[aout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "21",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        "-shortest",
        str(output),
    ]
    subprocess.run(cmd, check=True)

    if not _has_audio(output):
        raise RuntimeError("Rendered MP4 has no audio stream")


factory.select_ready_script = select_ready_script_with_slot
factory.next_publish_time = next_publish_time_for_slot
factory.render = render_fixed
factory.main()
