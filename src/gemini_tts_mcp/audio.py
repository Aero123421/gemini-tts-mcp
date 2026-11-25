"""WAV inspection, 48kHz resampling, concatenation, SRT, manifest.json, and ZIP packaging."""

from __future__ import annotations

import array
import io
import json
import re
import sys
import wave
import zipfile
from dataclasses import dataclass
from typing import Mapping, Sequence

_SLUG_RE = re.compile(r"[^a-z0-9]+")
# Split sentences without breaking decimals like "3.8" or stranding closing quotes like "」"
_SENTENCE_SPLIT_RE = re.compile(
    r"(?<=[。！？!?])(?![」』）\)\]])|(?<=\.)(?!\d)(?![」』）\)\]])"
)


@dataclass(frozen=True)
class WavInfo:
    sample_rate: int
    channels: int
    sample_width: int  # bytes per sample (2 = 16-bit)
    num_frames: int
    duration_sec: float
    pcm_bytes: bytes


@dataclass(frozen=True)
class SceneAudio:
    index: int
    slug: str
    filename: str  # e.g. "scenes/001_intro.wav"
    speaker: str
    voice: str
    style: str
    text: str
    wav_bytes: bytes
    sample_rate: int
    channels: int
    bit_depth: int
    sample_frames: int
    duration_sec: float


def sanitize_slug(raw: str | None, fallback: str) -> str:
    """Convert arbitrary scene id into a safe ASCII lowercase slug."""
    if not raw:
        return fallback
    cleaned = _SLUG_RE.sub("_", raw.strip().lower()).strip("_")
    return cleaned[:40] or fallback


