"""Process-wide JWT validator cache.

``JWTValidator``'s expensive state is its ``JWKSFetcher`` — OIDC discovery plus a
PyJWKClient whose JWKS cache lives on the *instance*. Constructing a fresh validator
per request/connect (the previous pattern at every call site) re-fetched
``.well-known/openid-configuration`` and the JWKS from Keycloak each time; with the
embedded widget reconnecting roughly every token lifetime, that is recurring latency
on every socket connect and steady background load on Keycloak.

``get_jwt_validator`` shares one ``JWKSFetcher`` per issuer and memoizes validators
per (issuer, expected_azp, expected_aud). Validators are stateless beyond the
fetcher, so sharing across requests/tasks is safe; a benign construction race at
worst builds one duplicate that is immediately superseded.
"""

from __future__ import annotations

from ringier_a2a_sdk.auth import JWTValidator
from ringier_a2a_sdk.auth.jwt_validator import JWKSFetcher

_jwks_fetchers: dict[str, JWKSFetcher] = {}
_validators: dict[tuple[str, tuple[str, ...] | str | None, str | None], JWTValidator] = {}


def get_jwt_validator(
    issuer: str,
    expected_azp: str | list[str] | None = None,
    expected_aud: str | None = None,
) -> JWTValidator:
    """Return a shared ``JWTValidator`` for the given expectations.

    Same-issuer validators share one ``JWKSFetcher`` (and therefore one OIDC
    discovery + JWKS cache) even when their azp/aud expectations differ.
    """
    azp_key: tuple[str, ...] | str | None = (
        tuple(sorted(expected_azp)) if isinstance(expected_azp, list) else expected_azp
    )
    key = (issuer, azp_key, expected_aud)
    validator = _validators.get(key)
    if validator is None:
        fetcher = _jwks_fetchers.get(issuer)
        if fetcher is None:
            fetcher = _jwks_fetchers[issuer] = JWKSFetcher(issuer)
        validator = _validators[key] = JWTValidator(
            issuer=issuer,
            expected_azp=expected_azp,
            expected_aud=expected_aud,
            jwks_fetcher=fetcher,
        )
    return validator


def clear_jwt_validator_cache() -> None:
    """Drop all cached fetchers/validators (tests, or an issuer key rollover)."""
    _jwks_fetchers.clear()
    _validators.clear()
