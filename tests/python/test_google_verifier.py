from __future__ import annotations

import asyncio
import base64
import json
import threading
from datetime import datetime, timedelta, timezone

import google.auth
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import NameOID

from voice.webauth.google_verifier import (
    DEFAULT_NETWORK_TIMEOUT_SECONDS,
    GOOGLE_CERTS_URL,
    MAX_CERT_RESPONSE_BYTES,
    MAX_JWT_BYTES,
    GoogleIDTokenVerifier,
    GoogleKeyCache,
    GoogleKeysUnavailable,
    InvalidIDToken,
    KeyFetchResponse,
    LoginRateLimitExceeded,
    LoginRateLimiter,
    UnknownKeyID,
)
from voice.webauth.policy import LoginNonceStore


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
AUDIENCE = "nano-claw-client.apps.googleusercontent.com"


class SequenceRng:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self, size: int) -> bytes:
        self.value += 1
        return self.value.to_bytes(size, "big")


class SequenceFetcher:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, float, int, int]] = []
        self._lock = threading.Lock()

    def __call__(self, url: str, timeout: float, cap: int) -> KeyFetchResponse:
        with self._lock:
            self.calls.append((url, timeout, cap, threading.get_ident()))
            if not self.responses:
                raise AssertionError("unexpected key refresh")
            response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def make_key_and_certificate(common_name: str):
    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, common_name)]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=30))
        .sign(private_key, hashes.SHA256())
    )
    certificate_pem = certificate.public_bytes(
        serialization.Encoding.PEM
    ).decode("ascii")
    return private_key, certificate_pem


@pytest.fixture(scope="module")
def signing_material():
    first_key, first_certificate = make_key_and_certificate("fake-google-one")
    second_key, second_certificate = make_key_and_certificate("fake-google-two")
    return {
        "key-1": (first_key, first_certificate),
        "key-2": (second_key, second_certificate),
    }


def claims_for(expected_nonce: str, **overrides):
    claims = {
        "iss": "https://accounts.google.com",
        "aud": AUDIENCE,
        "sub": "google-sub-123",
        "email": "person@example.test",
        "name": "Person Name",
        "nonce": expected_nonce,
        "iat": int(NOW.timestamp()),
        "exp": int((NOW + timedelta(hours=1)).timestamp()),
    }
    claims.update(overrides)
    return claims


def sign_token(
    private_key,
    claims,
    *,
    kid: str = "key-1",
    alg: str = "RS256",
    extra_header: dict | None = None,
) -> str:
    header = {"alg": alg, "kid": kid, "typ": "JWT"}
    if extra_header:
        header.update(extra_header)
    encoded_header = b64url(
        json.dumps(header, separators=(",", ":"), sort_keys=True).encode()
    )
    encoded_claims = b64url(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    )
    signed = f"{encoded_header}.{encoded_claims}".encode("ascii")
    signature = private_key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
    return f"{signed.decode('ascii')}.{b64url(signature)}"


def key_response(keys: dict[str, str], *, max_age: int = 600) -> KeyFetchResponse:
    return KeyFetchResponse(
        status=200,
        body=json.dumps(keys).encode("utf-8"),
        headers={"Cache-Control": f"public, max-age={max_age}", "Age": "0"},
    )


def make_nonce_store() -> LoginNonceStore:
    return LoginNonceStore(random_bytes=SequenceRng(), clock=lambda: NOW)


def verifier_with_challenge(signing_material, *, kid: str = "key-1"):
    nonce_store = make_nonce_store()
    challenge = nonce_store.issue(now=NOW)
    certificate = signing_material[kid][1]
    key_cache = GoogleKeyCache(
        initial_keys={kid: certificate},
        initial_expires_at=NOW + timedelta(hours=1),
    )
    verifier = GoogleIDTokenVerifier(
        key_cache=key_cache, nonce_store=nonce_store
    )
    return verifier, nonce_store, challenge


def verify(verifier: GoogleIDTokenVerifier, token: str, nonce: str, *, now=NOW):
    return asyncio.run(
        verifier.verify_id_token(
            token,
            now=now,
            expected_aud=AUDIENCE,
            expected_nonce=nonce,
        )
    )


