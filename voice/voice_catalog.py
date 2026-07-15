"""Combined voice catalog for the picker — Kokoro (EN+ES) + Piper (fast).

Static so the browser picker renders even while the native TTS service warms.
The Kokoro list is kept in sync with tts-service/voices.py by hand (small,
rarely changes). Engine is inferred here so the router in tts.py can dispatch.
"""

from voice.tts import VOICE_CATALOG as PIPER_CATALOG

# Mirror of tts-service/voices.py KOKORO_VOICES (kept in sync manually).
_KOKORO = [
    ("af_heart", "Heart", "American English", "a", "A"),
    ("af_bella", "Bella", "American English", "a", "A-"),
    ("af_nicole", "Nicole", "American English", "a", "B-"),
    ("af_aoede", "Aoede", "American English", "a", "C+"),
    ("af_kore", "Kore", "American English", "a", "C+"),
    ("af_sarah", "Sarah", "American English", "a", "C+"),
    ("af_nova", "Nova", "American English", "a", "C"),
    ("af_sky", "Sky", "American English", "a", "C-"),
    ("am_fenrir", "Fenrir", "American English", "a", "C+"),
    ("am_michael", "Michael", "American English", "a", "C+"),
    ("am_puck", "Puck", "American English", "a", "C+"),
    ("am_echo", "Echo", "American English", "a", "D"),
    ("bf_emma", "Emma", "British English", "b", "B-"),
    ("bf_isabella", "Isabella", "British English", "b", "C"),
    ("bm_george", "George", "British English", "b", "C"),
    ("bm_fable", "Fable", "British English", "b", "C"),
    ("bm_lewis", "Lewis", "British English", "b", "D+"),
    ("ef_dora", "Dora", "Spanish", "e", "—"),
    ("em_alex", "Alex", "Spanish", "e", "—"),
    ("em_santa", "Santa", "Spanish", "e", "—"),
]

_KOKORO_ENTRIES = [
    {"id": vid, "name": name, "engine": "kokoro", "lang": lang, "grade": grade, "group": group}
    for (vid, name, group, lang, grade) in _KOKORO
]

_PIPER_ENTRIES = [
    {"id": v["id"], "name": v["name"], "engine": "piper", "lang": v["lang"],
     "grade": None, "group": "Piper — fast"}
    for v in PIPER_CATALOG
]

_ALL = _KOKORO_ENTRIES + _PIPER_ENTRIES
_BY_ID = {v["id"]: v for v in _ALL}

DEFAULT_VOICE = "af_heart"


def combined_catalog() -> list[dict]:
    return list(_ALL)


def lookup(voice_id: str) -> dict | None:
    return _BY_ID.get(voice_id)


def grouped_for_ui() -> dict:
    order = ["American English", "British English", "Spanish", "Piper — fast"]
    groups = []
    for label in order:
        voices = [v for v in _ALL if v["group"] == label]
        if voices:
            groups.append({"label": label, "voices": voices})
    return {"groups": groups, "default": DEFAULT_VOICE}
