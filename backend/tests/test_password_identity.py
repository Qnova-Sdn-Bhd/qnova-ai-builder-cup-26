"""Identity Platform SDK boundary tests; no password or forged sign-in reaches the API."""

import pytest
from fastapi.testclient import TestClient

from handoff_guardian.api import create_app
from handoff_guardian.errors import AppError
from handoff_guardian.google_adapters import FirebaseIdentity


def identity_with_claims(monkeypatch, claims):
    from firebase_admin import auth

    calls = []
    identity = FirebaseIdentity.__new__(FirebaseIdentity)
    identity.app = object()

    def verify(token, **kwargs):
        calls.append((token, kwargs))
        return claims

    monkeypatch.setattr(auth, "verify_id_token", verify)
    return identity, calls


@pytest.mark.parametrize("privileged", [False, True])
def test_sdk_verified_password_identity_requires_verified_email_and_keeps_revocation_check(
    monkeypatch, privileged
):
    identity, calls = identity_with_claims(
        monkeypatch, {"uid": "reviewer", "email_verified": True, "firebase": {"sign_in_provider": "password"}}
    )
    assert identity.verify("opaque-signed-id-token", privileged) == "reviewer"
    assert calls == [("opaque-signed-id-token", {"app": identity.app, "check_revoked": privileged})]


@pytest.mark.parametrize("provider", ["google.com", "custom", "anonymous", "phone", "", None])
def test_verified_federated_or_custom_token_cannot_enter_password_only_application(monkeypatch, provider):
    identity, _ = identity_with_claims(
        monkeypatch, {"uid": "reviewer", "email_verified": True, "firebase": {"sign_in_provider": provider}}
    )
    with pytest.raises(AppError) as error:
        identity.verify("verified-other-provider-token", True)
    assert error.value.code == "UNAUTHENTICATED" and error.value.status == 401


@pytest.mark.parametrize("firebase", [None, "password", {}])
def test_provider_claim_is_mandatory_and_structured(monkeypatch, firebase):
    identity, _ = identity_with_claims(
        monkeypatch, {"uid": "reviewer", "email_verified": True, "firebase": firebase}
    )
    with pytest.raises(AppError, match="UNAUTHENTICATED"):
        identity.verify("signed-token", False)


@pytest.mark.parametrize("verified", [False, None, "true", 1])
def test_password_token_requires_strictly_verified_email(monkeypatch, verified):
    identity, _ = identity_with_claims(
        monkeypatch,
        {"uid": "reviewer", "email_verified": verified, "firebase": {"sign_in_provider": "password"}},
    )
    with pytest.raises(AppError) as error:
        identity.verify("signed-unverified-token", False)
    assert error.value.code == "EMAIL_NOT_VERIFIED" and error.value.status == 403


def test_unverified_account_cannot_reach_domain_binding_or_project_reads(system, monkeypatch):
    guardian, _ = system
    identity, _ = identity_with_claims(
        monkeypatch,
        {"uid": "reviewer", "email_verified": False, "firebase": {"sign_in_provider": "password"}},
    )
    monkeypatch.setattr(
        guardian, "actor_for_uid", lambda uid: pytest.fail("Unverified account reached domain state")
    )
    with TestClient(create_app(guardian, identity)) as client:
        response = client.get("/api/v1/projects", headers={"Authorization": "Bearer opaque-id-token"})
    assert response.status_code == 403 and response.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"


def test_verified_registration_alone_does_not_grant_membership(system, monkeypatch):
    guardian, _ = system
    identity, _ = identity_with_claims(
        monkeypatch,
        {"uid": "unprovisioned", "email_verified": True, "firebase": {"sign_in_provider": "password"}},
    )
    with TestClient(create_app(guardian, identity)) as client:
        response = client.get("/api/v1/projects", headers={"Authorization": "Bearer opaque-id-token"})
    assert response.status_code == 403 and response.json()["error"]["code"] == "IDENTITY_NOT_PROVISIONED"


def test_signature_or_revocation_failure_never_becomes_password_identity(monkeypatch):
    from firebase_admin import auth

    identity = FirebaseIdentity.__new__(FirebaseIdentity)
    identity.app = object()

    def reject(*args, **kwargs):
        raise auth.InvalidIdTokenError("SECRET_PROVIDER_ERROR")

    monkeypatch.setattr(auth, "verify_id_token", reject)
    with pytest.raises(AppError) as error:
        identity.verify("SECRET_TOKEN", True)
    assert error.value.code == "UNAUTHENTICATED" and "SECRET" not in error.value.message
