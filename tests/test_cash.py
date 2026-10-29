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
    os.environ["CASH_WORKER_TOKEN"] = "test-worker-token"
    os.environ["CASH_ADMIN_TOKEN"] = "test-admin-token"
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
    return {"Authorization": "Bearer test-worker-token"}


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


# ----------------------------------------------------------------------------- administration à distance (dépôt public)

def test_token_hashes_in_config_never_tokens():
    import hashlib
    from backend.cash_auth import _matches_hash
    from backend.cash_config import cash_config

    cfg = cash_config()
    assert len(cfg["admin_token_sha256"]) == 64 and len(cfg["worker_token_sha256"]) == 64
    assert _matches_hash("abc", hashlib.sha256(b"abc").hexdigest()) and not _matches_hash("abd", hashlib.sha256(b"abc").hexdigest())
    raw = (ROOT / "config.json").read_text(encoding="utf-8")
    assert "8626" not in raw and "1234" not in raw


def test_admin_diag_and_users_in_db(client, monkeypatch):
    from backend import cash_auth

    a = {"Authorization": "Bearer test-admin-token"}
    assert client.get("/cash/admin/diag").status_code == 401
    assert client.get("/cash/admin/diag", headers=_worker_headers()).status_code == 401
    d = client.get("/cash/admin/diag", headers=a).json()
    assert d["db"]["ok"] is True and d["env_present"]["CASH_USERS"] is True and "password" not in json.dumps(d).lower()

    # Comptes définis en base, codes hachés
    monkeypatch.delenv("CASH_USERS")
    r = client.put("/cash/admin/users", headers=a, json={"users": "1:9999:owner:Anthony,2:5555:manager:Responsable,3:5555:manager:Adjoint"})
    assert r.status_code == 200, r.text
    assert [u["id"] for u in r.json()["accounts"]] == ["1", "2", "3"] and r.json()["overridden_by_env"] is False
    from backend.database import SessionLocal
    from backend.cash_models import CashSetting

    db = SessionLocal()
    try:
        stored = db.get(CashSetting, "users").value
    finally:
        db.close()
    assert "9999" not in stored and "5555" not in stored and "pbkdf2$" in stored
    cash_auth.clear_cache()
    assert client.post("/cash/auth", json={"user_id": "3", "code": "5555"}).json()["role"] == "manager"
    tok = client.post("/cash/auth", json={"user_id": "1", "code": "9999"}).json()["token"]
    assert client.get("/cash/dashboard", headers={"Authorization": "Bearer " + tok}).status_code == 200
    assert client.put("/cash/admin/users", headers=a, json={"users": "2:5555:manager"}).status_code == 400  # pas de gérant
    assert client.put("/cash/admin/users", headers=a, json={"users": "1:9999:chef"}).status_code == 400


def test_login_throttle(client):
    for _ in range(5):
        assert client.post("/cash/auth", json={"user_id": "77", "code": "0000"}).status_code == 401
    assert client.post("/cash/auth", json={"user_id": "77", "code": "0000"}).status_code == 429


def test_photos_kept_in_database(client, monkeypatch):
    """Le disque de Render s'efface : la photo doit rester lisible depuis la base."""
    import pathlib as _p

    monkeypatch.setenv("CASH_ANALYSIS_MODE", "deferred")
    m = _login(client, "2468")
    sid = _deferred_upload(client, m, "leon_blum", ["perdu.jpg"])["submission_id"]
    item = _queue_item(client, sid)
    from backend.database import SessionLocal
    from backend.cash_models import CashDocument

    db = SessionLocal()
    try:
        path = db.get(CashDocument, item["documents"][0]["id"]).path
    finally:
        db.close()
    _p.Path(path).unlink()
    assert client.get(item["documents"][0]["url"], headers=_worker_headers()).content == b"fake-perdu.jpg"
    o = _login(client, "owner-secret")
    client.post(f"/cash/submissions/{sid}/cancel", headers=o)


def test_supabase_direct_url_gets_pooler_candidates(monkeypatch):
    from sqlalchemy.engine import make_url
    from backend.database import _supabase_pooler_candidates

    monkeypatch.setenv("SUPABASE_REGION", "eu-west-1")
    url = make_url("postgresql+psycopg://postgres:secret@db.frnmfmwmgzgbupqaxhgo.supabase.co:5432/postgres")
    cands = _supabase_pooler_candidates(url)
    assert cands[0].host == "aws-0-eu-west-1.pooler.supabase.com" and cands[1].host == "aws-1-eu-west-1.pooler.supabase.com"
    assert cands[0].username == "postgres.frnmfmwmgzgbupqaxhgo" and cands[0].password == "secret" and cands[0].port == 5432
    assert _supabase_pooler_candidates(make_url("postgresql+psycopg://u:p@my-db.example.com/x")) == []


