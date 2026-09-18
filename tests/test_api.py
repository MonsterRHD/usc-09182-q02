"""HTTP 接口测试：真实起服务，走完整复核流程。"""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from service.api import make_handler
from service.lineage import LineageService
from service.store import MemoryStore


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        svc = LineageService(MemoryStore())
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(svc))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def call(self, method, path, body=None, headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_health(self):
        status, body = self.call("GET", "/health")
        self.assertEqual((status, body["status"]), (200, "ok"))

    def test_full_review_flow_over_http(self):
        # 登记
        _, w = self.call("POST", "/workshops", {"name": "东街作坊"})
        _, inh = self.call("POST", "/persons", {
            "name": "张传承", "role": "inheritor", "workshop_id": w["id"]})
        _, master = self.call("POST", "/persons", {
            "name": "王师傅", "role": "master", "workshop_id": w["id"]})
        _, shape = self.call("POST", "/shapes", {
            "name": "斑鸠", "workshop_id": w["id"]})

        # 提交版本（带幂等键），重复提交返回同一版本
        payload = {"author_id": master["id"], "photo_summary": "sha256:p1",
                   "steps": [{"name": "打孔", "detail": "音孔两孔"}],
                   "materials": ["黄河胶泥"]}
        _, v1 = self.call("POST", f"/shapes/{shape['id']}/versions",
                          payload, headers={"Idempotency-Key": "http-k1"})
        _, v1b = self.call("POST", f"/shapes/{shape['id']}/versions",
                           payload, headers={"Idempotency-Key": "http-k1"})
        self.assertEqual(v1["id"], v1b["id"])
        self.assertFalse(v1["replayed"])
        self.assertTrue(v1b["replayed"])

        # 非传承人确认 → 403；传承人确认 → 200
        status, _ = self.call("POST", f"/versions/{v1['id']}/confirm",
                              {"confirmer_id": master["id"]})
        self.assertEqual(status, 403)
        status, v1c = self.call("POST", f"/versions/{v1['id']}/confirm",
                                {"confirmer_id": inh["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(v1c["status"], "confirmed")

        # 授权 → 公众可见；撤回 → 公众 403，审计仍在
        _, lic = self.call("POST", "/licenses", {
            "shape_id": shape["id"], "scope": "网络展示", "granted_by": inh["id"]})
        status, pub = self.call("GET", f"/public/versions/{v1['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(pub["public_scopes"], ["网络展示"])
        self.call("POST", f"/licenses/{lic['id']}/revoke",
                  {"revoked_by": inh["id"], "reason": "暂缓公开"})
        status, denied = self.call("GET", f"/public/versions/{v1['id']}")
        self.assertEqual(status, 403)
        status, audit = self.call("GET", "/audit", headers={"X-Person-Id": inh["id"]})
        self.assertEqual(status, 200)
        types = [e["type"] for e in audit["audit"]]
        self.assertIn("license_granted", types)
        self.assertIn("license_revoked", types)
        # 非传承人查审计 → 403
        status, _ = self.call("GET", "/audit", headers={"X-Person-Id": master["id"]})
        self.assertEqual(status, 403)

        # 权限解释接口
        status, expl = self.call(
            "GET", f"/permissions/explain?version_id={v1['id']}")
        self.assertEqual(status, 200)
        self.assertFalse(expl["allowed"])
        self.assertTrue(any("撤回" in r for r in expl["reasons"]))

        # 引用链接口
        status, chain = self.call("GET", f"/versions/{v1['id']}/chain")
        self.assertEqual(status, 200)
        self.assertEqual(chain["workshop_id"], w["id"])
        self.assertEqual(len(chain["chain"]), 1)

    def test_sync_endpoint_replays_batch(self):
        _, w = self.call("POST", "/workshops", {"name": "西巷作坊"})
        _, inh = self.call("POST", "/persons", {
            "name": "李传承", "role": "inheritor", "workshop_id": w["id"]})
        _, master = self.call("POST", "/persons", {
            "name": "赵师傅", "role": "master", "workshop_id": w["id"]})
        _, shape = self.call("POST", "/shapes", {
            "name": "燕子", "workshop_id": w["id"]})
        batch = {"device_id": "pad-1", "batch_id": "batch-1", "ops": [
            {"op": "submit_version", "idempotency_key": "s-op-1",
             "payload": {"shape_id": shape["id"], "author_id": master["id"],
                         "photo_summary": "sha256:s1",
                         "steps": [{"name": "塑形", "detail": "捏燕身"}]}},
        ]}
        _, r1 = self.call("POST", "/sync", batch)
        _, r2 = self.call("POST", "/sync", batch)
        self.assertFalse(r1["replayed"])
        self.assertTrue(r2["replayed"])
        self.assertEqual(r1["results"], r2["results"])
        self.assertTrue(r1["results"][0]["ok"])

    def test_unknown_route_404(self):
        status, body = self.call("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
