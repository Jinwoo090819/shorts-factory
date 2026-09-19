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
MIN_BROLL_CLIPS = 4
TARGET_BROLL_CLIPS = 5
MAX_BROLL_CLIPS = 6


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

    sentences = [
        x.strip()
        for x in re.split(r"(?<=[.!?。！？])\s+", text)
        if x.strip()
    ]

    blocks = []
    for sentence in sentences:
        blocks.extend(_wrap_sentence_two_lines(sentence))

    return blocks or [text]


def _ass_time(seconds):
    centiseconds = max(0, int(round(seconds * 100)))
    hours, centiseconds = divmod(centiseconds, 360000)
    minutes, centiseconds = divmod(centiseconds, 6000)
    secs, centiseconds = divmod(centiseconds, 100)
    return f"{hours}:{minutes:02}:{secs:02}.{centiseconds:02}"


def _ass_text(text):
    text = text.replace("\\", "／").replace("{", "(").replace("}", ")")
    return text.replace("\n", r"\N")


def make_ass_fixed(text, duration, output):
    chunks = split_caption_fixed(text)
    if not chunks:
        raise RuntimeError("No subtitle chunks generated")

    weights = [max(len(re.sub(r"\s", "", chunk)), 6) for chunk in chunks]
    total = sum(weights)
    cursor = 0.0

    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Shorts,Noto Sans CJK KR,64,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,1,2,120,120,220,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events = []
    for i, (chunk, weight) in enumerate(zip(chunks, weights), start=1):
        span = duration * weight / total
        end = duration if i == len(chunks) else min(duration, cursor + span)
        caption = _ass_text(chunk)
        effect = r"{\fad(80,60)\fscx94\fscy94\t(0,120,\fscx100\fscy100)}"
        events.append(
            f"Dialogue: 0,{_ass_time(cursor)},{_ass_time(end)},Shorts,,0,0,0,,{effect}{caption}"
        )
        cursor = end

    output.write_text(header + "\n".join(events) + "\n", encoding="utf-8")


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


def _pick_portrait_file(video):
    files = []
    for f in video.get("video_files", []):
        if f.get("file_type") != "video/mp4":
            continue
        width = int(f.get("width") or 0)
        height = int(f.get("height") or 0)
        link = str(f.get("link") or "")
        if not link or height < width or height < 720:
            continue
        # Prefer a moderate 720x1280-ish file to keep downloads fast while
        # retaining enough quality for 1080x1920 Shorts output.
        score = abs(width - 720) + abs(height - 1280)
        files.append((score, link, width, height))
    if not files:
        return None
    files.sort(key=lambda x: x[0])
    return files[0]


def pexels_videos_multi(queries, output_dir, desired=TARGET_BROLL_CLIPS):
    api_key = factory.required("PEXELS_API_KEY")
    output_dir.mkdir(parents=True, exist_ok=True)

    per_query = []
    seen_video_ids = set()
    last_error = None

    for query_text in [q for q in queries if q]:
        try:
            response = factory.requests.get(
                "https://api.pexels.com/v1/videos/search",
                params={
                    "query": query_text,
                    "orientation": "portrait",
                    "per_page": 12,
                },
                headers={"Authorization": api_key},
                timeout=30,
            )
            response.raise_for_status()

            query_candidates = []
            for video in response.json().get("videos", []):
                video_id = str(video.get("id") or "")
                if not video_id or video_id in seen_video_ids:
                    continue
                picked = _pick_portrait_file(video)
                if not picked:
                    continue
                _, link, width, height = picked
                query_candidates.append({
                    "id": video_id,
                    "query": query_text,
                    "download_url": link,
                    "video_url": str(video.get("url") or ""),
                    "creator": str(((video.get("user") or {}).get("name")) or ""),
                    "width": width,
                    "height": height,
                })
                seen_video_ids.add(video_id)
                if len(query_candidates) >= 2:
                    break
            if query_candidates:
                per_query.append(query_candidates)
        except Exception as exc:
            last_error = exc

    # Round-robin across queries so a single search phrase does not dominate.
    selected = []
    round_index = 0
    while len(selected) < min(desired, MAX_BROLL_CLIPS):
        added = False
        for bucket in per_query:
            if round_index < len(bucket):
                selected.append(bucket[round_index])
                added = True
                if len(selected) >= min(desired, MAX_BROLL_CLIPS):
                    break
        if not added:
            break
        round_index += 1

    if len(selected) < MIN_BROLL_CLIPS:
        detail = f" Last error: {last_error}" if last_error else ""
        raise RuntimeError(
            f"Need at least {MIN_BROLL_CLIPS} unique Pexels clips, found {len(selected)}.{detail}"
        )

    downloaded = []
    for i, item in enumerate(selected, start=1):
        local_path = output_dir / f"broll_{i:02}.mp4"
        with factory.requests.get(item["download_url"], stream=True, timeout=90) as vr:
            vr.raise_for_status()
            with local_path.open("wb") as f:
                for chunk in vr.iter_content(1024 * 1024):
                    if chunk:
                        f.write(chunk)
        clean = dict(item)
        clean.pop("download_url", None)
        clean["local_path"] = str(local_path)
        downloaded.append(clean)

    return downloaded