def test_sealed_values(monkeypatch):
    from backend import sealed

    monkeypatch.setenv("OPENAI_API_KEY", "sk-server-only")
    monkeypatch.delenv("CASH_SEAL_KEY", raising=False)
    tok = sealed.seal("postgresql://u:p@h/db")
    assert tok.startswith("v1.") and "p@h" not in tok and sealed.unseal(tok) == "postgresql://u:p@h/db"
    assert sealed.unseal(tok[:-4] + "AAAA") is None  # altéré
    monkeypatch.setenv("OPENAI_API_KEY", "sk-autre-cle")
    assert sealed.unseal(tok) is None  # autre serveur


def test_admin_seal_endpoint(client):
    a = {"Authorization": "Bearer test-admin-token"}
    assert client.post("/cash/admin/seal", json={"value": "x"}).status_code == 401
    r = client.post("/cash/admin/seal", headers=a, json={"value": "secret-value"}).json()
    from backend.sealed import unseal

    assert unseal(r["sealed"]) == "secret-value"
    r = client.post("/cash/admin/seal", headers=a, json={"value": "postgresql://u:p@127.0.0.1:1/x", "probe_database": True}).json()
    assert "sealed" not in r and r["attempts"][0]["error"]


def test_bank_import_from_pennylane(client, monkeypatch):
    from datetime import date as _d, timedelta as _td
    w = _worker_headers()
    o = _login(client, "owner-secret")
    today = _d.today()
    old = (today - _td(days=40)).isoformat()
    recent = (today - _td(days=4)).isoformat()
    # Le magasin utilise le site depuis 10 jours
    from datetime import datetime as _dt
    from sqlalchemy import select as _select
    from backend.database import SessionLocal
    from backend.cash_models import CashSubmission

    db = SessionLocal()
    try:
        first = db.execute(_select(CashSubmission).where(CashSubmission.store_id == "ornano").order_by(CashSubmission.created_at)).scalars().first()
        first.created_at = _dt.utcnow() - _td(days=10)
        db.commit()
    finally:
        db.close()
    txs = [
        {"id": "pl-1", "date": recent, "amount": "1245.0", "label": "DPT VRAC ESP 0005179421  221026\nM BOUSKILA ANTHONY\n- 513845"},
        {"id": "pl-2", "date": recent, "amount": "2535.84", "label": "CB JAB               221026\nCONTRAT 8045610"},  # carte : ignoré
        {"id": "pl-3", "date": old, "amount": "7275.0", "label": "DPT VRAC ESP 0004841125  081026"},
    ]
    r = client.post("/cash/worker/bank-transactions", headers=w, json={"entity": "JAB", "transactions": txs})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["store_id"] == "ornano" and res["added"] == 2 and res["ignored"] == 1
    # réimport : mise à jour, pas de doublon
    assert client.post("/cash/worker/bank-transactions", headers=w, json={"entity": "JAB", "transactions": txs}).json()["updated"] == 2
    assert client.post("/cash/worker/bank-transactions", headers=w, json={"entity": "XYZ", "transactions": []}).status_code == 400
    assert client.post("/cash/worker/bank-transactions", headers=_login(client, "2468"), json={"entity": "JAB", "transactions": []}).status_code == 401

    d = client.get("/cash/dashboard?month=" + recent[:7], headers=o).json()
    orn = next(s for s in d["stores"] if s["id"] == "ornano")
    row = next(b for b in orn["bank_deposits"] if b["id"] == "pl-1")
    assert row["bag_number"] == "0005179421" and row["amount"] == 1245.0
    assert d["integrations"]["pennylane"]["ornano"] is True and d["integrations"]["store_labels"]["ornano"] == "Ornano"
    assert orn["kpis"]["bank_cash_count"] >= 1

    # Un dépôt bancaire récent sans bordereau envoyé, après le premier envoi du magasin : alerte
    alerts = client.get("/cash/alerts", headers=o).json()["items"]
    assert any(a["kind"] == "bank_without_docs" and "1 245,00 €" in a["title"] for a in alerts)
    assert not any(a["kind"] == "bank_without_docs" and "7 275,00 €" in a["title"] for a in alerts)  # avant le site


def test_calendar_neutral_before_first_submission(client):
    from backend.cash_service import dashboard
    from backend.database import SessionLocal

    db = SessionLocal()
    try:
        d = dashboard(db, "leon_blum", "2026-01")  # mois bien antérieur au premier envoi
    finally:
        db.close()
    lb = d["stores"][0]
    assert lb["kpis"]["missing_days"] == 0 and {c["status"] for c in lb["calendar"]} <= {"before", "closed"}


