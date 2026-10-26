import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
import app.main as service


class ControlPlaneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB = Path(self.temp.name) / "test.db"
        self.client = TestClient(service.app)
        self.client.__enter__()
        result = self.client.post("/api/auth/register", json={"email": "owner@example.com", "password": "longpassword"})
        self.headers = {"Authorization": "Bearer " + result.json()["token"]}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def test_demo_is_exclusive_and_releases(self):
        demo = self.client.post("/api/demo", headers=self.headers).json()
        payload = {"device_id": demo["device_id"], "requester_machine_id": demo["requester_machine_id"]}
        first = self.client.post("/api/sessions", headers=self.headers, json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.client.post("/api/sessions", headers=self.headers, json=payload).status_code, 409)
        self.assertEqual(self.client.post("/api/sessions/" + first.json()["id"] + "/disconnect", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.post("/api/sessions", headers=self.headers, json=payload).status_code, 200)

    def test_hardware_connect_fails_honestly(self):
        for machine_id, devices in (("host-pc01", [{"id": "usb-flash01", "name": "Test USB", "kind": "mass-storage"}]), ("guest-pc1", [])):
            result = self.client.post("/api/machines/heartbeat", headers=self.headers, json={"machine": {"id": machine_id, "name": machine_id, "os": "Windows 11"}, "devices": devices})
            self.assertEqual(result.status_code, 200)
        result = self.client.post("/api/sessions", headers=self.headers, json={"device_id": "host-pc01:usb-flash01", "requester_machine_id": "guest-pc1"})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()["detail"]["code"], "CONNECTOR_UPDATE_REQUIRED")

    def test_identical_pnp_ids_on_two_computers_do_not_block_discovery(self):
        shared_pnp_id = "pnp-usb-root-hub"
        for machine_id in ("laptop-one", "laptop-two"):
            result = self.client.post("/api/machines/heartbeat", headers=self.headers, json={
                "machine": {"id": machine_id, "name": machine_id, "os": "Windows 11"},
                "devices": [{"id": shared_pnp_id, "name": "USB Root Hub", "kind": "usb-hub", "eligible": False}],
            })
            self.assertEqual(result.status_code, 200)
        devices = self.client.get("/api/overview", headers=self.headers).json()["devices"]
        self.assertEqual({item["id"] for item in devices}, {"laptop-one:" + shared_pnp_id, "laptop-two:" + shared_pnp_id})

    def test_other_account_cannot_see_devices(self):
        self.client.post("/api/demo", headers=self.headers)
        result = self.client.post("/api/auth/register", json={"email": "other@example.com", "password": "longpassword"})
        other = {"Authorization": "Bearer " + result.json()["token"]}
        overview = self.client.get("/api/overview", headers=other).json()
        self.assertEqual(overview["machines"], [])
        self.assertEqual(overview["devices"], [])


if __name__ == "__main__":
    unittest.main()
