"""GET /api/keys/exchanges carries the engine's leverage guard and flat
maintenance margin so the builder's go-live checklist reads them from the
API instead of duplicating the constants (consensus M28)."""
import os

from fastapi.testclient import TestClient

from backend.main import app
from backend.engine.contracts import MAINTENANCE_MARGIN
from backend.engine.settings_validator import LEVERAGE_WARN_ABOVE

HEADERS = {"X-API-Key": os.environ["MASTER_API_KEY"]}


def test_exchanges_payload_carries_leverage_guard_and_mmr():
    r = TestClient(app).get("/api/keys/exchanges", headers=HEADERS)
    assert r.status_code == 200
    rows = r.json()
    assert rows, "registry must list at least one exchange"
    for row in rows:
        assert row["leverage_warn_above"] == LEVERAGE_WARN_ABOVE
        assert row["maintenance_margin"] == MAINTENANCE_MARGIN
        assert 0 < row["maintenance_margin"] < 1
        assert row["leverage_warn_above"] >= 1
