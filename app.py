#!/usr/bin/env python3
"""
Reels Bot — Web Interface
Run with: python3 app.py
"""

import os, json, re, subprocess, threading, uuid, zipfile, webbrowser
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)

JOBS = {}
OUTPUT_BASE = Path(os.environ.get("OUTPUT_DIR", str(Path.home() / "reels_output")))
OUTPUT_BASE.mkdir(parents=True, exist_ok=True)


# ── Core logic ─────────────────────────────────────────────────────────────

def fmt_ts(seconds):
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"

def safe_filename(title, max_len=50):
    cleaned = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_")
    return cleaned[:max_len]

def download_video(url, output_dir):
    out_template = os.path.join(output_dir, "%(title).80s.%(ext)s")
    result = subprocess.run(
        ["yt-dlp", "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
         "--merge-output-format", "mp4", "-o", out_template, url],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed:\n{result.stderr}")
    mp4_files = list(Path(output_dir).glob("*.mp4"))
    if not mp4_files:
        raise RuntimeError("No MP4 file found after download.")
    return str(sorted(mp4_files, key=lambda p: p.stat().st_mtime)[-1])

def transcribe_video(video_path, model_size="base"):
    import whisper
    model = whisper.load_model(model_size)
    result = model.transcribe(video_path, word_timestamps=False, verbose=False)
    return [{"start": s["start"], "end": s["end"], "text": s["text"].strip()}
            for s in result["segments"]]

def find_reel_moments(segments, api_key, niche):
    from anthropic import Anthropic
    client = Anthropic(api_key=api_key)

    transcript_text = "\n".join(f"[{fmt_ts(s['start'])}] {s['text']}" for s in segments)

    prompt = f"""You are an expert content strategist specialized in Instagram Reels. You analyze YouTube transcripts and extract the absolute best moments for Reels of 15-60 seconds.

**The creator's niche/positioning:**
{niche}

**What makes a strong Reel clip:**
1. Hook in the first 3 seconds: pattern interrupt, contrarian statement, or a question that lands instantly
2. One core idea: one clear thought, no rambling explanation
3. Self-contained: no "as I just said" or missing context
4. Emotional or intellectual punch: aha-moment, recognition, or reframe
5. Quotable line: at least one sentence that works as caption or quote story
6. Length 20-50 seconds ideal, max 60
7. Provocative edge: grip attention, challenge mainstream advice, make people stop and rethink.

**What to avoid:**
- Generic statements without substance
- Long intros or disclaimers
- Clips where the power was in visual context (whiteboard, slides, etc.)
- Anything that sounds like every other creator in this niche

Transcript (timestamps are MM:SS):
{transcript_text}

Give me the top 5-8 clips, ranked strongest to weakest. Be strict: better 5 truly strong clips than 10 mediocre ones. If a clip scores below 7/10, leave it out.

Return ONLY a valid JSON array, no other text or markdown:
[
  {{
    "start": <start_in_seconds as number>,
    "end": <end_in_seconds as number>,
    "title": "<short catchy title, max 8 words>",
    "hook": "<exact first sentence that hooks viewers>",
    "core_idea": "<one sentence summary>",
    "rating": <number 7-10>
  }}
]

Rules:
- end - start must be between 20 and 60 (strictly)
- No overlapping clips
- Start a beat before the key line for natural entry
- Only include clips with rating 7 or above"""

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        raise RuntimeError(f"Could not parse Claude response:\n{raw}")
    return json.loads(match.group())

def get_video_dimensions(video_path):
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", video_path],
        capture_output=True, text=True,
    )
    info = json.loads(result.stdout)
    vs = next(s for s in info["streams"] if s["codec_type"] == "video")
    return int(vs["width"]), int(vs["height"])

def create_reel(video_path, start, end, output_path):
    orig_w, orig_h = get_video_dimensions(video_path)
    tw, th = 1080, 1920
    scale_ratio = max(tw / orig_w, th / orig_h)
    scaled_w = int(orig_w * scale_ratio)
    scaled_h = int(orig_h * scale_ratio)
    crop_x = (scaled_w - tw) // 2
    crop_y = (scaled_h - th) // 2
    vf = f"scale={scaled_w}:{scaled_h},crop={tw}:{th}:{crop_x}:{crop_y}"
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start), "-i", video_path, "-t", str(end - start),
        "-vf", vf, "-c:v", "libx264", "-preset", "fast", "-crf", "22",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr[-400:]}")


