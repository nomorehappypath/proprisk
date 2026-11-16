"""Contract regression: a validation failure always uses the
{"error": "<plain sentence>"} envelope — never FastAPI's detail list."""


def test_validation_error_uses_envelope_missing_field(client):
    resp = client.put("/api/settings/spending/cap", json={})
    assert resp.status_code == 422
    body = resp.json()
    assert "detail" not in body
    assert body == {"error": "Please fill in the budget cents."}


def test_validation_error_uses_envelope_bad_value(client):
    resp = client.put("/api/settings/spending/cap", json={"budget_cents": -5})
    assert resp.status_code == 422
    body = resp.json()
    assert "detail" not in body
    assert body["error"] == "Please check the budget cents."
