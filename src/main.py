import argparse
import asyncio
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import edge_tts
import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / os.getenv("OUTPUT_DIR", "output")
DATA = ROOT / "data"
SCRIPTS = DATA / "scripts"
SITE = ROOT / "site"
OUT.mkdir(parents=True, exist_ok=True)
DATA.mkdir(parents=True, exist_ok=True)
SCRIPTS.mkdir(parents=True, exist_ok=True)
SITE.mkdir(parents=True, exist_ok=True)
HISTORY_PATH = DATA / "history.json"
POST_META_PATH = OUT / "post.json"

KST = ZoneInfo("Asia/Seoul")
VOICE = os.getenv("TTS_VOICE", "ko-KR-SunHiNeural")
BUFFER_ENDPOINT = "https://api.buffer.com"

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
        json.dumps(history[-500:], ensure_ascii=False, indent=2),
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


def mark_script_scheduled(path: Path, index: int, raw, buffer_post_id: str, publish_at: datetime, media_url: str):
    now = datetime.now(KST).isoformat()
    target = raw[index] if isinstance(raw, list) else raw
    target["status"] = "scheduled"
    target["buffer_post_id"] = buffer_post_id
    target["publish_at"] = publish_at.isoformat()
    target["media_url"] = media_url
    target["scheduled_at"] = now
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
                "https://api.pexels.com/v1/videos/search",
                params={"query": query_text, "orientation": "portrait", "per_page": 8},
                headers={"Authorization": api_key},
                timeout=30,
            )
            r.raise_for_status()
            videos = r.json().get("videos", [])
            candidates = []
            for video in videos:
                video_url = str(video.get("url") or "")
                creator = str(((video.get("user") or {}).get("name")) or "")
                for f in video.get("video_files", []):
                    if f.get("file_type") != "video/mp4":
                        continue
                    width = f.get("width") or 0
                    height = f.get("height") or 0
                    link = f.get("link")
                    if link and height >= width and height >= 720:
                        candidates.append((width * height, link, video_url, creator))
            if not candidates:
                raise RuntimeError(f"No portrait MP4 found for: {query_text}")
            candidates.sort(reverse=True, key=lambda x: x[0])
            _, link, video_url, creator = candidates[0]
            with requests.get(link, stream=True, timeout=90) as vr:
                vr.raise_for_status()
                with output.open("wb") as f:
                    for chunk in vr.iter_content(1024 * 1024):
                        if chunk:
                            f.write(chunk)
            return {
                "query": query_text,
                "video_url": video_url,
                "creator": creator,
            }
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


def graphql(query: str, variables=None):
    token = required("BUFFER_API_KEY")
    response = requests.post(
        BUFFER_ENDPOINT,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"query": query, "variables": variables or {}},
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(f"Buffer GraphQL error: {payload['errors']}")
    return payload.get("data") or {}


def find_youtube_channel():
    preferred = os.getenv("BUFFER_CHANNEL_ID", "").strip()
    account_data = graphql(
        """
        query GetOrganizations {
          account {
            organizations { id name }
          }
        }
        """
    )
    organizations = ((account_data.get("account") or {}).get("organizations") or [])
    youtube_channels = []
    for organization in organizations:
        org_id = organization.get("id")
        if not org_id:
            continue
        channel_data = graphql(
            """
            query GetChannels($input: ChannelsInput!) {
              channels(input: $input) {
                id
                name
                service
                serviceId
              }
            }
            """,
            {"input": {"organizationId": org_id}},
        )
        for channel in channel_data.get("channels") or []:
            if str(channel.get("service", "")).lower() == "youtube":
                youtube_channels.append(channel)

    if preferred:
        for channel in youtube_channels:
            if str(channel.get("id")) == preferred:
                return channel
        raise RuntimeError("BUFFER_CHANNEL_ID does not match a connected YouTube channel")

    if len(youtube_channels) == 1:
        return youtube_channels[0]
    if not youtube_channels:
        raise RuntimeError("No YouTube channel is connected to Buffer")
    names = ", ".join(str(c.get("name")) for c in youtube_channels)
    raise RuntimeError(
        "Multiple YouTube channels are connected to Buffer. "
        f"Set repository variable BUFFER_CHANNEL_ID. Channels: {names}"
    )