def test_google_auth_import_resolves_and_valid_locally_signed_token_passes(
    signing_material,
):
    assert google.auth.__version__
    verifier, nonce_store, challenge = verifier_with_challenge(signing_material)
    token = sign_token(
        signing_material["key-1"][0], claims_for(challenge.nonce)
    )

    assert verify(verifier, token, challenge.nonce) == {
        "sub": "google-sub-123",
        "email": "person@example.test",
        "name": "Person Name",
    }
    assert len(nonce_store) == 0


@pytest.mark.parametrize(
    ("token_changes", "verify_changes"),
    [
        ({"alg": "HS256"}, {}),
        ({"claims": {"aud": "other-client"}}, {}),
        ({"claims": {"iss": "https://attacker.example"}}, {}),
        ({"claims": {"iss": ["https://accounts.google.com"]}}, {}),
        ({"claims": {"nonce": "wrong-nonce"}}, {}),
        ({"claims": {"sub": 12345}}, {}),
        ({"claims": {"email": "x" * 321}}, {}),
        ({}, {"expected_aud": "other-client"}),
    ],
)
def test_wrong_alg_aud_iss_nonce_and_typed_claims_are_rejected_without_consuming(
    signing_material, token_changes, verify_changes
):
    verifier, nonce_store, challenge = verifier_with_challenge(signing_material)
    claim_overrides = token_changes.get("claims", {})
    token = sign_token(
        signing_material["key-1"][0],
        claims_for(challenge.nonce, **claim_overrides),
        alg=token_changes.get("alg", "RS256"),
    )
    expected_aud = verify_changes.get("expected_aud", AUDIENCE)

    with pytest.raises(InvalidIDToken, match="Google ID token was rejected"):
        asyncio.run(
            verifier.verify_id_token(
                token,
                now=NOW,
                expected_aud=expected_aud,
                expected_nonce=challenge.nonce,
            )
        )
    assert nonce_store.expected_nonce(challenge.pre_auth_value, now=NOW) == (
        challenge.nonce
    )


def test_wrong_signature_is_rejected_by_google_auth(signing_material):
    verifier, nonce_store, challenge = verifier_with_challenge(signing_material)
    token = sign_token(
        signing_material["key-2"][0],
        claims_for(challenge.nonce),
        kid="key-1",
    )
    with pytest.raises(InvalidIDToken):
        verify(verifier, token, challenge.nonce)
    assert len(nonce_store) == 1


def test_unknown_kid_is_rejected_after_one_bounded_refresh(signing_material):
    old_certificate = signing_material["key-1"][1]
    fetcher = SequenceFetcher(key_response({"key-1": old_certificate}))
    cache = GoogleKeyCache(
        fetcher=fetcher,
        initial_keys={"key-1": old_certificate},
        initial_expires_at=NOW + timedelta(hours=1),
    )
    nonce_store = make_nonce_store()
    challenge = nonce_store.issue(now=NOW)
    verifier = GoogleIDTokenVerifier(key_cache=cache, nonce_store=nonce_store)
    token = sign_token(
        signing_material["key-2"][0],
        claims_for(challenge.nonce),
        kid="not-a-google-key",
    )

    with pytest.raises(UnknownKeyID):
        verify(verifier, token, challenge.nonce)
    assert len(fetcher.calls) == 1


@pytest.mark.parametrize(
    "claim_overrides",
    [
        {"exp": int((NOW - timedelta(seconds=61)).timestamp())},
        {
            "iat": int((NOW + timedelta(seconds=61)).timestamp()),
            "exp": int((NOW + timedelta(hours=1, seconds=61)).timestamp()),
        },
        {"exp": int((NOW + timedelta(hours=1, seconds=1)).timestamp())},
        {"exp": int((NOW + timedelta(hours=2)).timestamp())},
        {"iat": "not-a-number"},
        {"exp": float("inf")},
    ],
)
def test_expired_future_and_invalid_time_claims_are_rejected(
    signing_material, claim_overrides
):
    verifier, nonce_store, challenge = verifier_with_challenge(signing_material)
    token = sign_token(
        signing_material["key-1"][0],
        claims_for(challenge.nonce, **claim_overrides),
    )
    with pytest.raises(InvalidIDToken):
        verify(verifier, token, challenge.nonce)
    assert len(nonce_store) == 1


