"""API + service tests. Run:  python -m unittest discover -s tests -v
Uses a temporary data folder, so it never touches your real database."""
import http.server
import io
import json
import os
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_tmp = tempfile.mkdtemp(prefix="ss_test_")
os.environ["SS_DATA_DIR"] = _tmp
os.environ["SS_SEED_DEMO"] = "1"

import numpy as np  # noqa: E402

from backend.app import create_app  # noqa: E402
from backend import db  # noqa: E402
from backend.services import cloud_sync, dsp, impact, ml, simulator, synth_images  # noqa: E402

PW = "Sense@2026"
_TOK = {}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(start_workers=False)
        cls.c = cls.app.test_client()
        if not _TOK:
            for r in ("admin", "engineer", "viewer"):
                _TOK[r] = cls._login(cls, r)
        cls.H = _TOK

    def _login(self, role):
        r = self.c.post("/api/auth/login", json={"email": f"{role}@vitaforge.demo", "password": PW})
        assert r.status_code == 200, r.data
        return {"Authorization": "Bearer " + r.get_json()["token"]}


class TestAuth(Base):
    def test_requires_login(self):
        self.assertEqual(self.c.get("/api/structures").status_code, 401)

    def test_bad_password(self):
        r = self.c.post("/api/auth/login", json={"email": "engineer@vitaforge.demo", "password": "nope"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("error", r.get_json())

    def test_roles(self):
        self.assertEqual(self.c.post("/api/sim/inject", json={"structure_id": "STR-002", "type": "reset"}, headers=self.H["viewer"]).status_code, 403)
        self.assertEqual(self.c.get("/api/users", headers=self.H["engineer"]).status_code, 403)
        self.assertEqual(self.c.get("/api/users", headers=self.H["admin"]).status_code, 200)

    def test_signup_validation(self):
        self.assertEqual(self.c.post("/api/auth/register", json={"email": "bad", "name": "A", "password": "x"}).status_code, 400)
        r = self.c.post("/api/auth/register", json={"email": "new.user@example.com", "name": "New User", "password": "longenough1"})
        self.assertEqual(r.status_code, 201)
        h = {"Authorization": "Bearer " + r.get_json()["token"]}
        self.assertEqual(self.c.get("/api/auth/me", headers=h).get_json()["user"]["role"], "viewer")

    def test_password_not_stored_plain(self):
        row = db.one("SELECT pw_hash FROM users WHERE email='admin@vitaforge.demo'")
        self.assertNotIn(PW, row["pw_hash"])


class TestSeedAndHealth(Base):
    def test_structures_seeded(self):
        s = self.c.get("/api/structures", headers=self.H["viewer"]).get_json()
        self.assertEqual({x["id"] for x in s}, {"STR-001", "STR-002", "STR-003"})
        for x in s:
            self.assertIsNotNone(x["health"])
            self.assertTrue(0 <= x["health"]["score"] <= 100)

    def test_detail_has_components_and_provenance(self):
        d = self.c.get("/api/structures/STR-002", headers=self.H["viewer"]).get_json()
        self.assertEqual(set(d["health_full"]["components"]), {"vibration", "cracks", "strain_fatigue", "deflection", "environment"})
        self.assertTrue(d["sources_1h"].get("sim", 0) > 0)
        self.assertTrue(all(z["id"] in d["health_full"]["zones"] for z in d["zones"]))

    def test_unknown_structure(self):
        self.assertEqual(self.c.get("/api/structures/NOPE", headers=self.H["viewer"]).status_code, 404)

    def test_data_mode_live_excludes_sim(self):
        r = self.c.patch("/api/structures/STR-003", json={"data_mode": "live"}, headers=self.H["engineer"])
        self.assertEqual(r.status_code, 200)
        h = self.c.get("/api/structures/STR-003/health", headers=self.H["viewer"]).get_json()
        self.assertIsNone(h["score"] if h["coverage"] == 0 else None) if h["coverage"] == 0 else None
        # with live-only mode and no live data, coverage must collapse (sim data is not allowed to leak in)
        res = simulator.refresh_health("STR-003")
        self.assertEqual(res["coverage"], 0.0)
        self.assertIsNone(res["score"])
        self.c.patch("/api/structures/STR-003", json={"data_mode": "all"}, headers=self.H["engineer"])

    def test_bad_data_mode(self):
        self.assertEqual(self.c.patch("/api/structures/STR-003", json={"data_mode": "x"}, headers=self.H["engineer"]).status_code, 400)

    def test_frequency_drop_lowers_score_and_raises_alert(self):
        simulator.inject("STR-002", "reset")
        for d in db.q("SELECT * FROM devices WHERE structure_id='STR-002' AND channel='waveform'"):
            simulator.capture(d, 0.0)
        before = simulator.refresh_health("STR-002")
        simulator.inject("STR-002", "crack_step")
        simulator.inject("STR-002", "crack_step")
        simulator.inject("STR-002", "crack_step")
        for d in db.q("SELECT * FROM devices WHERE structure_id='STR-002' AND channel='waveform'"):
            simulator.capture(d, 0.3)
        after = simulator.refresh_health("STR-002")
        vb = min(v["score"] for v in before["metrics"]["vibration"] if v.get("score") is not None)
        va = min(v["score"] for v in after["metrics"]["vibration"] if v.get("score") is not None)
        self.assertLess(va, vb)
        self.assertTrue(any(a["kind"] == "freq_shift" for a in db.q("SELECT kind FROM alerts WHERE structure_id='STR-002' AND status IN ('open','acknowledged')")))


class TestDSP(unittest.TestCase):
    def test_known_frequency(self):
        fs, f0 = 1000, 123.4
        t = np.arange(4096) / fs
        x = np.sin(2 * np.pi * f0 * t) + 0.05 * np.random.default_rng(1).normal(size=t.size)
        f = dsp.analyze(x, fs)
        self.assertAlmostEqual(f["dom_freq"], f0, delta=0.5)

    def test_rejects_short(self):
        with self.assertRaises(ValueError):
            dsp.analyze([1, 2, 3], 100)

    def test_damage_shifts_frequency(self):
        rng = np.random.default_rng(3)
        prof = simulator.WAVE_PROFILES["bridge"]
        a = dsp.analyze(simulator.synth_waveform(prof, 0.0, rng), prof["fs"])["dom_freq"]
        b = dsp.analyze(simulator.synth_waveform(prof, 0.3, rng), prof["fs"])["dom_freq"]
        self.assertLess(b, a * 0.97)

    def test_rainflow_and_miner(self):
        self.assertEqual(len(ml.rainflow([0, 10, 0, 10, 0])) > 0, True)
        d_small, _ = ml.miner_damage([0, 200, 0, 200, 0, 200, 0] * 5)
        d_big, _ = ml.miner_damage([0, 400, 0, 400, 0, 400, 0] * 5)
        self.assertGreater(d_big, d_small * 4)


class TestImpactLab(Base):
    def test_parse_packet(self):
        self.assertEqual(impact.parse_packet("184,126,171"), (184.0, 126.0, 171.0))
        self.assertEqual(impact.parse_packet("S1:184,S2:126,S3:171"), (184.0, 126.0, 171.0))
        self.assertEqual(impact.parse_packet("S3:171, S1:184, S2:126"), (184.0, 126.0, 171.0))
        for bad in ("", "1,2", "a,b,c", "9999,1,1", "S1:1,S2:2"):
            with self.assertRaises(ValueError):
                impact.parse_packet(bad)

    def test_formula_matches_original(self):
        base = {"s1_mean": 180.0, "s2_mean": 165.0, "s3_mean": 174.0}
        r = impact.anomaly({"sensor1": 198.0, "sensor2": 115.5, "sensor3": 174.0}, base)
        self.assertAlmostEqual(r["combined_score"], (10 + 30 + 0) / 3, places=6)
        self.assertAlmostEqual(r["health_score"], 100 - 40 / 3, places=6)
        self.assertEqual(impact.classify(20.0, 20, 60), "NORMAL")
        self.assertEqual(impact.classify(20.1, 20, 60), "POSSIBLE ANOMALY")
        self.assertEqual(impact.classify(60.1, 20, 60), "URGENT REVIEW")

    def test_serial_lines_endpoint_and_errors(self):
        r = self.c.post("/api/ingest/serial-lines", json={"structure_id": "STR-001", "lines": ["184,126,171", "S1:180,S2:130,S3:170", "oops", "1,2"]}, headers=self.H["engineer"])
        j = r.get_json()
        self.assertEqual((j["accepted"], j["rejected"]), (2, 2))
        self.assertEqual(self.c.post("/api/ingest/serial-lines", json={"structure_id": "STR-001", "lines": ["1,2,3"]}).status_code, 401)
        ov = self.c.get("/api/impact/STR-001/overview", headers=self.H["viewer"]).get_json()
        self.assertEqual(ov["live"]["source"], "live")

    def _record(self, purpose, condition, scenario, seconds=1):
        sid = "STR-001"
        st = self.c.post(f"/api/impact/{sid}/record/start", json={"purpose": purpose, "condition": condition, "duration": seconds}, headers=self.H["engineer"]).get_json()
        sim = impact.TapSim(scenario, np.random.default_rng(5))
        end = time.time() + seconds + 0.7
        while time.time() < end:
            impact.write_packet(sid, sim.packet(), int(time.time() * 1000), "live")
            time.sleep(0.05)
        return self.c.post(f"/api/impact/{sid}/record/finish", json={"job_id": st["job_id"]}, headers=self.H["engineer"])

    def test_full_workflow_calibrate_then_trial(self):
        sid = "STR-001"
        db.run("DELETE FROM impact_baselines WHERE structure_id=?", (sid,))
        db.run("DELETE FROM impact_trials WHERE structure_id=?", (sid,))
        r = self.c.post(f"/api/impact/{sid}/baseline/save", json={}, headers=self.H["engineer"])
        self.assertEqual(r.status_code, 400)  # needs >= 3 trials
        for _ in range(3):
            self.assertEqual(self._record("calibration", "Intact", "normal").status_code, 200)
        self.assertEqual(self.c.get(f"/api/impact/{sid}/calibration", headers=self.H["viewer"]).get_json()["count"], 3)
        self.assertEqual(self.c.post(f"/api/impact/{sid}/baseline/save", json={"specimen_id": "t1"}, headers=self.H["engineer"]).status_code, 200)
        ok = self._record("trial", "Intact", "normal").get_json()["trial"]
        self.assertEqual(ok["status"], "NORMAL")
        self.assertEqual(ok["source"], "live")
        bad = self._record("trial", "Damaged", "severe").get_json()["trial"]
        self.assertIn(bad["status"], ("POSSIBLE ANOMALY", "URGENT REVIEW"))
        self.assertGreater(bad["anomaly_score"], ok["anomaly_score"])
        # health + alert integration
        al = self.c.get("/api/alerts?status=active&structure_id=STR-001", headers=self.H["viewer"]).get_json()
        self.assertTrue(any(a["kind"] == "impact_deviation" for a in al))
        an = self.c.get(f"/api/impact/{sid}/analysis", headers=self.H["viewer"]).get_json()
        self.assertEqual((an["n_intact"], an["n_damaged"]), (1, 1))
        csv_text = self.c.get(f"/api/impact/{sid}/export.csv", headers=self.H["viewer"]).data.decode()
        self.assertIn("LIVE HARDWARE DATA", csv_text)

    def test_finish_too_early_and_unknown(self):
        st = self.c.post("/api/impact/STR-001/record/start", json={"purpose": "trial", "condition": "Intact", "duration": 5}, headers=self.H["engineer"]).get_json()
        r = self.c.post("/api/impact/STR-001/record/finish", json={"job_id": st["job_id"]}, headers=self.H["engineer"])
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.c.post("/api/impact/STR-001/record/finish", json={"job_id": "zzz"}, headers=self.H["engineer"]).status_code, 422)

    def test_settings_validation(self):
        r = self.c.put("/api/impact/STR-001/settings", json={"anomaly_threshold_pct": 50, "urgent_threshold_pct": 30}, headers=self.H["engineer"])
        self.assertEqual(r.status_code, 400)
        r = self.c.put("/api/impact/STR-001/settings", json={"anomaly_threshold_pct": 20, "urgent_threshold_pct": 60}, headers=self.H["engineer"])
        self.assertEqual(r.status_code, 200)

    def test_no_rig_structure(self):
        self.assertEqual(self.c.get("/api/impact/STR-002/overview", headers=self.H["viewer"]).status_code, 409)


class TestIngest(Base):
    def test_device_key_flow(self):
        r = self.c.post("/api/devices", json={"structure_id": "STR-002", "id": "esp-test", "kind": "temperature", "unit": "C", "source_type": "esp32_http"}, headers=self.H["engineer"])
        self.assertEqual(r.status_code, 201)
        key = r.get_json()["api_key"]
        ok = self.c.post("/api/ingest/readings", json={"device_id": "esp-test", "readings": [{"value": 31.5}, {"value": 31.7}, {"value": "x"}]}, headers={"X-Device-Key": key})
        self.assertEqual(ok.get_json()["accepted"], 2)
        self.assertEqual(ok.get_json()["rejected"], 1)
        bad = self.c.post("/api/ingest/readings", json={"device_id": "esp-test", "readings": [{"value": 1}]}, headers={"X-Device-Key": "ssk_wrong"})
        self.assertEqual(bad.status_code, 401)
        other = self.c.post("/api/ingest/readings", json={"device_id": "s2-temp-01", "readings": [{"value": 1}]}, headers={"X-Device-Key": key})
        self.assertEqual(other.status_code, 401)  # a key only works for its own device
        rd = self.c.get("/api/structures/STR-002/readings?device_id=esp-test&hours=1", headers=self.H["viewer"]).get_json()
        self.assertTrue(all(p[2] == "live" for p in rd["points"]))
        self.assertEqual(self.c.post("/api/devices", json={"structure_id": "STR-002", "id": "esp-test", "kind": "strain"}, headers=self.H["engineer"]).status_code, 409)

    def test_waveform_ingest(self):
        d = db.one("SELECT id FROM devices WHERE structure_id='STR-002' AND kind='accelerometer'")
        t = np.arange(1024) / 100
        x = (np.sin(2 * np.pi * 5 * t)).tolist()
        r = self.c.post("/api/ingest/waveform", json={"device_id": d["id"], "fs": 100, "samples": x}, headers=self.H["engineer"])
        self.assertEqual(r.status_code, 200)
        self.assertAlmostEqual(r.get_json()["features"]["dom_freq"], 5.0, delta=0.3)
        self.assertEqual(self.c.post("/api/ingest/waveform", json={"device_id": d["id"], "fs": 100, "samples": [1, 2, 3]}, headers=self.H["engineer"]).status_code, 400)

    def test_csv_long_and_wide(self):
        long_csv = "timestamp,device_id,value\n2026-10-01T10:00:00Z,s2-temp-01,30.1\n2026-10-01T10:01:00Z,s2-temp-01,30.4\n2026-10-01T10:02:00Z,nope,1\n"
        r = self.c.post("/api/ingest/csv", data={"structure_id": "STR-002", "file": (io.BytesIO(long_csv.encode()), "a.csv")}, headers=self.H["engineer"], content_type="multipart/form-data")
        j = r.get_json()
        self.assertEqual((j["mode"], j["rows_ok"], j["rows_rejected"]), ("long", 2, 1))
        wide = "time,s2-temp-01,s2-moist-01\n1760000000,29.0,6.1\n1760000060,29.2,6.2\n"
        r = self.c.post("/api/ingest/csv", data={"structure_id": "STR-002", "file": (io.BytesIO(wide.encode()), "b.csv")}, headers=self.H["engineer"], content_type="multipart/form-data")
        self.assertEqual(r.get_json()["rows_ok"], 2)
        self.assertEqual(self.c.post("/api/ingest/csv", data={"structure_id": "STR-002", "file": (io.BytesIO(b"x\n"), "c.csv")}, headers=self.H["engineer"], content_type="multipart/form-data").status_code, 400)

    def test_csv_waveform(self):
        t = np.arange(2048) / 200
        body = "sample\n" + "\n".join(f"{v:.5f}" for v in np.sin(2 * np.pi * 7 * t))
        r = self.c.post("/api/ingest/csv", data={"structure_id": "STR-002", "mode": "waveform", "device_id": "s2-acc-01", "fs": "200", "file": (io.BytesIO(body.encode()), "w.csv")},
                        headers=self.H["engineer"], content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.data)
        w = self.c.get("/api/waveforms?device_id=s2-acc-01&limit=1", headers=self.H["viewer"]).get_json()[0]
        self.assertEqual(w["source"], "file")
        self.assertAlmostEqual(w["dom_freq"], 7.0, delta=0.4)


class TestVision(Base):
    def test_upload_and_reject(self):
        import cv2
        img, _ = synth_images.make_image(np.random.default_rng(8), width_px=5.0)
        ok, buf = cv2.imencode(".png", img)
        zone = db.one("SELECT id FROM zones WHERE structure_id='STR-003' LIMIT 1")["id"]
        r = self.c.post("/api/vision/analyze", data={"structure_id": "STR-003", "zone_id": zone, "scale_px_per_mm": "10", "image": (io.BytesIO(buf.tobytes()), "crack.png")},
                        headers=self.H["engineer"], content_type="multipart/form-data")
        self.assertEqual(r.status_code, 201, r.data)
        j = r.get_json()
        self.assertTrue(j["crack_detected"])
        self.assertAlmostEqual(j["max_width_mm"], 0.5, delta=0.2)
        img_resp = self.c.get(j["overlay_url"], headers=self.H["viewer"])
        self.assertEqual(img_resp.status_code, 200)
        self.assertEqual(img_resp.mimetype, "image/png")
        bad = self.c.post("/api/vision/analyze", data={"structure_id": "STR-003", "scale_px_per_mm": "10", "image": (io.BytesIO(b"not an image at all"), "x.jpg")},
                          headers=self.H["engineer"], content_type="multipart/form-data")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.get_json()["error"]["code"], "not_an_image")
        self.assertEqual(self.c.post("/api/vision/analyze", data={"structure_id": "STR-003", "zone_id": "STR-001-Z1", "scale_px_per_mm": "10", "image": (io.BytesIO(buf.tobytes()), "c.png")},
                                     headers=self.H["engineer"], content_type="multipart/form-data").status_code, 400)

    def test_clean_sample_has_no_crack(self):
        r = self.c.post("/api/vision/samples/sample_clean_pores.png/analyze", json={"structure_id": "STR-003"}, headers=self.H["engineer"])
        self.assertEqual(r.status_code, 201)
        self.assertFalse(r.get_json()["crack_detected"])

    def test_path_traversal_blocked(self):
        self.assertEqual(self.c.get("/api/files/uploads/..%2f..%2fetc%2fpasswd.png", headers=self.H["viewer"]).status_code, 404)
        self.assertEqual(self.c.get("/api/files/secret/x.png", headers=self.H["viewer"]).status_code, 404)