def create_buffer_post(channel_id: str, media_url: str, title: str, description: str, publish_at: datetime):
    due_at = publish_at.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    mutation = """
    mutation CreatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        ... on PostActionSuccess {
          post {
            id
            text
            dueAt
            status
          }
        }
        ... on MutationError {
          message
        }
      }
    }
    """
    variables = {
        "input": {
            "text": description,
            "channelId": channel_id,
            "schedulingType": "automatic",
            "mode": "customScheduled",
            "dueAt": due_at,
            "aiAssisted": True,
            "assets": [
                {
                    "video": {
                        "url": media_url,
                    }
                }
            ],
            "metadata": {
                "youtube": {
                    "title": title[:100],
                    "categoryId": "27",
                    "privacy": "public",
                    "madeForKids": False,
                    "notifySubscribers": True,
                    "isAiGenerated": True,
                }
            },
        }
    }
    data = graphql(mutation, variables)
    result = data.get("createPost") or {}
    if result.get("message"):
        raise RuntimeError(f"Buffer rejected post: {result['message']}")
    post = result.get("post") or {}
    if not post.get("id"):
        raise RuntimeError(f"Buffer returned no post id: {result}")
    return post


def build_one():
    script_path, script_index, content, _raw = select_ready_script()
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
    media_credit = pexels_video(queries, broll)
    render(broll, audio, subtitles, final)

    publish_at = next_publish_time()
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

    (SITE / "short.mp4").write_bytes(final.read_bytes())
    (SITE / "index.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>shorts-factory media</title><p>Media staging endpoint.</p>",
        encoding="utf-8",
    )

    metadata = {
        "script_file": str(script_path.relative_to(ROOT)),
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
        "publish_at": publish_at.isoformat(),
    }
    POST_META_PATH.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "stage": "built", **metadata}, ensure_ascii=False, indent=2))


def publish_built():
    media_url = required("MEDIA_URL")
    if not POST_META_PATH.exists():
        raise RuntimeError("output/post.json is missing; run build first")
    metadata = json.loads(POST_META_PATH.read_text(encoding="utf-8"))
    publish_at = datetime.fromisoformat(metadata["publish_at"])

    response = requests.head(media_url, allow_redirects=True, timeout=30)
    if response.status_code >= 400:
        raise RuntimeError(f"Public media URL is not reachable: {media_url} ({response.status_code})")

    channel = find_youtube_channel()
    post = create_buffer_post(
        str(channel["id"]),
        media_url,
        str(metadata["title"]),
        str(metadata["description"]),
        publish_at,
    )

    script_path = ROOT / metadata["script_file"]
    raw = json.loads(script_path.read_text(encoding="utf-8"))
    mark_script_scheduled(
        script_path,
        int(metadata["script_index"]),
        raw,
        str(post["id"]),
        publish_at,
        media_url,
    )
    save_history({
        "created_at": datetime.now(KST).isoformat(),
        "publish_at": publish_at.isoformat(),
        "script_file": metadata["script_file"],
        "fact_key": metadata.get("fact_key"),
        "category": metadata.get("category"),
        "fact": metadata.get("fact"),
        "title": metadata.get("title"),
        "source_1": metadata.get("source_1"),
        "source_2": metadata.get("source_2"),
        "pexels_query_used": metadata.get("pexels_query_used"),
        "pexels_video_url": metadata.get("pexels_video_url"),
        "pexels_creator": metadata.get("pexels_creator"),
        "buffer_post_id": post.get("id"),
        "buffer_due_at": post.get("dueAt"),
        "buffer_channel_id": channel.get("id"),
        "buffer_channel_name": channel.get("name"),
        "media_url": media_url,
        "status": "scheduled",
    })
    print(json.dumps({
        "ok": True,
        "stage": "scheduled",
        "buffer_post_id": post.get("id"),
        "channel": channel.get("name"),
        "publish_at": publish_at.isoformat(),
        "media_url": media_url,
    }, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["build", "publish"])
    args = parser.parse_args()
    if args.command == "build":
        build_one()
    else:
        publish_built()


if __name__ == "__main__":
    main()