def test_manual_analysis_request(client, monkeypatch):
    """« Analyser maintenant » lance la tâche Claude tout de suite via son déclencheur API."""
    import httpx
    from backend import cash_service as svc
    from backend.database import SessionLocal

    monkeypatch.setenv("CASH_ANALYSIS_MODE", "deferred")
    monkeypatch.delenv("CASH_ROUTINE_FIRE_URL", raising=False)
    monkeypatch.delenv("CASH_ROUTINE_TOKEN", raising=False)
    o = _login(client, "owner-secret")
    a = {"Authorization": "Bearer test-admin-token"}
    db = SessionLocal()
    try:  # vider la file laissée par les autres tests
        for item in svc.queue_items(db):
            db.get(svc.CashSubmission, item["submission_id"]).status = "cancelled"
        db.commit()
    finally:
        db.close()
    assert client.post("/cash/analysis/request", headers=_login(client, "2468")).status_code == 403

    # Sans déclencheur configuré : la demande est notée, l'analyse attend le prochain créneau
    r = client.post("/cash/analysis/request", headers=o).json()
    assert r["fired"] is False and "pas encore activé" in r["message"]

    # Configuration du déclencheur (scellé en base), contrôles de forme
    bad = client.put("/cash/admin/routine", headers=a, json={"url": "https://evil.example.com/fire", "token": "x" * 30})
    assert bad.status_code == 400
    url = "https://api.anthropic.com/v1/claude_code/routines/trig_TEST/fire"
    assert client.put("/cash/admin/routine", headers=a, json={"url": url, "token": "sk-ant-oat01-" + "y" * 30}).json()["configured"] is True
    db = SessionLocal()
    try:
        raw = db.get(svc.CashSetting, "sealed_routine_fire").value
    finally:
        db.close()
    assert "sk-ant-oat01" not in raw and "trig_TEST" not in raw

    calls = []

    class _Resp:
        status_code = 200
        content = b"x"

        def json(self):
            return {"type": "routine_fire", "claude_code_session_url": "https://claude.ai/code/session_X"}

    def fake_post(u, headers=None, json=None, timeout=None):
        calls.append((u, headers, json))
        return _Resp()

    monkeypatch.setattr(httpx, "post", fake_post)
    r = client.post("/cash/analysis/request", headers=o).json()
    assert r["fired"] is True and r["session_url"] == "https://claude.ai/code/session_X" and "lancée" in r["message"]
    u, h, body = calls[0]
    assert u == url and h["Authorization"].startswith("Bearer sk-ant-oat01-") and h["anthropic-beta"] == "experimental-cc-routine-2026-04-01"
    # Double clic : pas de second lancement pendant 3 minutes
    r = client.post("/cash/analysis/request", headers=o).json()
    assert r["fired"] is False and len(calls) == 1
    d = client.get("/cash/dashboard", headers=o).json()["integrations"]
    assert d["routine_fire_ready"] is True and d["routine_fired_at"]
    # La tâche lit should-run : la demande manuelle déclenche le travail s'il y a des photos
    sid = _deferred_upload(client, _login(client, "2468"), "leon_blum", ["z.jpg"])["submission_id"]
    sr = client.get("/cash/worker/should-run", headers=_worker_headers()).json()
    assert sr["run"] is True and sr["reason"] == "demande manuelle"
    client.post(f"/cash/submissions/{sid}/cancel", headers=o)
    client.post("/cash/worker/checks", headers=_worker_headers())
    assert client.get("/cash/worker/should-run", headers=_worker_headers()).json()["requested_at"] is None


def test_sheet_dates_without_year_and_titles():
    from datetime import date as _d
    from backend.cash_sheets import parse_fr_date, _year_from_title, parse_coffre_tab

    assert _year_from_title("Du 05/08") is None and _year_from_title("Du 15/06 au ...") is None
    assert _year_from_title("20250915") == 2025 and _year_from_title("du 18/10/25 au 15/01/26") == 2025
    today = _d.today()
    assert parse_fr_date("12 août").month == 8 and parse_fr_date("5 septembre").month == 9 and parse_fr_date("10-août").day == 10
    assert parse_fr_date("1er octobre").day == 1
    from datetime import timedelta as _td
    # Sans année : l'année en cours, sauf si la date tombe à plus de 45 jours dans le futur
    expected = today.year - 1 if _d(today.year, 12, 15) > today + _td(days=45) else today.year
    assert parse_fr_date("15/12").year == expected
    vals = [["", "", "", "", "", "", "", "", "", "Ecarts", "Personne", "Date"],
            ["", "", "", "", "", "", "", "", "", "-18,19", "cylia", "10-août", "achat "],
            ["", "", "", "", "", "", "", "", "", "0,26", "interim", "10-août"]]
    data = parse_coffre_tab(vals, "Du 05/08")
    first = data["register_diffs"][0]
    assert first["amount"] == -18.19 and first["extra_label"] == "achat" and first["date"].endswith("-08-10")


