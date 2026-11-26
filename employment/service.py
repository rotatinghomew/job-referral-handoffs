"""Core domain service for cross-window referrals, identity and ownership.

The service is storage-agnostic through a JSON-serializable state dictionary.
The HTTP layer is a thin adapter; tests and future integrations can call these
methods directly.
"""
from __future__ import annotations

from copy import deepcopy
from itertools import combinations
import json
import os
from pathlib import Path
import threading
from typing import Any, Iterable

from .errors import ApiError
from .timeutil import now_label, parse_time, time_label

DATA_FILE = Path(__file__).resolve().parents[1] / "data" / "referrals_20261207.json"
SNAPSHOT_AS_OF = "2026-12-07T16:00:00+08:00"


def fresh_state() -> dict[str, Any]:
    return {
        "stations": {},
        "registrants": {},
        "identity_cases": {},
        "opportunities": {},
        "grants": {},
        "materials": {},
        "referrals": {},
        "appointments": {},
        "updates": {},
        "transfers": {},
        "consents": {},
        "audit": [],
        "counters": {},
        "meta": {},
    }


class EmploymentService:
    """All methods are safe to call from the threaded stdlib HTTP server."""

    def __init__(self, state: dict[str, Any] | None = None, path: str | Path | None = None,
                 default_at: str | None = None):
        self.state = state or fresh_state()
        self.path = Path(path) if path else None
        self.default_at = default_at
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ setup
    @classmethod
    def from_packet(cls, packet: dict[str, Any], path: str | Path | None = None,
                    default_at: str | None = SNAPSHOT_AS_OF) -> "EmploymentService":
        svc = cls(state=fresh_state(), path=path, default_at=default_at)
        svc._load_packet(packet)
        if path:
            svc.save()
        return svc

    @classmethod
    def seeded(cls, path: str | Path | None = None) -> "EmploymentService":
        packet = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        if path and Path(path).exists():
            return cls.load(path)
        return cls.from_packet(packet, path=path)

    @classmethod
    def load(cls, path: str | Path) -> "EmploymentService":
        p = Path(path)
        state = json.loads(p.read_text(encoding="utf-8"))
        default_at = state.get("default_at") or state.get("meta", {}).get("default_at")
        return cls(state=state, path=p, default_at=default_at)

    @classmethod
    def load_or_seed(cls, path: str | Path | None = None) -> "EmploymentService":
        if path and Path(path).exists():
            return cls.load(path)
        return cls.seeded(path)

    def save(self) -> None:
        if not self.path:
            return
        self.state.setdefault("meta", {})["default_at"] = self.default_at
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def reset_to_seed(self) -> dict[str, Any]:
        with self.lock:
            packet = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            self.state = fresh_state()
            self.default_at = SNAPSHOT_AS_OF
            self._load_packet(packet)
            self.save()
            return {"ok": True, "reset": "seed", "as_of": "16:00"}

    def _load_packet(self, packet: dict[str, Any]) -> None:
        s = self.state
        s["stations"] = {
            "ST1": {"id": "ST1", "name": "社区就业站"},
            "ST2": {"id": "ST2", "name": "行业服务中心"},
        }
        for group in (
            "registrants", "identity_cases", "opportunities", "grants",
            "referrals", "appointments",
        ):
            for row in packet.get(group, []):
                s[group][row["id"]] = deepcopy(row)

        for row in packet.get("case_updates", []):
            update = deepcopy(row)
            s["updates"][update["id"]] = update

        # A referral manifest is not a new business authorization; it records
        # which authorized data items were sent by that window.
        for ref in s["referrals"].values():
            grant = s["grants"].get(ref.get("grant"))
            fields = list(grant.get("fields", [])) if grant else []
            ref.setdefault("items", [
                {
                    "id": f"{ref['id']}-ITEM-{field}",
                    "field": field,
                    "registrant": ref["registrant"],
                    "source_station": ref["station"],
                    "included": True,
                }
                for field in fields
            ])
            opp = s["opportunities"].get(ref["opportunity"], {})
            ref.setdefault("legacy_key_snapshot", opp.get("legacy_key"))
            ref.setdefault("opening_snapshot", opp.get("opening"))

        # Replay exchange facts in chronological order.  The stored snapshot
        # remains unchanged; derived fields make late receipts easy to query.
        for update in sorted(s["updates"].values(), key=lambda u: parse_time(u.get("occurred"))):
            self._apply_exchange_update(update, persist=False)

        for prefix, collection in (
            ("R", "registrants"), ("I", "identity_cases"), ("J", "opportunities"),
            ("G", "grants"), ("A", "referrals"), ("V", "appointments"),
            ("C", "updates"), ("T", "transfers"), ("M", "materials"),
        ):
            max_n = 0
            for key in s[collection]:
                tail = str(key)[len(prefix):] if str(key).startswith(prefix) else str(key)
                try:
                    max_n = max(max_n, int("".join(ch for ch in tail if ch.isdigit())))
                except ValueError:
                    pass
            s["counters"][prefix] = max_n

    # ------------------------------------------------------------- primitives
    def _cutoff(self, at: str | datetime | None) -> datetime:
        parsed = parse_time(at) if at is not None else None
        if parsed is not None:
            return parsed
        if self.default_at:
            default = parse_time(self.default_at)
            if default:
                return default
        return parse_time(now_label())

    def _label(self, value, cutoff: datetime) -> str | None:
        if value is None:
            return None
        return time_label(value) or parse_time(value).isoformat()

    def _next_id(self, prefix: str) -> str:
        collection_name = {
            "R": "registrants", "I": "identity_cases", "J": "opportunities",
            "G": "grants", "A": "referrals", "V": "appointments",
            "C": "updates", "T": "transfers", "M": "materials",
        }.get(prefix)
        max_n = int(self.state["counters"].get(prefix, 0))
        if collection_name:
            for key in self.state[collection_name]:
                text = str(key)
                if text.startswith(prefix):
                    text = text[len(prefix):]
                digits = "".join(ch for ch in text if ch.isdigit())
                try:
                    max_n = max(max_n, int(digits))
                except ValueError:
                    pass
        current = max_n + 1
        self.state["counters"][prefix] = current
        return f"{prefix}{current}"

    def _get(self, collection: str, row_id: str | None, what: str | None = None) -> dict[str, Any]:
        row = self.state.get(collection, {}).get(row_id)
        if row is None:
            raise ApiError(404, f"{collection[:-1]}_not_found", f"{what or collection}不存在: {row_id}")
        return row

    @staticmethod
    def _actor_station(actor: dict[str, Any] | None) -> str | None:
        if not actor:
            return None
        return actor.get("station") or actor.get("X-Station-Id")

    @staticmethod
    def _actor_employer(actor: dict[str, Any] | None) -> str | None:
        if not actor:
            return None
        return actor.get("employer") or actor.get("X-Employer-Id")

    def _require_station(self, actor, station: str | None = None) -> str:
        actor_station = self._actor_station(actor)
        effective = station or actor_station
        if not effective:
            raise ApiError(401, "station_required", "请通过 X-Station-Id 标明办理窗口")
        if actor_station and station and actor_station != station:
            raise ApiError(403, "station_identity_mismatch", "不能以其他窗口的来源维护材料")
        if effective not in self.state["stations"]:
            # Allow new station IDs in deployments, but record the display name.
            self.state["stations"][effective] = {"id": effective, "name": effective}
        return effective

    def _audit(self, action: str, actor: dict | None = None, details: dict | None = None) -> None:
        self.state["audit"].append({
            "at": now_label(), "action": action,
            "station": self._actor_station(actor),
            "employer": self._actor_employer(actor),
            "details": details or {},
        })

    # ------------------------------------------------------------ identity
    def create_registrant(self, station: str, registrant: str | None = None,
                          registrant_id: str | None = None,
                          hint_id: str | None = None, checked_contact: str | None = None,
                          actor: dict | None = None, **extra) -> dict[str, Any]:
        with self.lock:
            effective_station = self._require_station(actor, station)
            rid = registrant_id or registrant or extra.get("id") or self._next_id("R")
            if rid in self.state["registrants"]:
                raise ApiError(409, "registrant_exists", rid)
            row = {
                "id": rid, "station": effective_station,
                "hint_id": hint_id, "checked_contact": checked_contact,
            }
            self.state["registrants"][rid] = row
            self.save()
            return deepcopy(row)

    def identity_group(self, registrant_id: str, at: str | None = None) -> list[str]:
        """Return the active equivalence group for a local registrant number."""
        self._get("registrants", registrant_id, "登记记录")
        cutoff = self._cutoff(at)
        active_edges: dict[tuple[str, str], bool] = {}

        cases = sorted(
            (c for c in self.state["identity_cases"].values() if c.get("decided_at")),
            key=lambda c: parse_time(c["decided_at"]),
        )
        for case in cases:
            decided = parse_time(case["decided_at"])
            if decided > cutoff:
                continue
            records = sorted(set(case.get("records", [])))
            is_merge = case.get("kind") == "merge" and case.get("status", "decided") == "decided"
            for left, right in combinations(records, 2):
                active_edges[(left, right)] = is_merge

        adjacency: dict[str, set[str]] = {rid: set() for rid in self.state["registrants"]}
        for (left, right), active in active_edges.items():
            if active:
                adjacency.setdefault(left, set()).add(right)
                adjacency.setdefault(right, set()).add(left)

        seen = {registrant_id}
        stack = [registrant_id]
        while stack:
            current = stack.pop()
            for nxt in adjacency.get(current, ()):  # type: ignore[arg-type]
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return sorted(seen)

    def identity_suggestions(self, registrant_id: str) -> list[dict[str, Any]]:
        reg = self._get("registrants", registrant_id)
        out = []
        for other in self.state["registrants"].values():
            if other["id"] == registrant_id:
                continue
            reasons = []
            if reg.get("hint_id") and reg.get("hint_id") == other.get("hint_id"):
                reasons.append("same_hint_id")
            if reg.get("checked_contact") and reg.get("checked_contact") == other.get("checked_contact"):
                reasons.append("same_checked_contact")
            if reasons:
                out.append({
                    "registrant": other["id"], "station": other.get("station"),
                    "clues": reasons,
                    "decision": "verification_required",
                    "warning": "单个线索相同不足以归并身份",
                })
        return out

    def identity_view(self, registrant_id: str, at: str | None = None) -> dict[str, Any]:
        cutoff = self._cutoff(at)
        group = self.identity_group(registrant_id, cutoff)
        related_cases = [
            deepcopy(c) for c in self.state["identity_cases"].values()
            if registrant_id in c.get("records", [])
            or set(group) & set(c.get("records", []))
        ]
        related_cases.sort(key=lambda c: parse_time(c.get("decided_at") or c.get("submitted_at")))
        effective_cases = [
            c for c in related_cases
            if not c.get("decided_at") or parse_time(c["decided_at"]) <= cutoff
        ]
        return {
            "registrant": registrant_id,
            "as_of": time_label(cutoff) or cutoff.isoformat(),
            "current_group": group,
            "is_same_job_seeking_process": len(group) > 1,
            "decisions": effective_cases,
            "all_known_decisions": related_cases,
            "suggestions": self.identity_suggestions(registrant_id),
        }

    def create_identity_case(self, records: list[str], station: str | None = None,
                             kind: str | None = None, decision: str | None = None,
                             decided_at: str | None = None, at: str | None = None,
                             reason: str | None = None,
                             actor: dict | None = None) -> dict[str, Any]:
        with self.lock:
            effective_station = self._require_station(actor, station) if station or self._actor_station(actor) else station
            records = sorted(set(records or []))
            if len(records) < 2:
                raise ApiError(422, "at_least_two_records", "身份核验至少需要两条本地登记记录")
            for rid in records:
                self._get("registrants", rid, "登记记录")

            resolved_kind = kind or decision or "suspected_duplicate"
            decided_at = decided_at or at
            if resolved_kind not in {"suspected_duplicate", "merge", "keep_separate", "split"}:
                raise ApiError(422, "invalid_identity_decision", resolved_kind)

            cid = self._next_id("I")
            case = {
                "id": cid,
                "records": records,
                "kind": resolved_kind,
                "submitted_by": effective_station,
                "submitted_at": now_label(),
                "decided_at": decided_at if resolved_kind != "suspected_duplicate" else None,
                "status": "pending" if resolved_kind == "suspected_duplicate" else "decided",
                "reason": reason,
                "objections": [],
            }
            self.state["identity_cases"][cid] = case
            self._audit("identity_case_created", actor, {"id": cid, "kind": resolved_kind})
            self.save()
            return deepcopy(case)

    def decide_identity_case(self, case_id: str, decision: str, station: str | None = None,
                             decided_at: str | None = None, actor: dict | None = None) -> dict[str, Any]:
        with self.lock:
            case = self._get("identity_cases", case_id, "身份核验")
            if decision not in {"merge", "keep_separate", "split"}:
                raise ApiError(422, "invalid_identity_decision", decision)
            self._require_station(actor, station) if station or self._actor_station(actor) else None
            case["kind"] = decision
            case["status"] = "decided"
            case["decided_at"] = decided_at or now_label()
            case["decided_by"] = station or self._actor_station(actor)
            self._audit("identity_case_decided", actor, {"id": case_id, "decision": decision})
            self.save()
            return deepcopy(case)

    def object_to_identity_case(self, case_id: str, station: str, reason: str,
                                at: str | None = None, actor: dict | None = None) -> dict[str, Any]:
        with self.lock:
            case = self._get("identity_cases", case_id, "身份核验")
            effective_station = self._require_station(actor, station)
            objection = {
                "station": effective_station, "reason": reason,
                "received_at": at or now_label(), "status": "received",
            }
            case.setdefault("objections", []).append(objection)
            self.save()
            return deepcopy(case)

    # Vocabulary alias for callers describing the case as an identity dispute.
    file_identity_dispute = object_to_identity_case

    def split_identity(self, records: list[str] | None = None, case_id: str | None = None,
                       station: str | None = None, at: str | None = None,
                       reason: str | None = None, actor: dict | None = None) -> dict[str, Any]:
        with self.lock:
            source_case = self.state["identity_cases"].get(case_id) if case_id else None
            record_list = sorted(set(records or (source_case or {}).get("records", [])))
            if len(record_list) < 2:
                raise ApiError(422, "at_least_two_records", "分拆至少需要两条记录")
            split_case = self.create_identity_case(
                records=record_list, station=station, kind="split",
                decided_at=at or now_label(), reason=reason or "identity_objection_upheld",
                actor=actor,
            )
            if source_case and source_case.get("kind") == "merge":
                split_case["splits_case"] = source_case["id"]
                source_case.setdefault("split_by", []).append(split_case["id"])
            split_case["attribution"] = self.provenance_for_records(record_list)
            self.save()
            return deepcopy(split_case)

    # ------------------------------------------------------------ materials
    def verify_materials(self, registrant: str, station: str, items: Iterable[Any],
                         at: str | None = None, actor: dict | None = None) -> list[dict[str, Any]]:
        with self.lock:
            self._get("registrants", registrant, "登记记录")
            effective_station = self._require_station(actor, station)
            if not items:
                raise ApiError(422, "items_required", "请提供本窗口核实过的资料项")
            created = []
            for raw in items:
                if isinstance(raw, str):
                    field, title, content_ref = raw, raw, None
                else:
                    field = raw.get("field") or raw.get("name")
                    title = raw.get("title") or field
                    content_ref = raw.get("content_ref")
                if not field:
                    raise ApiError(422, "field_required", "资料项缺少 field")
                material_id = raw.get("id") if isinstance(raw, dict) and raw.get("id") else self._next_id("M")
                material = {
                    "id": material_id,
                    "registrant": registrant,
                    "field": field,
                    "title": title,
                    "content": raw.get("content"),
                    "content_ref": content_ref,
                    "source_station": effective_station,
                    "verified_by": effective_station,
                    "verified_at": at or now_label(),
                    "status": "verified",
                }
                self.state["materials"][material_id] = material
                created.append(deepcopy(material))
            self._audit("materials_verified", actor, {"registrant": registrant, "ids": [m["id"] for m in created]})
            self.save()
            return created

    def update_material(self, material_id: str, patch: dict[str, Any], actor: dict | None = None) -> dict:
        with self.lock:
            material = self._get("materials", material_id, "材料")
            actor_station = self._actor_station(actor)
            if actor_station and material["source_station"] != actor_station:
                raise ApiError(403, "only_source_window_can_modify", "只有核实该材料的窗口可以维护它")
            for key in ("title", "content_ref", "status"):
                if key in patch:
                    material[key] = patch[key]
            self.save()
            return deepcopy(material)

    def access_material_content(self, material_id: str, actor: dict | None = None,
                                employer: str | None = None, opportunity: str | None = None,
                                at: str | None = None) -> dict[str, Any]:
        """Return content only to a currently permitted business role.

        Delivery/provenance facts remain historical and are served separately;
        this method guards the actual content/reference after withdrawal.
        """
        material = self._get("materials", material_id, "材料")
        actor_station = self._actor_station(actor)
        actor_employer = self._actor_employer(actor) or employer
        actor_registrant = (actor or {}).get("registrant")

        if actor_registrant == material["registrant"]:
            allowed_by = "data_subject"
        elif actor_station == material["source_station"]:
            allowed_by = "source_verification_window"
        elif actor_employer:
            if not opportunity:
                raise ApiError(403, "opportunity_scope_required", "企业取材料内容必须说明 exact opportunity")
            grants = [
                g for g in self.state["grants"].values()
                if g["registrant"] == material["registrant"]
                and g["employer"] == actor_employer
                and g["opportunity"] == opportunity
                and material["field"] in g.get("fields", [])
            ]
            permitted = any(self.check_grant(g["id"], at=at).get("allowed") for g in grants)
            if not permitted:
                raise ApiError(403, "content_authorization_no_longer_active",
                                "授权已撤回或过期；历史送达记录仍可查，但不能继续取得内容")
            allowed_by = "current_employer_grant"
        else:
            raise ApiError(401, "business_role_required", "请提供窗口、企业或求职者身份")

        self._audit("material_content_accessed", actor, {
            "material": material_id, "allowed_by": allowed_by,
        })
        self.save()
        return {
            "material_id": material_id, "field": material["field"],
            "content": material.get("content"),
            "content_ref": material.get("content_ref"),
            "access": allowed_by,
        }

    def provenance_for_records(self, records: list[str]) -> dict[str, Any]:
        record_set = set(records)
        items: list[dict[str, Any]] = []

        for material in self.state["materials"].values():
            if material.get("registrant") in record_set:
                items.append({
                    "kind": "verified_material",
                    "material_id": material["id"],
                    "registrant": material["registrant"],
                    "field": material["field"],
                    "source_station": material["source_station"],
                    "verified_at": material.get("verified_at"),
                })

        for ref in self.state["referrals"].values():
            if ref.get("registrant") not in record_set:
                continue
            for item in ref.get("items", []):
                items.append({
                    "kind": "referral_item",
                    "referral_id": ref["id"],
                    "registrant": ref["registrant"],
                    "field": item.get("field"),
                    "source_station": ref["station"],
                    "sent_at": ref.get("sent"),
                    "received_at": ref.get("received"),
                    "opportunity": ref.get("opportunity"),
                })

        by_station: dict[str, list[dict]] = {}
        for item in items:
            by_station.setdefault(item["source_station"], []).append(item)
        return {"records": sorted(record_set), "items": items, "by_station": by_station}

    def material_provenance(self, registrant: str | None = None, identity_case: str | None = None,
                            at: str | None = None) -> dict[str, Any]:
        if identity_case:
            case = self._get("identity_cases", identity_case, "身份核验")
            records = case.get("records", [])
        elif registrant:
            records = self.identity_group(registrant, at)
        else:
            raise ApiError(400, "registrant_or_case_required", "请提供 registrant 或 identity_case")
        return self.provenance_for_records(records)

    # ------------------------------------------------------------ grants
    def create_grant(self, registrant: str, employer: str, opportunity: str, fields: list[str],
                     start: str | None = None, expires: str | None = None,
                     grant_id: str | None = None, actor: dict | None = None) -> dict[str, Any]:
        with self.lock:
            self._get("registrants", registrant, "登记记录")
            opp = self._get("opportunities", opportunity, "岗位机会")
            if opp.get("employer") != employer:
                raise ApiError(422, "employer_opportunity_mismatch", "授权企业与岗位机会所属企业不一致")
            if not fields:
                raise ApiError(422, "fields_required", "授权必须列明资料项")
            gid = grant_id or self._next_id("G")
            if gid in self.state["grants"]:
                raise ApiError(409, "grant_exists", gid)
            grant = {
                "id": gid, "registrant": registrant, "employer": employer,
                "opportunity": opportunity, "fields": list(fields),
                "start": start or now_label(), "expires": expires,
                "withdrawn": None,
            }
            self.state["grants"][gid] = grant
            self.save()
            return deepcopy(grant)

    def check_grant(self, grant_id: str, at: str | None = None, fields: list[str] | None = None,
                    registrant: str | None = None, opportunity: str | None = None,
                    employer: str | None = None) -> dict[str, Any]:
        grant = self._get("grants", grant_id, "授权")
        cutoff = self._cutoff(at)
        reasons = []
        if registrant and grant["registrant"] != registrant:
            reasons.append("registrant_mismatch")
        if opportunity and grant["opportunity"] != opportunity:
            reasons.append("opportunity_mismatch_exact_id_required")
        if employer and grant["employer"] != employer:
            reasons.append("employer_mismatch")
        if fields and set(fields) - set(grant.get("fields", [])):
            reasons.append("field_not_authorized")
        start = parse_time(grant.get("start"))
        expires = parse_time(grant.get("expires"))
        withdrawn = parse_time(grant.get("withdrawn"))
        if start and cutoff < start:
            reasons.append("not_yet_valid")
        if expires and cutoff >= expires:
            reasons.append("expired")
        if withdrawn and withdrawn <= cutoff:
            reasons.append("withdrawn")
        return {
            "grant_id": grant_id, "allowed": not reasons, "reasons": reasons,
            "scope": {
                "registrant": grant["registrant"], "employer": grant["employer"],
                "opportunity": grant["opportunity"], "fields": grant["fields"],
            },
            "withdrawn": grant.get("withdrawn"), "expires": grant.get("expires"),
        }

    def withdraw_grant(self, grant_id: str, at: str | None = None,
                       reason: str | None = None, actor: dict | None = None) -> dict[str, Any]:
        with self.lock:
            grant = self._get("grants", grant_id, "授权")
            changed = not grant.get("withdrawn")
            if changed:
                grant["withdrawn"] = at or now_label()
                grant["withdraw_reason"] = reason
                self.state["audit"].append({
                    "at": now_label(), "action": "grant_withdrawn",
                    "grant": grant_id, "reason": reason,
                    "effect": "stops_future_disclosure_only",
                })
                self.save()
            result = deepcopy(grant)
            result["changed"] = changed
            result["effect"] = "停止后续披露；已送达事实和已有约见不受影响"
            return result

    # ------------------------------------------------------------ opportunities
    def create_opportunity(self, employer: str, legacy_key: str, opening: int,
                            named: str, slots: int = 1, opportunity_id: str | None = None,
                            actor: dict | None = None) -> dict[str, Any]:
        with self.lock:
            actor_employer = self._actor_employer(actor)
            if actor_employer and actor_employer != employer:
                raise ApiError(404, "employer_not_found", "企业身份与机会雇主不一致")
            for opp in self.state["opportunities"].values():
                if (opp["employer"] == employer and opp["legacy_key"] == legacy_key
                        and int(opp["opening"]) == int(opening)):
                    raise ApiError(409, "opening_exists", "该雇主旧编号的 opening 批次已存在")
            oid = opportunity_id or self._next_id("J")
            opp = {
                "id": oid, "employer": employer, "legacy_key": legacy_key,
                "opening": int(opening), "named": named, "slots": int(slots),
                "status": "open", "closed_at": None,
            }
            self.state["opportunities"][oid] = opp
            self.save()
            return deepcopy(opp)

    def close_opportunity(self, opportunity_id: str, at: str | None = None,
                          closure_receipt: str | dict | None = None,
                          actor: dict | None = None) -> dict[str, Any]:
        with self.lock:
            opp = self._get("opportunities", opportunity_id, "岗位机会")
            actor_employer = self._actor_employer(actor)
            if actor_employer and opp.get("employer") != actor_employer:
                raise ApiError(404, "opportunity_not_found", "机会不存在")
            opp["status"] = "closed"
            opp["closed_at"] = at or now_label()
            opp["closure_receipt"] = closure_receipt
            self._append_update({
                "kind": "closed", "entity": opportunity_id,
                "occurred": opp["closed_at"], "closure_receipt": closure_receipt,
            })
            self.save()
            return deepcopy(opp)

    def reopen_opportunity(self, source_opportunity_id: str, named: str | None = None,
                           slots: int | None = None, at: str | None = None,
                           actor: dict | None = None) -> dict[str, Any]:
        with self.lock:
            old = self._get("opportunities", source_opportunity_id, "原岗位机会")
            actor_employer = self._actor_employer(actor)
            if actor_employer and old.get("employer") != actor_employer:
                raise ApiError(404, "opportunity_not_found", "机会不存在")
            next_opening = max(
                (int(o["opening"]) for o in self.state["opportunities"].values()
                 if o["employer"] == old["employer"] and o["legacy_key"] == old["legacy_key"]),
                default=int(old["opening"]),
            ) + 1
            return self.create_opportunity(
                employer=old["employer"], legacy_key=old["legacy_key"],
                opening=next_opening, named=named or old["named"],
                slots=slots if slots is not None else old.get("slots", 1),
            )

    def resolve_opportunity_by_legacy(self, employer: str, legacy_key: str,
                                      opening: int | None = None) -> dict[str, Any]:
        matches = [
            o for o in self.state["opportunities"].values()
            if o["employer"] == employer and o["legacy_key"] == legacy_key
        ]
        if opening is not None:
            matches = [o for o in matches if int(o["opening"]) == int(opening)]
        if not matches:
            raise ApiError(404, "opportunity_not_found", "找不到雇主旧编号对应的 opening 批次")
        if opening is None and len(matches) > 1:
            raise ApiError(409, "opening_required",
                            "同一旧编号曾关闭重开；回执必须携带 opening 或原转介编号",
                            {"openings": [{"id": o["id"], "opening": o["opening"]} for o in matches]})
        return matches[0]

    # ------------------------------------------------------------ referrals
    def create_referral(self, registrant: str | None = None, opportunity: str | None = None,
                        grant: str | None = None, station: str | None = None,
                        sent: str | None = None, received: str | None = None,
                        fields: list[str] | None = None, items: list[Any] | None = None,
                        employer: str | None = None, legacy_key: str | None = None,
                        opening: int | None = None, referral_id: str | None = None,
                        actor: dict | None = None, **extra) -> dict[str, Any]:
        with self.lock:
            effective_station = self._require_station(actor, station) if station or self._actor_station(actor) else None
            if not effective_station:
                raise ApiError(400, "station_required", "转介必须注明投送窗口 station")
            if not registrant:
                raise ApiError(422, "registrant_required", "转介必须注明本地登记记录")
            self._get("registrants", registrant, "登记记录")

            if opportunity:
                opp = self._get("opportunities", opportunity, "岗位机会")
            else:
                if not employer or not legacy_key:
                    raise ApiError(422, "opportunity_or_legacy_coordinates_required",
                                    "请提供 opportunity 编号，或 employer+legacy_key+opening")
                opp = self.resolve_opportunity_by_legacy(employer, legacy_key, opening)

            if opp.get("employer") != (employer or opp["employer"]):
                raise ApiError(422, "employer_opportunity_mismatch", "雇主与岗位机会不一致")
            if opp.get("status") == "closed":
                cutoff_sent = parse_time(sent or now_label())
                closed_at = parse_time(opp.get("closed_at"))
                if closed_at and cutoff_sent >= closed_at:
                    raise ApiError(409, "opportunity_closed", "原机会已关闭；重开机会是新的 opening，不能沿用原授权")

            grant_row = self._get("grants", grant, "授权") if grant else None
            if grant_row is None:
                raise ApiError(422, "grant_required", "转介必须引用求职者的逐项授权")
            included_fields = fields or [
                i if isinstance(i, str) else i.get("field") for i in (items or [])
            ] or list(grant_row.get("fields", []))
            check = self.check_grant(
                grant, at=sent or now_label(), fields=included_fields,
                registrant=registrant, opportunity=opp["id"], employer=opp["employer"],
            )
            if not check["allowed"]:
                raise ApiError(422, "authorization_invalid_for_referral", "授权不符合投送条件", check)

            rid = referral_id or extra.get("id") or self._next_id("A")
            if rid in self.state["referrals"]:
                raise ApiError(409, "referral_exists", rid)
            sent_at = sent or now_label()
            ref = {
                "id": rid, "registrant": registrant, "opportunity": opp["id"],
                "grant": grant, "station": effective_station,
                "sent": time_label(sent_at) if parse_time(sent_at) and len(str(sent_at)) == 5 else sent_at,
                "received": None,
                "items": [
                    {"id": f"{rid}-ITEM-{field}", "field": field,
                     "registrant": registrant, "source_station": effective_station,
                     "included": True}
                    for field in included_fields
                ],
                "legacy_key_snapshot": opp.get("legacy_key"),
                "opening_snapshot": opp.get("opening"),
            }
            if received:
                ref["received"] = received
                ref["receipts"] = [{"at": received, "source": "create"}]
            self.state["referrals"][rid] = ref
            self._audit("referral_created", actor, {"id": rid, "opportunity": opp["id"]})
            self.save()
            return deepcopy(ref)

    def receive_referral(self, referral_id: str, received_at: str | None = None,
                         employer_id: str | None = None, via: str | None = None,
                         actor: dict | None = None) -> dict[str, Any]:
        with self.lock:
            ref = self._get("referrals", referral_id, "转介")
            opp = self._get("opportunities", ref["opportunity"], "岗位机会")
            actor_employer = self._actor_employer(actor) or employer_id
            if actor_employer and opp["employer"] != actor_employer:
                # Deliberately 404: do not reveal another employer's referral.
                raise ApiError(404, "referral_not_found", "转介不存在或不属于该企业")

            at = received_at or now_label()
            receipts = ref.setdefault("receipts", [])
            receipts.append({"at": at, "via": via or "employer_receipt"})
            if not ref.get("received") or parse_time(at) < parse_time(ref["received"]):
                ref["received"] = at
            self._append_update({
                "kind": "received", "entity": referral_id,
                "occurred": at, "station": ref.get("station"),
            })
            self._audit("referral_receipt", actor, {"id": referral_id, "at": at})
            self.save()
            return {
                "referral_id": referral_id,
                "received": ref["received"],
                "matched_opportunity_id": opp["id"],
                "matched_opening": opp.get("opening"),
                "legacy_key": opp.get("legacy_key"),
                "match_rule": "submission_time_original_opportunity",
            }

    def legacy_receipt(self, employer: str, legacy_key: str, referral_id: str | None = None,
                       opening: int | None = None, received_at: str | None = None,
                       actor: dict | None = None, **extra) -> dict[str, Any]:
        if referral_id or extra.get("referral"):
            rid = referral_id or extra.get("referral")
            ref = self._get("referrals", rid, "转介")
            opp = self._get("opportunities", ref["opportunity"], "岗位机会")
            if opp["employer"] != employer or str(opp["legacy_key"]) != str(legacy_key):
                raise ApiError(409, "legacy_coordinates_do_not_match_original_referral",
                                "回执旧编号与原转介提交时的机会不一致")
        else:
            opp = self.resolve_opportunity_by_legacy(employer, legacy_key, opening)
            candidates = [r for r in self.state["referrals"].values() if r["opportunity"] == opp["id"]]
            if extra.get("legacy_referral_id"):
                candidates = [r for r in candidates if r.get("legacy_referral_id") == extra["legacy_referral_id"]]
            if len(candidates) != 1:
                raise ApiError(409, "referral_correlation_required",
                                "旧入口回执必须带原转介编号；同一机会可能有多份转介")
            rid = candidates[0]["id"]
        return self.receive_referral(rid, received_at=received_at, employer_id=employer, actor=actor)

    # ------------------------------------------------------------ updates
    def _append_update(self, update: dict[str, Any]) -> dict[str, Any]:
        uid = update.get("id") or self._next_id("C")
        update["id"] = uid
        self.state["updates"][uid] = deepcopy(update)
        return update

    def record_update(self, update: dict[str, Any], actor: dict | None = None) -> dict[str, Any]:
        with self.lock:
            kind = update.get("kind")
            if kind not in {"owner_choice", "acknowledge_contact", "received", "choose_slot", "closed", "cancelled"}:
                raise ApiError(422, "unknown_update_kind", kind)
            stored = self._append_update(deepcopy(update))
            self._apply_exchange_update(stored)
            self.save()
            return deepcopy(stored)

    def _apply_exchange_update(self, update: dict[str, Any], persist: bool = True) -> None:
        kind = update.get("kind")
        entity = update.get("entity")
        at = update.get("occurred")

        if kind == "received":
            ref = self.state["referrals"].get(entity)
            if ref:
                if not ref.get("received") or (at and parse_time(at) < parse_time(ref["received"])):
                    ref["received"] = at

        elif kind == "acknowledge_contact":
            appt = self.state["appointments"].get(entity)
            if appt:
                self._upsert_contact_ack(appt, update, persist=persist)

        elif kind == "choose_slot":
            appt = self.state["appointments"].get(entity)
            if appt:
                appt["seeker_choice_at"] = at
                appt["seeker_confirmed_at"] = at
                if update.get("slot"):
                    appt["seeker_slot"] = update["slot"]

        elif kind == "closed":
            opp = self.state["opportunities"].get(entity)
            if opp:
                opp["status"] = "closed"
                opp["closed_at"] = at

        elif kind == "cancelled":
            appt = self.state["appointments"].get(entity)
            if appt:
                appt["status"] = "cancelled"
                appt["cancelled_at"] = at

    def legacy_close(self, employer: str, legacy_key: str, opening: int | None = None,
                     closed_at: str | None = None, receipt: str | dict | None = None,
                     actor: dict | None = None) -> dict[str, Any]:
        opp = self.resolve_opportunity_by_legacy(employer, legacy_key, opening)
        return self.close_opportunity(
            opp["id"], at=closed_at, closure_receipt=receipt, actor=actor,
        )

    def record_owner_choice(self, opportunity: str, station: str, at: str | None = None,
                             registrant: str | None = None,
                             actor: dict | None = None) -> dict[str, Any]:
        with self.lock:
            opp = self._get("opportunities", opportunity, "岗位机会")
            if station not in self.state["stations"]:
                self.state["stations"][station] = {"id": station, "name": station}
            refs = [r for r in self.state["referrals"].values() if r["opportunity"] == opp["id"]]
            if registrant:
                group = set(self.identity_group(registrant, at))
                refs = [r for r in refs if r["registrant"] in group]
            candidate_warning = None
            if refs and not any(r["station"] == station for r in refs):
                # The seeker may ask a different authorized window to take over
                # an existing interview; record a warning rather than vetoing
                # their choice.  Transfer still requires employer confirmation.
                candidate_warning = "chosen_station_has_no_referral_for_this_opportunity"
            update = self._append_update({
                "kind": "owner_choice", "entity": opportunity,
                "station": station, "occurred": at or now_label(),
                "registrant": registrant, "warning": candidate_warning,
            })
            self._audit("owner_choice", actor, update)
            self.save()
            return deepcopy(update)

    def latest_owner_choice(self, opportunity_id: str, at: datetime) -> dict[str, Any] | None:
        choices = [
            u for u in self.state["updates"].values()
            if u.get("kind") == "owner_choice" and u.get("entity") == opportunity_id
            and parse_time(u.get("occurred")) <= at
        ]
        if not choices:
            return None
        return max(choices, key=lambda u: parse_time(u.get("occurred")))

    # ------------------------------------------------------------ appointments/transfers
    def create_appointment(self, referral: str, slot: str, start: str, end: str,
                           appointment_id: str | None = None, confirmed: str | None = None,
                           employer: str | None = None, actor: dict | None = None,
                           **extra) -> dict[str, Any]:
        with self.lock:
            ref = self._get("referrals", referral, "转介")
            opp = self._get("opportunities", ref["opportunity"], "岗位机会")
            actor_employer = self._actor_employer(actor) or employer or extra.get("employer")
            if actor_employer and opp["employer"] != actor_employer:
                raise ApiError(404, "opportunity_not_found", "岗位机会不属于该企业")
            vid = appointment_id or extra.get("id") or self._next_id("V")
            appt = {
                "id": vid, "referral": referral, "employer": opp["employer"],
                "slot": slot, "start": start, "end": end,
                "confirmed": confirmed, "status": "scheduled",
                "attendance_status": "scheduled",
            }
            self.state["appointments"][vid] = appt
            self.save()
            return deepcopy(appt)

    def _appointment_ref(self, appt: dict) -> dict:
        return self._get("referrals", appt["referral"], "转介")

    def _upsert_contact_ack(self, appt: dict, update: dict, persist: bool = True) -> dict:
        """Create or confirm a transfer from an exchange acknowledgement fact."""
        at = update.get("occurred")
        target = update.get("station")
        ref = self._appointment_ref(appt)
        opp_id = ref["opportunity"]
        authority = self.latest_owner_choice(opp_id, parse_time(at)) if at else None

        pending = next((
            t for t in self.state["transfers"].values()
            if t.get("appointment") == appt["id"] and t.get("status") == "pending_employer_ack"
            and t.get("to_station") == target
        ), None)
        if pending:
            transfer = pending
        else:
            tid = f"T-{update['id']}" if str(update.get("id", "")).startswith("C") else self._next_id("T")
            transfer = {
                "id": tid, "appointment": appt["id"], "referral": ref["id"],
                "opportunity": opp_id, "employer": appt["employer"],
                "from_station": ref["station"], "to_station": target,
                "requested_at": (authority or {}).get("occurred"),
                "acknowledged_at": None, "status": "pending_employer_ack",
                "basis_update": (authority or {}).get("id"),
                "authorization_status": "authorized" if authority and authority.get("station") == target else "authority_not_linked",
                "original_slot": appt.get("slot"),
                "seat_retained": True,
            }
            self.state["transfers"][tid] = transfer

        transfer["status"] = "employer_confirmed"
        transfer["acknowledged_at"] = at
        transfer["acknowledgement_update"] = update.get("id")
        transfer["seat_retained"] = True
        appt["contact_transfer_id"] = transfer["id"]
        return transfer

    def request_transfer(self, appointment_id: str, target_station: str,
                         at: str | None = None, actor: dict | None = None,
                         owner_choice_id: str | None = None,
                         reason: str | None = None) -> dict[str, Any]:
        with self.lock:
            appt = self._get("appointments", appointment_id, "约见")
            ref = self._appointment_ref(appt)
            cutoff = self._cutoff(at)
            contact = self.appointment_contact(appt, cutoff)
            actor_station = self._actor_station(actor)
            if actor_station and contact["station"] != actor_station:
                raise ApiError(403, "only_current_contact_can_transfer",
                                "只有当前联系窗口可以转交；请先完成已有联系人确认")

            authority = None
            if owner_choice_id:
                authority = self._get("updates", owner_choice_id, "求职者选择")
                if authority.get("kind") != "owner_choice" or authority.get("entity") != ref["opportunity"]:
                    raise ApiError(422, "invalid_owner_choice", "选择记录不适用该机会")
                if authority.get("station") != target_station:
                    raise ApiError(422, "owner_choice_station_mismatch", "求职者选择的跟进窗口与转入窗口不一致")
            else:
                choice = self.latest_owner_choice(ref["opportunity"], cutoff)
                if choice and choice.get("station") == target_station:
                    authority = choice

            if not authority:
                raise ApiError(422, "target_window_not_authorized",
                                "转入窗口必须已有求职者选择或单独的转交授权")

            tid = self._next_id("T")
            transfer = {
                "id": tid, "appointment": appointment_id, "referral": ref["id"],
                "opportunity": ref["opportunity"], "employer": appt["employer"],
                "from_station": contact["station"], "to_station": target_station,
                "requested_at": at or now_label(), "acknowledged_at": None,
                "status": "pending_employer_ack", "basis_update": authority.get("id"),
                "authorization_status": "authorized", "reason": reason,
                "original_slot": appt.get("slot"),
                "original_start": appt.get("start"), "original_end": appt.get("end"),
                "seat_retained": True,
            }
            self.state["transfers"][tid] = transfer
            self.save()
            return deepcopy(transfer)

    def acknowledge_transfer(self, transfer_id: str, employer_id: str | None = None,
                             at: str | None = None, actor: dict | None = None) -> dict[str, Any]:
        with self.lock:
            transfer = self._get("transfers", transfer_id, "转交")
            actor_employer = self._actor_employer(actor) or employer_id
            if actor_employer and transfer["employer"] != actor_employer:
                raise ApiError(404, "transfer_not_found", "转交记录不存在或不属于该企业")
            transfer["status"] = "employer_confirmed"
            transfer["acknowledged_at"] = at or now_label()
            update = self._append_update({
                "kind": "acknowledge_contact", "entity": transfer["appointment"],
                "station": transfer["to_station"], "occurred": transfer["acknowledged_at"],
            })
            transfer["acknowledgement_update"] = update["id"]
            self.save()
            return deepcopy(transfer)

    def acknowledge_contact_change(self, appointment_id: str, station: str,
                                   employer_id: str | None = None, at: str | None = None,
                                   actor: dict | None = None) -> dict[str, Any]:
        with self.lock:
            appt = self._get("appointments", appointment_id, "约见")
            actor_employer = self._actor_employer(actor) or employer_id
            if actor_employer and appt["employer"] != actor_employer:
                raise ApiError(404, "appointment_not_found", "约见不存在或不属于该企业")
            update = {
                "id": self._next_id("C"), "kind": "acknowledge_contact",
                "entity": appointment_id, "station": station,
                "occurred": at or now_label(),
            }
            stored = self._append_update(update)
            transfer = self._upsert_contact_ack(appt, stored)
            self.save()
            return {"update": deepcopy(stored), "transfer": deepcopy(transfer),
                    "contact": self.appointment_contact(appt, self._cutoff(at))}

    def appointment_contact(self, appt: dict[str, Any], at: datetime | None = None) -> dict[str, Any]:
        cutoff = at or self._cutoff(None)
        ref = self.state["referrals"].get(appt.get("referral"), {})
        original_station = ref.get("station")
        transfers = [
            t for t in self.state["transfers"].values()
            if t.get("appointment") == appt["id"] and parse_time(t.get("requested_at") or t.get("acknowledged_at")) <= cutoff
        ]
        confirmed = [
            t for t in transfers
            if t.get("status") == "employer_confirmed" and parse_time(t.get("acknowledged_at")) <= cutoff
        ]
        confirmed.sort(key=lambda t: parse_time(t.get("acknowledged_at")))
        current = confirmed[-1] if confirmed else None
        pending = next((
            t for t in sorted(transfers, key=lambda x: parse_time(x.get("requested_at")))
            if t.get("status") == "pending_employer_ack"
            or (t.get("status") == "employer_confirmed" and parse_time(t.get("acknowledged_at")) > cutoff)
        ), None)
        # The second branch is filtered above; keep this defensive for state files.
        if pending and parse_time(pending.get("acknowledged_at")) and parse_time(pending["acknowledged_at"]) > cutoff:
            pending = {**deepcopy(pending), "status": "pending_employer_ack"}
        elif pending and pending.get("status") != "pending_employer_ack":
            pending = None

        station = current["to_station"] if current else original_station
        result = {
            "station": station,
            "status": "employer_confirmed" if current else "original_window",
            "since": (current or {}).get("acknowledged_at"),
            "transfer_id": (current or {}).get("id"),
            "pending_transfer": None if not pending else {
                # A future acknowledgement replayed from a snapshot must not
                # expose its not-yet-arrived transfer/ack id as a live record.
                "transfer_id": pending["id"] if not pending.get("acknowledged_at") else None,
                "basis_update": pending.get("basis_update"),
                "to_station": pending["to_station"],
                "from_station": pending["from_station"],
                "requested_at": pending.get("requested_at"),
                "status": "pending_employer_ack",
            },
            "original_station": original_station,
            "slot": appt.get("slot"),
            "start": appt.get("start"), "end": appt.get("end"),
            "seat_retained": True,
        }

        # A job-seeker owner choice is also the authorization/request basis for
        # moving an existing interview.  Until the employer's acknowledgement
        # arrives, the old contact remains legally/operationally current.
        if not current and not pending:
            choice = self.latest_owner_choice(ref.get("opportunity"), cutoff) if ref.get("opportunity") else None
            if choice and choice.get("station") and choice["station"] != original_station:
                result["pending_transfer"] = {
                    "transfer_id": None,
                    "basis_update": choice["id"],
                    "to_station": choice["station"],
                    "from_station": original_station,
                    "requested_at": choice.get("occurred"),
                    "status": "pending_employer_ack",
                }
        return result

    # ------------------------------------------------------------ cancellation/conflicts
    def cancel_appointment(self, appointment_id: str, registrant: str | None = None,
                           at: str | None = None, reason: str | None = None,
                           actor: dict | None = None) -> dict[str, Any]:
        with self.lock:
            appt = self._get("appointments", appointment_id, "约见")
            ref = self._appointment_ref(appt)
            if registrant:
                group = set(self.identity_group(registrant, at or now_label()))
                if ref["registrant"] != registrant and ref["registrant"] not in group:
                    raise ApiError(403, "not_your_appointment", "只能取消本人的约见")
            appt["status"] = "cancelled"
            appt["cancelled_at"] = at or now_label()
            appt["cancel_reason"] = reason
            self._append_update({
                "kind": "cancelled", "entity": appointment_id,
                "occurred": appt["cancelled_at"], "registrant": registrant,
            })
            self.save()
            return {"appointment": deepcopy(appt),
                    "notice": "取消约见不影响材料授权；如需停止披露请另行撤回授权"}

    def choose_appointment_slot(self, appointment_id: str, registrant: str | None = None,
                                at: str | None = None, slot: str | None = None,
                                cancel_other_overlaps: bool = False,
                                handling: str | None = None,
                                actor: dict | None = None) -> dict[str, Any]:
        with self.lock:
            appt = self._get("appointments", appointment_id, "约见")
            if appt.get("status") == "cancelled":
                raise ApiError(409, "appointment_cancelled", "该约见已取消")
            ref = self._appointment_ref(appt)
            if registrant:
                group = set(self.identity_group(registrant, at or now_label()))
                if ref["registrant"] != registrant and ref["registrant"] not in group:
                    raise ApiError(403, "not_your_appointment", "只能确认本人的约见")
            if slot and appt.get("slot") and slot != appt["slot"]:
                raise ApiError(409, "different_slot_requires_reschedule",
                                "choose_slot 只能确认企业发放的原席位；改期需走改约")

            decided_at = at or now_label()
            appt["seeker_choice_at"] = decided_at
            appt["seeker_confirmed_at"] = decided_at
            appt["choice_handling"] = handling or ("choose_and_cancel_other_overlaps" if cancel_other_overlaps else "selected_slot_only")
            update = self._append_update({
                "kind": "choose_slot", "entity": appointment_id,
                "occurred": decided_at, "slot": appt.get("slot"),
                "station": self._actor_station(actor), "registrant": registrant,
            })

            cancelled = []
            if cancel_other_overlaps:
                start = parse_time(appt["start"])
                end = parse_time(appt["end"])
                identity_at = appt.get("start") or at
                group = set(self.identity_group(ref["registrant"], identity_at))
                for other in self.state["appointments"].values():
                    if other["id"] == appointment_id or other.get("status") == "cancelled":
                        continue
                    other_ref = self.state["referrals"].get(other.get("referral"), {})
                    if other_ref.get("registrant") not in group:
                        continue
                    o_start, o_end = parse_time(other["start"]), parse_time(other["end"])
                    if start < o_end and o_start < end:
                        other["status"] = "cancelled"
                        other["cancelled_at"] = decided_at
                        other["cancel_reason"] = "seeker_chose_overlapping_slot"
                        cancelled.append(other["id"])
            self.save()
            return {"choice": deepcopy(update), "appointment": deepcopy(appt),
                    "cancelled_overlaps": cancelled}

    def _interval(self, appt):
        return parse_time(appt["start"]), parse_time(appt["end"])

    def overlap_clusters(self, registrant: str, at: str | datetime | None = None) -> list[dict[str, Any]]:
        cutoff = self._cutoff(at)
        group = set(self.identity_group(registrant, cutoff))
        appts = []
        for appt in self.state["appointments"].values():
            ref = self.state["referrals"].get(appt.get("referral"), {})
            if ref.get("registrant") not in group:
                continue
            cancelled_at = parse_time(appt.get("cancelled_at"))
            if appt.get("status") == "cancelled" and (not cancelled_at or cancelled_at <= cutoff):
                continue
            confirmed_dt = parse_time(appt.get("confirmed")) if appt.get("confirmed") else None
            if confirmed_dt and confirmed_dt > cutoff:
                continue
            start, end = self._interval(appt)
            if start and end:
                appts.append((appt, start, end))

        overlaps: dict[str, set[str]] = {a["id"]: set() for a, _, _ in appts}
        for (a, s1, e1), (b, s2, e2) in combinations(appts, 2):
            if s1 < e2 and s2 < e1:
                overlaps[a["id"]].add(b["id"])
                overlaps[b["id"]].add(a["id"])

        clusters = []
        seen = set()
        for appt, _, _ in appts:
            aid = appt["id"]
            if aid in seen or not overlaps[aid]:
                continue
            stack = [aid]
            ids = set()
            while stack:
                cur = stack.pop()
                if cur in ids:
                    continue
                ids.add(cur)
                seen.add(cur)
                stack.extend(overlaps[cur] - ids)
            members = [self.state["appointments"][i] for i in sorted(ids)]
            choices = []
            for member in members:
                choice_time = parse_time(member.get("seeker_choice_at"))
                if member.get("seeker_choice_at") and choice_time <= cutoff:
                    choices.append({
                        "appointment": member["id"], "at": member["seeker_choice_at"],
                        "slot": member.get("seeker_slot") or member.get("slot"),
                    })
            decision = max(choices, key=lambda c: parse_time(c["at"])) if choices else None
            clusters.append({
                "appointments": sorted(ids),
                "overlap": True,
                "decision_required": decision is None,
                "decision": decision,
                "no_show_allowed": decision is not None,
            })
        return clusters

    def mark_no_show(self, appointment_id: str, employer_id: str | None = None,
                     at: str | None = None, reason: str | None = None,
                     actor: dict | None = None) -> dict[str, Any]:
        with self.lock:
            appt = self._get("appointments", appointment_id, "约见")
            actor_employer = self._actor_employer(actor) or employer_id
            if actor_employer and appt["employer"] != actor_employer:
                raise ApiError(404, "appointment_not_found", "约见不存在或不属于该企业")
            if appt.get("status") == "cancelled":
                raise ApiError(409, "appointment_cancelled", "已取消约见不能报爽约")

            ref = self._appointment_ref(appt)
            # Identity is judged at the overlapping appointment time, so a
            # later mistaken split cannot erase the protection retroactively.
            identity_at = appt.get("start")
            report_cutoff = self._cutoff(at)
            group = set(self.identity_group(ref["registrant"], identity_at))
            original_group = self.identity_group(ref["registrant"], identity_at)
            unresolved = []
            for other in self.state["appointments"].values():
                if other["id"] == appointment_id or other.get("status") == "cancelled":
                    continue
                other_ref = self.state["referrals"].get(other.get("referral"), {})
                if other_ref.get("registrant") not in original_group:
                    continue
                s1, e1 = parse_time(appt["start"]), parse_time(appt["end"])
                s2, e2 = parse_time(other["start"]), parse_time(other["end"])
                if not (s1 < e2 and s2 < e1):
                    continue
                chosen_at = parse_time(appt.get("seeker_choice_at"))
                # A choice of either overlapping appointment resolves the
                # pre-decision no-show block.
                other_chosen = parse_time(other.get("seeker_choice_at"))
                appt_chosen = appt.get("seeker_choice_at") and chosen_at <= report_cutoff
                other_choice_done = other.get("seeker_choice_at") and other_chosen <= report_cutoff
                if not appt_chosen and not other_choice_done:
                    unresolved.append(other["id"])
            if unresolved:
                raise ApiError(409, "overlap_decision_pending",
                                "时间相交的有效邀约尚待本人选择；原约见可查，不能报爽约",
                                {"overlapping_appointments": unresolved})

            appt["attendance_status"] = "no_show"
            appt["no_show_at"] = at or now_label()
            appt["no_show_reason"] = reason
            self.save()
            return deepcopy(appt)

    # ------------------------------------------------------------ ownership/view
    def _referral_delivery(self, ref: dict[str, Any], cutoff: datetime) -> dict[str, Any]:
        opp = self.state["opportunities"].get(ref["opportunity"], {})
        grant = self.state["grants"].get(ref.get("grant"), {})
        received_dt = parse_time(ref.get("received"))
        received_by_cutoff = bool(received_dt and received_dt <= cutoff)
        sent_dt = parse_time(ref.get("sent"))
        sent_by_cutoff = bool(sent_dt and sent_dt <= cutoff)

        grant_check = self.check_grant(ref["grant"], at=ref.get("sent") or cutoff,
                                       fields=[i["field"] for i in ref.get("items", [])]) if grant else {
            "allowed": False, "reasons": ["grant_missing"]
        }
        current_grant = self.check_grant(ref["grant"], at=cutoff) if grant else {"allowed": False}

        items = []
        for item in ref.get("items", []):
            row = deepcopy(item)
            row["delivery_status"] = "employer_receipt_confirmed" if received_by_cutoff else (
                "sent_awaiting_receipt" if sent_by_cutoff else "not_sent_yet")
            row["delivered"] = received_by_cutoff
            items.append(row)

        owner_choice_after = None
        choice = self.latest_owner_choice(ref["opportunity"], cutoff)
        if choice and received_dt and parse_time(choice.get("occurred")) < received_dt:
            owner_choice_after = True

        return {
            "referral_id": ref["id"],
            "registrant": ref["registrant"],
            "source_station": ref["station"],
            "opportunity": {
                "id": opp.get("id"), "employer": opp.get("employer"),
                "named": opp.get("named"), "legacy_key": opp.get("legacy_key"),
                "opening": opp.get("opening"), "status": opp.get("status"),
            },
            "grant_id": ref.get("grant"),
            "sent_at": ref.get("sent"),
            "received_at": ref.get("received") if received_by_cutoff else None,
            "future_or_late_receipt_at": ref.get("received") if ref.get("received") and not received_by_cutoff else None,
            "delivery_status": "confirmed_delivered" if received_by_cutoff else (
                "sent_waiting_employer_receipt" if sent_by_cutoff else "not_sent_yet"),
            "items": items,
            "submission_authorization_valid": grant_check["allowed"],
            "submission_authorization_reasons": grant_check.get("reasons", []),
            "grant_currently_disclosable": current_grant.get("allowed", False),
            "grant_current_reasons": current_grant.get("reasons", []),
            "receipt_matched_opportunity": ref["opportunity"],
            "receipt_matched_opening": opp.get("opening"),
            "late_receipt_after_owner_choice": bool(owner_choice_after),
        }

    def resolve_opportunity_ownership(self, opportunity_id: str, registrant: str | None = None,
                                      at: str | None = None) -> dict[str, Any]:
        cutoff = self._cutoff(at)
        opp = self._get("opportunities", opportunity_id, "岗位机会")
        refs = [r for r in self.state["referrals"].values() if r["opportunity"] == opportunity_id]
        if registrant:
            group = set(self.identity_group(registrant, cutoff))
            refs = [r for r in refs if r["registrant"] in group]

        candidates = []
        for ref in refs:
            check = self.check_grant(ref["grant"], at=ref.get("sent") or cutoff)
            received_dt = parse_time(ref.get("received"))
            received = bool(received_dt and received_dt <= cutoff)
            candidates.append({
                "referral_id": ref["id"], "registrant": ref["registrant"],
                "station": ref["station"], "submitted_at": ref.get("sent"),
                "authorized_at_submission": check["allowed"],
                "authorization_reasons": check.get("reasons", []),
                "employer_receipt": received,
                "received_at": ref.get("received") if received else None,
                "qualified": check["allowed"] and received,
            })

        choice = self.latest_owner_choice(opportunity_id, cutoff)
        if registrant and choice and choice.get("registrant") and choice["registrant"] != registrant:
            # A choice explicitly made for another identity stream does not bind.
            choice = None

        qualified_stations = sorted({c["station"] for c in candidates if c["qualified"]})
        pending_stations = sorted({
            c["station"] for c in candidates
            if c["authorized_at_submission"] and not c["employer_receipt"]
        })

        if choice:
            owner = {
                "opportunity": opportunity_id,
                "owner_station": choice["station"],
                "responsible_station": choice["station"],
                "basis": "job_seeker_choice",
                "basis_update": choice["id"],
                "decided_at": choice["occurred"],
                "exclusive_followup_allowed": True,
                "candidates": candidates,
            }
        elif len(qualified_stations) == 1:
            owner = {
                "opportunity": opportunity_id,
                "owner_station": qualified_stations[0],
                "responsible_station": qualified_stations[0],
                "basis": "authorized_received_referral",
                "exclusive_followup_allowed": True,
                "candidates": candidates,
            }
        elif len(qualified_stations) > 1:
            owner = {
                "opportunity": opportunity_id, "owner_station": None,
                "responsible_station": None, "basis": "multiple_qualified_need_seeker_choice",
                "exclusive_followup_allowed": False,
                "qualified_stations": qualified_stations, "candidates": candidates,
            }
        else:
            owner = {
                "opportunity": opportunity_id, "owner_station": None,
                "responsible_station": None, "basis": "awaiting_employer_receipt",
                "exclusive_followup_allowed": False,
                "pending_stations": pending_stations,
                "first_submission_does_not_create_priority": True,
                "candidates": candidates,
            }
        owner["opportunity_snapshot"] = {
            "id": opp["id"], "employer": opp["employer"], "legacy_key": opp["legacy_key"],
            "opening": opp["opening"], "named": opp["named"],
        }
        return owner

    # A short alias useful to callers thinking in terms of the business rule.
    determine_owner = resolve_opportunity_ownership

    def job_seeker_view(self, registrant: str, at: str | None = None,
                        appointment_id: str | None = None) -> dict[str, Any]:
        cutoff = self._cutoff(at)
        identity = self.identity_view(registrant, cutoff)
        group = identity["current_group"]
        group_set = set(group)

        refs = [r for r in self.state["referrals"].values() if r["registrant"] in group_set]
        refs.sort(key=lambda r: parse_time(r.get("sent")) or cutoff)
        deliveries = [self._referral_delivery(r, cutoff) for r in refs]

        opp_ids = sorted({r["opportunity"] for r in refs})
        ownership = [self.resolve_opportunity_ownership(oid, registrant, cutoff) for oid in opp_ids]

        appts = []
        for appt in self.state["appointments"].values():
            ref = self.state["referrals"].get(appt.get("referral"), {})
            if ref.get("registrant") not in group_set:
                continue
            confirmed_dt = parse_time(appt.get("confirmed")) if appt.get("confirmed") else None
            if confirmed_dt and confirmed_dt > cutoff:
                continue
            row = deepcopy(appt)
            row["opportunity_id"] = ref.get("opportunity")
            row["source_referral"] = ref.get("id")
            row["source_station"] = ref.get("station")
            row["contact"] = self.appointment_contact(appt, cutoff)
            row["seeker_confirmed"] = bool(appt.get("seeker_choice_at") and parse_time(appt["seeker_choice_at"]) <= cutoff)
            appts.append(row)
        appts.sort(key=lambda a: parse_time(a.get("start")) or cutoff)

        overlaps = self.overlap_clusters(registrant, cutoff)
        overlapping_ids = {aid for cluster in overlaps for aid in cluster["appointments"]}
        unresolved_ids = {
            aid for cluster in overlaps if cluster["decision_required"] for aid in cluster["appointments"]
        }
        resolved_unchosen_ids = set()
        chosen_ids = set()
        for cluster in overlaps:
            if cluster.get("decision_required") or not cluster.get("decision"):
                continue
            chosen = cluster["decision"]["appointment"]
            chosen_ids.add(chosen)
            resolved_unchosen_ids.update(aid for aid in cluster["appointments"] if aid != chosen)

        to_confirm = []
        to_cancel = []
        for row in appts:
            if row.get("status") == "cancelled":
                continue
            if row["id"] in unresolved_ids:
                row["seeker_action"] = "resolve_overlapping_invitations"
                to_confirm.append(row)
            elif row["id"] in resolved_unchosen_ids:
                row["seeker_action"] = "cancel_unchosen_overlapping_appointment"
                to_cancel.append(row)
            elif not row["seeker_confirmed"]:
                row["seeker_action"] = "confirm_slot_or_cancel"
                to_confirm.append(row)
            else:
                row["seeker_action"] = None

        focus = None
        if appointment_id:
            focus = next((a for a in appts if a["id"] == appointment_id), None)
            if focus is None:
                raise ApiError(404, "appointment_not_in_query_scope", "该约见不在当前求职者身份范围内")

        upcoming = [a for a in appts if a.get("status") != "cancelled" and parse_time(a.get("start")) and parse_time(a["start"]) >= cutoff]
        contact_focus = focus or (upcoming[0] if upcoming else (appts[0] if appts else None))
        current_contact = contact_focus.get("contact") if contact_focus else None

        return {
            "queried_registrant": registrant,
            "as_of": time_label(cutoff) or cutoff.isoformat(),
            "identity": identity,
            "material_deliveries": deliveries,
            "confirmed_delivered_items": [
                item for delivery in deliveries for item in delivery["items"] if item["delivered"]
            ],
            "follow_up_by_opportunity": ownership,
            "current_contact": current_contact,
            "current_window": (current_contact or {}).get("station"),
            "responsible_window": (current_contact or {}).get("station"),
            "focus_appointment": contact_focus,
            "appointments_needing_confirmation": to_confirm,
            "appointments": appts,
            "overlap_decisions": overlaps,
            "action_required": {
                "appointments_to_confirm": to_confirm,
                "appointments_needing_confirmation": to_confirm,
                "overlap_clusters": [c for c in overlaps if c["decision_required"]],
                "appointments_to_cancel": to_cancel,
                "all_open_items": to_confirm + to_cancel,
            },
            "provenance": self.provenance_for_records(group),
        }

    # ------------------------------------------------------------ employer API
    def employer_opportunity_view(self, employer_id: str, opportunity_id: str,
                                  at: str | None = None) -> dict[str, Any]:
        opp = self.state["opportunities"].get(opportunity_id)
        if not opp or opp.get("employer") != employer_id:
            raise ApiError(404, "opportunity_not_found", "机会不存在")
        cutoff = self._cutoff(at)
        refs = [r for r in self.state["referrals"].values() if r["opportunity"] == opportunity_id]
        referral_views = []
        for ref in sorted(refs, key=lambda r: r["id"]):
            delivery = self._referral_delivery(ref, cutoff)
            appts = []
            for appt in self.state["appointments"].values():
                if appt.get("referral") != ref["id"]:
                    continue
                row = deepcopy(appt)
                row["contact"] = self.appointment_contact(appt, cutoff)
                appts.append(row)
            referral_views.append({
                "referral_id": ref["id"],
                "registrant": ref["registrant"],
                "source_station": ref["station"],
                "sent_at": ref.get("sent"),
                "received_at": delivery["received_at"],
                "delivery_status": delivery["delivery_status"],
                "items": [{
                    "field": i["field"], "delivered": i["delivered"],
                    "delivery_status": i["delivery_status"],
                } for i in delivery["items"]],
                "current_contact": (appts[0].get("contact") if appts else None),
                "appointments": appts,
            })
        return {
            "opportunity": deepcopy(opp),
            "referrals": referral_views,
            "isolation_notice": "仅返回该企业该 opportunity 批次；不能用其他 opportunity 或旧编号探测跨窗口资料",
        }

    def employer_referral_view(self, employer_id: str, referral_id: str, at: str | None = None) -> dict[str, Any]:
        ref = self._get("referrals", referral_id, "转介")
        opp = self._get("opportunities", ref["opportunity"], "岗位机会")
        if opp["employer"] != employer_id:
            raise ApiError(404, "referral_not_found", "转介不存在")
        return self.employer_opportunity_view(employer_id, opp["id"], at)
