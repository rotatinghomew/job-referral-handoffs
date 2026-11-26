"""Behavior tests for the cross-window employment backend."""
from __future__ import annotations

import json
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from employment import ApiError, EmploymentService
from employment.server import create_handler

ROOT = Path(__file__).resolve().parents[1]
PACKET = json.loads((ROOT / "data" / "referrals_20261207.json").read_text(encoding="utf-8"))


def service() -> EmploymentService:
    return EmploymentService.from_packet(PACKET)


class IdentityTimelineTest(unittest.TestCase):
    def setUp(self):
        self.svc = service()

    def test_merge_keep_separate_and_later_split(self):
        self.assertEqual(self.svc.identity_group("R1", "11:59"), ["R1"])
        self.assertEqual(self.svc.identity_group("R1", "12:00"), ["R1", "R2"])
        self.assertEqual(self.svc.identity_group("R1", "12:15"), ["R1", "R2"])
        self.assertNotIn("R3", self.svc.identity_group("R1", "13:00"))
        self.assertEqual(self.svc.identity_group("R1", "16:00"), ["R1"])
        self.assertEqual(self.svc.identity_group("R2", "16:01"), ["R2"])

    def test_clues_are_not_auto_merge(self):
        suggestions = self.svc.identity_suggestions("R1")
        candidates = {row["registrant"]: row for row in suggestions}
        self.assertIn("R2", candidates)
        self.assertIn("R3", candidates)
        self.assertTrue(all("verification_required" == row["decision"] for row in suggestions))

    def test_split_case_traces_source_window(self):
        provenance = self.svc.material_provenance(identity_case="I3")
        self.assertEqual(set(provenance["by_station"]), {"ST1", "ST2"})
        a1 = [item for item in provenance["items"] if item.get("referral_id") == "A1"]
        a2 = [item for item in provenance["items"] if item.get("referral_id") == "A2"]
        self.assertTrue(a1)
        self.assertTrue(a2)
        self.assertEqual({item["source_station"] for item in a1}, {"ST1"})
        self.assertEqual({item["source_station"] for item in a2}, {"ST2"})

    def test_objection_can_record_and_split(self):
        case = self.svc.create_identity_case(["R1", "R2"], station="ST1", kind="merge", decided_at="10:00")
        self.svc.object_to_identity_case(case["id"], "ST2", "求职者提出身份异议", at="10:30")
        split = self.svc.split_identity(case_id=case["id"], station="ST2", at="11:00")
        self.assertEqual(split["kind"], "split")
        self.assertEqual(split["splits_case"], case["id"])
        self.assertEqual(self.svc.identity_group("R1", "10:30"), ["R1", "R2"])
        self.assertEqual(self.svc.identity_group("R1", "11:00"), ["R1"])


class ReferralOwnershipAndDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.svc = service()

    def test_confirmed_parts_and_late_receipt(self):
        before = self.svc.job_seeker_view("R1", at="13:00")
        a1 = next(d for d in before["material_deliveries"] if d["referral_id"] == "A1")
        a2 = next(d for d in before["material_deliveries"] if d["referral_id"] == "A2")
        self.assertEqual(a1["delivery_status"], "confirmed_delivered")
        self.assertEqual({i["field"] for i in a1["items"] if i["delivered"]},
                         {"work_history", "contact_route"})
        self.assertEqual(a2["delivery_status"], "sent_waiting_employer_receipt")
        self.assertFalse(any(i["delivered"] for i in a2["items"]))

        after = self.svc.job_seeker_view("R1", at="13:10")
        a2_after = next(d for d in after["material_deliveries"] if d["referral_id"] == "A2")
        self.assertEqual(a2_after["delivery_status"], "confirmed_delivered")
        self.assertTrue(all(i["delivered"] for i in a2_after["items"]))
        self.assertTrue(a2_after["late_receipt_after_owner_choice"])
        self.assertEqual(a2_after["receipt_matched_opportunity"], "J1")
        self.assertEqual(a2_after["receipt_matched_opening"], 1)

    def test_two_pending_windows_have_no_exclusive_owner(self):
        owner = self.svc.resolve_opportunity_ownership("J1", at="09:10")
        self.assertIsNone(owner["owner_station"])
        self.assertFalse(owner["exclusive_followup_allowed"])
        self.assertEqual(set(owner["pending_stations"]), {"ST1", "ST2"})
        self.assertTrue(owner["first_submission_does_not_create_priority"])

        received = self.svc.resolve_opportunity_ownership("J1", at="09:20")
        self.assertEqual(received["owner_station"], "ST1")
        self.assertEqual(received["basis"], "authorized_received_referral")

    def test_seeker_choice_has_priority(self):
        owner = self.svc.resolve_opportunity_ownership("J1", at="12:40")
        self.assertEqual(owner["owner_station"], "ST2")
        self.assertEqual(owner["basis"], "job_seeker_choice")
        self.assertTrue(owner["exclusive_followup_allowed"])

    def test_legacy_receipt_uses_original_opening(self):
        result = self.svc.legacy_receipt("E1", "A-7", referral_id="A2", received_at="13:05")
        self.assertEqual(result["matched_opportunity_id"], "J1")
        self.assertEqual(result["matched_opening"], 1)
        with self.assertRaises(ApiError) as ctx:
            self.svc.resolve_opportunity_by_legacy("E1", "A-7")
        self.assertEqual(ctx.exception.code, "opening_required")
        self.assertEqual(self.svc.resolve_opportunity_by_legacy("E1", "A-7", 2)["id"], "J2")


