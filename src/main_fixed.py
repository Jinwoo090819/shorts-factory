import asyncio
import json
import re
import subprocess

import main as factory


ORIGINAL_SELECT_READY_SCRIPT = factory.select_ready_script
CURRENT_TARGET_PUBLISH_TIME = "19:00"
MIN_NARRATION_SECONDS = 50.0
MAX_NARRATION_SECONDS = 85.0
SUBTITLE_MAX_LINE_WIDTH = 15.0


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


def _visual_width(text):
    """Approximate on-screen width. Korean/CJK glyphs count as 1, ASCII as narrower."""
    width = 0.0
    for ch in text:
        if ch.isspace():
            width += 0.45
        elif ord(ch) < 128:
            width += 0.58
        else:
            width += 1.0
    return width


def _wrap_sentence_two_lines(sentence):
    """Return caption blocks with at most 2 lines and never split a word."""
    words = sentence.split()
    if not words:
        return []

    blocks = []
    lines = []
    current = ""

    for word in words:
        candidate = word if not current else f"{current} {word}"
        if not current or _visual_width(candidate) <= SUBTITLE_MAX_LINE_WIDTH:
            current = candidate
            continue

        lines.append(current)
        current = word

        if len(lines) == 2:
            blocks.append("\n".join(lines))
            lines = []

    if current:
        lines.append(current)
    if lines:
        blocks.append("\n".join(lines[:2]))

    return blocks


def split_caption_fixed(text):
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []

    # Keep sentence punctuation attached so captions end at natural semantic boundaries.
    sentences = [
        x.strip()
        for x in re.split(r"(?<=[.!?。！？])\s+", text)
        if x.strip()
    ]

    blocks = []
    for sentence in sentences:
        blocks.extend(_wrap_sentence_two_lines(sentence))

    return blocks or [text]


def make_srt_fixed(text, duration, output):
    chunks = split_caption_fixed(text)
    if not chunks:
        raise RuntimeError("No subtitle chunks generated")

    # Weight timing by spoken-character count while ignoring line breaks/spaces.
    weights = [max(len(re.sub(r"\s", "", chunk)), 6) for chunk in chunks]
    total = sum(weights)
    cursor = 0.0
    lines = []

    for i, (chunk, weight) in enumerate(zip(chunks, weights), start=1):
        span = duration * weight / total
        end = duration if i == len(chunks) else min(duration, cursor + span)
        lines.extend([
            str(i),
            f"{factory.srt_time(cursor)} --> {factory.srt_time(end)}",
            chunk,
            "",
        ])
        cursor = end

    output.write_text("\n".join(lines), encoding="utf-8")


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
        f"subtitles='{subtitle_path}':force_style='FontName=Noto Sans CJK KR,FontSize=17,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=1,"
        "Alignment=2,MarginL=120,MarginR=120,MarginV=220'"
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


def build_one_long():
    script_path, script_index, content, _raw = factory.select_ready_script()
    script = str(content["script"]).strip()
    title = str(content.get("title") or "오늘의 잡지식").strip()
    fact = str(content.get("fact") or "").strip()
    source_1 = str(content.get("source_1") or "").strip()
    source_2 = str(content.get("source_2") or "").strip()
    queries = [
        str(content.get("pexels_query_1") or "").strip(),
        str(content.get("pexels_query_2") or "").strip(),
        str(content.get("pexels_query_3") or "").strip(),
    ]
    if not any(queries):
        raise RuntimeError("Selected script has no Pexels search queries")

    audio = factory.OUT / "narration.mp3"
    broll = factory.OUT / "broll.mp4"
    subtitles = factory.OUT / "captions.srt"
    final = factory.OUT / "short.mp4"

    asyncio.run(factory.make_tts(script, audio))
    duration = factory.ffprobe_duration(audio)
    if duration < MIN_NARRATION_SECONDS:
        raise RuntimeError(
            f"Narration is too short ({duration:.1f}s). Minimum is {MIN_NARRATION_SECONDS:.0f}s."
        )
    if duration > MAX_NARRATION_SECONDS:
        raise RuntimeError(
            f"Narration is too long ({duration:.1f}s). Maximum is {MAX_NARRATION_SECONDS:.0f}s."
        )

    factory.make_srt(script, duration, subtitles)
    media_credit = factory.pexels_video(queries, broll)
    factory.render(broll, audio, subtitles, final)

    publish_at = factory.next_publish_time()
    source_lines = [x for x in [source_1, source_2] if x]
    credit = "영상 소스: Pexels"
    if media_credit.get("creator"):
        credit += f" / {media_credit['creator']}"
    if media_credit.get("video_url"):
        credit += f"\n{media_credit['video_url']}"
    else:
        credit += "\nhttps://www.pexels.com"

    description = (
        f"{fact}\n\n"
        + ("출처:\n" + "\n".join(source_lines) + "\n\n" if source_lines else "")
        + credit
        + "\n\n#잡지식 #상식 #shorts"
    )

    (factory.SITE / "short.mp4").write_bytes(final.read_bytes())
    (factory.SITE / "index.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>shorts-factory media</title><p>Media staging endpoint.</p>",
        encoding="utf-8",
    )

    metadata = {
        "script_file": str(script_path.relative_to(factory.ROOT)),
        "script_index": script_index,
        "fact_key": content.get("fact_key"),
        "category": content.get("category"),
        "fact": fact,
        "title": title,
        "description": description,
        "source_1": source_1,
        "source_2": source_2,
        "pexels_query_used": media_credit.get("query"),
        "pexels_video_url": media_credit.get("video_url"),
        "pexels_creator": media_credit.get("creator"),
        "narration_duration_seconds": round(duration, 3),
        "publish_at": publish_at.isoformat(),
    }
    factory.POST_META_PATH.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"ok": True, "stage": "built", **metadata}, ensure_ascii=False, indent=2))


factory.select_ready_script = select_ready_script_with_slot
factory.next_publish_time = next_publish_time_for_slot
factory.split_caption = split_caption_fixed
factory.make_srt = make_srt_fixed
factory.render = render_fixed
factory.build_one = build_one_long
factory.main()
