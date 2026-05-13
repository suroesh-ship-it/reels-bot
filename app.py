#!/usr/bin/env python3
"""
Reels Bot — Web Interface
Run with: python3 app.py
"""

import os, json, re, subprocess, threading, uuid, zipfile, webbrowser
from pathlib import Path
from flask import Flask, request, jsonify, send_file, Response

app = Flask(__name__)

JOBS = {}
OUTPUT_BASE = Path(os.environ.get("OUTPUT_DIR", str(Path.home() / "reels_output")))
OUTPUT_BASE.mkdir(parents=True, exist_ok=True)


# ── Core logic (from main.py) ──────────────────────────────────────────────

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
7. Provocative edge: the clip should grip attention, go against the grain, challenge mainstream advice, or make people stop and rethink something they took for granted.

**What to avoid:**
- Generic statements without substance
- Long intros or disclaimers
- Clips where the power was in visual context (whiteboard, slides, etc.)
- Anything that sounds like every other creator in this niche

Transcript (timestamps are MM:SS):
{transcript_text}

Give me the top 5-8 clips, ranked strongest to weakest. Be strict: better 5 truly strong clips than 10 mediocre ones. If a clip scores below 7/10, leave it out. Select clips that best match the creator's niche and would resonate with their specific audience.

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
        "-ss", str(start),
        "-i", video_path,
        "-t", str(end - start),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "22",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
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
            _log(job_id, "⬇️  Downloading video...")
            video_path = download_video(url, tmp)
            _log(job_id, f"✅  Downloaded: {Path(video_path).name}")

            _log(job_id, f"🎙️  Transcribing with Whisper ({model})... this takes a while for long videos")
            segments = transcribe_video(video_path, model)
            _log(job_id, f"✅  {len(segments)} segments transcribed")

            _log(job_id, "🤖  Analyzing with Claude...")
            moments = find_reel_moments(segments, api_key, niche)
            _log(job_id, f"✅  {len(moments)} reel moments found")

            JOBS[job_id]["moments"] = [
                {"title": m["title"], "hook": m["hook"],
                 "start": fmt_ts(m["start"]), "end": fmt_ts(m["end"]),
                 "rating": m.get("rating", "?")}
                for m in moments
            ]

            _log(job_id, "✂️   Cutting reels...")
            files = []
            for i, m in enumerate(moments, 1):
                safe = safe_filename(m["title"])
                filename = f"reel_{i:02d}_{safe}.mp4"
                out_path = os.path.join(out_dir, filename)
                try:
                    create_reel(video_path, m["start"], m["end"], out_path)
                    size_mb = os.path.getsize(out_path) / (1024 * 1024)
                    files.append(filename)
                    _log(job_id, f"✅  Reel {i}/{len(moments)}: {m['title']} ({size_mb:.1f} MB)")
                except Exception as e:
                    _log(job_id, f"❌  Reel {i} failed: {e}")

            JOBS[job_id]["files"] = files
            JOBS[job_id]["status"] = "done"
            _log(job_id, f"🎉  Done! {len(files)}/{len(moments)} reels ready to download.")

    except Exception as e:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["error"] = str(e)
        _log(job_id, f"❌  Error: {e}")


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return HTML

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
            fp = out_dir / f
            if fp.exists():
                zf.write(str(fp), f)
    return send_file(str(zip_path), as_attachment=True, download_name="reels.zip")


