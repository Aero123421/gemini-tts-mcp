"""FastMCP Server for Gemini 3.8 TTS (ChatGPT / Claude / Gemini OAuth 2.1 compatible)."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Annotated, Any, AsyncIterator, Literal
from urllib.parse import quote

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .audio import (
    SceneAudio,
    build_video_assets,
    build_zip_from_assets,
    inspect_wav,
    resample_wav_if_needed,
    sanitize_slug,
)
from .auth import authorize_user_claims, build_auth_provider
from .config import MODEL_MAP, Settings
from .guides import GUIDES, SERVER_INSTRUCTIONS
from .jobs import JobArtifacts, JobManager, JobState
from .rate_limit import SlidingWindowRateLimiter
from .tts import GeminiTTSClient, TurnSpec
from .usage import UsageTracker

logger = logging.getLogger(__name__)

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_SCENE_FILE_RE = re.compile(r"^[0-9]{3}_[a-z0-9_]+\.wav$")

_COMMON_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
_PLAYER_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; "
    "media-src 'self'; frame-ancestors 'none'; base-uri 'none'"
)


class SpeechSegment(BaseModel):
    """One speech segment (a single line, a dialogue turn, or a video scene)."""

    text: str = Field(
        ...,
        description=(
            "Verbatim transcript to read aloud. Do NOT put stage directions like '(cheerfully)' "
            "here — Gemini 3.8 reads text literally. English inline tags like <short pause> or "
            "<laugh> are allowed."
        ),
    )
    id: str | None = Field(
        default=None,
        max_length=64,
        description="Optional short ASCII scene slug (e.g. 'intro', 'demo') used for split filenames.",
    )
    speaker: str = Field(
        default="narrator",
        max_length=64,
        description=(
            "Speaker label. For multi-speaker dialogue (output_mode='merged' with 2+ segments), "
            "each speaker must have a unique label consistently mapped to one voice."
        ),
    )
    voice: str = Field(
        default="Kore",
        max_length=128,
        description="Prebuilt voice (e.g. Kore, Puck, Charon, Zephyr, Sulafat) or custom voice_... ID.",
    )
    style: str = Field(
        default="",
        max_length=500,
        description="Turn-level delivery instruction (e.g. 'calm and clear, moderate pace').",
    )


def _as_json_text(payload: dict[str, Any]) -> str:
    """Compact JSON TextContent for 100% Gemini/Claude/ChatGPT compatibility & low token usage."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _validate_job_id(job_id: str) -> bool:
    return bool(_JOB_ID_RE.fullmatch(job_id))


