"""Kokoro-82M voice catalog — English (American/British) + Spanish only.

Grades are from the Kokoro-82M v1.0 release notes (A best … F worst); they help
the picker be honest about weaker voices. Spanish voices have no published
letter grade, so they are marked "—".
"""

# lang_code (KPipeline): "a"=American English, "b"=British English, "e"=Spanish
LANG_BY_PREFIX = {"a": "a", "b": "b", "e": "e"}

KOKORO_VOICES = [
    # American English
    {"id": "af_heart",   "name": "Heart",   "group": "American English", "lang": "a", "grade": "A"},
    {"id": "af_bella",   "name": "Bella",   "group": "American English", "lang": "a", "grade": "A-"},
    {"id": "af_nicole",  "name": "Nicole",  "group": "American English", "lang": "a", "grade": "B-"},
    {"id": "af_aoede",   "name": "Aoede",   "group": "American English", "lang": "a", "grade": "C+"},
    {"id": "af_kore",    "name": "Kore",    "group": "American English", "lang": "a", "grade": "C+"},
    {"id": "af_sarah",   "name": "Sarah",   "group": "American English", "lang": "a", "grade": "C+"},
    {"id": "af_nova",    "name": "Nova",    "group": "American English", "lang": "a", "grade": "C"},
    {"id": "af_sky",     "name": "Sky",     "group": "American English", "lang": "a", "grade": "C-"},
    {"id": "am_fenrir",  "name": "Fenrir",  "group": "American English", "lang": "a", "grade": "C+"},
    {"id": "am_michael", "name": "Michael", "group": "American English", "lang": "a", "grade": "C+"},
    {"id": "am_puck",    "name": "Puck",    "group": "American English", "lang": "a", "grade": "C+"},
    {"id": "am_echo",    "name": "Echo",    "group": "American English", "lang": "a", "grade": "D"},
    # British English
    {"id": "bf_emma",     "name": "Emma",     "group": "British English", "lang": "b", "grade": "B-"},
    {"id": "bf_isabella", "name": "Isabella", "group": "British English", "lang": "b", "grade": "C"},
    {"id": "bm_george",   "name": "George",   "group": "British English", "lang": "b", "grade": "C"},
    {"id": "bm_fable",    "name": "Fable",    "group": "British English", "lang": "b", "grade": "C"},
    {"id": "bm_lewis",    "name": "Lewis",    "group": "British English", "lang": "b", "grade": "D+"},
    # Spanish (for testing Spanish speaking)
    {"id": "ef_dora",  "name": "Dora",  "group": "Spanish", "lang": "e", "grade": "—"},
    {"id": "em_alex",  "name": "Alex",  "group": "Spanish", "lang": "e", "grade": "—"},
    {"id": "em_santa", "name": "Santa", "group": "Spanish", "lang": "e", "grade": "—"},
]


def lang_code_for(voice_id: str) -> str:
    """Return the KPipeline lang_code for a voice id, by first-char prefix."""
    return LANG_BY_PREFIX[voice_id[0]]
