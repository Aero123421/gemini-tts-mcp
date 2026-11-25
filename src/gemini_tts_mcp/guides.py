"""AI-facing MCP server instructions and progressive-disclosure guide content."""

from __future__ import annotations

SERVER_INSTRUCTIONS = """\
Gemini 3.8 TTS MCP Server — generate single-speaker speech, multi-speaker dialogue, \
or scene-by-scene video narration packages (WAV + combined WAV + UTF-8 SRT + manifest.json).

Core rules:
1. Default to quality="fast" (gemini-3.8-flash-lite-tts). Use quality="studio" (gemini-3.8-flash-tts) \
only for final studio narration, complex multi-speaker acting, or difficult pronunciations.
2. NEVER put stage directions inside `text` (Gemini 3.8 reads `text` verbatim). \
Put tone/pace/emotion in `style` (e.g., "calm and friendly", "slightly faster").
3. For momentary vocal events inside `text`, use English angle-bracket tags even for Japanese scripts: \
<short pause>, <laugh>, <sigh>, <gasp>, <breath>, <cough>.
4. For multi-speaker dialogue (`output_mode="merged"` with 2+ segments), give each speaker a distinct \
`speaker` name and consistent `voice`.
5. For video narration / voice-over, pass all scenes in `segments` with `output_mode="split"` \
(and `sample_rate=48000` for NLE compatibility). Use returned `duration_sec` to check pacing; \
if a scene is too long/short, adjust the script or `style` and regenerate only that scene.
6. Long batches may return `status="working"` with a `job_id` before client 60s timeout. \
Call `get_job(job_id)` to wait for completion (long-polling).
7. If `warnings` appear (e.g., high generation frequency), tell the user before doing more retries. \
If blocked by a rate limit, do NOT auto-retry.
8. Always share the returned `player_url` and `zip_url` / `wav_url` / `combined_wav_url` links directly \
with the user (files live in Cloud Run memory and expire when idle, so advise quick download).
9. Call `get_guide(topic)` if you need voice recommendations, inline tags, or video-editing workflow tips.
"""

GUIDES: dict[str, str] = {
    "overview": (
        "# Gemini 3.8 TTS MCP Quick Guide\n\n"
        "- **Tools**: `generate_speech`, `get_job`, `list_voices`, `get_usage`, `get_guide`.\n"
        "- **Models**: `fast` -> `gemini-3.8-flash-lite-tts` (default, low latency & low cost), "
        "`studio` -> `gemini-3.8-flash-tts` (studio fidelity & rich acting).\n"
        "- **Single speech**: Pass 1 item in `segments` with `output_mode='merged'` (returns `wav_url` & `player_url`).\n"
        "- **Dialogue**: Pass multiple turns in `segments` with distinct `speaker` and `voice`, `output_mode='merged'`.\n"
        "- **Video VO**: Pass scenes in `segments` with `id` (e.g. 'intro', 'demo'), `output_mode='split'`, "
        "`sample_rate=48000`, `gap_sec=0.4`. Returns `zip_url`, `combined_wav_url`, and per-scene timings.\n"
        "- **Topics available**: `overview`, `video`, `tags`, `voices`, `limits`."
    ),
    "video": (
        "# Video Narration / Voice-Over Workflow\n\n"
        "1. Split the script into logical scenes (each ideally 5-40 seconds).\n"
        "2. Call `generate_speech` with `output_mode='split'`, `sample_rate=48000` (video standard), "
        "and `gap_sec=0.4`.\n"
        "3. Check `scenes[].duration_sec` in the response (measured from actual WAV frames).\n"
        "4. Gemini 3.8 TTS has no hard 'exact-seconds' parameter; adjust pacing by editing text length "
        "or setting `style` (e.g., 'slightly brisk, clear narration') and regenerating individual scenes.\n"
        "5. Download `zip_url` immediately (in-memory cache). Drop `combined.wav` + `subtitles.srt` "
        "into Premiere Pro / DaVinci Resolve / Final Cut Pro / CapCut, or read `manifest.json` in Manim."
    ),
    "tags": (
        "# Style & Inline Vocal Tags (Gemini 3.8 TTS)\n\n"
        "- **Turn-level `style` field** (NOT inside `text`): emotions, pace, tone, volume.\n"
        "  - Good: `style='warm, clear documentary narrator, moderate pace'`\n"
        "  - Bad: putting `(明るく)` or `Say cheerfully:` inside `text` (it will be spoken aloud!).\n"
        "- **Point-in-time inline tags inside `text`** (use English even for Japanese scripts):\n"
        "  - `<short pause>` — brief natural beat\n"
        "  - `<laugh>` / `<chuckle>` — laughter\n"
        "  - `<sigh>` / `<breath>` / `<gasp>` / `<cough>` — vocal bursts\n"
        "- Japanese fillers like `えーと` or `あのー` should be written directly in Japanese text."
    ),
    "voices": (
        "# Prebuilt Studio Voices & Custom Voices\n\n"
        "Popular prebuilt voices (all auto-detect Japanese and 100+ languages):\n"
        "- **Kore** (Firm, clear — great default narrator)\n"
        "- **Puck** (Upbeat, lively — great for energetic videos / co-host)\n"
        "- **Charon** (Informative, calm — great for tech explainers)\n"
        "- **Zephyr** (Bright, approachable)\n"
        "- **Sulafat** (Warm, gentle)\n"
        "- **Aoede** (Breezy, conversational)\n"
        "- **Gacrux** (Mature, grounded)\n\n"
        "You can also pass any custom `voice_...` ID created in Google AI Studio "
        "(Voice Design / Remixing / Replication). Call `list_voices` to browse available voices."
    ),
    "limits": (
        "# Guardrails, Timeouts & Quotas\n\n"
        "- **Input limits (defaults)**: max 4,000 chars/segment, max 20,000 total chars, max 20 segments/call. "
        "Oversized inputs are rejected with an error — split into multiple calls.\n"
        "- **Single-request audio max**: ~8.5 minutes of Japanese speech (16,384 output tokens).\n"
        "- **Timeout safety**: `generate_speech` waits up to ~25s; if still running, it returns "
        "`status='working'` and `job_id`. Call `get_job(job_id)` to continue waiting.\n"
        "- **Rate limits (defaults)**: soft warning at 10/min or 60/hour; hard stop at 30/min or 120/hour. "
        "Note: `output_mode='split'` consumes 1 unit per segment, while `merged` consumes 1 unit total.\n"
        "- **In-memory storage**: Audio URLs expire after ~15 min or when Cloud Run scales to zero (256MB LRU cap)."
    ),
}