@pytest.mark.parametrize(
    "claim_overrides",
    [
        {
            "iat": int((NOW - timedelta(hours=1)).timestamp()),
            "exp": int((NOW - timedelta(seconds=59)).timestamp()),
        },
        {
            "iat": int((NOW + timedelta(seconds=59)).timestamp()),
            "exp": int((NOW + timedelta(hours=1)).timestamp()),
        },
    ],
)
def test_time_claims_inside_the_bounded_skew_are_accepted(
    signing_material, claim_overrides
):
    verifier, _, challenge = verifier_with_challenge(signing_material)
    token = sign_token(
        signing_material["key-1"][0],
        claims_for(challenge.nonce, **claim_overrides),
    )
    assert verify(verifier, token, challenge.nonce)["sub"] == "google-sub-123"


@pytest.mark.parametrize(
    "credential",
    [
        "not-a-jwt",
        "a.b.c.d",
        "@@@.e30.signature",
        f"{b64url(b'[]')}.e30.signature",
    ],
)
def test_malformed_tokens_are_rejected_before_key_access(
    signing_material, credential
):
    class NoKeyAccess:
        async def get_key(self, kid, *, now):
            raise AssertionError("malformed JWT reached key cache")

    nonce_store = make_nonce_store()
    challenge = nonce_store.issue(now=NOW)
    verifier = GoogleIDTokenVerifier(
        key_cache=NoKeyAccess(), nonce_store=nonce_store
    )
    with pytest.raises(InvalidIDToken):
        verify(verifier, credential, challenge.nonce)


def test_oversized_jwt_is_rejected_before_key_access():
    class NoKeyAccess:
        async def get_key(self, kid, *, now):
            raise AssertionError("oversized JWT reached key cache")

    nonce_store = make_nonce_store()
    challenge = nonce_store.issue(now=NOW)
    verifier = GoogleIDTokenVerifier(
        key_cache=NoKeyAccess(), nonce_store=nonce_store
    )
    with pytest.raises(InvalidIDToken):
        verify(verifier, "x" * (MAX_JWT_BYTES + 1), challenge.nonce)


def test_non_rsa_or_small_key_is_rejected_at_cache_boundary():
    ec_key = ec.generate_private_key(ec.SECP256R1())
    ec_public = ec_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    with pytest.raises(GoogleKeysUnavailable):
        GoogleKeyCache(initial_keys={"ec-key": ec_public})

    small_key = rsa.generate_private_key(public_exponent=65_537, key_size=1_024)
    small_public = small_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    with pytest.raises(GoogleKeysUnavailable):
        GoogleKeyCache(initial_keys={"small-key": small_public})


def test_cache_rotation_refreshes_unknown_kid_off_the_event_loop(signing_material):
    caller_thread = threading.get_ident()
    fetcher = SequenceFetcher(
        key_response({"key-2": signing_material["key-2"][1]}, max_age=900)
    )
    cache = GoogleKeyCache(
        fetcher=fetcher,
        initial_keys={"key-1": signing_material["key-1"][1]},
        initial_expires_at=NOW + timedelta(hours=1),
    )
    nonce_store = make_nonce_store()
    challenge = nonce_store.issue(now=NOW)
    verifier = GoogleIDTokenVerifier(key_cache=cache, nonce_store=nonce_store)
    token = sign_token(
        signing_material["key-2"][0],
        claims_for(challenge.nonce),
        kid="key-2",
    )

    assert verify(verifier, token, challenge.nonce)["sub"] == "google-sub-123"
    assert cache.cached_kids == frozenset({"key-2"})
    assert len(fetcher.calls) == 1
    url, timeout, cap, fetch_thread = fetcher.calls[0]
    assert (url, timeout, cap) == (
        GOOGLE_CERTS_URL,
        DEFAULT_NETWORK_TIMEOUT_SECONDS,
        MAX_CERT_RESPONSE_BYTES,
    )
    assert fetch_thread != caller_thread


