import random
from voice.backoff import Backoff


def test_ceiling_grows_by_factor_until_cap():
    # Force full-jitter to return its upper bound so we can see the ceiling.
    b = Backoff(base=0.5, factor=2.0, cap=8.0)
    random.seed(1)
    ceilings = []
    # Patch uniform to return its high arg so we observe the ceiling sequence.
    import voice.backoff as mod
    orig = mod.random.uniform
    mod.random.uniform = lambda lo, hi: hi
    try:
        ceilings = [b.next() for _ in range(6)]
    finally:
        mod.random.uniform = orig
    assert ceilings == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0]  # doubles, then capped


def test_jitter_within_bounds_and_attempts_increment():
    b = Backoff(base=1.0, factor=2.0, cap=10.0)
    random.seed(0)
    for expected_ceiling in (1.0, 2.0, 4.0):
        d = b.next()
        assert 0.0 <= d <= expected_ceiling
    assert b.attempts == 3


def test_reset_returns_to_base():
    b = Backoff(base=0.5, factor=2.0, cap=8.0)
    b.next(); b.next(); b.next()
    assert b.attempts == 3
    b.reset()
    assert b.attempts == 0