def render_fixed(videos, audio, subtitles, output):
    duration = factory.ffprobe_duration(audio)
    subtitle_path = str(subtitles).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    video_paths = [str(v) for v in videos]
    if len(video_paths) < MIN_BROLL_CLIPS:
        raise RuntimeError(f"Not enough B-roll clips for render: {len(video_paths)}")

    # Divide narration evenly across B-roll clips. The final segment absorbs
    # rounding so total video duration exactly matches the narration.
    base_segment = duration / len(video_paths)
    segments = [base_segment] * len(video_paths)
    segments[-1] = duration - sum(segments[:-1])

    cmd = ["ffmpeg", "-y"]
    for video_path in video_paths:
        cmd.extend(["-stream_loop", "-1", "-i", video_path])
    audio_index = len(video_paths)
    cmd.extend(["-i", str(audio)])

    filters = []
    for i, segment in enumerate(segments):
        filters.append(
            f"[{i}:v:0]trim=duration={segment:.3f},setpts=PTS-STARTPTS,"
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,fps=30,format=yuv420p"
            f"[v{i}]"
        )

    concat_inputs = "".join(f"[v{i}]" for i in range(len(video_paths)))
    filters.append(
        f"{concat_inputs}concat=n={len(video_paths)}:v=1:a=0[basev]"
    )
    filters.append(
        f"[basev]eq=brightness=-0.08:saturation=0.9,subtitles='{subtitle_path}'[vout]"
    )
    filters.append(
        f"[{audio_index}:a:0]aresample=48000,aformat=channel_layouts=stereo,"
        "volume=1.7,highpass=f=70,loudnorm=I=-15:LRA=7:TP=-1.5[voice]"
    )
    filters.append(
        f"sine=frequency=174.61:sample_rate=48000:duration={duration:.3f},volume=0.022[bg1]"
    )
    filters.append(
        f"sine=frequency=220:sample_rate=48000:duration={duration:.3f},volume=0.016[bg2]"
    )
    filters.append(
        f"sine=frequency=261.63:sample_rate=48000:duration={duration:.3f},volume=0.010[bg3]"
    )
    filters.append(
        "[bg1][bg2][bg3]amix=inputs=3:duration=longest:normalize=0,"
        "lowpass=f=1100,tremolo=f=0.16:d=0.22,volume=0.80[bgm]"
    )
    filters.append(
        "[voice][bgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
        "alimiter=limit=0.95[aout]"
    )

    cmd.extend([
        "-t", f"{duration:.3f}",
        "-filter_complex", ";".join(filters),
        "-map", "[vout]",
        "-map", "[aout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "21",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        "-shortest",
        str(output),
    ])
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
    broll_dir = factory.OUT / "broll"
    subtitles = factory.OUT / "captions.ass"
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

    make_ass_fixed(script, duration, subtitles)
    media_credits = pexels_videos_multi(queries, broll_dir, TARGET_BROLL_CLIPS)
    video_paths = [item["local_path"] for item in media_credits]
    factory.render(video_paths, audio, subtitles, final)

    publish_at = factory.next_publish_time()
    source_lines = [x for x in [source_1, source_2] if x]

    credit_lines = ["영상 소스: Pexels"]
    for item in media_credits:
        line = "- "
        if item.get("creator"):
            line += str(item["creator"])
        else:
            line += "Pexels creator"
        if item.get("video_url"):
            line += f" / {item['video_url']}"
        credit_lines.append(line)

    description = (
        f"{fact}\n\n"
        + ("출처:\n" + "\n".join(source_lines) + "\n\n" if source_lines else "")
        + "\n".join(credit_lines)
        + "\n\n#잡지식 #상식 #shorts"
    )

    (factory.SITE / "short.mp4").write_bytes(final.read_bytes())
    (factory.SITE / "index.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>shorts-factory media</title><p>Media staging endpoint.</p>",
        encoding="utf-8",
    )

    clean_credits = []
    for item in media_credits:
        clean_credits.append({
            "id": item.get("id"),
            "query": item.get("query"),
            "video_url": item.get("video_url"),
            "creator": item.get("creator"),
            "width": item.get("width"),
            "height": item.get("height"),
        })

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
        "pexels_video_count": len(clean_credits),
        "pexels_videos": clean_credits,
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
factory.render = render_fixed
factory.build_one = build_one_long
factory.main()