def test_cache_honors_max_age_and_fails_closed_while_refresh_is_throttled(
    signing_material,
):
    certificate = signing_material["key-1"][1]
    fetcher = SequenceFetcher(
        key_response({"key-1": certificate}, max_age=2),
        key_response({"key-1": certificate}, max_age=600),
    )
    cache = GoogleKeyCache(fetcher=fetcher)

    assert asyncio.run(cache.get_key("key-1", now=NOW)) == certificate
    assert asyncio.run(
        cache.get_key("key-1", now=NOW + timedelta(seconds=1))
    ) == certificate
    with pytest.raises(GoogleKeysUnavailable):
        asyncio.run(cache.get_key("key-1", now=NOW + timedelta(seconds=2)))
    assert len(fetcher.calls) == 1
    assert asyncio.run(
        cache.get_key("key-1", now=NOW + timedelta(seconds=30))
    ) == certificate
    assert len(fetcher.calls) == 2


def test_oversized_certificate_response_is_rejected(signing_material):
    oversized = KeyFetchResponse(
        status=200,
        body=b"x" * (MAX_CERT_RESPONSE_BYTES + 1),
        headers={"Cache-Control": "max-age=600"},
    )
    cache = GoogleKeyCache(fetcher=SequenceFetcher(oversized))
    with pytest.raises(GoogleKeysUnavailable):
        asyncio.run(cache.get_key("key-1", now=NOW))


def test_jwks_unreachable_fails_new_login_closed_with_non_secret_error(
    signing_material,
):
    fetcher = SequenceFetcher(OSError("private network detail"))
    cache = GoogleKeyCache(fetcher=fetcher)
    nonce_store = make_nonce_store()
    challenge = nonce_store.issue(now=NOW)
    verifier = GoogleIDTokenVerifier(key_cache=cache, nonce_store=nonce_store)
    token = sign_token(
        signing_material["key-1"][0], claims_for(challenge.nonce)
    )

    with pytest.raises(
        GoogleKeysUnavailable, match="Google sign-in is temporarily unavailable"
    ) as caught:
        verify(verifier, token, challenge.nonce)
    assert "private" not in str(caught.value)
    assert nonce_store.expected_nonce(challenge.pre_auth_value, now=NOW) == (
        challenge.nonce
    )
    with pytest.raises(GoogleKeysUnavailable):
        verify(verifier, token, challenge.nonce)
    assert len(fetcher.calls) == 1


def test_replayed_nonce_is_rejected(signing_material):
    verifier, nonce_store, challenge = verifier_with_challenge(signing_material)
    token = sign_token(
        signing_material["key-1"][0], claims_for(challenge.nonce)
    )
    assert verify(verifier, token, challenge.nonce)["sub"] == "google-sub-123"
    with pytest.raises(InvalidIDToken):
        verify(verifier, token, challenge.nonce)
    assert len(nonce_store) == 0


def test_concurrent_nonce_replay_allows_exactly_one_login(signing_material):
    verifier, nonce_store, challenge = verifier_with_challenge(signing_material)
    token = sign_token(
        signing_material["key-1"][0], claims_for(challenge.nonce)
    )

    async def exercise():
        return await asyncio.gather(
            *(
                verifier.verify_id_token(
                    token,
                    now=NOW,
                    expected_aud=AUDIENCE,
                    expected_nonce=challenge.nonce,
                )
                for _ in range(2)
            ),
            return_exceptions=True,
        )

    results = asyncio.run(exercise())
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, InvalidIDToken) for result in results) == 1
    assert len(nonce_store) == 0