def test_sheet_values_detail_and_synthesis(client, monkeypatch):
    from datetime import date as _d, timedelta as _td
    monkeypatch.setenv("CASH_ANALYSIS_MODE", "deferred")
    w = _worker_headers()
    o = _login(client, "owner-secret")
    day1 = _d.today() - _td(days=3)
    day2 = _d.today() - _td(days=2)
    f = lambda d: d.strftime("%d/%m")
    values = [
        ["", "", "", "", "", "", "", "", "", "Liste depots", "Date", "", "", "Ajout monnaie", "Date", "", "", "Factures", "Date"],
        ["", "", "", "", "", "", "", "", "", "2140", f(day2), "", "", "", "", "", "", "18,19", f(day1)],
        ["", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "6,13", f(day2)],
        [], ["", "", "", "", "", "", "", "", "Total", "2140", "", "", "Total", "0", "", "", "Total", "24,32"], [],
        ["", "", "", "", "", "", "", "", "", "Ecarts", "Personne", "Date"],
        ["", "", "", "", "", "", "", "", "", "-5,85", "clarisse", f(day1)],
        ["", "", "", "", "", "", "", "", "", "-3,09", "cedric", f(day1)],
        ["", "", "", "", "", "", "", "", "", "0,40", "Clarisse", f(day2)],
    ]
    r = client.post("/cash/worker/sheet-values", headers=w, json={"store_id": "leon_blum", "tab_title": "Du 05/08", "values": values})
    assert r.status_code == 200, r.text and r.json()["register_diffs"] == 3
    assert client.post("/cash/worker/sheet-values", headers=w, json={"store_id": "nope", "tab_title": "x", "values": []}).status_code == 400
    assert client.post("/cash/worker/sheet-values", headers=o, json={"store_id": "leon_blum", "tab_title": "x", "values": []}).status_code == 200  # gérant accepté
    assert {s["store_id"] for s in client.get("/cash/worker/sheets", headers=w).json()["stores"]} == {"leon_blum", "ornano"}

    # Envoi couvrant day1-day2 : le détail montre les écarts du sheet de ces journées
    m = _login(client, "2468")
    sid = _deferred_upload(client, m, "leon_blum", ["r.jpg"])["submission_id"]
    doc = _queue_item(client, sid)["documents"][0]["id"]
    ex = dict(RELEVE_LB_1920, period_start=day1.strftime("%d/%m/%Y"), period_end=day2.strftime("%d/%m/%Y"), cash_amount=2150.87)
    client.post(f"/cash/worker/submissions/{sid}/extractions", headers=w, json={"documents": [{"document_id": doc, "extraction": ex}]})
    s = client.get(f"/cash/submissions/{sid}", headers=o).json()["sheet"]
    assert s["available"] is True and [e["person"] for e in s["register_diffs"]] == ["Cedric", "Clarisse", "Clarisse"]
    assert s["total"] == -8.54 and s["expenses_total"] == 24.32

    syn = client.get(f"/cash/synthesis?month={day1.strftime('%Y-%m')}&store_id=leon_blum", headers=o).json()["stores"][0]
    cl = next(p for p in syn["persons"] if p["person"] == "Clarisse")  # « clarisse » et « Clarisse » regroupés
    assert cl["days"] == (1 if day1 == day2 else 2) and cl["total"] == -5.45 and cl["shortfall"] == -5.85 and cl["surplus"] == 0.4
    assert [(e["date"], e["amount"]) for e in cl["entries"]] == sorted([(day1.isoformat(), -5.85), (day2.isoformat(), 0.4)])
    if day1.month == day2.month:
        assert syn["invoices"]["sheet_count"] == 2 and syn["invoices"]["sheet_total"] == 24.32
        assert syn["invoices"]["count"] == 2 and syn["invoices"]["total"] == 24.32
    assert client.get("/cash/synthesis", headers=m).status_code == 403


def test_person_aliases_merge_typos():
    from backend.cash_service import person_aliases
    names = ["camille"] * 3 + ["camile", "cam", "mohammed", "mohammed", "mohamed", "rony", "rony", "RONY", "tony",
                                "interim", "interim", "interim3", "interiml", "yvenson", "yvenson", "yvenso,"]
    a = person_aliases(names)
    assert a["camile"] == a["cam"] == "camille" and a["mohamed"] == "mohammed" and a["interiml"] == "interim"
    assert a["yvenso"] == "yvenson" and a["tony"] == "tony" and a["interim3"] == "interim3" and a["rony"] == "rony"
