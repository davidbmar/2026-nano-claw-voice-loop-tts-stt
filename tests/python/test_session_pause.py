from voice.audio.audio_queue import AudioQueue


class FakeSource:
    def __init__(self):
        self.generator = None
    def set_generator(self, g): self.generator = g
    def clear_generator(self): self.generator = None


# Minimal stand-in exercising the same pause/resume/cancel logic Session uses.
# (Session wires these to self._audio_source/self._audio_queue; we assert the
#  contract: pause detaches generator but keeps the queue; resume re-attaches;
#  cancel clears both.)
class PausableSpeaker:
    def __init__(self, source, queue, generator):
        self._audio_source = source
        self._audio_queue = queue
        self._tts_generator = generator
        self._paused = False
    def pause_speaking(self):
        self._paused = True
        self._audio_source.clear_generator()
    def resume_speaking(self):
        self._paused = False
        self._audio_source.set_generator(self._tts_generator)
    def cancel_stream(self):
        self._paused = False
        self._audio_queue.clear()
        self._audio_source.clear_generator()
    def is_paused(self):
        return self._paused


def test_pause_detaches_generator_but_keeps_queue():
    src, q = FakeSource(), AudioQueue()
    q.enqueue(b"\x01\x02" * 100)
    sp = PausableSpeaker(src, q, generator="GEN")
    src.set_generator("GEN")
    sp.pause_speaking()
    assert src.generator is None          # source silent
    assert q.available == 200             # audio retained
    assert sp.is_paused() is True


def test_resume_reattaches_generator_and_keeps_queue():
    src, q = FakeSource(), AudioQueue()
    q.enqueue(b"\x01\x02" * 100)
    sp = PausableSpeaker(src, q, generator="GEN")
    sp.pause_speaking()
    sp.resume_speaking()
    assert src.generator == "GEN"
    assert q.available == 200
    assert sp.is_paused() is False


def test_cancel_clears_queue_and_generator():
    src, q = FakeSource(), AudioQueue()
    q.enqueue(b"\x01\x02" * 100)
    sp = PausableSpeaker(src, q, generator="GEN")
    src.set_generator("GEN")
    sp.cancel_stream()
    assert src.generator is None
    assert q.available == 0
    assert sp.is_paused() is False
