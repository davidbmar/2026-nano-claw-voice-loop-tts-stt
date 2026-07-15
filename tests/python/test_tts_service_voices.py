import importlib.util
from pathlib import Path

# Load tts-service/voices.py by path (tts-service is not a package)
_spec = importlib.util.spec_from_file_location(
    "tts_voices", Path(__file__).resolve().parents[2] / "tts-service" / "voices.py"
)
voices = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(voices)


def test_only_english_and_spanish():
    langs = {v["lang"] for v in voices.KOKORO_VOICES}
    assert langs == {"a", "b", "e"}, f"unexpected langs: {langs}"


def test_default_voice_present():
    ids = {v["id"] for v in voices.KOKORO_VOICES}
    assert "af_heart" in ids
    assert "ef_dora" in ids  # a Spanish voice for testing


def test_lang_code_for_maps_by_prefix():
    assert voices.lang_code_for("af_heart") == "a"
    assert voices.lang_code_for("bf_emma") == "b"
    assert voices.lang_code_for("ef_dora") == "e"


def test_lang_code_for_rejects_unsupported():
    import pytest
    with pytest.raises(KeyError):
        voices.lang_code_for("jf_alpha")  # Japanese — out of scope