def create_server(settings: Settings | None = None) -> FastMCP:
    cfg = settings or Settings.from_env()
    auth_provider = build_auth_provider(cfg)

    tts_client = GeminiTTSClient(
        api_key=cfg.gemini_api_key,
        max_concurrency=cfg.tts_concurrency,
        timeout_sec=cfg.tts_timeout_sec,
    )
    rate_limiter = SlidingWindowRateLimiter(
        warn_per_min=cfg.rate_warn_per_min,
        block_per_min=cfg.rate_block_per_min,
        warn_per_hour=cfg.rate_warn_per_hour,
        block_per_hour=cfg.rate_block_per_hour,
    )
    usage_tracker = UsageTracker(
        monthly_budget_usd=cfg.monthly_budget_usd,
        gcp_project=cfg.gcp_project,
        database=cfg.firestore_database,
        collection_prefix=cfg.firestore_prefix,
    )
    job_manager = JobManager(
        max_cached_jobs=cfg.max_cached_jobs,
        max_cache_bytes=cfg.max_cache_bytes,
        ttl_sec=cfg.job_ttl_sec,
    )
    zip_semaphore = asyncio.Semaphore(2)

    @asynccontextmanager
    async def _lifespan(_server: FastMCP) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await job_manager.shutdown()
            await usage_tracker.close()

    mcp = FastMCP(
        name="Gemini TTS MCP",
        instructions=SERVER_INSTRUCTIONS,
        auth=auth_provider,
        lifespan=_lifespan,
    )

    def _require_allowed_user() -> str:
        """Fail-closed user authorization requiring a verified email claim."""
        token = get_access_token()
        if token is None:
            raise PermissionError("Authentication required.")
        claims = getattr(token, "claims", None)
        return authorize_user_claims(
            claims=claims if isinstance(claims, dict) else None,
            allowed_emails=cfg.allowed_emails,
            allow_any_user=cfg.allow_any_user,
        )

    async def _safe_record_usage(
        state: JobState,
        model: str,
        input_chars: int,
        duration_sec: float,
        requests: int,
        user_email: str,
    ) -> dict[str, Any] | None:
        """Record usage without failing the completed audio job if Firestore has a transient error."""
        try:
            return await asyncio.wait_for(
                usage_tracker.record(
                    model=model,
                    input_chars=input_chars,
                    duration_sec=duration_sec,
                    requests=requests,
                    user_email=user_email,
                ),
                timeout=5.0,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to record monthly usage for job %s", state.job_id)
            state.warnings.append(
                "Usage recording temporarily failed; audio was generated successfully."
            )
            return None

    # --- HTTP Endpoints (Health, Player, WAV, On-demand ZIP) ---

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> Response:
        return JSONResponse({"status": "ok"}, headers=_COMMON_SECURITY_HEADERS)

    @mcp.custom_route("/dl/{job_id}.zip", methods=["GET"])
    async def download_zip(request: Request) -> Response:
        job_id = request.path_params["job_id"]
        if not _validate_job_id(job_id):
            return Response("Invalid job ID.", status_code=400, headers=_COMMON_SECURITY_HEADERS)
        job = job_manager.get(job_id)
        if not job or not job.artifacts or not job.artifacts.scene_wavs or not job.artifacts.manifest:
            return Response("ZIP expired or not found.", status_code=404, headers=_COMMON_SECURITY_HEADERS)

        async with zip_semaphore:
            zip_bytes = await asyncio.to_thread(
                build_zip_from_assets,
                job.artifacts.scene_wavs,
                job.artifacts.primary_wav,
                job.artifacts.srt_text or "",
                job.artifacts.manifest,
                job.artifacts.bundle_name,
            )
        return Response(
            content=zip_bytes,
            media_type="application/zip",
            headers={
                **_COMMON_SECURITY_HEADERS,
                "Cross-Origin-Resource-Policy": "same-origin",
                "Content-Disposition": f'attachment; filename="narration-{job_id[:8]}.zip"',
            },
        )

    @mcp.custom_route("/audio/{job_id}/combined.wav", methods=["GET"])
    async def download_combined_wav(request: Request) -> Response:
        job_id = request.path_params["job_id"]
        if not _validate_job_id(job_id):
            return Response("Invalid job ID.", status_code=400, headers=_COMMON_SECURITY_HEADERS)
        job = job_manager.get(job_id)
        if not job or not job.artifacts:
            return Response("Audio expired or not found.", status_code=404, headers=_COMMON_SECURITY_HEADERS)
        return Response(
            content=job.artifacts.primary_wav,
            media_type="audio/wav",
            headers={
                **_COMMON_SECURITY_HEADERS,
                "Cross-Origin-Resource-Policy": "same-origin",
                "Content-Disposition": f'inline; filename="gemini-tts-{job_id[:8]}.wav"',
            },
        )

    @mcp.custom_route("/audio/{job_id}/scenes/{scene_file}", methods=["GET"])
    async def download_scene_wav(request: Request) -> Response:
        job_id = request.path_params["job_id"]
        scene_file = request.path_params["scene_file"]
        if not _validate_job_id(job_id) or not _SCENE_FILE_RE.fullmatch(scene_file):
            return Response("Invalid request.", status_code=400, headers=_COMMON_SECURITY_HEADERS)
        job = job_manager.get(job_id)
        if not job or not job.artifacts:
            return Response("Audio expired or not found.", status_code=404, headers=_COMMON_SECURITY_HEADERS)
        wav_bytes = job.artifacts.scene_wavs.get(f"scenes/{scene_file}")
        if not wav_bytes:
            return Response("Scene file not found.", status_code=404, headers=_COMMON_SECURITY_HEADERS)
        return Response(
            content=wav_bytes,
            media_type="audio/wav",
            headers={
                **_COMMON_SECURITY_HEADERS,
                "Cross-Origin-Resource-Policy": "same-origin",
                "Content-Disposition": f'inline; filename="{scene_file}"',
            },
        )

    @mcp.custom_route("/play/{job_id}", methods=["GET"])
    async def serve_player(request: Request) -> Response:
        job_id = request.path_params["job_id"]
        if not _validate_job_id(job_id):
            return Response("Invalid job ID.", status_code=400, headers=_COMMON_SECURITY_HEADERS)
        job = job_manager.get(job_id)
        if not job or not job.artifacts:
            return HTMLResponse(
                "<h1>Expired</h1><p>This audio clip is no longer in Cloud Run memory.</p>",
                status_code=404,
                headers={
                    **_COMMON_SECURITY_HEADERS,
                    "Content-Security-Policy": _PLAYER_CSP,
                    "X-Frame-Options": "DENY",
                },
            )
        art = job.artifacts
        safe_title = html.escape(art.title)
        safe_jid = quote(job_id, safe="")
        wav_url = f"/audio/{safe_jid}/combined.wav"
        zip_btn = (
            f'<a class="btn" href="/dl/{safe_jid}.zip" download="narration-{job_id[:8]}.zip">'
            "動画編集用ZIP（シーン別WAV＋SRT＋manifest）をダウンロード</a>"
            if art.scene_wavs and art.manifest
            else ""
        )
        scene_rows = ""
        if art.manifest and art.manifest.get("scenes"):
            items = []
            for sc in art.manifest["scenes"]:
                fname = sc["file"].split("/")[-1]
                items.append(
                    f"<li><strong>{html.escape(sc['id'])}</strong> "
                    f"({sc['duration_sec']}s / {sc['start_tc']}–{sc['end_tc']}) "
                    f'<a href="/audio/{safe_jid}/scenes/{quote(fname, safe="")}">WAV</a></li>'
                )
            scene_rows = f"<h3>Scenes</h3><ul>{''.join(items)}</ul>"

        page = f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Gemini TTS Player</title>
  <style>
    body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 640px; margin: 40px auto; padding: 0 16px; color: #111; }}
    audio {{ width: 100%; margin: 16px 0; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 20px; }}
    a.btn {{ display: inline-block; padding: 9px 14px; border: 1px solid #222; border-radius: 6px; text-decoration: none; color: #111; font-size: 14px; }}
    a.btn:hover {{ background: #f2f2f2; }}
    ul {{ padding-left: 20px; line-height: 1.7; font-size: 14px; }}
  </style>
</head>
<body>
  <h2>{safe_title}</h2>
  <audio controls autoplay src="{wav_url}"></audio>
  <div class="actions">
    <a class="btn" href="{wav_url}" download="gemini-tts-{job_id[:8]}.wav">WAVをダウンロード</a>
    {zip_btn}
  </div>
  {scene_rows}
</body>
</html>"""
        return HTMLResponse(
            page,
            headers={
                **_COMMON_SECURITY_HEADERS,
                "Content-Security-Policy": _PLAYER_CSP,
                "X-Frame-Options": "DENY",
            },
        )

    # --- MCP Tools (5 tools) ---

    @mcp.tool
    async def generate_speech(
        segments: Annotated[
            list[SpeechSegment],
            Field(description="1+ speech segments. Pass 1 item for single speech, or multiple for dialogue / video scenes."),
        ],
        output_mode: Annotated[
            Literal["merged", "split"],
            Field(
                description=(
                    "'merged': single audio clip (single speech or multi-speaker dialogue). "
                    "'split': scene-by-scene video VO bundle (scenes/*.wav + combined.wav + subtitles.srt + manifest.json in a ZIP)."
                )
            ),
        ] = "merged",
        quality: Annotated[
            Literal["fast", "studio"],
            Field(description="'fast' = gemini-3.8-flash-lite-tts (default), 'studio' = gemini-3.8-flash-tts."),
        ] = "fast",
        sample_rate: Annotated[
            Literal[24000, 48000],
            Field(description="Output WAV sample rate. 24000 (native) or 48000 (video editing standard)."),
        ] = 24000,
        gap_sec: Annotated[
            float,
            Field(ge=0.0, le=5.0, description="Silence gap in seconds between scenes in combined.wav (split mode)."),
        ] = 0.4,
        bundle_name: Annotated[
            str,
            Field(max_length=64, description="Short ASCII folder name inside the ZIP (e.g. 'product_demo')."),
        ] = "narration",
    ) -> str:
        """Generate speech audio from text (single speech, multi-speaker dialogue, or video VO batch).

        Returns a JSON string containing exact `duration_sec`, `player_url`, `wav_url`,
        and (in split mode) `zip_url`, `combined_wav_url`, plus per-scene timings. If a batch takes
        longer than ~25s, returns `status='working'` and `job_id` — call `get_job(job_id)` to wait.
        """
        user_email = _require_allowed_user()

        if not segments:
            raise ValueError("`segments` must contain at least one segment.")
        if len(segments) > cfg.max_segments:
            raise ValueError(
                f"Too many segments ({len(segments)} > max {cfg.max_segments}). "
                "Split into smaller batches and call generate_speech again."
            )

        total_chars = 0
        for idx, seg in enumerate(segments, start=1):
            tlen = len(seg.text.strip())
            if tlen == 0:
                raise ValueError(f"Segment #{idx} text must not be empty.")
            if tlen > cfg.max_chars_per_segment:
                raise ValueError(
                    f"Segment #{idx} is too long ({tlen} chars > max {cfg.max_chars_per_segment}). "
                    "Split the text into shorter segments."
                )
            total_chars += tlen

        if total_chars > cfg.max_total_chars:
            raise ValueError(
                f"Total text length ({total_chars} chars) exceeds max ({cfg.max_total_chars}). "
                "Split into multiple calls."
            )

        # Validate speaker -> voice consistency in merged multi-speaker dialogue
        if output_mode == "merged" and len(segments) > 1:
            speaker_voice_map: dict[str, str] = {}
            for idx, seg in enumerate(segments, start=1):
                spk = seg.speaker.strip() or "narrator"
                prev_voice = speaker_voice_map.get(spk)
                if prev_voice is not None and prev_voice != seg.voice:
                    raise ValueError(
                        f"Segment #{idx} assigns voice '{seg.voice}' to speaker '{spk}', "
                        f"which already uses '{prev_voice}'. "
                        "Give each speaker a unique `speaker` name for multi-speaker dialogue."
                    )
                speaker_voice_map[spk] = seg.voice

        api_calls_needed = len(segments) if output_mode == "split" else 1
        rl = rate_limiter.check_and_record(user_email, units=api_calls_needed)
        if not rl.allowed:
            return _as_json_text(
                {
                    "status": "rate_limited",
                    "error": rl.block_reason,
                    "count_last_min": rl.count_last_min,
                    "count_last_hour": rl.count_last_hour,
                }
            )

        model = MODEL_MAP[quality]
        title = (segments[0].text.strip().splitlines()[0] or "Gemini TTS")[:80]

        async def _run_job(state: JobState) -> None:
            if output_mode == "merged":
                if len(segments) == 1:
                    s = segments[0]
                    raw_wav = await tts_client.synthesize_single(
                        text=s.text, model=model, voice=s.voice, style=s.style
                    )
                else:
                    turns = [
                        TurnSpec(text=s.text, speaker=s.speaker, voice=s.voice, style=s.style)
                        for s in segments
                    ]
                    raw_wav = await tts_client.synthesize_dialogue(turns=turns, model=model)

                wav_bytes = await asyncio.to_thread(resample_wav_if_needed, raw_wav, sample_rate)
                info = await asyncio.to_thread(inspect_wav, wav_bytes)
                state.completed_scenes = len(segments)

                job_manager.set_artifacts(
                    state,
                    JobArtifacts(primary_wav=wav_bytes, title=title),
                )
                usage_rec = await _safe_record_usage(
                    state=state,
                    model=model,
                    input_chars=total_chars,
                    duration_sec=info.duration_sec,
                    requests=1,
                    user_email=user_email,
                )
                state.result_summary = {
                    "status": "completed",
                    "job_id": state.job_id,
                    "output_mode": "merged",
                    "model": model,
                    "sample_rate": info.sample_rate,
                    "duration_sec": round(info.duration_sec, 3),
                    "bytes": len(wav_bytes),
                    "player_url": f"{cfg.base_url}/play/{state.job_id}",
                    "wav_url": f"{cfg.base_url}/audio/{state.job_id}/combined.wav",
                    "usage": usage_rec,
                    "warnings": state.warnings,
                    "note": "Stored in Cloud Run memory; expires when idle (~15 min) or on scale-to-zero.",
                }
                return

            # Split mode: track completed scenes even on partial failure so spent cost is recorded
            succeeded: list[tuple[int, SceneAudio]] = []

            async def _gen_one(i: int, seg: SpeechSegment) -> None:
                raw_wav = await tts_client.synthesize_single(
                    text=seg.text, model=model, voice=seg.voice, style=seg.style
                )
                wav_bytes = await asyncio.to_thread(resample_wav_if_needed, raw_wav, sample_rate)
                info = await asyncio.to_thread(inspect_wav, wav_bytes)
                slug = sanitize_slug(seg.id, f"scene_{i:03d}")
                fname = f"scenes/{i:03d}_{slug}.wav"
                sc = SceneAudio(
                    index=i,
                    slug=f"{i:03d}_{slug}",
                    filename=fname,
                    speaker=seg.speaker,
                    voice=seg.voice,
                    style=seg.style,
                    text=seg.text,
                    wav_bytes=wav_bytes,
                    sample_rate=info.sample_rate,
                    channels=info.channels,
                    bit_depth=info.sample_width * 8,
                    sample_frames=info.num_frames,
                    duration_sec=info.duration_sec,
                )
                succeeded.append((i, sc))
                state.completed_scenes += 1

            tasks = [
                asyncio.create_task(_gen_one(i, seg))
                for i, seg in enumerate(segments, start=1)
            ]
            usage_recorded = False
            try:
                await asyncio.gather(*tasks)
            except BaseException:
                for t in tasks:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if succeeded:
                    partial_chars = sum(len(sc.text.strip()) for _, sc in succeeded)
                    partial_sec = sum(sc.duration_sec for _, sc in succeeded)
                    await _safe_record_usage(
                        state=state,
                        model=model,
                        input_chars=partial_chars,
                        duration_sec=partial_sec,
                        requests=len(succeeded),
                        user_email=user_email,
                    )
                    usage_recorded = True
                raise

            scene_audios = [sc for _, sc in sorted(succeeded, key=lambda x: x[0])]
            combined_wav, srt_text, manifest = await asyncio.to_thread(
                build_video_assets,
                scene_audios,
                model,
                gap_sec,
            )

            job_manager.set_artifacts(
                state,
                JobArtifacts(
                    primary_wav=combined_wav,
                    title=f"{bundle_name} ({len(scene_audios)} scenes)",
                    bundle_name=bundle_name,
                    srt_text=srt_text,
                    manifest=manifest,
                    scene_wavs={s.filename: s.wav_bytes for s in scene_audios},
                ),
            )

            usage_rec = None
            if not usage_recorded:
                total_speech_sec = sum(s.duration_sec for s in scene_audios)
                usage_rec = await _safe_record_usage(
                    state=state,
                    model=model,
                    input_chars=total_chars,
                    duration_sec=total_speech_sec,
                    requests=len(scene_audios),
                    user_email=user_email,
                )

            state.result_summary = {
                "status": "completed",
                "job_id": state.job_id,
                "output_mode": "split",
                "model": model,
                "sample_rate": manifest["audio"]["sample_rate"],
                "scene_count": len(scene_audios),
                "total_duration_sec": manifest["total_duration_sec"],
                "player_url": f"{cfg.base_url}/play/{state.job_id}",
                "zip_url": f"{cfg.base_url}/dl/{state.job_id}.zip",
                "combined_wav_url": f"{cfg.base_url}/audio/{state.job_id}/combined.wav",
                "zip_contents": [
                    "scenes/*.wav",
                    "combined.wav",
                    "subtitles.srt",
                    "manifest.json",
                ],
                "scenes": [
                    {
                        "id": sc["id"],
                        "duration_sec": sc["duration_sec"],
                        "start_sec": sc["start_sec"],
                        "end_sec": sc["end_sec"],
                        "wav_url": f"{cfg.base_url}/audio/{state.job_id}/{sc['file']}",
                    }
                    for sc in manifest["scenes"]
                ],
                "usage": usage_rec,
                "warnings": state.warnings,
                "note": "Stored in Cloud Run memory; download zip_url promptly before scale-to-zero.",
            }

        job = job_manager.create_job(
            owner_email=user_email,
            model=model,
            output_mode=output_mode,
            total_scenes=len(segments),
            warnings=rl.warnings,
            runner=_run_job,
        )
        result = await job_manager.wait_or_progress(job, timeout_sec=cfg.sync_wait_sec)
        return _as_json_text(result)

    @mcp.tool
    async def get_job(
        job_id: Annotated[str, Field(description="The job_id returned when generate_speech was still working.")],
    ) -> str:
        """Long-poll an in-progress speech generation job (waits up to ~25s before returning status)."""
        user_email = _require_allowed_user()
        if not _validate_job_id(job_id):
            return _as_json_text({"status": "not_found", "job_id": job_id, "error": "Invalid job_id format."})
        job = job_manager.get(job_id)
        if not job:
            return _as_json_text(
                {
                    "status": "not_found",
                    "job_id": job_id,
                    "error": "Job not found or expired from memory.",
                }
            )
        if job.owner_email != user_email:
            raise PermissionError("You do not own this job.")

        result = await job_manager.wait_or_progress(job, timeout_sec=cfg.poll_wait_sec)
        return _as_json_text(result)

    @mcp.tool
    async def list_voices(
        query: Annotated[str, Field(max_length=100, description="Optional keyword filter (e.g. 'warm', 'narrator').")] = "",
        gender: Annotated[Literal["", "female", "male", "neutral"], Field(description="Optional gender filter.")] = "",
        language_code: Annotated[str, Field(max_length=32, description="Optional BCP-47 language code (e.g. 'ja-JP', 'en-US').")] = "",
        limit: Annotated[int, Field(ge=1, le=50, description="Max voices to return.")] = 20,
    ) -> str:
        """List available Gemini TTS voices (your custom AI Studio voices appear first, followed by prebuilt voices)."""
        _require_allowed_user()
        voices = await tts_client.list_voices(
            query=query,
            gender=gender,
            language_code=language_code,
            page_size=limit,
        )
        return _as_json_text({"count": len(voices), "voices": voices})

    @mcp.tool
    async def get_usage(
        month: Annotated[
            str,
            Field(
                pattern=r"^(\d{4}-(0[1-9]|1[0-2]))?$",
                description="Optional YYYY-MM (UTC). Leave empty for the current month.",
            ),
        ] = "",
    ) -> str:
        """Return real-time monthly TTS usage (requests, audio minutes, estimated USD cost vs budget)."""
        _require_allowed_user()
        summary = await usage_tracker.get_monthly_summary(month=month.strip() or None)
        return _as_json_text(summary)

    @mcp.tool
    async def get_guide(
        topic: Annotated[
            Literal["overview", "video", "tags", "voices", "limits"],
            Field(description="Guide topic to read."),
        ] = "overview",
    ) -> str:
        """Read detailed AI usage instructions (video VO workflow, inline tags, voices, limits)."""
        _require_allowed_user()
        return GUIDES.get(topic, GUIDES["overview"])

    return mcp


def main() -> None:
    cfg = Settings.from_env()
    mcp = create_server(cfg)
    mcp.run(transport="http", host="0.0.0.0", port=cfg.port)  # noqa: S104  # Required by Cloud Run


if __name__ == "__main__":
    main()