class TestAlertsMaintenance(Base):
    def test_ack_resolve_flow_and_roles(self):
        a = self.c.get("/api/alerts?status=open", headers=self.H["viewer"]).get_json()
        self.assertTrue(a, "seed data should contain open alerts")
        aid = a[0]["id"]
        self.assertEqual(self.c.post(f"/api/alerts/{aid}/ack", headers=self.H["viewer"]).status_code, 403)
        self.assertEqual(self.c.post(f"/api/alerts/{aid}/ack", headers=self.H["engineer"]).status_code, 200)
        self.assertEqual(self.c.post(f"/api/alerts/{aid}/ack", headers=self.H["engineer"]).status_code, 409)
        self.assertEqual(self.c.post(f"/api/alerts/{aid}/resolve", json={}, headers=self.H["engineer"]).status_code, 400)
        r = self.c.post(f"/api/alerts/{aid}/resolve", json={"note": "Inspected bearings, all fine."}, headers=self.H["engineer"])
        self.assertEqual(r.status_code, 200)
        row = db.one("SELECT * FROM alerts WHERE id=?", (aid,))
        self.assertEqual((row["status"], row["resolved_by"]), ("resolved", "Engineer (demo)"))

    def test_maintenance_transitions(self):
        t = self.c.get("/api/maintenance?status=active", headers=self.H["viewer"]).get_json()
        prop = next(x for x in t if x["status"] == "proposed")
        self.assertEqual(self.c.patch(f"/api/maintenance/{prop['id']}", json={"status": "done"}, headers=self.H["engineer"]).status_code, 409)
        self.assertEqual(self.c.patch(f"/api/maintenance/{prop['id']}", json={"status": "approved"}, headers=self.H["engineer"]).status_code, 200)
        row = db.one("SELECT * FROM maintenance_tasks WHERE id=?", (prop["id"],))
        self.assertEqual(row["signed_off_by"], "Engineer (demo)")