def inspect_wav(wav_bytes: bytes) -> WavInfo:
    """Parse WAV bytes and return exact frame count and duration."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        sr = wf.getframerate()
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        nframes = wf.getnframes()
        pcm = wf.readframes(nframes)
    expected_bytes = nframes * ch * sw
    if len(pcm) != expected_bytes:
        raise ValueError(f"Truncated WAV: expected {expected_bytes} PCM bytes, received {len(pcm)}.")
    duration = nframes / sr if sr > 0 else 0.0
    return WavInfo(
        sample_rate=sr,
        channels=ch,
        sample_width=sw,
        num_frames=nframes,
        duration_sec=duration,
        pcm_bytes=pcm,
    )


def _encode_wav(pcm_bytes: bytes, sample_rate: int, channels: int, sample_width: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def resample_wav_if_needed(wav_bytes: bytes, target_sample_rate: int) -> bytes:
    """Resample 16-bit mono PCM WAV to target_sample_rate (e.g. 24000 -> 48000).

    Run inside `asyncio.to_thread` from async handlers to avoid blocking the event loop.
    """
    info = inspect_wav(wav_bytes)
    if info.sample_rate == target_sample_rate or info.num_frames == 0:
        return wav_bytes
    if info.sample_width != 2 or info.channels != 1:
        raise ValueError(
            f"Unsupported WAV format for resampling: {info.sample_width * 8}-bit, "
            f"{info.channels}ch (expected 16-bit mono)."
        )

    samples = array.array("h")
    samples.frombytes(info.pcm_bytes)
    if sys.byteorder != "little":
        samples.byteswap()

    n = len(samples)
    ratio = target_sample_rate / info.sample_rate
    out_len = int(round(n * ratio))
    out = array.array("h", [0]) * out_len

    if target_sample_rate == info.sample_rate * 2:
        for i in range(n - 1):
            s0 = samples[i]
            s1 = samples[i + 1]
            out[2 * i] = s0
            out[2 * i + 1] = (s0 + s1) >> 1
        out[2 * (n - 1)] = samples[n - 1]
        if out_len > 2 * (n - 1) + 1:
            out[2 * (n - 1) + 1] = samples[n - 1]
    else:
        for j in range(out_len):
            src_pos = j / ratio
            idx = int(src_pos)
            frac = src_pos - idx
            if idx + 1 < n:
                val = int(round(samples[idx] * (1.0 - frac) + samples[idx + 1] * frac))
            else:
                val = samples[n - 1]
            out[j] = max(-32768, min(32767, val))

    if sys.byteorder != "little":
        out.byteswap()
    return _encode_wav(out.tobytes(), target_sample_rate, 1, 2)


def format_srt_timecode_ms(ms_total: int) -> str:
    """Format integer milliseconds as SRT timecode HH:MM:SS,mmm."""
    ms_clamped = max(0, ms_total)
    hours, rem = divmod(ms_clamped, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def format_srt_timecode(seconds: float) -> str:
    """Format seconds as SRT timecode HH:MM:SS,mmm."""
    return format_srt_timecode_ms(int(round(seconds * 1000)))


def _strip_inline_tags(text: str) -> str:
    """Remove Gemini TTS inline vocal tags (<short pause>, <laugh>, etc.) from subtitles."""
    cleaned = re.sub(r"<[a-zA-Z0-9 _-]+>", "", text)
    return re.sub(r"\s+", " ", cleaned).strip()


def _wrap_two_lines(cue: str, line_limit: int = 21) -> str:
    """Insert a single newline into cues longer than line_limit for clean NLE rendering."""
    if len(cue) <= line_limit:
        return cue
    mid = len(cue) // 2
    # Prefer sentence/clause punctuation (keeping closing brackets attached) over plain spaces
    for pattern in (r"[。！？!?][」』）\)\]]?", r"[、,][」』）\)\]]?", r" "):
        best_pos: int | None = None
        best_dist = len(cue)
        for m in re.finditer(pattern, cue):
            pos = m.end()
            if 4 <= pos <= len(cue) - 4:
                dist = abs(pos - mid)
                if dist < best_dist:
                    best_dist = dist
                    best_pos = pos
        if best_pos is not None:
            return f"{cue[:best_pos].rstrip()}\n{cue[best_pos:].lstrip()}"
    return f"{cue[:mid].rstrip()}\n{cue[mid:].lstrip()}"


def _split_subtitle_chunks(text: str, max_chars_per_cue: int = 42) -> list[str]:
    """Split long narration text into NLE-friendly subtitle cues (up to 2 lines of ~21 chars)."""
    clean = _strip_inline_tags(text)
    if not clean:
        return []
    if len(clean) <= max_chars_per_cue:
        return [_wrap_two_lines(clean)]

    raw_parts = _SENTENCE_SPLIT_RE.split(clean)
    cues: list[str] = []
    buf = ""
    for part in raw_parts:
        if not part:
            continue
        if not buf:
            part = part.lstrip()
        if len(part.lstrip()) > max_chars_per_cue:
            if buf:
                cues.append(buf)
                buf = ""
            part = part.lstrip()
            subparts = re.split(r"(?<=[、,])(?![」』）\)\]])", part)
            subbuf = ""
            for sp in subparts:
                if not sp:
                    continue
                if len(subbuf) + len(sp) <= max_chars_per_cue:
                    subbuf += sp
                else:
                    if subbuf:
                        cues.append(subbuf)
                    while len(sp) > max_chars_per_cue:
                        cues.append(sp[:max_chars_per_cue])
                        sp = sp[max_chars_per_cue:]
                    subbuf = sp
            if subbuf:
                cues.append(subbuf)
        elif len(buf) + len(part) <= max_chars_per_cue:
            buf = f"{buf}{part}" if buf else part
        else:
            if buf:
                cues.append(buf)
            buf = part.lstrip()
    if buf:
        cues.append(buf)
    return [_wrap_two_lines(c) for c in cues]


def build_video_assets(
    scenes: Sequence[SceneAudio],
    model: str,
    gap_sec: float = 0.4,
) -> tuple[bytes, str, dict]:
    """Build combined.wav, subtitles.srt, and manifest.json (without duplicating ZIP in RAM)."""
    if not scenes:
        raise ValueError("Cannot build video assets with zero scenes.")

    first_info = inspect_wav(scenes[0].wav_bytes)
    base_sr = first_info.sample_rate
    base_ch = first_info.channels
    base_sw = first_info.sample_width
    gap_frames = max(0, int(round(gap_sec * base_sr)))
    silence_pcm = b"\x00" * (gap_frames * base_ch * base_sw)

    seen_filenames: set[str] = set()
    combined_pcm_parts: list[bytes] = []
    manifest_scenes: list[dict] = []
    srt_lines: list[str] = []
    srt_index = 1
    cursor_frames = 0

    for i, sc in enumerate(scenes):
        if sc.filename in seen_filenames:
            raise ValueError(f"Duplicate scene filename detected: {sc.filename}")
        seen_filenames.add(sc.filename)

        info = inspect_wav(sc.wav_bytes)
        if (info.sample_rate, info.channels, info.sample_width) != (base_sr, base_ch, base_sw):
            raise ValueError(
                f"Scene '{sc.filename}' format mismatch: got "
                f"{info.sample_rate}Hz/{info.channels}ch/{info.sample_width * 8}bit, "
                f"expected {base_sr}Hz/{base_ch}ch/{base_sw * 8}bit."
            )

        start_sec = cursor_frames / base_sr
        end_frames = cursor_frames + info.num_frames
        end_sec = end_frames / base_sr

        combined_pcm_parts.append(info.pcm_bytes)
        if i < len(scenes) - 1 and gap_frames > 0:
            combined_pcm_parts.append(silence_pcm)
            next_cursor = end_frames + gap_frames
        else:
            next_cursor = end_frames

        manifest_scenes.append(
            {
                "index": sc.index,
                "id": sc.slug,
                "file": sc.filename,
                "speaker": sc.speaker,
                "voice": sc.voice,
                "style": sc.style,
                "text": sc.text,
                "sample_frames": info.num_frames,
                "duration_sec": round(info.duration_sec, 3),
                "start_sec": round(start_sec, 3),
                "end_sec": round(end_sec, 3),
                "start_tc": format_srt_timecode(start_sec),
                "end_tc": format_srt_timecode(end_sec),
            }
        )

        # Integer-millisecond SRT timing guarantees end_ms >= start_ms + 1
        start_ms = int(round(start_sec * 1000))
        end_ms = int(round(end_sec * 1000))
        scene_ms = end_ms - start_ms
        cues = _split_subtitle_chunks(sc.text)
        if cues and scene_ms > 0:
            # Collapse into single cue if scene is too short to allocate >= 1ms per cue
            if scene_ms < len(cues):
                cues = ["\n".join(cues)]
            total_len = sum(len(c) for c in cues) or 1
            cue_cursor_ms = start_ms
            for ci, cue_text in enumerate(cues):
                if ci == len(cues) - 1:
                    cue_end_ms = end_ms
                else:
                    remaining_cues = len(cues) - ci - 1
                    alloc = max(1, int(round(scene_ms * (len(cue_text) / total_len))))
                    max_allowed_end = end_ms - remaining_cues
                    cue_end_ms = min(cue_cursor_ms + alloc, max_allowed_end)
                    cue_end_ms = max(cue_cursor_ms + 1, cue_end_ms)
                srt_lines.append(
                    f"{srt_index}\n"
                    f"{format_srt_timecode_ms(cue_cursor_ms)} --> {format_srt_timecode_ms(cue_end_ms)}\n"
                    f"{cue_text}\n"
                )
                srt_index += 1
                cue_cursor_ms = cue_end_ms

        cursor_frames = next_cursor

    combined_wav = _encode_wav(b"".join(combined_pcm_parts), base_sr, base_ch, base_sw)
    total_duration_sec = round(cursor_frames / base_sr, 3)
    srt_text = "\n".join(srt_lines)

    manifest = {
        "schema_version": "1.0",
        "model": model,
        "audio": {
            "sample_rate": base_sr,
            "channels": base_ch,
            "bit_depth": base_sw * 8,
        },
        "gap_sec": gap_sec,
        "total_duration_sec": total_duration_sec,
        "combined_file": "combined.wav",
        "srt_file": "subtitles.srt",
        "scenes": manifest_scenes,
    }
    return combined_wav, srt_text, manifest


def build_zip_from_assets(
    scene_wavs: Mapping[str, bytes],
    combined_wav: bytes,
    srt_text: str,
    manifest: dict,
    bundle_name: str = "narration",
) -> bytes:
    """Create a ZIP archive on demand to avoid storing duplicate compressed bytes in RAM."""
    root_dir = sanitize_slug(bundle_name, "narration")
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for fname, wav_bytes in scene_wavs.items():
            zf.writestr(f"{root_dir}/{fname}", wav_bytes)
        zf.writestr(f"{root_dir}/combined.wav", combined_wav)
        zf.writestr(f"{root_dir}/subtitles.srt", srt_text.encode("utf-8"))
        zf.writestr(
            f"{root_dir}/manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    return zip_buf.getvalue()


def build_video_bundle(
    scenes: Sequence[SceneAudio],
    model: str,
    gap_sec: float = 0.4,
    bundle_name: str = "narration",
) -> tuple[bytes, bytes, str, dict]:
    """Convenience helper returning (zip_bytes, combined_wav, srt_text, manifest)."""
    combined_wav, srt_text, manifest = build_video_assets(scenes, model=model, gap_sec=gap_sec)
    scene_wavs = {sc.filename: sc.wav_bytes for sc in scenes}
    zip_bytes = build_zip_from_assets(scene_wavs, combined_wav, srt_text, manifest, bundle_name)
    return zip_bytes, combined_wav, srt_text, manifest
