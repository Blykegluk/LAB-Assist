"""Envoi des alertes par e-mail (SMTP).

Variables d'env :
  SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD, SMTP_FROM
  CASH_ALERT_EMAIL_TO   destinataire des alertes (le gérant), plusieurs adresses séparées par des virgules
  CASH_PUBLIC_URL       URL publique du site, pour les liens dans les e-mails (ex: https://lab-assist.example.com)
Sans configuration SMTP, les alertes sont seulement journalisées.
"""
from __future__ import annotations

import html
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Iterable

log = logging.getLogger("cash.notify")

SEVERITY_LABEL = {"critical": "🔴 Critique", "warning": "🟠 À vérifier", "info": "🔵 Info"}


def configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv("CASH_ALERT_EMAIL_TO"))


def _recipients() -> list[str]:
    return [a.strip() for a in (os.getenv("CASH_ALERT_EMAIL_TO") or "").split(",") if a.strip()]


def send_email(subject: str, html_body: str, text_body: str = "") -> bool:
    if not configured():
        log.info("[email non configuré] %s\n%s", subject, text_body or html_body)
        return False
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT") or 587)
    user = os.getenv("SMTP_USER")
    pwd = os.getenv("SMTP_PASSWORD")
    sender = os.getenv("SMTP_FROM") or user or "lab-assist@localhost"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(_recipients())
    msg.attach(MIMEText(text_body or html_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.ehlo()
            if port != 25:
                try:
                    s.starttls()
                except smtplib.SMTPException:
                    pass
            if user and pwd:
                s.login(user, pwd)
            s.sendmail(sender, _recipients(), msg.as_string())
        return True
    except Exception as e:
        log.error("Envoi e-mail échoué: %s", e)
        return False


def render_alerts_email(alerts: Iterable[dict], heading: str) -> tuple[str, str]:
    base = (os.getenv("CASH_PUBLIC_URL") or "").rstrip("/")
    rows_html = []
    rows_txt = []
    for a in alerts:
        sev = SEVERITY_LABEL.get(a.get("severity"), a.get("severity"))
        rows_html.append(
            f"<tr><td style='padding:6px 10px;white-space:nowrap'>{html.escape(sev)}</td>"
            f"<td style='padding:6px 10px'><b>{html.escape(a.get('store_label') or a.get('store_id') or '')}</b></td>"
            f"<td style='padding:6px 10px'><b>{html.escape(a.get('title') or '')}</b><br>"
            f"<span style='color:#475569'>{html.escape(a.get('message') or '')}</span></td></tr>"
        )
        rows_txt.append(f"- [{sev}] {a.get('store_label') or a.get('store_id')} : {a.get('title')} — {a.get('message')}")
    link = f"<p><a href='{base}/caisse/'>Ouvrir le tableau de bord</a></p>" if base else ""
    html_body = (
        f"<div style='font-family:Arial,sans-serif;color:#0f172a'><h2 style='margin:0 0 12px'>{html.escape(heading)}</h2>"
        f"<table style='border-collapse:collapse;border:1px solid #e2e8f0'>{''.join(rows_html)}</table>{link}</div>"
    )
    text_body = heading + "\n\n" + "\n".join(rows_txt) + (f"\n\n{base}/caisse/" if base else "")
    return html_body, text_body
