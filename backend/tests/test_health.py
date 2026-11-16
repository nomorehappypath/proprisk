from fastapi.testclient import TestClient

from app.main import app


def test_health() -> None:
    resp = TestClient(app, base_url="http://127.0.0.1").get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["app"] == "PropRisk"
