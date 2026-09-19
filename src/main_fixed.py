import json
import os
import subprocess

import requests

import main as factory

EDGE_MAKE_TTS = factory.make_tts
TYPECAST_URL = "https://api.typecast.ai/v1/text-to-speech"
DEFAULT_TYPECAST_VOICE = "tc_672c5f5ce59fac2a48faeaee"


async def make_tts_typecast(text, output):
    api_key = os.getenv("TYPECAST_API_KEY", "").strip()
    voice_id = os.getenv("TYPECAST_VOICE_ID", DEFAULT_TYPECAST_VOICE).strip() or DEFAULT_TYPECAST_VOICE

    # Keep the factory alive if Typecast is unavailable or free credits are exhausted.
    if not api_key:
        print("TYPECAST_API_KEY missing; falling back to Edge TTS")
        await EDGE_MAKE_TTS(text, output)
        return

    payload = {
        "voice_id": voice_id,
        "text": text,
        "model": "ssfm-v30",
        "language": "kor",
        "prompt": {
            "emotion_type": "smart"
        },
        "output": {
            "target_lufs": -14.0,
            "audio_tempo": 1.05,
            "audio_format": "mp3"
        }
    }

    try:
        response = requests.post(
            TYPECAST_URL,
            headers={
                "X-API-KEY": api_key,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        )
        response.raise_for_status()
        if len(response.content) < 1024:
            raise RuntimeError("Typecast returned an unexpectedly small audio file")
        output.write_bytes(response.content)
        print(f"Typecast TTS generated with voice {voice_id}")
    except Exception as exc:
        print(f"Typecast TTS failed ({exc}); falling back to Edge TTS")
        await EDGE_MAKE_TTS(text, output)


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

    # Pexels source audio is never mapped. Input 1 is always the narration track.
    # BGM is generated locally by FFmpeg, keeping it original and royalty-free.
    filter_complex = (
        f"[0:v:0]{video_chain}[vout];"
        "[1:a:0]aresample=48000,volume=1.35,loudnorm=I=-16:LRA=7:TP=-1.5[voice];"
        f"sine=frequency=174.61:sample_rate=48000:duration={duration:.3f},volume=0.028[bg1];"
        f"sine=frequency=220:sample_rate=48000:duration={duration:.3f},volume=0.020[bg2];"
        f"sine=frequency=261.63:sample_rate=48000:duration={duration:.3f},volume=0.014[bg3];"
        "[bg1][bg2][bg3]amix=inputs=3:duration=longest:normalize=0,"
        "lowpass=f=1200,tremolo=f=0.18:d=0.25,volume=0.75[bgm];"
        "[voice][bgm]amix=inputs=2:duration=first:dropout_transition=0,"
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


factory.make_tts = make_tts_typecast
factory.render = render_fixed
factory.main()
