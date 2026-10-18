"""Offline proof of explicit keyless operator credentials; no Google API calls."""

import copy
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_operator_scripts import DB, fake_firestore, project  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import bootstrap_reviewer as bootstrap  # noqa: E402

FAKE_TOKEN = "offline-only-service-access-token-never-a-real-credential"


def args(**changes):
    value = dict(
        google_cloud_project="test-project",
        database="(default)",
        project_id="p",
        user_id="reviewer",
        email="reviewer@example.com",
        uid=None,
        display_name="Reviewer",
        role="reviewer",
        operator_id="operator",
        change_role=False,
        revoke=False,
        gcloud_account="operator@example.com",
        impersonate_service_account="hg-api@test-project.iam.gserviceaccount.com",
        gcloud_executable="trusted-google-cli.cmd",
    )
    return SimpleNamespace(**(value | changes))


def mock_cli(monkeypatch, returncode=0, stdout=FAKE_TOKEN, stderr=""):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    return calls


def test_cli_credentials_are_explicit_short_lived_in_memory_and_silent(
    monkeypatch, capsys
):
    calls = mock_cli(monkeypatch)
    credential = bootstrap.cli_credentials(args())
    assert credential.token == FAKE_TOKEN and credential.refresh_token is None
    assert credential.quota_project_id == "test-project"
    expiry = credential.expiry.replace(tzinfo=UTC)
    assert (
        datetime.now(UTC) + timedelta(minutes=8)
        < expiry
        < datetime.now(UTC) + timedelta(minutes=10)
    )
    command, options = calls[0]
    assert command[:3] == ["trusted-google-cli.cmd", "auth", "print-access-token"]
    assert "--account=operator@example.com" in command
    assert (
        "--impersonate-service-account=hg-api@test-project.iam.gserviceaccount.com"
        in command
    )
    assert "--lifetime=600s" in command and FAKE_TOKEN not in " ".join(command)
    assert options == {
        "capture_output": True,
        "text": True,
        "timeout": 60,
        "check": False,
    }
    assert capsys.readouterr() == ("", "")


def test_default_adc_selection_never_invokes_cli(monkeypatch):
    calls = mock_cli(monkeypatch)
    assert (
        bootstrap.cli_credentials(
            args(gcloud_account=None, impersonate_service_account=None)
        )
        is None
    )
    assert calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"gcloud_account": None},
        {"impersonate_service_account": None},
        {"gcloud_account": "operator@example.com&print-secret"},
        {
            "impersonate_service_account": "hg-api@foreign-project.iam.gserviceaccount.com"
        },
        {
            "impersonate_service_account": "hg-api@test-project.iam.gserviceaccount.com;command"
        },
    ],
)
def test_cli_credentials_reject_missing_or_unsafe_inputs_before_invocation(
    monkeypatch, changes
):
    calls = mock_cli(monkeypatch)
    with pytest.raises(ValueError):
        bootstrap.cli_credentials(args(**changes))
    assert calls == []


def test_cli_credentials_reject_emulator_before_minting(monkeypatch):
    calls = mock_cli(monkeypatch)
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8089")
    with pytest.raises(ValueError, match="emulator"):
        bootstrap.cli_credentials(args())
    assert calls == []


@pytest.mark.parametrize(
    "returncode,stdout", [(1, FAKE_TOKEN), (0, ""), (0, "token\nSECOND_PRIVATE_LINE")]
)
def test_cli_failure_cannot_echo_captured_output(
    monkeypatch, capsys, returncode, stdout
):
    mock_cli(monkeypatch, returncode, stdout, stderr="SECRET_UPSTREAM_ERROR")
    with pytest.raises(ValueError) as captured:
        bootstrap.cli_credentials(args())
    assert "SECRET" not in str(captured.value) and FAKE_TOKEN not in str(captured.value)
    assert capsys.readouterr() == ("", "")


def test_cli_service_credential_is_supported_by_firebase_without_adc(monkeypatch):
    import firebase_admin

    mock_cli(monkeypatch)
    credential = bootstrap.cli_credentials(args())
    app = firebase_admin.initialize_app(
        credential=credential,
        options={"projectId": "test-project"},
        name="offline-cli-credential-proof",
    )
    try:
        assert app.credential.get_credential() is credential
    finally:
        firebase_admin.delete_app(app)


def setup_identity(
    monkeypatch,
    provider=True,
    disabled=False,
    email="reviewer@example.com",
    email_verified=True,
    provider_id="password",
):
    import firebase_admin
    from firebase_admin import auth

    captured = {}

    def initialize(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(firebase_admin, "initialize_app", initialize)
    identity = SimpleNamespace(
        uid="real-existing-uid",
        email=email,
        disabled=disabled,
        email_verified=email_verified,
        display_name="Real reviewer",
        provider_data=[SimpleNamespace(provider_id=provider_id)] if provider else [],
    )
    monkeypatch.setattr(auth, "get_user_by_email", lambda email, app: identity)
    for method in ("create_user", "create_custom_token", "set_custom_user_claims"):
        monkeypatch.setattr(
            auth,
            method,
            lambda *a, **kw: pytest.fail("Bootstrap must not forge a sign-in"),
        )
    return captured


@pytest.mark.usefixtures("fake_firestore")
def test_bootstrap_shares_service_credential_and_requires_existing_verified_password_identity(
    monkeypatch,
):
    mock_cli(monkeypatch)
    firebase = setup_identity(monkeypatch)
    db, firestore = DB(project()), {}

    def cloud_client(*positional, **kwargs):
        firestore.update(kwargs)
        return db

    monkeypatch.setattr(bootstrap, "cloud_client", cloud_client)
    result = bootstrap.provision(args())
    assert result["changed"] and result["role"] == "reviewer"
    assert firestore["credential"] is firebase["credential"]
    assert firebase["options"]["projectId"] == "test-project"
    assert db.docs["projects/p/memberships/reviewer"] == {
        "user_id": "reviewer",
        "role": "reviewer",
    }
    assert FAKE_TOKEN not in json.dumps(db.docs)
    assert len([key for key in db.docs if key.startswith("identity_bindings/")]) == 1


@pytest.mark.parametrize(
    "identity",
    [
        {"provider": False},
        {"disabled": True},
        {"email": "foreign@example.com"},
        {"email_verified": False},
        {"provider_id": "google.com"},
        {"provider_id": "custom"},
    ],
)
@pytest.mark.usefixtures("fake_firestore")
def test_bootstrap_does_not_grant_role_without_matching_verified_password_identity(
    monkeypatch, identity
):
    mock_cli(monkeypatch)
    setup_identity(monkeypatch, **identity)
    db = DB(project())
    before = copy.deepcopy(db.docs)
    monkeypatch.setattr(bootstrap, "cloud_client", lambda *positional, **kwargs: db)
    with pytest.raises(ValueError):
        bootstrap.provision(args())
    assert db.docs == before
