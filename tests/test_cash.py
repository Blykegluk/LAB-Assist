"""Tests du module dépôts d'espèces (extraction simulée, rapprochement, alertes, synthèse, sheet)."""
from __future__ import annotations

import json
import os
import pathlib
import sys
from datetime import date, timedelta

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("cash")
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/test.db"
    os.environ["CASH_MOCK_EXTRACTION"] = "1"
    os.environ["CASH_SCHEDULER"] = "0"
    os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")
    os.environ["CASH_MANAGER_PIN"] = "2468"
    os.environ["CASH_OWNER_PASSWORD"] = "owner-secret"
    os.environ.pop("SMTP_HOST", None)
    os.environ.pop("GOOGLE_SERVICE_ACCOUNT_JSON", None)
    os.environ.pop("PENNYLANE_API_KEY", None)
    os.chdir(ROOT)
    from fastapi.testclient import TestClient
    from backend.app import app
    from backend.database import Base, engine
    from backend import cash_config

    cash_config.cash_config.cache_clear()
    cash_config.cash_config()["docs_dir"] = str(tmp / "docs")
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c


def _login(client, code):
    r = client.post("/cash/auth", json={"code": code})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _upload(client, headers, store_id, docs: list[dict]):
    files = [("files", (f"doc{i}.json", json.dumps(d).encode("utf-8"), "application/json")) for i, d in enumerate(docs)]
    r = client.post("/cash/submissions", data={"store_id": store_id}, files=files, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# --- documents simulés, calqués sur les vrais exemples (Léon Blum 19-20/10, Ornano 20-21/10)
RELEVE_LB_1920 = {
    "type": "releve_storemate", "confidence": 0.95, "store_name": "NATURALIA LEON BLUM", "store_number": "4920",
    "period_start": "19/10/2026", "period_end": "20/10/2026", "cash_amount": 1606.85, "cash_count": 159, "total_amount": 12374.46,
}
BORDEREAU_LB_1595 = {
    "type": "bordereau", "confidence": 0.9, "deposit_date": "21/10/2026", "deposit_amount": 1595, "bag_number": "0005179420",
    "account_holder": "AEJB", "depositor": "Camille",
    "breakdown": [{"denomination": 100, "count": 1, "amount": 100}, {"denomination": 50, "count": 17, "amount": 850},
                  {"denomination": 20, "count": 22, "amount": 440}, {"denomination": 10, "count": 18, "amount": 180}, {"denomination": 5, "count": 5, "amount": 25}],
}
RECU_LB_1595 = {"type": "recu_gab", "confidence": 0.98, "deposit_date": "21/10/2026", "deposit_time": "17:30:15", "deposit_amount": 1595.0,
                "bag_number": "0005179420", "operation_number": "4789"}
RELEVE_LB_1618 = {"type": "releve_storemate", "confidence": 0.95, "store_name": "NATURALIA LEON BLUM", "store_number": "4920",
                  "period_start": "16/10/2026", "period_end": "18/10/2026", "cash_amount": 2101.30, "total_amount": 22223.98}
BORDEREAU_LB_2100 = {"type": "bordereau", "confidence": 0.85, "deposit_date": "19/10/2026", "deposit_amount": 2100, "bag_number": "0005179418", "account_holder": "AEJB"}
RECU_LB_2100 = {"type": "recu_gab", "confidence": 0.98, "deposit_date": "21/10/2026", "deposit_amount": 2100.0, "bag_number": "0005179418", "operation_number": "4790"}
RELEVE_ORIKA = {
    "type": "releve_orika", "confidence": 0.9, "store_name": "ORNANO", "store_number": "4956", "period_start": "20/10/2026", "period_end": "21/10/2026",
    "cash_amount": 1243.11, "handwritten_amount": 1245, "handwritten_notes": "1245€ (pièces dans le coffre)",
    "operators": [
        {"name": "Aliocha (2)", "fdc": 400, "cash": 232.37, "prelevement": 232.83, "theorique": 399.54, "declaration": 400, "difference": 0.46},
        {"name": "Benjamin Benjamin (10)", "fdc": 400.08, "cash": 575.95, "prelevement": 576.91, "theorique": 399.12, "declaration": 400.08, "difference": 0.96},
        {"name": "Camille Camille (4)", "fdc": 0, "cash": 232.34, "prelevement": 237.31, "theorique": -4.97, "declaration": 200, "difference": 204.97},
        {"name": "Clarisse (3)", "fdc": 200, "cash": 38.37, "prelevement": 38.28, "theorique": 200.09, "declaration": 200, "difference": -0.09},
        {"name": "Rony Rony (1)", "fdc": 200, "cash": 108.53, "prelevement": 108.58, "theorique": 199.95, "declaration": 200, "difference": 0.05},
        {"name": "Yvenson Yvenson (11)", "fdc": 400, "cash": 55.55, "prelevement": 55.65, "theorique": 399.90, "declaration": 400, "difference": 0.10},
    ],
    "operators_total": {"fdc": 1802.18, "cash": 1243.11, "prelevement": 1249.56, "theorique": 1795.73, "declaration": 2002.18, "difference": 206.45},
}


def test_auth_roles(client):
    assert client.post("/cash/auth", json={"code": "nope"}).status_code == 401
    m = _login(client, "2468")
    o = _login(client, "owner-secret")
    assert client.get("/cash/me", headers=m).json()["role"] == "manager"
    assert client.get("/cash/me", headers=o).json()["role"] == "owner"
    assert client.get("/cash/dashboard", headers=m).status_code == 403
    assert client.get("/cash/stores").status_code == 401
    stores = client.get("/cash/stores", headers=m).json()["stores"]
    assert {s["id"] for s in stores} == {"leon_blum", "ornano"}


def test_wrong_store_is_blocked(client):
    m = _login(client, "2468")
    res = _upload(client, m, "ornano", [RELEVE_LB_1920, BORDEREAU_LB_1595, RECU_LB_1595])
    a = res["analysis"]
    assert a["blocked"] is True
    codes = [i["code"] for i in a["issues"]]
    assert "wrong_store" in codes
    # Le relevé (n° magasin 4920) et le bordereau (titulaire AEJB) pointent tous deux vers Léon Blum
    checks = {d["kind"]: d for d in a["documents"]}
    assert checks["releve_storemate"]["store_check"] == "mismatch" and "Léon Blum" in checks["releve_storemate"]["detected_store"]
    assert checks["bordereau"]["store_check"] == "mismatch"
    r = client.post(f"/cash/submissions/{res['submission_id']}/confirm", json={}, headers=m)
    assert r.status_code == 400 and "Léon Blum" in r.json()["detail"]
    client.post(f"/cash/submissions/{res['submission_id']}/cancel", headers=m)


def test_leon_blum_flow_with_ecart_alert(client):
    m = _login(client, "2468")
    o = _login(client, "owner-secret")
    res = _upload(client, m, "leon_blum", [RELEVE_LB_1920, BORDEREAU_LB_1595, RECU_LB_1595])
    a = res["analysis"]
    assert a["blocked"] is False
    assert a["cash_collected"] == 1606.85
    assert a["deposit_amount"] == 1595.0
    assert a["ecart"] == 11.85
    assert a["ecart_alert"] is True  # seuil 10 €
    assert a["deposits"][0]["bag_number"] == "0005179420" and a["deposits"][0]["has_receipt"] is True
    assert a["coverage"]["days"] == ["2026-10-19", "2026-10-20"]
    sid = res["submission_id"]
    r = client.post(f"/cash/submissions/{sid}/confirm", json={"note": "RAS"}, headers=m)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["submission"]["status"] == "confirmed"
    kinds = [al["kind"] for al in out["alerts"]]
    assert "ecart_depot" in kinds
    alerts = client.get("/cash/alerts", headers=o).json()["items"]
    assert any(al["kind"] == "ecart_depot" and al["severity"] == "critical" and al["submission_id"] == sid for al in alerts)
    # Une seconde confirmation est refusée
    assert client.post(f"/cash/submissions/{sid}/confirm", json={}, headers=m).status_code == 400


def test_manual_correction_and_duplicate_detection(client):
    m = _login(client, "2468")
    res = _upload(client, m, "leon_blum", [RELEVE_LB_1618, BORDEREAU_LB_2100, RECU_LB_2100])
    a = res["analysis"]
    assert a["ecart"] == 1.3 and a["ecart_alert"] is False
    # date du dépôt = date du ticket GAB (21/10), pas celle écrite sur le bordereau (19/10)
    assert a["deposits"][0]["deposit_date"] == "2026-10-21" and a["deposits"][0]["slip_date"] == "2026-10-19"
    # correction manuelle : la responsable rectifie le montant espèces lu
    a["reports"][0]["cash_amount"] = "2 101,30"
    r = client.post(f"/cash/submissions/{res['submission_id']}/confirm", json={"reports": a["reports"], "deposits": a["deposits"]}, headers=m)
    assert r.status_code == 200, r.text
    assert r.json()["submission"]["expected_amount"] == 2101.3
    # Renvoyer le même relevé => doublon signalé
    res2 = _upload(client, m, "leon_blum", [RELEVE_LB_1618, BORDEREAU_LB_2100])
    codes = [i["code"] for i in res2["analysis"]["issues"]]
    assert "duplicate_period" in codes and "duplicate_deposit" in codes
    client.post(f"/cash/submissions/{res2['submission_id']}/cancel", headers=m)


def test_orika_report_and_gap(client):
    m = _login(client, "2468")
    res = _upload(client, m, "ornano", [RELEVE_ORIKA])
    a = res["analysis"]
    assert not a["blocked"]
    assert a["cash_collected"] == 1243.11 and a["deposit_amount"] is None and a["ecart"] is None
    assert any(i["code"] == "no_deposit" for i in a["issues"])
    r = client.post(f"/cash/submissions/{res['submission_id']}/confirm", json={}, headers=m)
    assert r.status_code == 200, r.text
    # Un relevé plus tard avec un trou de 2 jours => info "gap_before"
    later = dict(RELEVE_ORIKA, period_start="24/10/2026", period_end="25/10/2026", cash_amount=900.0)
    res2 = _upload(client, m, "ornano", [later])
    assert res2["analysis"]["coverage"]["gap_days"] == ["2026-10-22", "2026-10-23"]
    client.post(f"/cash/submissions/{res2['submission_id']}/cancel", headers=m)


def test_checks_and_dashboard(client, monkeypatch):
    o = _login(client, "owner-secret")
    # On se place au 30/10/2026 pour que les contrôles de délai s'appliquent
    import backend.cash_service as svc

    class FakeDate(date):
        @classmethod
        def today(cls):
            return date(2026, 10, 30)

    monkeypatch.setattr(svc, "date", FakeDate)
    r = client.post("/cash/checks/run", headers=o)
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["stores"]["leon_blum"]["missing_days"]["missing"] >= 4  # 21..25/10 sans relevé
    assert res["stores"]["ornano"]["undeposited"]["undeposited"] == 1  # relevé Orika sans dépôt
    assert res["stores"]["leon_blum"]["bank"]["checked"] == 0  # Pennylane non configuré
    alerts = client.get("/cash/alerts", headers=o).json()["items"]
    kinds = {a["kind"] for a in alerts}
    assert {"missing_day", "undeposited", "ecart_depot"} <= kinds

    d = client.get("/cash/dashboard?month=2026-10", headers=o).json()
    lb = next(s for s in d["stores"] if s["id"] == "leon_blum")
    assert lb["kpis"]["cash_collected"] == pytest.approx(1606.85 + 2101.30)
    assert lb["kpis"]["deposited"] == pytest.approx(1595 + 2100)
    assert lb["kpis"]["ecart_total"] == pytest.approx(11.85 + 1.3)
    cal = {c["date"]: c["status"] for c in lb["calendar"]}
    assert cal["2026-10-19"] == "alert" and cal["2026-10-17"] == "ok" and cal["2026-10-22"] == "missing" and cal["2026-10-15"] == "before"
    orn = next(s for s in d["stores"] if s["id"] == "ornano")
    assert orn["operators_diff"]["Camille Camille (4)"] == 204.97

    # Acquitter une alerte
    aid = alerts[0]["id"]
    assert client.post(f"/cash/alerts/{aid}/ack", headers=o).json()["status"] == "acked"
    # Réglages : seuil modifiable
    r = client.put("/cash/settings", json={"ecart_threshold_eur": 25}, headers=o)
    assert r.json()["rules"]["ecart_threshold_eur"] == 25
    client.put("/cash/settings", json={"ecart_threshold_eur": 10}, headers=o)


def test_sheet_parser():
    from backend.cash_sheets import parse_coffre_tab, parse_fr_date, parse_amount, pick_current_tab

    assert parse_amount("1 695") == 1695 and parse_amount("-21,96") == -21.96 and parse_amount("325,00") == 325.0 and parse_amount("") is None
    assert parse_fr_date("17/6/26") == date(2026, 6, 17)
    assert parse_fr_date("02/07", 2026) == date(2026, 7, 2)
    assert parse_fr_date("13/08/2026") == date(2026, 8, 13)
    assert parse_fr_date("19-juin", 2026) == date(2026, 6, 19)
    assert pick_current_tab(["Ornano >>", "Du 15/06 au ...", "Du 13/04 au 14/06"]) == "Du 15/06 au ..."
    values = json.loads((ROOT / "tests" / "fixtures" / "sheet_ornano.json").read_text(encoding="utf-8"))
    data = parse_coffre_tab(values, "Du 15/06 au ...", default_year=2026)
    assert data["deposits"][0] == {"amount": 1695.0, "date": "2026-06-17", "date_raw": "17/6/26", "row": 2}
    assert len(data["deposits"]) == 33 and data["totals"]["deposits"] == 70845.0
    assert data["totals"]["coins_added"] == 535.0 and data["totals"]["expenses"] == pytest.approx(115.02)
    assert data["register_diffs"][0]["person"] == "rony" and data["register_diffs"][0]["amount"] == 0.12
    achat = next(x for x in data["register_diffs"] if x["extra_label"] == "achat")
    assert achat["amount"] == -21.96 and achat["extra_amount"] == -17.34 and achat["person"] == "benjamin"
    assert data["count"]["solde_precedent"] == 771.44


def test_bank_matching():
    from backend.cash_bank import match_deposit

    txs = [
        {"id": "1", "date": "2026-10-21", "amount": 2100.0, "label": "DPT VRAC ESP 0005179418  211026\nM BOUSKILA ANTHONY"},
        {"id": "2", "date": "2026-10-21", "amount": 1595.0, "label": "DPT VRAC ESP 0005179420  211026"},
        {"id": "3", "date": "2026-10-22", "amount": 1078.01, "label": "CB NATURALIA PARIS"},
    ]
    assert match_deposit({"bag_number": "0005179420", "amount": 1595, "deposit_date": "2026-10-21"}, txs)["id"] == "2"
    assert match_deposit({"bag_number": None, "amount": 2100, "deposit_date": "2026-10-19"}, txs)["id"] == "1"
    assert match_deposit({"bag_number": "0009999999", "amount": 999, "deposit_date": "2026-10-19"}, txs) is None
