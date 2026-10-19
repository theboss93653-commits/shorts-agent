"""Agent Shorts : idée -> script -> voix -> vidéo -> upload YouTube.

Variables d'environnement requises (Secrets GitHub) :
  GEMINI_API_KEY, PEXELS_API_KEY, YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN
Variables optionnelles : NICHE, LANGUE, VOICE, PRIVACY, GEMINI_MODEL
"""
import asyncio
import json
import os
import random
import subprocess
import tempfile
from pathlib import Path

import edge_tts
import requests

NICHE = os.getenv("NICHE", "faits insolites sur la science")
LANGUE = os.getenv("LANGUE", "français")
VOICE = os.getenv("VOICE", "fr-FR-DeniseNeural")
PRIVACY = os.getenv("PRIVACY", "private")  # private | unlisted | public
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
HISTORY = Path("used_topics.json")


def run(cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# ---------- 1. Idée + script (Gemini, offre gratuite) ----------
def gen_script(history):
    avoid = ", ".join(history[-60:]) or "aucun"
    prompt = f"""Tu écris des YouTube Shorts en {LANGUE}. Niche : {NICHE}.
Sujets déjà traités (à éviter) : {avoid}.
Crée un nouveau short de 30 à 40 secondes (90 à 110 mots).
Règles : accroche choc dans la première phrase, faits exacts et vérifiables,
phrases courtes, pas de "salut à tous", termine par une question ou une chute
qui donne envie de revoir la vidéo.
Réponds UNIQUEMENT en JSON :
{{"topic": "sujet en quelques mots", "title": "titre accrocheur, 70 caractères max",
"script": "texte lu à voix haute, sans émojis ni didascalies",
"keywords": ["3 à 5 mots-clés EN ANGLAIS pour chercher des vidéos de stock verticales"]}}"""
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 1.0},
        },
        timeout=90,
    )
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


# ---------- 2. Voix off + horodatage des mots (edge-tts, gratuit) ----------
async def _tts(text, mp3_path):
    comm = edge_tts.Communicate(text, VOICE, boundary="WordBoundary")
    words = []
    with open(mp3_path, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 1e7
                end = (chunk["offset"] + chunk["duration"]) / 1e7
                words.append((start, end, chunk["text"]))
    return words


def audio_duration(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]
    )
    return float(out)


# ---------- 3. Sous-titres (format ASS, gravés par ffmpeg) ----------
ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,78,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,2,60,60,600,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ass_time(t):
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_ass(words, path):
    chunks = [words[i : i + 3] for i in range(0, len(words), 3)]
    lines = []
    for i, c in enumerate(chunks):
        start = c[0][0]
        end = chunks[i + 1][0][0] if i + 1 < len(chunks) else c[-1][1] + 0.3
        text = " ".join(w[2] for w in c).upper().replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{text}")
    path.write_text(ASS_HEADER + "\n".join(lines) + "\n", encoding="utf-8")


# ---------- 4. Vidéos de stock (Pexels, gratuit) ----------
def fetch_clip(keyword, dest, used):
    r = requests.get(
        "https://api.pexels.com/videos/search",
        headers={"Authorization": os.environ["PEXELS_API_KEY"]},
        params={"query": keyword, "orientation": "portrait", "per_page": 10},
        timeout=30,
    )
    r.raise_for_status()
    vids = [v for v in r.json().get("videos", []) if v["id"] not in used]
    if not vids:
        return False
    v = random.choice(vids)
    mp4s = [f for f in v["video_files"] if f.get("file_type") == "video/mp4"]
    portrait = [f for f in mp4s if (f.get("height") or 0) > (f.get("width") or 0)]
    files = portrait or mp4s
    if not files:
        return False
    best = min(files, key=lambda x: abs((x.get("width") or 0) - 1080))
    used.add(v["id"])
    with requests.get(best["link"], stream=True, timeout=120) as d:
        d.raise_for_status()
        with open(dest, "wb") as out:
            for chunk in d.iter_content(1 << 20):
                out.write(chunk)
    return True


# ---------- 5. Montage (ffmpeg, gratuit) ----------
def build_video(keywords, total, work):
    n = min(10, max(4, round(total / 4)))
    used, clips = set(), []
    for i in range(n):
        name = f"clip{i}.mp4"
        if fetch_clip(keywords[i % len(keywords)], work / name, used):
            clips.append(name)
    if not clips:
        raise RuntimeError("Aucune vidéo trouvée sur Pexels pour ces mots-clés.")
    seg = total / len(clips)
    segs = []
    for i, c in enumerate(clips):
        s = f"seg{i}.mp4"
        run(
            [
                "ffmpeg", "-y", "-stream_loop", "-1", "-i", c, "-t", f"{seg:.2f}",
                "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30",
                "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", s,
            ],
            cwd=work,
        )
        segs.append(s)
    (work / "list.txt").write_text("".join(f"file '{s}'\n" for s in segs))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "raw.mp4"], cwd=work)
    run(
        [
            "ffmpeg", "-y", "-i", "raw.mp4", "-i", "voice.mp3",
            "-vf", "subtitles=subs.ass",
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "160k", "-shortest", "final.mp4",
        ],
        cwd=work,
    )
    return work / "final.mp4"


# ---------- 6. Upload YouTube (API gratuite) ----------
def yt_token():
    r = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": os.environ["YT_CLIENT_ID"],
            "client_secret": os.environ["YT_CLIENT_SECRET"],
            "refresh_token": os.environ["YT_REFRESH_TOKEN"],
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def upload(video, title, description):
    token = yt_token()
    meta = {
        "snippet": {"title": title[:100], "description": description, "categoryId": "27"},
        "status": {"privacyStatus": PRIVACY, "selfDeclaredMadeForKids": False},
    }
    init = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
        },
        data=json.dumps(meta),
        timeout=30,
    )
    init.raise_for_status()
    with open(video, "rb") as f:
        up = requests.put(
            init.headers["Location"], data=f, headers={"Content-Type": "video/mp4"}, timeout=900
        )
    up.raise_for_status()
    return up.json()["id"]


def main():
    history = json.loads(HISTORY.read_text()) if HISTORY.exists() else []
    info = gen_script(history)
    print("Sujet :", info["topic"])

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        words = asyncio.run(_tts(info["script"], work / "voice.mp3"))
        if not words:
            raise RuntimeError("La synthèse vocale n'a renvoyé aucun mot.")
        total = audio_duration(work / "voice.mp3")
        build_ass(words, work / "subs.ass")
        video = build_video(info["keywords"], total, work)

        title = f"{info['title']} #Shorts"
        description = f"{info['script']}\n\n#Shorts #{NICHE.split()[0].lower()}"
        video_id = upload(video, title, description)
        print("Publié :", f"https://youtube.com/shorts/{video_id}", f"({PRIVACY})")

    history.append(info["topic"])
    HISTORY.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