# ── Job runner ─────────────────────────────────────────────────────────────

def _log(job_id, msg):
    JOBS[job_id]["log"].append(msg)

def _run_job(job_id, url, api_key, model, niche, out_dir):
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _log(job_id, "Downloading video...")
            video_path = download_video(url, tmp)
            _log(job_id, f"Downloaded: {Path(video_path).name}")

            _log(job_id, f"Transcribing with Whisper ({model})... this takes a while for long videos")
            segments = transcribe_video(video_path, model)
            _log(job_id, f"{len(segments)} segments transcribed")

            _log(job_id, "Analyzing with Claude...")
            moments = find_reel_moments(segments, api_key, niche)
            _log(job_id, f"{len(moments)} reel moments found")

            JOBS[job_id]["moments"] = [
                {"title": m["title"], "hook": m["hook"],
                 "start": fmt_ts(m["start"]), "end": fmt_ts(m["end"]),
                 "rating": m.get("rating", "?")}
                for m in moments
            ]

            _log(job_id, "Cutting reels...")
            files = []
            for i, m in enumerate(moments, 1):
                safe = safe_filename(m["title"])
                filename = f"reel_{i:02d}_{safe}.mp4"
                out_path = os.path.join(out_dir, filename)
                try:
                    create_reel(video_path, m["start"], m["end"], out_path)
                    size_mb = os.path.getsize(out_path) / (1024 * 1024)
                    files.append(filename)
                    _log(job_id, f"Reel {i}/{len(moments)}: {m['title']} ({size_mb:.1f} MB)")
                except Exception as e:
                    files.append(None)
                    _log(job_id, f"Reel {i} failed: {e}")

            JOBS[job_id]["files"] = files
            JOBS[job_id]["status"] = "done"
            _log(job_id, f"Done! {len([f for f in files if f])}/{len(moments)} reels ready.")

    except Exception as e:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["error"] = str(e)
        _log(job_id, f"Error: {e}")


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/run", methods=["POST"])
def run():
    data = request.json or {}
    url = data.get("url", "").strip()
    api_key = data.get("api_key", "").strip()
    model = data.get("model", "base")
    niche = data.get("niche", "").strip()

    if not url:
        return jsonify({"error": "YouTube URL is required"}), 400
    if not api_key:
        return jsonify({"error": "Anthropic API key is required"}), 400
    if not niche:
        return jsonify({"error": "Please describe your niche/positioning"}), 400

    job_id = uuid.uuid4().hex[:8]
    out_dir = str(OUTPUT_BASE / job_id)
    os.makedirs(out_dir)

    JOBS[job_id] = {"status": "running", "log": [], "files": [], "moments": [], "error": None}
    threading.Thread(target=_run_job, args=(job_id, url, api_key, model, niche, out_dir), daemon=True).start()

    return jsonify({"job_id": job_id})

@app.route("/poll/<job_id>")
def poll(job_id):
    after = int(request.args.get("after", 0))
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "log": job["log"][after:],
        "log_total": len(job["log"]),
        "files": job["files"],
        "moments": job.get("moments", []),
        "error": job.get("error"),
    })

@app.route("/download/<job_id>/<filename>")
def download(job_id, filename):
    job = JOBS.get(job_id)
    if not job:
        return "Not found", 404
    out_dir = OUTPUT_BASE / job_id
    file_path = (out_dir / filename).resolve()
    if not str(file_path).startswith(str(out_dir.resolve())):
        return "Forbidden", 403
    if not file_path.exists():
        return "Not found", 404
    return send_file(str(file_path), as_attachment=True)

@app.route("/download_all/<job_id>")
def download_all(job_id):
    job = JOBS.get(job_id)
    if not job or not job["files"]:
        return "Not found", 404
    out_dir = OUTPUT_BASE / job_id
    zip_path = out_dir / "reels.zip"
    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
        for f in job["files"]:
            if f:
                fp = out_dir / f
                if fp.exists():
                    zf.write(str(fp), f)
    return send_file(str(zip_path), as_attachment=True, download_name="reels.zip")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    is_local = os.environ.get("RAILWAY_ENVIRONMENT") is None
    if is_local:
        print(f"Reels Bot starting at http://localhost:{port}")
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    app.run(host="0.0.0.0", port=port, debug=False)
