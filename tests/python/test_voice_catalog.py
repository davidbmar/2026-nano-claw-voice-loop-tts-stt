from voice import voice_catalog


def test_lookup_identifies_engine():
    assert voice_catalog.lookup("af_heart")["engine"] == "kokoro"
    assert voice_catalog.lookup("en_US-lessac-medium")["engine"] == "piper"
    assert voice_catalog.lookup("does-not-exist") is None


def test_grouped_for_ui_order_and_default():
    ui = voice_catalog.grouped_for_ui()
    labels = [g["label"] for g in ui["groups"]]
    assert labels == ["American English", "British English", "Spanish", "Piper — fast"]
    assert ui["default"] == "af_heart"


def test_spanish_group_populated():
    ui = voice_catalog.grouped_for_ui()
    spanish = next(g for g in ui["groups"] if g["label"] == "Spanish")
    assert any(v["id"] == "ef_dora" for v in spanish["voices"])
