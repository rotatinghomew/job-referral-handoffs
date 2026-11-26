"""JSON HTTP adapter for the employment-window backend using stdlib only."""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import ApiError
from .service import EmploymentService

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = ROOT / "var" / "employment-state.json"

ROUTE_DOCS = {
    "service": "employment-window-backend",
    "endpoints": [
        "GET /health",
        "GET /api/query?registrant=R1&at=13:00",
        "GET /api/job-seeker/view?registrant=R1&at=13:00",
        "GET /api/ownership?opportunity=J1&at=12:40",
        "POST /api/registrants",
        "POST /api/referrals",
        "POST /api/opportunities/{id}/referrals",
        "POST /api/referrals/{id}/receipt",
        "POST /api/referrals/receive",
        "POST /api/exchange/updates",
        "POST /api/exchange/legacy-receipt",
        "POST /api/identity-cases",
        "POST /api/identity-cases/{id}/decision",
        "POST /api/identity-cases/{id}/objection",
        "POST /api/identity-cases/{id}/split",
        "POST /api/identity-disputes",
        "GET /api/materials/provenance?identity_case=I3",
        "POST /api/materials/verifications",
        "POST /api/grants",
        "POST /api/grants/{id}/withdraw",
        "POST /api/opportunities",
        "POST /api/opportunities/{id}/close",
        "POST /api/opportunities/{id}/reopen",
        "POST /api/appointments",
        "POST /api/appointments/{id}/transfer",
        "POST /api/transfers",
        "POST /api/appointments/{id}/contact-acknowledgement",
        "POST /api/contact-acknowledgements",
        "POST /api/transfers/{id}/acknowledge",
        "POST /api/appointments/{id}/slot-choice",
        "POST /api/appointments/{id}/cancel",
        "POST /api/appointments/{id}/no-show",
        "GET /api/employer/opportunities/{id}/referrals",
        "GET /api/employers/{employerId}/opportunities/{id}/referrals",
    ],
    "auth_headers": ["X-Station-Id: ST1", "X-Employer-Id: E1", "X-Registrant-Id: R1"],
}


def _query_one(query: dict[str, list[str]], key: str, default=None):
    values = query.get(key)
    return values[0] if values else default


_PATH_ALIASES = {
    "identity_cases": "identity-cases",
    "job_seeker": "job-seeker",
    "case_updates": "case-updates",
}


def _canonical_path(path: list[str]) -> list[str]:
    path = [_PATH_ALIASES.get(part, part) for part in path]
    if path and path[0] != "api" and path[0] not in {"health", "healthz"}:
        path = ["api", *path]
    return path


def _query_at(query) -> str | None:
    return _query_one(query, "at") or _query_one(query, "as_of") or _query_one(query, "asOf")


def _query_registrant(query, actor: dict | None = None) -> str | None:
    return (_query_one(query, "registrant") or _query_one(query, "registrant_id")
            or _query_one(query, "id") or ((actor or {}).get("registrant")))


