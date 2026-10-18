#!/usr/bin/env python3
"""Trusted Identity Platform membership provisioning, outside the public API.

Run with the backend's locked environment. This never creates login credentials or
stores user tokens. The real user must first register and verify an Identity Platform
email/password account through the deployed application.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta


def timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise argparse.ArgumentTypeError("Use an opaque ID without path separators.")
    return value


def cli_credentials(args):
    """Obtain an explicitly selected, short-lived service identity entirely in memory.

    Google CLI end-user OAuth is only the source for service-account impersonation;
    it never becomes an application sign-in or a synthetic Identity Platform user.
    """
    from google.oauth2.credentials import Credentials

    account = getattr(args, "gcloud_account", None)
    target = getattr(args, "impersonate_service_account", None)
    if not account and not target:
        return None
    if not account or not target:
        raise ValueError(
            "CLI credentials require both --gcloud-account and --impersonate-service-account."
        )
    check_operator_environment()
    # Keep batch-file arguments free of shell metacharacters on Windows as well.
    if not re.fullmatch(r"[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+", account):
        raise ValueError("Use an explicit real Google CLI account email.")
    if not re.fullmatch(
        rf"[a-z][a-z0-9-]*@{re.escape(args.google_cloud_project)}\.iam\.gserviceaccount\.com",
        target,
    ):
        raise ValueError(
            "The impersonated service account must belong to the selected project."
        )
    command = [
        getattr(args, "gcloud_executable", None)
        or ("gcloud.cmd" if os.name == "nt" else "gcloud"),
        "auth",
        "print-access-token",
        f"--account={account}",
        f"--project={args.google_cloud_project}",
        f"--impersonate-service-account={target}",
        "--lifetime=600s",
        "--quiet",
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=60, check=False
    )
    token = result.stdout.strip()
    if (
        result.returncode != 0
        or len(token) < 20
        or any(character.isspace() for character in token)
    ):
        raise ValueError(
            "Google CLI could not obtain the requested short-lived service identity; check operator IAM."
        )
    # A conservative local expiry leaves one minute for transport/clock differences.
    # This one-operation credential deliberately has no refresh token or key material.
    return Credentials(
        token=token,
        quota_project_id=args.google_cloud_project,
        expiry=datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=540),
    )


def check_operator_environment():
    forbidden = [key for key in os.environ if "EMULATOR" in key and os.environ[key]]
    forbidden += [
        key
        for key in ("GOOGLE_API_USE_MTLS_ENDPOINT", "FIRESTORE_HOST")
        if os.environ.get(key)
    ]
    if forbidden:
        raise ValueError("Operator commands reject emulator or endpoint overrides.")


def cloud_client(project: str, database: str, credential=None):
    import google.auth
    from google.cloud import firestore
    from google.oauth2 import service_account

    check_operator_environment()
    credentials = credential
    if credentials is None:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    if isinstance(credentials, service_account.Credentials):
        raise ValueError(
            "Service-account key files are forbidden; use approved ADC or attached identity."
        )
    return firestore.Client(project=project, database=database, credentials=credentials)


def audit(project: dict, operator: str, event: str, target: str, summary: str) -> dict:
    return {
        "id": f"aud_{uuid.uuid4().hex}",
        "project_id": project["id"],
        "actor_id": identifier(f"operator:{operator}"),
        "occurred_at": timestamp(),
        "event_type": event,
        "target_id": target,
        "summary": summary,
        "evidence_revision": project["evidence_revision"],
        "board_revision": project["board_revision"],
        "payload_hash": None,
    }


def provision(args) -> dict:
    import firebase_admin
    from firebase_admin import auth
    from google.cloud import firestore

    credential = cli_credentials(args)
    db = cloud_client(args.google_cloud_project, args.database, credential=credential)
    app = firebase_admin.initialize_app(
        credential=credential,
        options={"projectId": args.google_cloud_project, "httpTimeout": 30},
        name="hg-trusted-bootstrap",
    )
    identity = (
        auth.get_user(args.uid, app=app)
        if args.uid
        else auth.get_user_by_email(args.email, app=app)
    )
    if (
        identity.disabled
        or not identity.email
        or identity.email.casefold() != args.email.casefold()
    ):
        raise ValueError(
            "Existing enabled UID must match the explicitly supplied real account email."
        )
    if not any(
        provider.provider_id == "password" for provider in identity.provider_data
    ):
        raise ValueError(
            "The intended account must first register an email/password Identity Platform account."
        )
    if identity.email_verified is not True:
        raise ValueError(
            "The intended account must verify its email address before reviewer provisioning."
        )
    binding_id = hashlib.sha256(
        f"{args.google_cloud_project}:{identity.uid}".encode()
    ).hexdigest()
    binding_ref = db.collection("identity_bindings").document(binding_id)
    project_ref = db.collection("projects").document(args.project_id)
    user_ref = db.collection("users").document(args.user_id)
    member_ref = project_ref.collection("memberships").document(args.user_id)
    audit_id = f"aud_{uuid.uuid4().hex}"

    @firestore.transactional
    def commit(transaction):
        project = project_ref.get(transaction=transaction).to_dict()
        existing_binding = binding_ref.get(transaction=transaction).to_dict()
        existing_user = user_ref.get(transaction=transaction).to_dict()
        membership = member_ref.get(transaction=transaction).to_dict()
        if project is None:
            raise ValueError(
                "Create the target project with the trusted seed command first."
            )
        if existing_binding and existing_binding.get("user_id") != args.user_id:
            raise ValueError(
                "UID is already bound to another domain user; refusing reassignment."
            )
        if (
            existing_user
            and existing_user.get("email", "").casefold() != identity.email.casefold()
        ):
            raise ValueError(
                "Domain user is already assigned to another email; refusing reassignment."
            )
        if membership and membership["role"] != args.role and not args.change_role:
            raise ValueError(
                "Existing role differs; an intentional role change requires --change-role."
            )
        binding = {
            "uid": identity.uid,
            "identity_project_id": args.google_cloud_project,
            "user_id": args.user_id,
            "enabled": not args.revoke,
            "version": 1 if not existing_binding else existing_binding["version"] + 1,
        }
        if (
            existing_binding
            and existing_binding.get("enabled") == binding["enabled"]
            and membership == {"user_id": args.user_id, "role": args.role}
        ):
            return {
                "changed": False,
                "user_id": args.user_id,
                "role": args.role,
                "enabled": existing_binding["enabled"],
                "identity_binding_version": existing_binding["version"],
            }
        event = audit(
            project,
            args.operator_id,
            "identity_binding_revoked"
            if args.revoke
            else "identity_membership_provisioned",
            args.user_id,
            "Trusted operator updated the verified UID binding and explicit project membership.",
        )
        event["id"] = audit_id
        transaction.set(
            user_ref,
            {
                "id": args.user_id,
                "email": identity.email,
                "display_name": args.display_name
                or identity.display_name
                or "Reviewer",
            },
        )
        transaction.set(binding_ref, binding)
        transaction.set(member_ref, {"user_id": args.user_id, "role": args.role})
        transaction.create(
            project_ref.collection("audit_events").document(audit_id), event
        )
        return {
            "changed": True,
            "user_id": args.user_id,
            "role": args.role,
            "enabled": binding["enabled"],
            "identity_binding_version": binding["version"],
            "audit_id": audit_id,
        }

    return commit(db.transaction(max_attempts=5))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--google-cloud-project", required=True, type=identifier)
    parser.add_argument("--database", default="(default)")
    parser.add_argument("--project-id", required=True, type=identifier)
    parser.add_argument("--user-id", required=True, type=identifier)
    parser.add_argument(
        "--email",
        required=True,
        help="Actual verified email/password account address, never a fixture .test address.",
    )
    parser.add_argument(
        "--uid",
        help="Optional existing Identity Platform UID; email is independently verified.",
    )
    parser.add_argument("--display-name")
    parser.add_argument(
        "--gcloud-account",
        help="Explicit already-authenticated Google CLI account for impersonation.",
    )
    parser.add_argument(
        "--impersonate-service-account",
        help="Dedicated target service account in this project; requires Token Creator permission.",
    )
    parser.add_argument(
        "--gcloud-executable",
        help="Explicit trusted Google CLI executable path, if absent from PATH.",
    )
    parser.add_argument("--role", choices=["reviewer", "viewer"], default="reviewer")
    parser.add_argument(
        "--operator-id",
        required=True,
        type=identifier,
        help="Opaque named operator ID for audit attribution, without email/path characters.",
    )
    parser.add_argument("--change-role", action="store_true")
    parser.add_argument(
        "--revoke",
        action="store_true",
        help="Disable this UID binding globally; preserves history.",
    )
    args = parser.parse_args()
    try:
        print(json.dumps(provision(args), sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001 -- sanitize remote errors at the trusted CLI boundary
        # Never print remote exceptions, identity claims, tokens or source bodies.
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": str(exc)
                    if isinstance(exc, ValueError)
                    else "ADC/Identity Platform/Firestore operation failed; check operator IAM and project.",
                    "error_type": type(exc).__name__,
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
