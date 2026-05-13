#!/usr/bin/env python3
"""
Reels Bot
Extracts the best 30-60s reels from a YouTube video using Whisper + Claude.

Usage:
    python main.py <youtube_url>
    python main.py <youtube_url> --output ./my_reels
    python main.py <youtube_url> --model medium   # whisper model size
"""

import os
import sys
import json
import re
import subprocess
import tempfile
import argparse
from pathlib import Path

# ── Helpers ────────────────────────────────────────────────────────────────

def fmt_ts(seconds: float) -> str:
    """Format seconds as MM:SS"""
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"



def safe_filename(title: str, max_len: int = 50) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_")
    return cleaned[:max_len]


# ── Step 1: Download ───────────────────────────────────────────────────────

def download_video(url: str, output_dir: str) -> str:
    print("⬇️  Downloading video...")
    out_template = os.path.join(output_dir, "%(title).80s.%(ext)s")

    result = subprocess.run(
        [
            "yt-dlp",
            "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
            "-o", out_template,
            url,
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed:\n{result.stderr}")

    mp4_files = list(Path(output_dir).glob("*.mp4"))
    if not mp4_files:
        raise RuntimeError("No MP4 file found after download.")

    video_path = str(sorted(mp4_files, key=lambda p: p.stat().st_mtime)[-1])
    print(f"   ✅ {Path(video_path).name}\n")
    return video_path


# ── Step 2: Transcribe ─────────────────────────────────────────────────────

def transcribe_video(video_path: str, model_size: str = "base") -> list[dict]:
    print(f"🎙️  Transcribing with Whisper ({model_size})...")

    try:
        import whisper
    except ImportError:
        raise RuntimeError(
            "whisper not installed. Run:  pip install openai-whisper"
        )

    model = whisper.load_model(model_size)
    result = model.transcribe(video_path, word_timestamps=False, verbose=False)

    segments = [
        {"start": s["start"], "end": s["end"], "text": s["text"].strip()}
        for s in result["segments"]
    ]

    total = result["segments"][-1]["end"] if result["segments"] else 0
    print(f"   ✅ {len(segments)} segments — {fmt_ts(total)} total\n")
    return segments


# ── Step 3: Find reel moments ──────────────────────────────────────────────

def find_reel_moments(segments: list[dict]) -> list[dict]:
    print("🤖 Analyzing transcript with Claude...")

    try:
        from anthropic import Anthropic
    except ImportError:
        raise RuntimeError(
            "anthropic not installed. Run:  pip install anthropic"
        )

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable not set.\n"
            "Export it:  export ANTHROPIC_API_KEY=sk-ant-..."
        )

    client = Anthropic()

    transcript_text = "\n".join(
        f"[{fmt_ts(s['start'])}] {s['text']}" for s in segments
    )

    prompt = f"""You are an expert content strategist specialized in Instagram Reels for coaches and thought leaders. You analyze YouTube transcripts and extract the absolute best moments for Reels of 15-60 seconds.

**My positioning:** Subconscious mind coach for high-performing entrepreneurs. I help entrepreneurs who have external success but are stuck internally: limiting beliefs, self-sabotage, perfectionism, need for control, making themselves small in relationships.

**My core angles:**
- "Rewiring the minds of the next wave of millionaires"
- "This is not just mindset. It's deeper than that"
- "The ceiling is always the mind"
- Conscious mind / subconscious mind / nervous system framework
- PBB model (Psychological Basic Needs, from Self-Determination Theory)

**What makes a strong Reel clip:**
1. Hook in the first 3 seconds: pattern interrupt, contrarian statement, or a question that lands instantly
2. One core idea: one clear thought, no rambling explanation
3. Self-contained: no "as I just said" or missing context
4. Emotional or intellectual punch: aha-moment, recognition, or reframe
5. Quotable line: at least one sentence that works as caption or quote story
6. Length 20-50 seconds ideal, max 60
7. Provocative edge: the clip should grip attention, go against the grain, challenge mainstream advice, or make people stop and rethink something they took for granted. Contrarian, sharp, even uncomfortable is good. Safe and agreeable is not.

**What to avoid:**
- Generic mindset statements (think positive, believe in yourself, etc.)
- Long intros or disclaimers
- Clips where the power was in visual context (whiteboard, slides, etc.)
- Spiritual buzzwords without substance
- Anything that sounds like every other coach on Instagram

Transcript (timestamps are MM:SS):
{transcript_text}

Give me the top 5-8 clips, ranked strongest to weakest. Be strict: better 5 truly strong clips than 10 mediocre ones. If a clip scores below 7/10, leave it out. Prioritize clips that make the viewer stop scrolling and think "wait, what?"

Return ONLY a valid JSON array, no other text or markdown:
[
  {{
    "start": <start_in_seconds as number>,
    "end": <end_in_seconds as number>,
    "title": "<short catchy title, max 8 words>",
    "hook": "<exact first sentence that hooks viewers>",
    "core_idea": "<one sentence summary>",
    "quotable_line": "<exact quote for caption or story>",
    "audience_fit": "<which pain point or desire this hits>",
    "contrarian_angle": "<what mainstream belief this challenges>",
    "caption_angle": "<1 sentence suggested caption angle>",
    "broll": "<'Talking-head is strong enough: keep raw' OR 'Add b-roll overlay: [specific suggestion]'>",
    "rating": <number 7-10>,
    "rating_reason": "<short reason>"
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

    # Strip markdown code fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    json_match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not json_match:
        raise RuntimeError(f"Could not parse Claude response:\n{raw}")

    moments = json.loads(json_match.group())

    print(f"   ✅ {len(moments)} moments identified\n")
    return moments


# ── Step 4: Create reels ───────────────────────────────────────────────────


def get_video_dimensions(video_path: str) -> tuple[int, int]:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", video_path],
        capture_output=True,
        text=True,
    )
    info = json.loads(result.stdout)
    vs = next(s for s in info["streams"] if s["codec_type"] == "video")
    return int(vs["width"]), int(vs["height"])


def create_reel(
    video_path: str,
    start: float,
    end: float,
    output_path: str,
):
    orig_w, orig_h = get_video_dimensions(video_path)

    # Target: 9:16 vertical (1080×1920)
    tw, th = 1080, 1920

    # Scale so the shorter side fills the target, then center-crop
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
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "22",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr[-600:]}")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Extract reels from a YouTube video")
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument("--output", "-o", default="./reels_output", help="Output directory")
    parser.add_argument(
        "--model", "-m",
        default="base",
        choices=["tiny", "base", "small", "medium", "large"],
        help="Whisper model size (larger = more accurate but slower)",
    )
    args = parser.parse_args()

    output_dir = os.path.abspath(args.output)
    os.makedirs(output_dir, exist_ok=True)

    print(f"\n{'─'*60}")
    print(f"🎬  Reels Bot")
    print(f"{'─'*60}")
    print(f"URL     : {args.url}")
    print(f"Output  : {output_dir}")
    print(f"Whisper : {args.model}")
    print(f"{'─'*60}\n")

    with tempfile.TemporaryDirectory() as tmp_dir:
        # 1. Download
        video_path = download_video(args.url, tmp_dir)

        # 2. Transcribe
        segments = transcribe_video(video_path, model_size=args.model)

        # 3. Find moments
        moments = find_reel_moments(segments)

        # 4. Print summary
        print("✨  Reel moments:\n")
        for i, m in enumerate(moments, 1):
            duration = m["end"] - m["start"]
            print(f"  {i}. [{m.get('rating','?')}/10] {m['title']}")
            print(f"     ⏱  {fmt_ts(m['start'])} → {fmt_ts(m['end'])} ({duration:.0f}s)")
            print(f"     🪝  \"{m['hook']}\"")
            print(f"     💡  {m.get('core_idea', m.get('reason',''))}")
            print(f"     💬  \"{m.get('quotable_line','')}\"")
            print(f"     🎯  {m.get('audience_fit','')}")
            print(f"     ⚡  Contrarian: {m.get('contrarian_angle','')}")
            print(f"     📝  Caption: {m.get('caption_angle','')}")
            print(f"     🎬  {m.get('broll','')}\n")

        # 5. Cut reels
        print("✂️   Cutting reels...\n")
        created = []

        for i, m in enumerate(moments, 1):
            safe = safe_filename(m["title"])
            out_path = os.path.join(output_dir, f"reel_{i:02d}_{safe}.mp4")
            print(f"  [{i}/{len(moments)}] {m['title']} ...")

            try:
                create_reel(video_path, m["start"], m["end"], out_path)
                size_mb = os.path.getsize(out_path) / (1024 * 1024)
                print(f"  ✅  {Path(out_path).name}  ({size_mb:.1f} MB)\n")
                created.append(out_path)
            except Exception as e:
                print(f"  ❌  Failed: {e}\n")

    print(f"{'─'*60}")
    print(f"🎉  Done!  {len(created)}/{len(moments)} reels created")
    print(f"📁  {output_dir}/")
    print(f"{'─'*60}\n")


if __name__ == "__main__":
    main()
