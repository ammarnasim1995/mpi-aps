"""Authentication and role-based authorisation (server-side).

Production: users file (MPI_USERS_FILE) with SHA-256 token hashes, kept OUTSIDE source control.
Dev: MPI_DEV_AUTH=1 enables built-in DEMO users selected by X-Demo-User, refused unless bound to loopback.
Create a user entry:  python -m mpi_aps.auth create-user <user> <role> [plant,plant|*]
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sys
from dataclasses import dataclass

ALL = "*"
PERMS = {
    "central_planning_admin": {"view", "edit_schedule", "master_data", "import", "run_scenarios", "approve", "publish",
                               "view_all_plants", "ack_stale", "settings", "view_audit"},
    "central_planner": {"view", "edit_schedule", "import", "run_scenarios", "approve", "publish", "view_all_plants", "ack_stale", "view_audit"},
    "plant_planner": {"view", "edit_schedule", "run_scenarios", "import"},
    "production_supervisor": {"view"},
    "procurement_user": {"view", "view_all_plants"},
    "management_viewer": {"view", "view_all_plants", "view_audit"},
}
DEMO_USERS = {
    "demo.admin": ("central_planning_admin", ALL), "demo.central": ("central_planner", ALL),
    "demo.pk_planner": ("plant_planner", "PK-1"), "demo.pk_supervisor": ("production_supervisor", "PK-1"),
    "demo.procurement": ("procurement_user", ALL), "demo.management": ("management_viewer", ALL),
    "demo.central2": ("central_planner", ALL),
}


@dataclass(frozen=True)
class Principal:
    user: str
    role: str
    plants: tuple

    def can(self, perm: str) -> bool:
        return perm in PERMS.get(self.role, ())

    def sees_plant(self, plant: str) -> bool:
        return ALL in self.plants or self.can("view_all_plants") or plant in self.plants

    def edits_plant(self, plant: str) -> bool:
        return self.can("edit_schedule") and (ALL in self.plants or plant in self.plants)


def hash_token(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


class Authenticator:
    def __init__(self, users_file: str | None, dev: bool, host: str):
        self.dev = dev
        if dev and host not in ("127.0.0.1", "localhost", "::1"):
            raise RuntimeError("MPI_DEV_AUTH=1 is only allowed on a loopback host.")
        self.users = {}
        if users_file:
            with open(users_file) as f:
                for u in json.load(f):
                    self.users[u["token_sha256"]] = Principal(u["user"], u["role"], tuple(u.get("plants", [ALL])))

    def authenticate(self, headers) -> Principal | None:
        auth = headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            h = hash_token(auth[7:].strip())
            for known, p in self.users.items():
                if hmac.compare_digest(known, h):
                    return p
        if self.dev and headers.get("X-Demo-User") in DEMO_USERS:
            role, plants = DEMO_USERS[headers["X-Demo-User"]]
            return Principal(headers["X-Demo-User"], role, tuple(plants.split(",")))
        return None


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "create-user":
        tok = secrets.token_urlsafe(32)
        plants = (sys.argv[4] if len(sys.argv) > 4 else ALL).split(",")
        print(json.dumps({"user": sys.argv[2], "role": sys.argv[3], "plants": plants, "token_sha256": hash_token(tok)}, indent=2))
        print("TOKEN (shown once, give to the user, do not store in git):", tok, file=sys.stderr)
    else:
        print(__doc__)