class TestMisc(Base):
    def test_models_and_provenance(self):
        m = self.c.get("/api/models", headers=self.H["viewer"]).get_json()
        self.assertEqual(len(m["models"]), 5)
        self.assertTrue(all(x["status"] in ("REAL", "REAL_SIM_TRAINED") for x in m["models"]))
        p = self.c.get("/api/system/provenance", headers=self.H["viewer"]).get_json()
        self.assertTrue({"REAL", "SIMULATED", "EXTERNAL", "FUTURE"} <= {x["status"] for x in p})

    def test_report_html_and_csv(self):
        r = self.c.get("/api/structures/STR-002/report", headers=self.H["viewer"])
        self.assertIn(b"Structural health report", r.data)
        self.assertIn(b"decision-support prototype", r.data)
        c = self.c.get("/api/structures/STR-002/report?format=csv&hours=2", headers=self.H["viewer"])
        self.assertTrue(c.data.startswith(b"timestamp_iso"))

    def test_business_quote(self):
        q = self.c.post("/api/business/imaas-quote", json={"structures": 30, "kit": "standard"}, headers=self.H["viewer"]).get_json()
        self.assertEqual(q["volume_discount"], 0.2)
        self.assertLess(q["per_structure"]["capex_low"], q["per_structure"]["capex_high"])
        self.assertIn("ILLUSTRATIVE", q["disclaimer"])
        self.assertEqual(self.c.post("/api/business/imaas-quote", json={"structures": -1}, headers=self.H["viewer"]).status_code, 400)

    def test_simulator_scenarios(self):
        self.assertEqual(self.c.post("/api/sim/scenario", json={"structure_id": "STR-002", "scenario": "nope"}, headers=self.H["engineer"]).status_code, 400)
        r = self.c.post("/api/sim/inject", json={"structure_id": "STR-003", "type": "moisture_spike"}, headers=self.H["engineer"])
        self.assertEqual(r.status_code, 200)
        self.assertGreater(r.get_json()["moisture_boost"], 4)
        self.c.post("/api/sim/inject", json={"structure_id": "STR-003", "type": "reset"}, headers=self.H["engineer"])

    def test_sse_stream_headers(self):
        r = self.c.get("/api/stream", headers=self.H["viewer"], buffered=False)
        self.assertEqual(r.mimetype, "text/event-stream")
        first = next(iter(r.response))
        self.assertIn(b"retry", first)
        r.close()

    def test_static_and_404(self):
        self.assertEqual(self.c.get("/api/does-not-exist").status_code, 404)

    def test_audit_trail(self):
        self.assertTrue(len(self.c.get("/api/system/audit", headers=self.H["engineer"]).get_json()) > 0)