# ── HTML ───────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Reels Bot</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f5f5f7; color: #1d1d1f; min-height: 100vh; }
  .container { max-width: 680px; margin: 0 auto; padding: 40px 20px; }
  h1 { font-size: 32px; font-weight: 700; margin-bottom: 6px; }
  .subtitle { color: #6e6e73; margin-bottom: 32px; font-size: 15px; }
  .card { background: #fff; border-radius: 16px; padding: 28px;
          box-shadow: 0 2px 12px rgba(0,0,0,0.08); margin-bottom: 20px; }
  label { display: block; font-size: 13px; font-weight: 600;
          color: #6e6e73; margin-bottom: 6px; text-transform: uppercase; letter-spacing: .5px; }
  input, select, textarea { width: 100%; padding: 12px 14px; border: 1.5px solid #d2d2d7;
                  border-radius: 10px; font-size: 15px; margin-bottom: 16px;
                  outline: none; transition: border-color .2s; background: #fafafa;
                  font-family: inherit; resize: vertical; }
  input:focus, select:focus, textarea:focus { border-color: #0071e3; background: #fff; }
  .key-wrap { position: relative; }
  .key-wrap input { padding-right: 70px; }
  .show-btn { position: absolute; right: 12px; top: 12px; background: none;
              border: none; color: #0071e3; cursor: pointer; font-size: 13px;
              font-weight: 600; padding: 0; }
  .btn { width: 100%; padding: 14px; background: #0071e3; color: #fff;
         border: none; border-radius: 10px; font-size: 16px; font-weight: 600;
         cursor: pointer; transition: background .2s; }
  .btn:hover { background: #0077ed; }
  .btn:disabled { background: #a1a1a6; cursor: not-allowed; }
  .btn-outline { background: none; border: 1.5px solid #0071e3; color: #0071e3;
                 margin-top: 10px; }
  .btn-outline:hover { background: #f0f7ff; }
  .progress { display: none; }
  .log { background: #1d1d1f; color: #f5f5f7; border-radius: 10px;
         padding: 16px; font-family: monospace; font-size: 13px;
         max-height: 240px; overflow-y: auto; line-height: 1.7; }
  .results { display: none; }
  .moment { border: 1.5px solid #e5e5ea; border-radius: 10px; padding: 14px 16px;
            margin-bottom: 10px; }
  .moment-title { font-weight: 600; font-size: 15px; margin-bottom: 4px; }
  .moment-meta { font-size: 13px; color: #6e6e73; margin-bottom: 8px; }
  .moment-hook { font-size: 13px; color: #3a3a3c; font-style: italic; margin-bottom: 10px; }
  .dl-btn { display: inline-block; padding: 7px 16px; background: #34c759;
            color: #fff; border: none; border-radius: 8px; font-size: 13px;
            font-weight: 600; cursor: pointer; text-decoration: none; }
  .dl-btn:hover { background: #28a745; }
  .badge { display: inline-block; padding: 2px 8px; background: #f5f5f7;
           border-radius: 20px; font-size: 12px; font-weight: 600; color: #3a3a3c; }
  .error-msg { color: #ff3b30; font-size: 14px; margin-top: 10px; }
  .spinner { display: inline-block; width: 14px; height: 14px;
             border: 2px solid rgba(255,255,255,.3); border-top-color: #fff;
             border-radius: 50%; animation: spin .7s linear infinite; margin-right: 8px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .section-title { font-size: 18px; font-weight: 700; margin-bottom: 16px; }
</style>
</head>
<body>
<div class="container">
  <h1>🎬 Reels Bot</h1>
  <p class="subtitle">Paste a YouTube URL and get the best 30-60s clips for Instagram Reels.</p>

  <div class="card" id="form-card">
    <div>
      <label>YouTube URL</label>
      <input id="url" type="url" placeholder="https://youtu.be/..." />
    </div>
    <div>
      <label>Your Niche & Positioning</label>
      <textarea id="niche" rows="3" placeholder="e.g. I'm a fitness coach for busy moms over 40. I help them lose weight without giving up their social life. My angle is: sustainable habits over extreme diets."></textarea>
    </div>
    <div>
      <label>Anthropic API Key</label>
      <div class="key-wrap">
        <input id="api_key" type="password" placeholder="sk-ant-..." />
        <button type="button" class="show-btn" onclick="toggleKey()">Show</button>
      </div>
    </div>
    <div>
      <label>Whisper Model</label>
      <select id="model">
        <option value="tiny">Tiny — fastest (~10 min for 1hr video, less accurate)</option>
        <option value="base" selected>Base — balanced (~30 min for 1hr video)</option>
        <option value="small">Small — more accurate (~60 min for 1hr video)</option>
      </select>
    </div>
    <button class="btn" id="start-btn" onclick="startJob()">Generate Reels</button>
    <div class="error-msg" id="form-error"></div>
  </div>

  <div class="card progress" id="progress-card">
    <p class="section-title">Progress</p>
    <div class="log" id="log"></div>
  </div>

  <div class="results" id="results-card">
    <div class="card">
      <p class="section-title">Your Reels</p>
      <div id="moments-list"></div>
      <button class="btn btn-outline" id="dl-all-btn" onclick="downloadAll()" style="display:none">
        ⬇️  Download All as ZIP
      </button>
    </div>
  </div>
</div>

<script>
let jobId = null;
let logOffset = 0;
let pollInterval = null;

function toggleKey() {
  const inp = document.getElementById('api_key');
  const btn = document.querySelector('.show-btn');
  if (inp.type === 'password') { inp.type = 'text'; btn.textContent = 'Hide'; }
  else { inp.type = 'password'; btn.textContent = 'Show'; }
}

async function startJob() {
  const url = document.getElementById('url').value.trim();
  const api_key = document.getElementById('api_key').value.trim();
  const model = document.getElementById('model').value;
  const niche = document.getElementById('niche').value.trim();
  const errEl = document.getElementById('form-error');
  errEl.textContent = '';

  if (!url) { errEl.textContent = 'Please enter a YouTube URL.'; return; }
  if (!niche) { errEl.textContent = 'Please describe your niche.'; return; }
  if (!api_key) { errEl.textContent = 'Please enter your Anthropic API key.'; return; }

  const btn = document.getElementById('start-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Starting...';

  try {
    const res = await fetch('/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, api_key, model, niche }),
    });
    const data = await res.json();

    if (data.error) {
      errEl.textContent = data.error;
      btn.disabled = false;
      btn.textContent = 'Generate Reels';
      return;
    }

    jobId = data.job_id;
    logOffset = 0;
    document.getElementById('progress-card').style.display = 'block';
    pollInterval = setInterval(doPoll, 2000);
    doPoll();
  } catch (e) {
    errEl.textContent = 'Connection error: ' + e.message;
    btn.disabled = false;
    btn.textContent = 'Generate Reels';
  }
}

async function doPoll() {
  if (!jobId) return;
  const res = await fetch(`/poll/${jobId}?after=${logOffset}`);
  const data = await res.json();

  const logEl = document.getElementById('log');
  for (const line of data.log) {
    logEl.textContent += line + '\n';
  }
  logOffset += data.log.length;
  logEl.scrollTop = logEl.scrollHeight;

  if (data.status === 'done') {
    clearInterval(pollInterval);
    showResults(data);
  } else if (data.status === 'error') {
    clearInterval(pollInterval);
    const logEl = document.getElementById('log');
    logEl.textContent += '\\n❌ ' + (data.error || 'Unknown error');
    logEl.scrollTop = logEl.scrollHeight;
    const btn = document.getElementById('start-btn');
    btn.disabled = false;
    btn.textContent = 'Try Again';
  }
}

function showResults(data) {
  document.getElementById('results-card').style.display = 'block';
  const list = document.getElementById('moments-list');
  list.innerHTML = '';

  data.moments.forEach((m, i) => {
    const filename = data.files[i];
    const div = document.createElement('div');
    div.className = 'moment';
    div.innerHTML = `
      <div class="moment-title">${i+1}. ${m.title} <span class="badge">${m.rating}/10</span></div>
      <div class="moment-meta">${m.start} → ${m.end}</div>
      <div class="moment-hook">"${m.hook}"</div>
      ${filename
        ? `<a class="dl-btn" href="/download/${jobId}/${filename}" download>⬇️ Download</a>`
        : `<span style="color:#ff3b30;font-size:13px">Failed to cut</span>`
      }
    `;
    list.appendChild(div);
  });

  if (data.files.length > 1) {
    document.getElementById('dl-all-btn').style.display = 'block';
  }

  const btn = document.getElementById('start-btn');
  btn.disabled = false;
  btn.textContent = 'Generate Reels from Another Video';
}

function downloadAll() {
  window.location.href = `/download_all/${jobId}`;
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    is_local = os.environ.get("RAILWAY_ENVIRONMENT") is None
    if is_local:
        print(f"🎬 Reels Bot starting at http://localhost:{port}")
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    app.run(host="0.0.0.0", port=port, debug=False)
