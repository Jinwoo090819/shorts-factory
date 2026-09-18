import asyncio
import json
import os
import random
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import edge_tts
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / os.getenv("OUTPUT_DIR", "output")
DATA = ROOT / "data"
OUT.mkdir(parents=True, exist_ok=True)
DATA.mkdir(parents=True, exist_ok=True)
HISTORY_PATH = DATA / "history.json"

KST = ZoneInfo("Asia/Seoul")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
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
    HISTORY_PATH.write_text(json.dumps(history[-300:], ensure_ascii=False, indent=2), encoding="utf-8")


def safe_source(text: str) -> bool:
    lowered = text.lower()
    return not any(word.lower() in lowered for word in BLOCKED)


def get_wikipedia_candidate():
    history = load_history()
    used_titles = {x.get("source_title") for x in history}
    headers = {"User-Agent": "shorts-factory/1.0 (educational automation)"}

    for _ in range(15):
        r = requests.get(
            "https://ko.wikipedia.org/api/rest_v1/page/random/summary",
            headers=headers,
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        title = (data.get("title") or "").strip()
        extract = (data.get("extract") or "").strip()
        page_url = ((data.get("content_urls") or {}).get("desktop") or {}).get("page", "")
        if not title or len(extract) < 140 or title in used_titles:
            continue
        if not safe_source(title + " " + extract):
            continue
        if data.get("type") == "disambiguation":
            continue
        return {"title": title, "extract": extract, "url": page_url}

    raise RuntimeError("Could not find a suitable Wikipedia source after retries")


def call_gemini(source):
    api_key = required("GEMINI_API_KEY")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={api_key}"
    )
    prompt = f"""
너는 한국어 YouTube Shorts 잡지식 편집자다.
아래 '출처 원문'에 명시된 사실만 사용한다. 원문에 없는 숫자, 원인, 추론, 인물 관계를 새로 만들지 마라.
출처가 애매하거나 한 가지 흥미로운 사실을 안전하게 만들 수 없으면 usable=false로 답해라.

콘텐츠 규칙:
- 첫 문장은 정확히 '그거 아세요?'
- 한 영상에 사실 하나만
- 자연스러운 한국어 구어체
- 약 20~30초 분량, 130~220자 정도
- 구조: 그거 아세요? → 사실 공개 → 왜/어떻게 설명 → 마지막 한 방
- 과장, 공포 조장, 음모론, 위험 행동, 성인 주제 금지
- 제목은 짧고 호기심을 유발하되 낚시성 금지
- pexels_query는 영상 검색용 영어 명사구 2~5단어

출처 제목: {source['title']}
출처 원문:
{source['extract']}

반드시 아래 JSON 형식으로만 답해라:
{{
  "usable": true,
  "fact": "한 문장 사실",
  "script": "그거 아세요? ...",
  "title": "쇼츠 제목",
  "pexels_query": "english search phrase"
}}
""".strip()
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.35,
            "responseMimeType": "application/json",
        },
    }
    r = requests.post(url, json=payload, timeout=60)
    r.raise_for_status()
    obj = r.json()
    text = obj["candidates"][0]["content"]["parts"][0]["text"]
    result = json.loads(text)
    if not result.get("usable"):
        raise ValueError("Gemini rejected source as unusable")
    script = str(result.get("script", "")).strip()
    if not script.startswith("그거 아세요?"):
        raise ValueError("Script does not start with required hook")
    if not safe_source(script):
        raise ValueError("Generated script hit safety filter")
    result["title"] = str(result.get("title", "잡지식 한 스푼"))[:85]
    return result


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


def pexels_video(query_text: str, output: Path):
    api_key = required("PEXELS_API_KEY")
    r = requests.get(
        "https://api.pexels.com/videos/search",
        params={"query": query_text, "orientation": "portrait", "per_page": 8},
        headers={"Authorization": api_key},
        timeout=30,
    )
    r.raise_for_status()
    videos = r.json().get("videos", [])
    if not videos:
        raise RuntimeError(f"No Pexels videos found for query: {query_text}")

    candidates = []
    for video in videos:
        for f in video.get("video_files", []):
            if f.get("file_type") != "video/mp4":
                continue
            width = f.get("width") or 0
            height = f.get("height") or 0
            if height >= width and height >= 720:
                candidates.append((width * height, f.get("link")))
    if not candidates:
        raise RuntimeError("No suitable portrait MP4 found on Pexels")
    candidates.sort(reverse=True)
    link = candidates[0][1]
    with requests.get(link, stream=True, timeout=90) as vr:
        vr.raise_for_status()
        with output.open("wb") as f:
            for chunk in vr.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)


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
            "title": title,
            "description": description,
            "categoryId": "27",
            "tags": ["잡지식", "상식", "shorts"],
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": publish_at.isoformat(),
        },
    }
    media = MediaFileUpload(str(video), chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk()
    return response["id"]


def build_one():
    last_error = None
    for _ in range(6):
        try:
            source = get_wikipedia_candidate()
            content = call_gemini(source)
            break
        except Exception as exc:
            last_error = exc
    else:
        raise RuntimeError(f"Could not generate a valid short: {last_error}")

    audio = OUT / "narration.mp3"
    broll = OUT / "broll.mp4"
    subtitles = OUT / "captions.srt"
    final = OUT / "short.mp4"

    asyncio.run(make_tts(content["script"], audio))
    duration = ffprobe_duration(audio)
    if duration < 12 or duration > 45:
        raise RuntimeError(f"Unexpected narration duration: {duration:.1f}s")
    make_srt(content["script"], duration, subtitles)
    pexels_video(content["pexels_query"], broll)
    render(broll, audio, subtitles, final)

    publish_at = next_publish_time()
    description = (
        f"{content['fact']}\n\n"
        f"출처: {source['url']}\n"
        "#잡지식 #상식 #shorts"
    )
    video_id = upload_youtube(final, content["title"], description, publish_at)
    save_history({
        "created_at": datetime.now(KST).isoformat(),
        "publish_at": publish_at.isoformat(),
        "source_title": source["title"],
        "source_url": source["url"],
        "fact": content["fact"],
        "title": content["title"],
        "youtube_id": video_id,
    })
    print(json.dumps({
        "ok": True,
        "youtube_id": video_id,
        "publish_at": publish_at.isoformat(),
        "title": content["title"],
        "source": source["url"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    build_one()
