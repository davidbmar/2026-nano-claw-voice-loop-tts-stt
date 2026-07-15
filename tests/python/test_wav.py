import struct

from voice.wav import pcm_to_wav


def test_wav_header_is_valid():
    pcm = b"\x00\x00" * 2400  # 2400 int16 samples
    wav = pcm_to_wav(pcm, 48000)
    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"
    # data chunk length equals the PCM byte length
    data_idx = wav.index(b"data")
    (data_len,) = struct.unpack("<I", wav[data_idx + 4:data_idx + 8])
    assert data_len == len(pcm)
    # sample rate stored correctly (offset 24 in the standard 44-byte header)
    (rate,) = struct.unpack("<I", wav[24:28])
    assert rate == 48000