def test_nonce_is_bound_to_pre_auth_value_and_expires_at_boundary():
    store = make_nonce_store()
    challenge = store.issue(now=NOW)
    other = store.issue(now=NOW)

    assert len(challenge.pre_auth_value) == 43
    assert len(challenge.nonce) == 43
    assert store.expected_nonce("unbound-cookie", now=NOW) is None
    assert not store.consume(
        challenge.nonce,
        pre_auth_value=other.pre_auth_value,
        now=NOW,
    )
    assert store.expected_nonce(challenge.pre_auth_value, now=NOW) == (
        challenge.nonce
    )
    assert store.expected_nonce(
        challenge.pre_auth_value, now=challenge.expires_at
    ) is None


def test_unknown_kid_refresh_is_globally_throttled_not_one_fetch_per_kid(
    signing_material,
):
    certificate = signing_material["key-1"][1]
    fetcher = SequenceFetcher(
        key_response({"key-1": certificate}),
        key_response({"key-1": certificate}),
    )
    cache = GoogleKeyCache(
        fetcher=fetcher,
        initial_keys={"key-1": certificate},
        initial_expires_at=NOW + timedelta(hours=1),
    )

    for kid in ("forged-a", "forged-b", "forged-a"):
        with pytest.raises(UnknownKeyID):
            asyncio.run(cache.get_key(kid, now=NOW))
    assert len(fetcher.calls) == 1

    with pytest.raises(UnknownKeyID):
        asyncio.run(
            cache.get_key("forged-c", now=NOW + timedelta(seconds=30))
        )
    assert len(fetcher.calls) == 2


def test_concurrent_unknown_kid_refresh_is_single_flight(signing_material):
    certificate = signing_material["key-1"][1]

    class AsyncFetcher:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, url, timeout, cap):
            self.calls += 1
            await asyncio.sleep(0.01)
            return key_response({"key-1": certificate})

    fetcher = AsyncFetcher()
    cache = GoogleKeyCache(
        fetcher=fetcher,
        initial_keys={"key-1": certificate},
        initial_expires_at=NOW + timedelta(hours=1),
    )

    async def exercise():
        results = await asyncio.gather(
            *(cache.get_key("forged-kid", now=NOW) for _ in range(20)),
            return_exceptions=True,
        )
        return results

    results = asyncio.run(exercise())
    assert fetcher.calls == 1
    assert all(isinstance(result, UnknownKeyID) for result in results)


def test_login_rate_limit_ip_and_sub_boundaries_and_pluggable_source():
    request = {"trusted_ip": "2001:0db8::1"}
    limiter = LoginRateLimiter(
        ip_source=lambda value: value["trusted_ip"],
        ip_attempts=2,
        sub_attempts=1,
        window=timedelta(seconds=60),
    )

    limiter.check_request(request, now=NOW)
    limiter.check_ip("2001:db8::1", now=NOW + timedelta(seconds=59))
    with pytest.raises(LoginRateLimitExceeded) as ip_limited:
        limiter.check_ip("2001:db8::1", now=NOW + timedelta(seconds=59))
    assert ip_limited.value.scope == "ip"
    assert ip_limited.value.retry_after == 1
    assert "2001" not in str(ip_limited.value)

    # The oldest attempt expires exactly at the sliding-window boundary.
    limiter.check_ip("2001:db8::1", now=NOW + timedelta(seconds=60))

    limiter.check_sub("stable-google-sub", now=NOW)
    with pytest.raises(LoginRateLimitExceeded) as sub_limited:
        limiter.check_sub("stable-google-sub", now=NOW + timedelta(seconds=1))
    assert sub_limited.value.scope == "sub"
    limiter.check_sub("another-google-sub", now=NOW + timedelta(seconds=1))


def test_login_rate_limit_bucket_cap_fails_closed_for_new_keys():
    limiter = LoginRateLimiter(
        ip_attempts=2,
        sub_attempts=2,
        window=timedelta(minutes=1),
        max_buckets=1,
    )
    limiter.check_ip("192.0.2.1", now=NOW)
    with pytest.raises(LoginRateLimitExceeded):
        limiter.check_ip("192.0.2.2", now=NOW)
    limiter.check_ip("192.0.2.2", now=NOW + timedelta(minutes=1))
