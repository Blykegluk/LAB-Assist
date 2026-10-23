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
    os.environ["CASH_ANALYSIS_MODE"] = "immediate"  # les tests du mode différé le changent localement
    os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")
    os.environ["CASH_USERS"] = "1:8626:owner:Anthony,2:2468:manager:Responsable"
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
    uid = "1" if code == "owner-secret" else "2"
    r = client.post("/cash/auth", json={"user_id": uid, "code": "8626" if code == "owner-secret" else code})
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
    assert client.post("/cash/auth", json={"user_id": "2", "code": "nope"}).status_code == 401
    assert client.post("/cash/auth", json={"user_id": "9", "code": "2468"}).status_code == 401
    m = _login(client, "2468")
    o = _login(client, "owner-secret")
    me = client.get("/cash/me", headers=m).json()
    assert me["role"] == "manager" and me["user_id"] == "2" and me["label"] == "Responsable"
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


def test_extractor_helpers():
    from backend.cash_extract import _parse_json, CashDocExtractor
    import os
    assert _parse_json('```json\n{"type": "recu_gab", "deposit_amount": 1595}\n```')["deposit_amount"] == 1595
    assert _parse_json('Voici : {"type": "bordereau"} fin')["type"] == "bordereau"
    assert _parse_json("rien")["type"] == "autre"
    os.environ.pop("CASH_VISION_PROVIDER", None); os.environ.pop("CASH_VISION_MODEL", None)
    ex = CashDocExtractor()
    assert ex.provider == "anthropic" and ex.model == "claude-opus-5-5"
    blocks = ex._anthropic_blocks(b"%PDF-1.4 fake", "application/pdf")
    assert blocks[0]["type"] == "document" and blocks[0]["source"]["media_type"] == "application/pdf"
    import fitz
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 3000, 2000), False)
    big = pix.tobytes("png")
    blocks = ex._anthropic_blocks(big, "image/png")
    assert blocks[0]["type"] == "image" and blocks[0]["source"]["media_type"] == "image/jpeg"
    assert fitz.Pixmap(__import__("base64").b64decode(blocks[0]["source"]["data"])).width == 2000


# ----------------------------------------------------------------------------- analyse différée (tâche Claude planifiée)

def _worker_headers():
    from backend.cash_auth import worker_token

    return {"Authorization": "Bearer " + worker_token()}


