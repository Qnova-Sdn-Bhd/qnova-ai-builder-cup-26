"""Create once or update an explicitly named managed ADK runtime using ADC.

Run with `uv --directory agent run python ../scripts/deploy_agent.py --help`.
The prepared manifest is safe to review without --allow-deploy; no cloud calls occur then.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent" / "src"))
REQUIREMENTS_FILE = ROOT / "agent" / "requirements.txt"


def deployment_requirements(path=REQUIREMENTS_FILE):
    """Pass pinned PEP 508 entries, including environment markers, to the SDK.

    SDK 2.5.0 reads a file path with splitlines() and parses even uv's annotation
    comments as package constraints. Filtering here leaves the lock export intact.
    """
    from packaging.requirements import Requirement

    requirements = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        pins = list(requirement.specifier)
        if (
            requirement.url
            or len(pins) != 1
            or pins[0].operator != "=="
            or "*" in pins[0].version
        ):
            raise ValueError(
                "Agent deployment requires exact pinned dependencies from the lock export"
            )
        requirements.append(line)
    if not requirements:
        raise ValueError("The agent dependency lock export is empty")
    return requirements


def manifest(args):
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", args.project):
        raise ValueError("A concrete Google Cloud project ID is required")
    if args.resource_name and not re.fullmatch(
        rf"projects/(?:{re.escape(args.project)}|[0-9]+)/locations/"
        rf"{re.escape(args.location)}/reasoningEngines/[0-9]+",
        args.resource_name,
    ):
        raise ValueError("Runtime must belong to the selected project/location")
    if not args.service_account.endswith(f"@{args.project}.iam.gserviceaccount.com"):
        raise ValueError("Use the dedicated agent account in the selected project")
    if args.model == "gemini-3.8-flash" and args.model_location not in {
        "us",
        "eu",
        "global",
    }:
        raise ValueError(
            "Gemini 3.8 Flash requires us, eu or global for --model-location"
        )
    return {
        "display_name": "Handoff Guardian",
        "description": "Read-only bounded handoff evidence analysis; no business mutation tools",
        "staging_bucket": f"gs://{args.staging_bucket.removeprefix('gs://')}",
        "requirements": deployment_requirements(),
        "extra_packages": ["handoff_agent"],
        "service_account": args.service_account,
        "env_vars": {
            # Agent Runtime injects project/location; these names are reserved.
            # The serialized app already carries the explicit project/location.
            "GEMINI_LOCATION": args.model_location,
            "GOOGLE_GENAI_USE_VERTEXAI": "true",
            "GEMINI_MODEL": args.model,
            # SDK 2.5.0 enables event logging independently of enable_tracing.
            "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "false",
        },
        "min_instances": 0,
        "max_instances": 2,
        "container_concurrency": 1,
        "python_version": "3.12",
        "resource_limits": {"cpu": "2", "memory": "4Gi"},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--location", default="us-central1")
    parser.add_argument(
        "--model-location",
        default="us",
        help="Gemini inference location; separate from the regional managed runtime",
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--staging-bucket", required=True)
    parser.add_argument("--service-account", required=True)
    parser.add_argument(
        "--resource-name",
        help="Update this existing runtime; omit only for first create",
    )
    parser.add_argument("--allow-deploy", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        help="Write observed deployment metadata (no credentials)",
    )
    args = parser.parse_args()
    if args.output:
        args.output = args.output.resolve()
    config = manifest(args)
    if not args.allow_deploy:
        print(
            json.dumps(
                {
                    "status": "prepared",
                    "project": args.project,
                    "resource_name": args.resource_name,
                    "working_directory": str(ROOT / "agent" / "src"),
                    "config": config,
                },
                indent=2,
            )
        )
        return 0
    import agentplatform
    from handoff_agent.workflow import create_app

    app = create_app(
        args.project, args.location, args.model, model_location=args.model_location
    )
    client = agentplatform.Client(project=args.project, location=args.location)
    # SDK tar.add preserves path components. Package from this directory so the
    # managed runtime imports handoff_agent at archive root on both Windows/Linux.
    os.chdir(ROOT / "agent" / "src")
    # Reuse the emitted name on every subsequent deploy. A failed create must be reconciled
    # using the Runtime inventory before retrying; the script never retries ambiguous creates.
    if args.resource_name:
        deployed = client.runtimes.update(
            name=args.resource_name, agent=app, config=config
        )
    else:
        deployed = client.runtimes.create(agent=app, config=config)
    name = deployed.api_resource.name
    schemas = deployed.operation_schemas()
    result = {
        "status": "deployed",
        "resource_name": name,
        "model": args.model,
        "sdk_version": agentplatform.__version__,
        "operation_schemas": schemas,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, indent=2, default=str), encoding="utf-8"
        )
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
