from voice import voice_catalog


def test_lookup_identifies_engine():
    assert voice_catalog.lookup("af_heart")["engine"] == "kokoro"
    assert voice_catalog.lookup("en_US-lessac-medium")["engine"] == "piper"
    assert voice_catalog.lookup("lux_heart")["engine"] == "luxtts"
    assert voice_catalog.lookup("does-not-exist") is None


def test_grouped_for_ui_order_and_default():
    ui = voice_catalog.grouped_for_ui()
    labels = [g["label"] for g in ui["groups"]]
    assert labels == ["American English", "British English", "Spanish",
                      "LuxTTS — cloned", "Piper — fast"]
    assert ui["default"] == "af_heart"


def test_lux_group_populated():
    ui = voice_catalog.grouped_for_ui()
    lux = next(g for g in ui["groups"] if g["label"] == "LuxTTS — cloned")
    lux_ids = {v["id"] for v in lux["voices"]}
    assert "lux_heart" in lux_ids
    # One clone per Kokoro voice (20 total, incl. Spanish-reference clones)
    assert len(lux_ids) == 20
    assert {"lux_george", "lux_dora", "lux_fenrir"} <= lux_ids


def test_lux_clones_mirror_kokoro_catalog():
    # Every Kokoro voice should have a lux_<name> clone so the groups stay in
    # sync when voices are added (the catalogs are maintained by hand).
    kokoro_names = {vid.split("_", 1)[1] for (vid, *_rest) in voice_catalog._KOKORO}
    lux_names = {v["id"].removeprefix("lux_") for v in voice_catalog._LUX_ENTRIES}
    assert lux_names >= kokoro_names


def test_spanish_group_populated():
    ui = voice_catalog.grouped_for_ui()
    spanish = next(g for g in ui["groups"] if g["label"] == "Spanish")
    assert any(v["id"] == "ef_dora" for v in spanish["voices"])
