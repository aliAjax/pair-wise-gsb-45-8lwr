import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app import build_service
from src.http_api import create_server


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
HEADERS = {"X-User-Id": "demo-admin", "X-Role": "admin", "X-Org": "demo", "Content-Type": "application/json"}
VOYAGE = {'vessel': 'HaiYun', 'berth': 'B12', 'vessel_length_m': 180, 'berth_length_m': 220, 'draft_m': 10.2, 'berth_depth_m': 11.5, 'eta_hour': 6, 'etd_hour': 10, 'risk_level': 'medium', 'dangerous_goods': False, 'dangerous_class': ''}


class HttpApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service = build_service(str(Path(self.temp.name) / "test.db"))
        self.server = create_server("127.0.0.1", 0, service, STATIC_DIR)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        url = "http://127.0.0.1:%s%s" % (self.port, path)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers or HEADERS)
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_maintenance_flow_over_http(self):
        status, _ = self.request("GET", "/health")
        self.assertEqual(status, 200)
        status, voyage = self.request("POST", "/api/records", {"reference": "VOY-1", "data": VOYAGE})
        self.assertEqual(status, 201)
        status, order = self.request("POST", "/api/maintenance", {"reference": "M-1", "data": {"berth": "B12", "start_hour": 8, "end_hour": 14, "work_content": "吊机检修"}})
        self.assertEqual(status, 201)
        self.assertEqual(order["state"], "draft")
        status, order = self.request("POST", "/api/maintenance/%s/actions/confirm" % order["id"], {"expected_version": order["version"], "data": {}})
        self.assertEqual(status, 200)
        self.assertEqual(order["state"], "locked")
        self.assertEqual(order["payload"]["last_result"]["moves"][0]["to"], [14, 18])
        status, queue = self.request("GET", "/api/berths/B12/queue")
        self.assertEqual(status, 200)
        self.assertEqual(queue["locks"][0]["work_content"], "吊机检修")
        self.assertEqual(queue["queue"][0]["eta_hour"], 14)
        status, blocked = self.request("POST", "/api/records", {"reference": "VOY-2", "data": dict(VOYAGE, eta_hour=10, etd_hour=12)})
        self.assertEqual(status, 409)
        status, events = self.request("GET", "/api/maintenance/%s/audit" % order["id"])
        self.assertEqual(status, 200)
        self.assertEqual([event["action"] for event in events["items"]], ["created", "confirm"])

    def test_missing_identity_is_rejected(self):
        status, body = self.request("GET", "/api/maintenance", headers={"Content-Type": "application/json"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "permission_denied")
