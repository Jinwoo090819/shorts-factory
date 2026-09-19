import asyncio
import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import edge_tts
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / os.getenv("OUTPUT_DIR", "output")
DATA = ROOT / "data"
SCRIPTS = DATA / "scripts"
OUT.mkdir(parents=True, exist_ok=True)
DATA.mkdir(parents=True, exist_ok=True)
SCRIPTS.mkdir(parents=True, exist_ok=True)
HISTORY_PATH = DATA / "history.json"

KST = ZoneInfo("Asia/Seoul")
VOICE = os.getenv("TTS_VOICE", "ko-KR-SunHiNeural")

BLOCKED = {
    "총", "소총", "권총", "폭탄", "폭발물", "마약", "대마", "도박", "카지노",
    "자해", "자살", "살인", "고문", "성인물", "포르노", "담배", "전자담배",
}


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required secret: {name}")
    return value


def load_history():
    if not HISTORY_PATH.exists():
        return []
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_history(item):
    history = load_history()
    history.append(item)
    HISTORY_PATH.write_text(
        json.dumps(history[-300:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def safe_text(text: str) -> bool:
    lowered = text.lower()
    return not any(word.lower() in lowered for word in BLOCKED)


def _normalize_script_payload(raw, path: Path):
    if isinstance(raw, list):
        candidates = raw
    elif isinstance(raw, dict):
        candidates = [raw]
    else:
        return []

    out = []
    for index, item in enumerate(candidates):
        if not isinstance(item, dict):
            continue
        if str(item.get("status", "ready")).lower() != "ready":
            continue
        script = str(item.get("script", "")).strip()
        if not script.startswith("그거 아세요?"):
            continue
        if not safe_text(script):
            continue
        out.append((path, index, item, raw))
    return out


def select_ready_script():
    for path in sorted(SCRIPTS.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        candidates = _normalize_script_payload(raw, path)
        if candidates:
            return candidates[0]
    raise RuntimeError("No ready script found in data/scripts")


def mark_script_published(path: Path, index: int, raw, youtube_id: str, publish_at: datetime):
    now = datetime.now(KST).isoformat()
    if isinstance(raw, list):
        target = raw[index]
    else:
        target = raw
    target["status"] = "published"
    target["youtube_id"] = youtube_id
    target["publish_at"] = publish_at.isoformat()
    target["used_at"] = now
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")


async def make_tts(text: str, output: Path):
    communicate = edge_tts.Communicate(text=text, voice=VOICE, rate="+3%")
    await communicate.save(str(output))


def ffprobe_duration(path: Path) -> float:
    p = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(p.stdout.strip())


def split_caption(text: str):
    text = re.sub(r"\s+", " ", text).strip()
    sentences = [x.strip() for x in re.split(r"(?<=[.!?。])\s+", text) if x.strip()]
    chunks = []
    for sentence in sentences:
        words = sentence.split()
        current = []
        for word in words:
            current.append(word)
            if len(" ".join(current)) >= 18:
                chunks.append(" ".join(current))
                current = []
        if current:
            chunks.append(" ".join(current))
    return chunks or [text]


def srt_time(seconds: float):
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def make_srt(text: str, duration: float, output: Path):
    chunks = split_caption(text)
    weights = [max(len(re.sub(r"\s", "", x)), 4) for x in chunks]
    total = sum(weights)
    cursor = 0.0
    lines = []
    for i, (chunk, weight) in enumerate(zip(chunks, weights), start=1):
        span = duration * weight / total
        end = min(duration, cursor + span)
        lines.extend([str(i), f"{srt_time(cursor)} --> {srt_time(end)}", chunk, ""])
        cursor = end
    output.write_text("\n".join(lines), encoding="utf-8")


def pexels_video(queries, output: Path):
    api_key = required("PEXELS_API_KEY")
    last_error = None
    for query_text in [q for q in queries if q]:
        try:
            r = requests.get(
                "https://api.pexels.com/videos/search",
                params={"query": query_text, "orientation": "portrait", "per_page": 8},
                headers={"Authorization": api_key},
                timeout=30,
            )
            r.raise_for_status()
            videos = r.json().get("videos", [])
            candidates = []
            for video in videos:
                for f in video.get("video_files", []):
                    if f.get("file_type") != "video/mp4":
                        continue
                    width = f.get("width") or 0
                    height = f.get("height") or 0
                    link = f.get("link")
                    if link and height >= width and height >= 720:
                        candidates.append((width * height, link))
            if not candidates:
                raise RuntimeError(f"No portrait MP4 found for: {query_text}")
            candidates.sort(reverse=True)
            link = candidates[0][1]
            with requests.get(link, stream=True, timeout=90) as vr:
                vr.raise_for_status()
                with output.open("wb") as f:
                    for chunk in vr.iter_content(1024 * 1024):
                        if chunk:
                            f.write(chunk)
            return query_text
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Pexels search failed for all queries: {last_error}")


def render(video: Path, audio: Path, srt: Path, output: Path):
    duration = ffprobe_duration(audio)
    subtitle_path = str(srt).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    vf = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        "eq=brightness=-0.08:saturation=0.9,"
        f"subtitles='{subtitle_path}':force_style='FontName=Noto Sans CJK KR,FontSize=18,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=1,"
        "Alignment=2,MarginV=210'"
    )
    cmd = [
        "ffmpeg", "-y", "-stream_loop", "-1", "-i", str(video), "-i", str(audio),
        "-t", f"{duration:.3f}", "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", "-crf", "21",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        "-movflags", "+faststart", "-shortest", str(output),
    ]
    subprocess.run(cmd, check=True)


def next_publish_time():
    now = datetime.now(KST)
    publish = now.replace(hour=19, minute=0, second=0, microsecond=0)
    if now >= publish - timedelta(minutes=10):
        publish += timedelta(days=1)
    return publish


def youtube_client():
    creds = Credentials(
        token=None,
        refresh_token=required("YOUTUBE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=required("YOUTUBE_CLIENT_ID"),
        client_secret=required("YOUTUBE_CLIENT_SECRET"),
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload_youtube(video: Path, title: str, description: str, publish_at: datetime):
    youtube = youtube_client()
    body = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "categoryId": "27",
            "tags": ["잡지식", "상식", "shorts"],
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": publish_at.isoformat(),
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(
        str(video),
        chunksize=8 * 1024 * 1024,
        resumable=True,
        mimetype="video/mp4",
    )
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk()
    return response["id"]


def build_one():
    script_path, script_index, content, raw = select_ready_script()
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

    audio = OUT / "narration.mp3"
    broll = OUT / "broll.mp4"
    subtitles = OUT / "captions.srt"
    final = OUT / "short.mp4"

    asyncio.run(make_tts(script, audio))
    duration = ffprobe_duration(audio)
    if duration < 12 or duration > 45:
        raise RuntimeError(f"Unexpected narration duration: {duration:.1f}s")
    make_srt(script, duration, subtitles)
    used_query = pexels_video(queries, broll)
    render(broll, audio, subtitles, final)

    publish_at = next_publish_time()
    source_lines = [x for x in [source_1, source_2] if x]
    description = (
        f"{fact}\n\n"
        + ("출처:\n" + "\n".join(source_lines) + "\n\n" if source_lines else "")
        + "#잡지식 #상식 #shorts"
    )
    video_id = upload_youtube(final, title, description, publish_at)

    mark_script_published(script_path, script_index, raw, video_id, publish_at)
    save_history({
        "created_at": datetime.now(KST).isoformat(),
        "publish_at": publish_at.isoformat(),
        "script_file": str(script_path.relative_to(ROOT)),
        "fact_key": content.get("fact_key"),
        "category": content.get("category"),
        "fact": fact,
        "title": title,
        "source_1": source_1,
        "source_2": source_2,
        "pexels_query_used": used_query,
        "youtube_id": video_id,
    })

    print(json.dumps({
        "ok": True,
        "youtube_id": video_id,
        "publish_at": publish_at.isoformat(),
        "title": title,
        "script_file": str(script_path.relative_to(ROOT)),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    build_one()