def create_handler(service: EmploymentService):
    class EmploymentHandler(BaseHTTPRequestHandler):
        server_version = "EmploymentBackend/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            # Keep test output clean; errors are still returned as JSON.
            if os.environ.get("EMPLOYMENT_HTTP_DEBUG"):
                super().log_message(fmt, *args)

        def _actor(self) -> dict[str, str | None]:
            return {
                "station": self.headers.get("X-Station-Id"),
                "employer": self.headers.get("X-Employer-Id"),
                "registrant": self.headers.get("X-Registrant-Id"),
            }

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ApiError(400, "invalid_json", "请求体必须是 UTF-8 JSON") from exc
            if not isinstance(data, dict):
                raise ApiError(400, "json_object_required", "请求体必须是 JSON 对象")
            return data

        def do_GET(self) -> None:  # noqa: N802
            try:
                parsed = urlparse(self.path)
                path = _canonical_path([p for p in parsed.path.split("/") if p])
                query = parse_qs(parsed.query)
                result = self._route_get(path, query)
                if result is None:
                    raise ApiError(404, "route_not_found", parsed.path)
                self._send_json(200, result)
            except ApiError as exc:
                status, body = exc.to_response()
                self._send_json(status, body)
            except Exception as exc:  # pragma: no cover - defensive HTTP boundary
                self._send_json(500, {"error": {"code": "internal_error", "message": str(exc)}})

        def do_POST(self) -> None:  # noqa: N802
            try:
                parsed = urlparse(self.path)
                path = _canonical_path([p for p in parsed.path.split("/") if p])
                body = self._read_json()
                body.pop("actor", None)
                for old_name, new_name in (
                    ("registrant_id", "registrant"),
                    ("opportunity_id", "opportunity"),
                    ("target_window", "target_station"),
                    ("decided_time", "decided_at"),
                    ("received_time", "received_at"),
                ):
                    if old_name in body:
                        value = body.pop(old_name)
                        body.setdefault(new_name, value)
                result = self._route_post(path, body)
                if result is None:
                    raise ApiError(404, "route_not_found", parsed.path)
                status = 201 if isinstance(result, dict) and result.pop("_created", False) else 200
                self._send_json(status, result)
            except ApiError as exc:
                status, payload = exc.to_response()
                self._send_json(status, payload)
            except Exception as exc:  # pragma: no cover
                self._send_json(500, {"error": {"code": "internal_error", "message": str(exc)}})

        def _route_get(self, path: list[str], query) -> Any:
            actor = self._actor()
            at = _query_at(query)

            if path == [] or path == ["api"] or path == ["api", "docs"]:
                return ROUTE_DOCS
            if path in (["health"], ["healthz"], ["api", "health"], ["api", "healthz"]):
                return {"ok": True, "service": "employment-window-backend"}
            if path in (["api", "query"], ["api", "inquiry"]):
                registrant = _query_registrant(query, actor)
                if not registrant:
                    raise ApiError(400, "registrant_required", "缺少 registrant")
                return service.job_seeker_view(
                    registrant, at=at, appointment_id=_query_one(query, "appointment"),
                )

            if actor.get("employer") and not actor.get("station") and (
                path[:2] == ["api", "identity-cases"]
                or path == ["api", "identity-graph"]
                or path[:2] == ["api", "registrants"]
                or path[:2] == ["api", "job-seeker"]
                or path in (["api", "query"], ["api", "inquiry"], ["api", "ownership"])
                or path == ["api", "materials", "provenance"]
            ):
                raise ApiError(404, "route_not_found", "企业入口不提供跨机会身份或来源查询")

            if path == ["api", "stations"]:
                return list(service.state["stations"].values())

            if path == ["api", "registrants"]:
                return list(service.state["registrants"].values())
            if len(path) == 3 and path[:2] == ["api", "registrants"]:
                return service._get("registrants", path[2], "登记记录")

            if path == ["api", "identity-cases"]:
                return list(service.state["identity_cases"].values())
            if len(path) == 3 and path[:2] == ["api", "identity-cases"]:
                return service._get("identity_cases", path[2], "身份核验")
            if path == ["api", "identity-graph"]:
                registrant = _query_one(query, "registrant")
                if not registrant:
                    raise ApiError(400, "registrant_required", "缺少 registrant")
                return service.identity_view(registrant, at)

            if path == ["api", "materials", "provenance"]:
                return service.material_provenance(
                    registrant=_query_one(query, "registrant"),
                    identity_case=_query_one(query, "identity_case"), at=at,
                )
            if len(path) == 4 and path[:2] == ["api", "materials"] and path[3] == "content":
                return service.access_material_content(
                    path[2], actor=actor,
                    employer=_query_one(query, "employer"),
                    opportunity=_query_one(query, "opportunity"), at=at,
                )

            if len(path) == 3 and path[:2] == ["api", "materials"]:
                material = service._get("materials", path[2], "材料")
                return {key: value for key, value in material.items() if key != "content"}

            if path == ["api", "grants"]:
                registrant = _query_one(query, "registrant")
                rows = list(service.state["grants"].values())
                if registrant:
                    rows = [g for g in rows if g["registrant"] == registrant]
                return rows

            if path == ["api", "opportunities"]:
                rows = list(service.state["opportunities"].values())
                if actor.get("employer") and not actor.get("station"):
                    rows = [o for o in rows if o["employer"] == actor["employer"]]
                return rows
            if len(path) == 3 and path[:2] == ["api", "opportunities"]:
                opp = service._get("opportunities", path[2], "岗位机会")
                if actor.get("employer") and not actor.get("station") and opp["employer"] != actor["employer"]:
                    raise ApiError(404, "opportunity_not_found", "机会不存在")
                return opp
            if len(path) == 4 and path[:2] == ["api", "opportunities"] and path[3] == "referrals":
                if actor.get("employer") and not actor.get("station"):
                    return service.employer_opportunity_view(actor["employer"], path[2], at)
                opp = service._get("opportunities", path[2], "岗位机会")
                refs = [r for r in service.state["referrals"].values() if r["opportunity"] == opp["id"]]
                return {"opportunity": opp, "referrals": refs}

            if path == ["api", "referrals"]:
                rows = list(service.state["referrals"].values())
                if actor.get("employer") and not actor.get("station"):
                    employer = actor["employer"]
                    rows = [r for r in rows
                            if service.state["opportunities"].get(r["opportunity"], {}).get("employer") == employer]
                return rows
            if len(path) == 3 and path[:2] == ["api", "referrals"]:
                ref = service._get("referrals", path[2], "转介")
                if actor.get("employer") and not actor.get("station"):
                    return service.employer_referral_view(actor["employer"], ref["id"], at)
                return ref

            if len(path) == 3 and path[:2] == ["api", "appointments"]:
                appt = service._get("appointments", path[2], "约见")
                if actor.get("employer") and not actor.get("station") and appt["employer"] != actor["employer"]:
                    raise ApiError(404, "appointment_not_found", "约见不存在")
                return appt

            if path == ["api", "job-seeker", "view"] or path == ["api", "job-seeker"]:
                registrant = _query_registrant(query, actor)
                if not registrant:
                    raise ApiError(400, "registrant_required", "缺少 registrant 或 X-Registrant-Id")
                appointment = _query_one(query, "appointment") or _query_one(query, "appointment_id")
                return service.job_seeker_view(registrant, at=at, appointment_id=appointment)

            if path == ["api", "case-updates"]:
                return sorted(service.state["updates"].values(),
                              key=lambda u: str(u.get("occurred") or ""))

            if path == ["api", "ownership"]:
                opportunity = _query_one(query, "opportunity")
                if not opportunity:
                    raise ApiError(400, "opportunity_required", "缺少 opportunity")
                return service.resolve_opportunity_ownership(
                    opportunity, registrant=_query_registrant(query), at=at,
                )

            if (len(path) == 6 and path[0] == "api"
                    and path[1] in {"employer", "employers"}
                    and path[3] == "opportunities" and path[5] == "referrals"):
                employer = path[2]
                if actor.get("employer") and not actor.get("station") and actor["employer"] != employer:
                    raise ApiError(404, "opportunity_not_found", "机会不存在")
                return service.employer_opportunity_view(employer, path[4], at)

            if (len(path) == 5 and path[:2] == ["api", "employer"]
                    and path[2] == "opportunities" and path[4] == "referrals"):
                employer = actor.get("employer") or _query_one(query, "employer")
                if not employer:
                    raise ApiError(401, "employer_required", "请通过 X-Employer-Id 标明企业")
                return service.employer_opportunity_view(employer, path[3], at)

            if len(path) == 4 and path[:2] == ["api", "employer", "referrals"]:
                employer = actor.get("employer") or _query_one(query, "employer")
                if not employer:
                    raise ApiError(401, "employer_required", "请通过 X-Employer-Id 标明企业")
                return service.employer_referral_view(employer, path[3], at)

            return None

        def _route_post(self, path: list[str], body: dict[str, Any]) -> Any:
            actor = self._actor()

            if path == ["api", "admin", "reset"]:
                return service.reset_to_seed()

            if path == ["api", "registrants"]:
                result = service.create_registrant(actor=actor, **body)
                result["_created"] = True
                return result

            if path == ["api", "identity-cases"]:
                return service.create_identity_case(actor=actor, **body)
            if len(path) == 4 and path[:2] == ["api", "identity-cases"] and path[3] == "decision":
                return service.decide_identity_case(path[2], actor=actor, **body)
            if len(path) == 4 and path[:2] == ["api", "identity-cases"] and path[3] in {"objection", "dispute"}:
                return service.object_to_identity_case(path[2], actor=actor, **body)
            if path == ["api", "identity-disputes"]:
                case_id = body.pop("case_id", None) or body.pop("identity_case", None)
                if not case_id:
                    raise ApiError(400, "case_id_required", "身份异议必须带 case_id")
                return service.object_to_identity_case(case_id, actor=actor, **body)
            if len(path) == 4 and path[:2] == ["api", "identity-cases"] and path[3] == "split":
                return service.split_identity(case_id=path[2], actor=actor, **body)
            if path == ["api", "identity", "split"]:
                return service.split_identity(actor=actor, **body)

            if path == ["api", "materials", "verifications"]:
                return service.verify_materials(actor=actor, **body)

            if path == ["api", "grants"]:
                return service.create_grant(actor=actor, **body)
            if len(path) == 4 and path[:2] == ["api", "grants"] and path[3] == "withdraw":
                return service.withdraw_grant(path[2], actor=actor, **body)

            if path == ["api", "opportunities"]:
                return service.create_opportunity(actor=actor, **body)
            if len(path) == 4 and path[:2] == ["api", "opportunities"] and path[3] == "close":
                return service.close_opportunity(path[2], actor=actor, **body)
            if len(path) == 4 and path[:2] == ["api", "opportunities"] and path[3] == "reopen":
                return service.reopen_opportunity(path[2], actor=actor, **body)

            if (len(path) == 4 and path[:2] == ["api", "opportunities"]
                    and path[3] == "referrals"):
                body = {**body, "opportunity": path[2]}
                result = service.create_referral(actor=actor, **body)
                result["_created"] = True
                return result

            if path == ["api", "referrals"]:
                result = service.create_referral(actor=actor, **body)
                result["_created"] = True
                return result
            if len(path) == 4 and path[:2] == ["api", "referrals"] and path[3] == "receipt":
                return service.receive_referral(path[2], actor=actor, **body)
            if len(path) == 3 and path[:2] == ["api", "referrals"] and path[2] == "receive":
                referral_id = body.pop("referral_id", None) or body.pop("id", None) or body.pop("referral", None)
                if not referral_id:
                    raise ApiError(400, "referral_id_required", "收件回执必须带 referral_id")
                return service.receive_referral(referral_id, actor=actor, **body)

            if path in (
                ["api", "exchange", "updates"],
                ["api", "exchange", "case-updates"],
                ["api", "case-updates"],
            ):
                # Accept either one update object or a {"updates": [...]} batch.
                rows = body.get("updates") or body.get("case_updates")
                if isinstance(rows, list):
                    return {"results": [service.record_update(u, actor=actor) for u in rows]}
                return service.record_update(body, actor=actor)
            if path == ["api", "exchange", "legacy-receipt"]:
                return service.legacy_receipt(actor=actor, **body)
            if path == ["api", "exchange", "legacy-closure"]:
                return service.legacy_close(actor=actor, **body)

            if path == ["api", "owner-choices"]:
                return service.record_owner_choice(actor=actor, **body)

            if path == ["api", "appointments"]:
                result = service.create_appointment(actor=actor, **body)
                result["_created"] = True
                return result
            if len(path) == 4 and path[:2] == ["api", "appointments"]:
                action = path[3]
                if action == "transfer":
                    return service.request_transfer(path[2], actor=actor, **body)
                if action == "contact-acknowledgement":
                    return service.acknowledge_contact_change(path[2], actor=actor, **body)
                if action in {"slot-choice", "choose-slot"}:
                    return service.choose_appointment_slot(path[2], actor=actor, **body)
                if action == "cancel":
                    return service.cancel_appointment(path[2], actor=actor, **body)
                if action == "no-show":
                    return service.mark_no_show(path[2], actor=actor, **body)

            if path in (["api", "transfers"], ["api", "contact-transfers"]):
                appointment_id = (body.pop("appointment", None) or body.pop("appointment_id", None)
                                 or body.pop("entity", None))
                target_station = body.pop("target_station", None) or body.pop("to_station", None) or body.pop("station", None)
                if not appointment_id or not target_station:
                    raise ApiError(400, "appointment_and_target_required",
                                    "转交必须提供 appointment/appointment_id 和 target_station")
                return service.request_transfer(
                    appointment_id, target_station, actor=actor, **body,
                )

            if len(path) == 4 and path[:2] == ["api", "transfers"] and path[3] == "acknowledge":
                return service.acknowledge_transfer(path[2], actor=actor, **body)
            if path == ["api", "contact-acknowledgements"]:
                appointment_id = body.pop("appointment", None) or body.pop("appointment_id", None) or body.pop("entity", None)
                if not appointment_id:
                    raise ApiError(400, "appointment_required", "联系人确认必须带 appointment")
                to_station = body.pop("to_station", None)
                target_station = body.pop("target_station", None)
                station = body.get("station") or to_station or target_station
                if station:
                    body["station"] = station
                return service.acknowledge_contact_change(appointment_id, actor=actor, **body)

            return None

    return EmploymentHandler


def create_server(host: str | int | None = None, port: int | str | None = None,
                  state_path: str | Path | None = None) -> ThreadingHTTPServer:
    # Convenience for callers that invoke create_server(0) or create_server(8000).
    if isinstance(host, int):
        port = host
        host = None
    path = Path(os.environ.get("EMPLOYMENT_STATE", state_path or DEFAULT_STATE))
    service = EmploymentService.load_or_seed(path)
    handler = create_handler(service)
    host = host or os.environ.get("HOST", "127.0.0.1")
    if port is None:
        port = int(os.environ.get("PORT", "8000"))
    else:
        port = int(port)
    server = ThreadingHTTPServer((host, port), handler)
    server.employment_service = service  # type: ignore[attr-defined]
    return server


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Employment-window referral backend")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--state", default=os.environ.get("EMPLOYMENT_STATE", str(DEFAULT_STATE)))
    args = parser.parse_args()

    server = create_server(args.host, args.port, args.state)
    host, port = server.server_address[:2]
    print(f"Employment window backend listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