def _deferred_upload(client, headers, store_id, names, note=None):
    files = [("files", (n, f"fake-{n}".encode(), "image/jpeg")) for n in names]
    data = {"store_id": store_id}
    if note:
        data["note"] = note
    r = client.post("/cash/submissions", data=data, files=files, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _queue_item(client, sid):
    items = client.get("/cash/worker/queue", headers=_worker_headers()).json()["items"]
    return next((i for i in items if i["submission_id"] == sid), None)


def test_deferred_analysis_by_worker(client, monkeypatch):
    monkeypatch.setenv("CASH_ANALYSIS_MODE", "deferred")
    m = _login(client, "2468")
    o = _login(client, "owner-secret")
    st = client.get("/cash/stores", headers=m).json()
    assert st["analysis_mode"] == "deferred" and "vers" in st["next_analysis"]

    body = _deferred_upload(client, m, "leon_blum", ["releve.jpg", "bordereau.jpg", "ticket.jpg"], note="dépôt du lundi")
    assert body["status"] == "queued" and body["documents"] == 3
    sid = body["submission_id"]
    recent = client.get("/cash/manager/recent?store_id=leon_blum", headers=m).json()["items"]
    assert recent[0]["id"] == sid and recent[0]["status"] == "queued" and recent[0]["documents"] == 3

    # Accès : jeton de la tâche ou gérant, jamais la responsable
    assert client.get("/cash/worker/queue").status_code == 401
    assert client.get("/cash/worker/queue", headers=m).status_code == 401
    assert client.get("/cash/worker/queue", headers=o).status_code == 200
    w = _worker_headers()
    item = _queue_item(client, sid)
    assert item["store_label"] == "Léon Blum" and item["pos_store_numbers"] == ["4920"] and item["note"] == "dépôt du lundi"
    assert "SAC SÉCURISÉ BILLETS" in client.get("/cash/worker/prompt", headers=w).text
    assert client.get(item["documents"][0]["url"], headers=w).content == b"fake-releve.jpg"
    assert client.get(item["documents"][0]["url"], headers=m).status_code == 401

    ids = [d["id"] for d in item["documents"]]
    extractions = [
        dict(RELEVE_LB_1920, period_start="02/11/2026", period_end="03/11/2026", cash_amount=1500.0),
        dict(BORDEREAU_LB_1595, deposit_date="04/11/2026", deposit_amount=1470, bag_number="0005179999"),
        dict(RECU_LB_1595, deposit_date="04/11/2026", deposit_amount=1470.0, bag_number="0005179999"),
    ]
    payload = {"documents": [{"document_id": i, "extraction": e} for i, e in zip(ids, extractions)]}
    r = client.post(f"/cash/worker/submissions/{sid}/extractions", headers=w, json=payload)
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["ecart"] == 30.0 and res["deposit_amount"] == 1470.0 and any(t.startswith("Écart de dépôt") for t in res["alerts"])
    assert client.post(f"/cash/worker/submissions/{sid}/extractions", headers=w, json=payload).status_code == 400
    assert _queue_item(client, sid) is None
    sub = client.get(f"/cash/submissions/{sid}", headers=o).json()
    assert sub["status"] == "confirmed" and sub["note"] == "dépôt du lundi" and sub["submitted_by"].startswith("Responsable")
    assert {d["kind"] for d in sub["documents"]} == {"releve_storemate", "bordereau", "recu_gab"}
    # Un document d'un autre envoi est refusé
    other = _deferred_upload(client, m, "leon_blum", ["x.jpg"])
    bad = {"documents": [{"document_id": ids[0], "extraction": extractions[0]}]}
    assert client.post(f"/cash/worker/submissions/{other['submission_id']}/extractions", headers=w, json=bad).status_code == 400
    client.post(f"/cash/submissions/{other['submission_id']}/cancel", headers=o)


def test_deferred_wrong_store_and_incomplete(client, monkeypatch):
    monkeypatch.setenv("CASH_ANALYSIS_MODE", "deferred")
    m = _login(client, "2468")
    w = _worker_headers()
    sid = _deferred_upload(client, m, "ornano", ["releve.jpg", "bordereau.jpg"])["submission_id"]
    ids = [d["id"] for d in _queue_item(client, sid)["documents"]]
    extractions = [
        dict(RELEVE_LB_1920, period_start="09/11/2026", period_end="10/11/2026"),  # relevé Léon Blum (n° 4920)
        {"type": "bordereau", "confidence": 0.3, "bag_number": "0005170000", "account_holder": "JAB"},  # montant illisible
    ]
    r = client.post(f"/cash/worker/submissions/{sid}/extractions", headers=w,
                    json={"documents": [{"document_id": i, "extraction": e} for i, e in zip(ids, extractions)]})
    assert r.status_code == 200, r.text
    res = r.json()
    assert "Document d'un autre magasin" in res["alerts"] and "Analyse incomplète d'un envoi" in res["alerts"]
    assert res["incomplete"] == ["un dépôt sans date ou sans montant lisible"]
    o = _login(client, "owner-secret")
    assert client.get(f"/cash/submissions/{sid}", headers=o).json()["status"] == "confirmed"


def test_stale_queue_alert_and_dashboard(client, monkeypatch):
    monkeypatch.setenv("CASH_ANALYSIS_MODE", "deferred")
    from datetime import datetime, timedelta
    from backend.database import SessionLocal
    from backend.cash_models import CashSubmission

    m = _login(client, "2468")
    o = _login(client, "owner-secret")
    sid = _deferred_upload(client, m, "ornano", ["a.jpg"])["submission_id"]
    db = SessionLocal()
    try:
        db.get(CashSubmission, sid).created_at = datetime.utcnow() - timedelta(hours=20)
        db.commit()
    finally:
        db.close()
    res = client.post("/cash/worker/checks", headers=_worker_headers())
    assert res.status_code == 200, res.text
    alerts = client.get("/cash/alerts", headers=o).json()["items"]
    assert any(a["kind"] == "stale_queue" and a["submission_id"] == sid for a in alerts)
    d = client.get("/cash/dashboard?month=" + datetime.utcnow().strftime("%Y-%m"), headers=o).json()
    orn = next(s for s in d["stores"] if s["id"] == "ornano")
    assert orn["kpis"]["queued"] >= 1 and any(q["id"] == sid for q in orn["queued"])
    assert d["integrations"]["analysis_mode"] == "deferred" and d["integrations"]["last_worker_run_at"]


def test_next_analysis_label(monkeypatch):
    from datetime import datetime
    from backend.cash_service import next_analysis_label

    monkeypatch.delenv("CASH_ANALYSIS_TIMES", raising=False)
    assert next_analysis_label(datetime(2026, 10, 23, 9, 0)) == "aujourd'hui vers 12h05"
    assert next_analysis_label(datetime(2026, 10, 23, 15, 0)) == "aujourd'hui vers 18h05"
    assert next_analysis_label(datetime(2026, 10, 23, 19, 0)) == "demain vers 12h05"
    monkeypatch.setenv("CASH_ANALYSIS_TIMES", "12:30,18:30")
    assert next_analysis_label(datetime(2026, 10, 23, 12, 45)) == "aujourd'hui vers 18h30"
