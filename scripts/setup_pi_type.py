"""
Create the PiType Twin Type and the Thing `<namespace>:pi_0` for the Raspberry Pi
telemetry sync (notes/PI_TELEMETRY_SPEC_2026-10-11.md section 8).

DRY-RUN BY DEFAULT: with no flag this prints the requests it would send and makes
none -- not even a read. Only `--apply` talks to the local Ditto / Extended API
(the same endpoints scripts/setup_types_and_twins.py uses), and only to:
  GET  policy, PUT the Type, POST create the Thing, GET the Thing to verify.
pi_0 is deliberately NOT linked under any rack: it sits outside the rack tree.

Run from the repo root:
  python scripts/setup_pi_type.py            # show the plan
  python scripts/setup_pi_type.py --apply    # do it (needs the port-forwards up)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from twin import pi_protocol as pp  # noqa: E402
from twin.config import POLICY_ID  # noqa: E402
from twin.ditto_client import ditto, enc  # noqa: E402
from twin.pi_receiver import PI_LINK_FEATURE, PI_THING_ID, PI_TYPE_ID  # noqa: E402

HARDWARE_MODEL = "Raspberry Pi 3 Model B Plus Rev 1.3"  # read from the device, 2026-10-11


def pi_type_body() -> Dict[str, Any]:
    units = "; ".join(f"{name} {unit}" for name, unit in pp.FEATURE_UNITS.items())
    description = (
        "Raspberry Pi 3 B+ physical telemetry twin type. Units: " + units + ". "
        f"{PI_LINK_FEATURE}: session_id, last_seq, last_received_epoch_ms (receiver clock, epoch UTC ms)."
    )
    return {
        "policyId": POLICY_ID,
        "attributes": {
            "name": "PiType",
            "text_description": description,
            "hardware_model": HARDWARE_MODEL,
        },
        # Empty properties on purpose: NodeType seeds 0.0, but a Pi that has never
        # reported must not look like it measured 0.0 ("omitted is not fresh").
        "features": {name: {"properties": {}} for name in tuple(pp.FEATURES) + (PI_LINK_FEATURE,)},
    }


def pi_twin_body() -> Dict[str, Any]:
    return {"attributes": {"name": "pi_0", "hardware_model": HARDWARE_MODEL}}


def plan() -> List[Dict[str, Any]]:
    return [
        {
            "label": f"create Twin Type {PI_TYPE_ID}",
            "method": "PUT",
            "path": f"/api/types/{enc(PI_TYPE_ID)}",
            "body": pi_type_body(),
        },
        {
            "label": f"create Thing {PI_THING_ID} from {PI_TYPE_ID}",
            "method": "POST",
            "path": f"/api/types/{enc(PI_TYPE_ID)}/create/{enc(PI_THING_ID)}",
            "body": pi_twin_body(),
        },
    ]


def print_plan() -> None:
    print("DRY RUN -- the requests below are what --apply would send to the Extended API.")
    print(f"Policy (must already exist): {POLICY_ID}")
    for step in plan():
        print(f"\n# {step['label']}")
        print(f"{step['method']} {step['path']}")
        print(json.dumps(step["body"], indent=2))
    print("\nDRY RUN: no request was made. Re-run with --apply to send these.")


def apply_plan() -> None:
    # Imported only on --apply: setup_types_and_twins.py is the existing script whose put_type() and
    # create_twin_from_type() are reused so the idempotency rules stay identical to the original setup.
    from setup_types_and_twins import create_twin_from_type, put_type

    r = ditto("GET", f"/api/2/policies/{enc(POLICY_ID)}")
    if r.status_code != 200:
        raise SystemExit(
            f"Policy {POLICY_ID} not found (HTTP {r.status_code}); nothing was written. "
            "Run scripts/setup_types_and_twins.py first, or check the port-forwards."
        )
    put_type(PI_TYPE_ID, pi_type_body())
    create_twin_from_type(PI_TYPE_ID, PI_THING_ID, pi_twin_body())
    r = ditto("GET", f"/api/2/things/{enc(PI_THING_ID)}")
    if r.status_code != 200:
        raise RuntimeError(f"verify GET {PI_THING_ID}: HTTP {r.status_code} {r.text[:300]}")
    features = sorted((r.json().get("features") or {}).keys())
    print(f"OK verified {PI_THING_ID}; features: {features}")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Create PiType and pi_0 (dry-run unless --apply).")
    p.add_argument("--apply", action="store_true", help="actually send the requests (default: print the plan only)")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.apply:
        apply_plan()
    else:
        print_plan()
    return 0


if __name__ == "__main__":
    sys.exit(main())