class ContactTransferAndConflictTest(unittest.TestCase):
    def setUp(self):
        self.svc = service()

    def test_contact_change_keeps_original_seat(self):
        pending = self.svc.job_seeker_view("R1", at="12:40")
        v1 = next(a for a in pending["appointments"] if a["id"] == "V1")
        self.assertEqual(v1["contact"]["station"], "ST1")
        self.assertEqual(v1["contact"]["pending_transfer"]["to_station"], "ST2")

        confirmed = self.svc.job_seeker_view("R1", at="12:45")
        v1 = next(a for a in confirmed["appointments"] if a["id"] == "V1")
        contact = v1["contact"]
        self.assertEqual(contact["station"], "ST2")
        self.assertEqual(contact["status"], "employer_confirmed")
        self.assertTrue(contact["seat_retained"])
        self.assertEqual(contact["slot"], "W13")
        self.assertEqual((v1["start"], v1["end"]), ("15:00", "15:30"))

    def test_overlap_requires_choice_and_blocks_no_show(self):
        view = self.svc.job_seeker_view("R1", at="13:00")
        self.assertEqual({a["id"] for a in view["action_required"]["appointments_to_confirm"]},
                         {"V1", "V2"})
        self.assertTrue(view["action_required"]["overlap_clusters"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.mark_no_show("V1", employer_id="E1", at="13:05")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "overlap_decision_pending")

        chosen = self.svc.job_seeker_view("R1", at="13:20")
        self.assertEqual(chosen["action_required"]["appointments_to_confirm"], [])
        self.assertEqual({a["id"] for a in chosen["action_required"]["appointments_to_cancel"]}, {"V2"})
        marked = self.svc.mark_no_show("V1", employer_id="E1", at="13:20")
        self.assertEqual(marked["attendance_status"], "no_show")

    def test_withdrawal_and_cancellation_are_separate(self):
        grant = self.svc.withdraw_grant("G2", at="13:30")
        self.assertEqual(grant["withdrawn"], "13:30")
        check = self.svc.check_grant("G2", at="14:00")
        self.assertFalse(check["allowed"])
        self.assertIn("withdrawn", check["reasons"])
        v2 = self.svc._get("appointments", "V2")
        self.assertNotEqual(v2.get("status"), "cancelled")

        cancelled = self.svc.cancel_appointment("V2", registrant="R2", at="14:00")
        self.assertEqual(cancelled["appointment"]["status"], "cancelled")
        self.assertEqual(self.svc.check_grant("G3", at="15:00")["allowed"], True)

    def test_full_authorized_transfer_flow(self):
        self.svc.create_opportunity("E9", "L-1", 1, "理货", opportunity_id="J9")
        self.svc.create_grant("R1", "E9", "J9", ["compact_resume"], "10:00", "18:00", grant_id="G9")
        ref = self.svc.create_referral(
            registrant="R1", opportunity="J9", grant="G9", station="ST1", sent="10:05",
            actor={"station": "ST1"},
        )
        self.svc.receive_referral(ref["id"], "10:08", employer_id="E9")
        self.svc.record_owner_choice("J9", "ST2", at="10:10")
        appt = self.svc.create_appointment(ref["id"], "W1", "11:00", "11:20", confirmed="10:20")
        transfer = self.svc.request_transfer(appt["id"], "ST2", at="10:12", actor={"station": "ST1"})
        self.assertEqual(transfer["status"], "pending_employer_ack")
        self.svc.acknowledge_transfer(transfer["id"], employer_id="E9", at="10:15")
        contact = self.svc.appointment_contact(self.svc._get("appointments", appt["id"]),
                                               __import__("employment.timeutil", fromlist=["parse_time"]).parse_time("10:20"))
        self.assertEqual(contact["station"], "ST2")
        self.assertEqual(contact["slot"], "W1")


class AuthorizationAndIsolationTest(unittest.TestCase):
    def setUp(self):
        self.svc = service()

    def test_grant_is_exact_opportunity_even_when_legacy_key_reused(self):
        check = self.svc.check_grant("G1", at="10:00", opportunity="J2")
        self.assertFalse(check["allowed"])
        self.assertIn("opportunity_mismatch_exact_id_required", check["reasons"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.create_referral(
                registrant="R1", opportunity="J2", grant="G1", station="ST1", sent="10:00",
                actor={"station": "ST1"},
            )
        self.assertEqual(ctx.exception.code, "authorization_invalid_for_referral")

    def test_employer_sees_only_its_exact_opportunity(self):
        j1 = self.svc.employer_opportunity_view("E1", "J1")
        self.assertEqual({r["referral_id"] for r in j1["referrals"]}, {"A1", "A2"})
        j2 = self.svc.employer_opportunity_view("E1", "J2")
        self.assertEqual(j2["referrals"], [])
        serialized = json.dumps(j1, ensure_ascii=False)
        self.assertNotIn("A3", serialized)
        self.assertNotIn("J3", serialized)
        with self.assertRaises(ApiError) as ctx:
            self.svc.employer_opportunity_view("E2", "J1")
        self.assertEqual(ctx.exception.status, 404)

    def test_window_can_only_modify_own_material_source(self):
        materials = self.svc.verify_materials(
            "R1", "ST1", [{"field": "certificate", "title": "证书"}], at="10:00",
            actor={"station": "ST1"},
        )
        mid = materials[0]["id"]
        with self.assertRaises(ApiError) as ctx:
            self.svc.update_material(mid, {"title": "被 ST2 改写"}, actor={"station": "ST2"})
        self.assertEqual(ctx.exception.status, 403)
        updated = self.svc.update_material(mid, {"title": "新证书"}, actor={"station": "ST1"})
        self.assertEqual(updated["title"], "新证书")


class HttpApiTest(unittest.TestCase):
    def setUp(self):
        self.svc = service()
        handler = partial(create_handler(self.svc))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, body=None, headers=None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = Request(self.base + path, data=data, method=method, headers=headers or {})
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urlopen(req, timeout=5) as reply:
                return reply.status, json.loads(reply.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health_and_job_query(self):
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        status, body = self.request("GET", "/api/job-seeker/view?registrant=R1&at=13:00")
        self.assertEqual(status, 200)
        self.assertEqual(body["current_contact"]["station"], "ST2")
        self.assertTrue(body["action_required"]["overlap_clusters"])

    def test_employer_header_isolates_opportunity(self):
        status, _ = self.request("GET", "/api/employer/opportunities/J1/referrals",
                                  headers={"X-Employer-Id": "E2"})
        self.assertEqual(status, 404)
        status, body = self.request("GET", "/api/employer/opportunities/J1/referrals",
                                    headers={"X-Employer-Id": "E1"})
        self.assertEqual(status, 200)
        self.assertEqual({r["referral_id"] for r in body["referrals"]}, {"A1", "A2"})

    def test_http_withdraw_grant_stops_future_disclosure(self):
        status, body = self.request("POST", "/api/grants/G2/withdraw", {"at": "13:30"})
        self.assertEqual(status, 200)
        self.assertEqual(body["withdrawn"], "13:30")
        status, body = self.request("GET", "/api/job-seeker/view?registrant=R2&at=14:00")
        self.assertEqual(status, 200)
        g2_delivery = next(d for d in body["material_deliveries"] if d["grant_id"] == "G2")
        self.assertFalse(g2_delivery["grant_currently_disclosable"])


if __name__ == "__main__":
    unittest.main()
