"""Comprehensive unit tests for audio.py including review edge cases."""

from __future__ import annotations

import io
import json
import struct
import unittest
import wave
import zipfile

from gemini_tts_mcp.audio import (
    SceneAudio,
    build_video_bundle,
    format_srt_timecode,
    inspect_wav,
    resample_wav_if_needed,
    sanitize_slug,
)


def _make_wav(
    sample_rate: int = 24000,
    duration_sec: float = 0.5,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    nframes = int(round(sample_rate * duration_sec))
    if sample_width == 2:
        vals = ([1000, -1000] * ((nframes * channels) // 2 + 1))[: nframes * channels]
        pcm = struct.pack(f"<{len(vals)}h", *vals)
    else:
        pcm = bytes([128] * (nframes * channels * sample_width))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


class TestAudioModule(unittest.TestCase):
    def test_sanitize_slug(self) -> None:
        self.assertEqual(sanitize_slug("Intro Scene 01!", "fallback"), "intro_scene_01")
        self.assertEqual(sanitize_slug("日本語のみ", "scene_001"), "scene_001")
        self.assertEqual(sanitize_slug(None, "scene_002"), "scene_002")

    def test_inspect_and_resample_24k_to_48k_and_non_2x(self) -> None:
        wav24 = _make_wav(24000, 0.5)
        wav48 = resample_wav_if_needed(wav24, 48000)
        info48 = inspect_wav(wav48)
        self.assertEqual(info48.sample_rate, 48000)
        self.assertEqual(info48.num_frames, 24000)
        self.assertAlmostEqual(info48.duration_sec, 0.5, places=4)

        # Non-2x resample (24kHz -> 16kHz)
        wav16 = resample_wav_if_needed(wav24, 16000)
        info16 = inspect_wav(wav16)
        self.assertEqual(info16.sample_rate, 16000)
        self.assertAlmostEqual(info16.duration_sec, 0.5, places=3)

    def test_resample_rejects_stereo(self) -> None:
        stereo_wav = _make_wav(24000, 0.2, channels=2)
        with self.assertRaises(ValueError):
            resample_wav_if_needed(stereo_wav, 48000)

    def test_format_srt_timecode(self) -> None:
        self.assertEqual(format_srt_timecode(0.0), "00:00:00,000")
        self.assertEqual(format_srt_timecode(65.432), "00:01:05,432")

    def test_build_video_bundle_srt_decimal_quotes_and_tags(self) -> None:
        w1 = _make_wav(48000, 1.0)
        w2 = _make_wav(48000, 1.5)
        scenes = [
            SceneAudio(
                index=1,
                slug="001_intro",
                filename="scenes/001_intro.wav",
                speaker="narrator",
                voice="Kore",
                style="calm",
                text="「Gemini 3.8はすごい。」<short pause>次の文です。",
                wav_bytes=w1,
                sample_rate=48000,
                channels=1,
                bit_depth=16,
                sample_frames=48000,
                duration_sec=1.0,
            ),
            SceneAudio(
                index=2,
                slug="002_tags_only",
                filename="scenes/002_tags_only.wav",
                speaker="narrator",
                voice="Kore",
                style="warm",
                text="<short pause>",
                wav_bytes=w2,
                sample_rate=48000,
                channels=1,
                bit_depth=16,
                sample_frames=72000,
                duration_sec=1.5,
            ),
        ]

        zip_bytes, combined_wav, srt_text, manifest = build_video_bundle(
            scenes=scenes,
            model="gemini-3.8-flash-lite-tts",
            gap_sec=0.4,
            bundle_name="demo_video",
        )

        self.assertAlmostEqual(inspect_wav(combined_wav).duration_sec, 2.9, places=3)
        self.assertNotIn("<short pause>", srt_text)
        # Decimal "3.8" must not be split, and closing quote must not be orphaned
        self.assertIn("Gemini 3.8", srt_text)
        self.assertNotIn("\n」", srt_text)

        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            names = set(zf.namelist())
            self.assertIn("demo_video/scenes/001_intro.wav", names)
            self.assertIn("demo_video/combined.wav", names)
            self.assertIn("demo_video/subtitles.srt", names)
            self.assertIn("demo_video/manifest.json", names)
            parsed = json.loads(zf.read("demo_video/manifest.json").decode("utf-8"))
            self.assertEqual(len(parsed["scenes"]), 2)

    def test_build_video_bundle_rejects_mismatched_sr_and_duplicates(self) -> None:
        w24 = _make_wav(24000, 0.5)
        w48 = _make_wav(48000, 0.5)
        s1 = SceneAudio(1, "001_a", "scenes/001_a.wav", "n", "Kore", "", "a", w24, 24000, 1, 16, 12000, 0.5)
        s2_bad_sr = SceneAudio(2, "002_b", "scenes/002_b.wav", "n", "Kore", "", "b", w48, 48000, 1, 16, 24000, 0.5)
        with self.assertRaises(ValueError):
            build_video_bundle([s1, s2_bad_sr], model="gemini-3.8-flash-lite-tts")

        s2_dup = SceneAudio(2, "001_a", "scenes/001_a.wav", "n", "Kore", "", "b", w24, 24000, 1, 16, 12000, 0.5)
        with self.assertRaises(ValueError):
            build_video_bundle([s1, s2_dup], model="gemini-3.8-flash-lite-tts")

    def test_very_short_scene_srt_has_no_inverted_or_zero_duration_cues(self) -> None:
        tiny_wav = _make_wav(24000, 0.002)  # 2ms audio
        sc = SceneAudio(
            1,
            "001_tiny",
            "scenes/001_tiny.wav",
            "n",
            "Kore",
            "",
            "一文目です。二文目です。三文目です。四文目です。",
            tiny_wav,
            24000,
            1,
            16,
            48,
            0.002,
        )
        _, _, srt_text, _ = build_video_bundle([sc], model="gemini-3.8-flash-lite-tts")
        for line in srt_text.splitlines():
            if " --> " in line:
                start_tc, end_tc = line.split(" --> ")
                self.assertLess(start_tc, end_tc)


if __name__ == "__main__":
    unittest.main()
