"""Speaker and voice constraints for a single merged TTS interaction."""

from collections.abc import Iterable


def validate_dialogue_voices(turns: Iterable[tuple[str, str]]) -> None:
    voices: dict[str, str] = {}
    for speaker, voice in turns:
        if voices.setdefault(speaker, voice) != voice:
            raise ValueError(
                f"Speaker '{speaker}' has multiple voices. Use a unique speaker name or output_mode='split'."
            )
    if len(voices) > 2:
        raise ValueError("Merged dialogue supports at most two speakers. Use output_mode='split'.")
    if len(voices) > 1 and any(voice.startswith("voice_") for voice in voices.values()):
        raise ValueError("Custom voices in multi-speaker dialogue require output_mode='split'.")
