# send_bollettino_domani.py

import os
import re
import ssl
import smtplib
from email.message import EmailMessage
from datetime import datetime
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# --- Config da env (impostati da GitHub Actions) ---
TARGET_URL = os.getenv(
    "TARGET_URL",
    "https://mappe.protezionecivile.gov.it/it/mappe-rischi/bollettino-di-criticita/",
)
SCREENSHOT_NAME = os.getenv("SCREENSHOT_NAME", "bollettino_domani.png")
ROME_HOUR_GATE = os.getenv("ROME_HOUR_GATE", "").strip()  # es. "17" per inviare solo alle 17 Europe/Rome
FORCE_SEND = os.getenv("FORCE_SEND", "false").strip().lower() == "true"

# Email/SMTP
SMTP_HOST = os.environ["SMTP_HOST"]
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASS = os.environ["SMTP_PASS"]
FROM_EMAIL = os.environ["FROM_EMAIL"]
TO_EMAIL = os.getenv("TO_EMAIL", "luca.molini@cimafoundation.org")
EMAIL_SUBJECT = os.getenv("EMAIL_SUBJECT", "Bollettino di criticità — mappa di domani")
EMAIL_BODY = os.getenv("EMAIL_BODY", "In allegato lo screenshot della mappa (fase previsionale di domani).")

# --- Funzioni ---
def guard_by_rome_hour() -> bool:
    """Se FORCE_SEND è attivo, bypassa il gate orario. Altrimenti invia solo all'ora impostata."""
    if FORCE_SEND:
        print("[INFO] FORCE_SEND=true -> bypass controllo orario Europe/Rome.")
        return True
    if not ROME_HOUR_GATE:
        return True
    now_rome = datetime.now(ZoneInfo("Europe/Rome"))
    if str(now_rome.hour) != str(ROME_HOUR_GATE):
        print(f"[INFO] Ora Europe/Rome: {now_rome:%Y-%m-%d %H:%M:%S} — gate={ROME_HOUR_GATE} -> skip invio.")
        return False
    return True

def capture_screenshot_domani(out_path: str):
    """
    Apre la pagina, seleziona 'Domani' e cattura uno screenshot della mappa.
    Selettori robusti: prova prima un bottone/elemento con testo 'domani',
    poi fallback su radio/tab/label; per la mappa usa il container Leaflet.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()
        page.set_default_timeout(15000)

        # 1) Vai alla pagina
        page.goto(TARGET_URL, wait_until="domcontentloaded")

        # 1a) Prova a chiudere eventuale banner cookie
        try:
            page.get_by_role("button", name=re.compile("accett", re.I)).first.click(timeout=4000)
        except Exception:
            pass

        # 2) Clic su "Domani"
        clicked = False
        candidates = [
            lambda: page.get_by_role("button", name=re.compile(r"\bdomani\b", re.I)).first,
            lambda: page.get_by_text(re.compile(r"\bdomani\b", re.I)).first,
            lambda: page.locator("label:has-text('domani')").first,
            lambda: page.locator("button:has-text('domani')").first,
            lambda: page.locator("a:has-text('domani')").first,
            lambda: page.locator("input[type=radio] + label:has-text('domani')").first,
        ]
        for cand in candidates:
            try:
                el = cand()
                el.wait_for(state="visible", timeout=4000)
                el.click()
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            # fallback: se ci sono due tab previsionali, prova il secondo come "Domani"
            try:
                tabs = page.locator("button, a, label").filter(
                    has_text=re.compile(r"fase|prevision|oggi|domani", re.I)
                )
                if tabs.count() >= 2:
                    tabs.nth(1).click()
                    clicked = True
            except Exception:
                pass
        if not clicked:
            raise RuntimeError("Impossibile trovare/cliccare la scheda 'Domani'.")

        # 3) Attendi aggiornamento mappa/tiles
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PWTimeout:
            pass

        # 4) Individua il contenitore mappa e screenshot mirato
        map_locators = [
            "div.leaflet-container",
            "div#map",
            "div[class*='leaflet']",
            "div[data-map]",
        ]
        target = None
        for sel in map_locators:
            loc = page.locator(sel).first
            try:
                loc.wait_for(state="visible", timeout=4000)
                bbox = loc.bounding_box()
                if bbox and bbox["width"] > 400 and bbox["height"] > 300:
                    target = loc
                    break
            except Exception:
                continue

        if target is None:
            page.screenshot(path=out_path, full_page=True)
        else:
            target.screenshot(path=out_path)

        context.close()
        browser.close()

def send_email_with_attachment(path: str):
    msg = EmailMessage()
    msg["From"] = FROM_EMAIL
    msg["To"] = TO_EMAIL
    msg["Subject"] = EMAIL_SUBJECT
    msg.set_content(EMAIL_BODY)

    with open(path, "rb") as f:
        data = f.read()
    filename = os.path.basename(path)
    msg.add_attachment(data, maintype="image", subtype="png", filename=filename)

    context = ssl.create_default_context()
    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls(context=context)
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)

def main():
    if not guard_by_rome_hour():
        return

    today_rome = datetime.now(ZoneInfo("Europe/Rome")).strftime("%Y-%m-%d")
    base = os.path.splitext(SCREENSHOT_NAME)[0]
    out_path = f"{base}_{today_rome}.png"

    capture_screenshot_domani(out_path)
    print(f"[OK] Screenshot salvato: {out_path}")

    send_email_with_attachment(out_path)
    print(f"[OK] Email inviata a {TO_EMAIL} con allegato {out_path}")

if __name__ == "__main__":
    main()
