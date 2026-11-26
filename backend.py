"""Convenience facade for callers that prefer a top-level ``backend`` module.

The implementation lives in the :mod:`employment` package.  These helpers make
it possible to evaluate the snapshot without running HTTP:

    from backend import seeker_view
    seeker_view("R1", at="13:00")
"""
from __future__ import annotations

from pathlib import Path

from employment.errors import ApiError
from employment.service import DATA_FILE, EmploymentService, fresh_state

__all__ = [
    "ApiError",
    "EmploymentService",
    "fresh_state",
    "load_service",
    "seeker_view",
    "resolve_owner",
    "identity_group",
]


def load_service(state_path: str | Path | None = None) -> EmploymentService:
    if state_path:
        return EmploymentService.load_or_seed(state_path)
    return EmploymentService.seeded()


def seeker_view(registrant: str = "R1", at: str | None = None,
                appointment_id: str | None = None) -> dict:
    return EmploymentService.seeded().job_seeker_view(registrant, at, appointment_id)


def resolve_owner(opportunity: str = "J1", registrant: str | None = None,
                  at: str | None = None) -> dict:
    return EmploymentService.seeded().resolve_opportunity_ownership(opportunity, registrant, at)


def identity_group(registrant: str = "R1", at: str | None = None) -> list[str]:
    return EmploymentService.seeded().identity_group(registrant, at)


if __name__ == "__main__":
    import json

    print(json.dumps(seeker_view(at="13:00"), ensure_ascii=False, indent=2))