class _Mock(http.server.BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        _Mock.received.append((self.path, dict(self.headers), json.loads(self.rfile.read(n) or b"[]")))
        self.send_response(201)
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"[]")

    def log_message(self, *a):
        pass


class TestCloudSync(Base):
    def test_supabase_mirror_against_mock(self):
        srv = socketserver.TCPServer(("127.0.0.1", 0), _Mock)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        try:
            self.assertFalse(cloud_sync.status()["configured"])
            db.set_setting("cloud.url", f"http://127.0.0.1:{port}")
            db.set_setting("cloud.key", "service-key-" + "x" * 20)
            self.assertTrue(cloud_sync.test_connection()["ok"])
            n = cloud_sync.enqueue_all()
            self.assertGreater(n, 50)
            res = cloud_sync.flush()
            self.assertTrue(res["ok"], res)
            self.assertEqual(cloud_sync.status()["pending"], 0)
            paths = {p for p, _, _ in _Mock.received}
            self.assertIn("/rest/v1/structures?on_conflict=id", paths)
            self.assertIn("/rest/v1/health_scores?on_conflict=id", paths)
            hdr = next(h for p, h, _ in _Mock.received if "structures" in p)
            self.assertEqual(hdr.get("Prefer"), "resolution=merge-duplicates,return=minimal")
            devs = next(b for p, _, b in _Mock.received if "devices" in p)
            self.assertNotIn("api_key_hash", devs[0])  # secrets never leave the server
            wf = next(b for p, _, b in _Mock.received if "waveforms" in p)
            self.assertNotIn("samples", wf[0])  # raw samples are not mirrored
            st = self.c.get("/api/cloud/status", headers=self.H["viewer"]).get_json()
            self.assertNotIn("service-key", json.dumps(st))
        finally:
            srv.shutdown()
            db.run("DELETE FROM settings WHERE key IN ('cloud.url','cloud.key')")

    def test_cloud_failure_is_reported_not_raised(self):
        db.set_setting("cloud.url", "http://127.0.0.1:9")
        db.set_setting("cloud.key", "k" * 25)
        try:
            cloud_sync.enqueue("structures", "STR-001")
            res = cloud_sync.flush()
            self.assertFalse(res["ok"])
            self.assertFalse(cloud_sync.test_connection()["ok"])
        finally:
            db.run("DELETE FROM settings WHERE key IN ('cloud.url','cloud.key')")
            db.run("DELETE FROM sync_queue")


if __name__ == "__main__":
    unittest.main()
