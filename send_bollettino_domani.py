# send_bollettino_domani.py

import os
import re
import ssl
import smtplib
import shutil
from email.message import EmailMessage
from datetime import datetime
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---- Config da ENV (settati dal workflow) ------------------------------------
TARGET_URL = os.getenv(
    "TARGET_URL",
    "https://mappe.protezionecivile.gov.it/it/mappe-rischi/bollettino-di-criticita/",
)
SCREENSHOT_NAME = os.getenv("SCREENSHOT_NAME", "bollettino_domani.png")
ROME_HOUR_GATE = os.getenv("ROME_HOUR_GATE", "").strip()   # es. "17" -> invia solo alle 17 Europe/Rome
FORCE_SEND = os.getenv("FORCE_SEND", "false").strip().lower() == "true"

TO_EMAIL = os.getenv("TO_EMAIL", "luca.molini@cimafoundation.org")
EMAIL_SUBJECT = os.getenv("EMAIL_SUBJECT", "Bollettino di criticità — mappa di domani")
EMAIL_BODY = os.getenv("EMAIL_BODY", "In allegato lo screenshot della mappa (fase previsionale di domani).")

# opzionali per invio
DEBUG_SMTP = os.getenv("DEBUG_SMTP", "0").strip() == "1"
CC_EMAILS = os.getenv("CC_EMAILS", "").strip()   # es. "planning@cimafoundation.org"
BCC_EMAILS = os.getenv("BCC_EMAILS", "").strip() # es. "ops@cimafoundation.org"


# ---- Utility -----------------------------------------------------------------
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
    Selettori robusti e attese su Leaflet per ridurre il rischio di screenshot vuoti.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()
        page.set_default_timeout(15000)

        # 1) Vai alla pagina
        print(f"[INFO] Apertura pagina: {TARGET_URL}")
        page.goto(TARGET_URL, wait_until="domcontentloaded")

        # 1a) Chiudi eventuale banner cookie (accetta)
        try:
            page.get_by_role("button", name=re.compile("accett", re.I)).first.click(timeout=4000)
            print("[INFO] Banner cookie chiuso.")
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
                print("[INFO] Scheda 'Domani' selezionata.")
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
                    print("[INFO] Scheda 'Domani' selezionata (fallback).")
            except Exception:
                pass

        if not clicked:
            raise RuntimeError("Impossibile trovare/cliccare la scheda 'Domani'.")

        # 3) Attendi caricamento tiles Leaflet
        try:
            page.wait_for_selector("img.leaflet-tile", state="visible", timeout=10000)
        except PWTimeout:
            pass
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
            print("[INFO] Screenshot a pagina intera (fallback).")
        else:
            target.screenshot(path=out_path)
            print("[INFO] Screenshot del contenitore mappa completato.")

        context.close()
        browser.close()


def send_email_with_attachment(path: str) -> bool:
    """
    Legge la config SMTP dagli ENV (qui dentro per evitare KeyError a monte),
    invia l'email con allegato. Restituisce True/False.
    """
    SMTP_HOST = os.getenv("SMTP_HOST")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
    SMTP_USER = os.getenv("SMTP_USER")
    SMTP_PASS = os.getenv("SMTP_PASS")
    FROM_EMAIL = os.getenv("FROM_EMAIL")

    # Validazione minima
    missing = [k for k, v in {
        "SMTP_HOST": SMTP_HOST,
        "SMTP_USER": SMTP_USER,
        "SMTP_PASS": SMTP_PASS,
        "FROM_EMAIL": FROM_EMAIL,
    }.items() if not v]
    if missing:
        print(f"[ERROR] Config SMTP mancante: {', '.join(missing)}. Email NON inviata.")
        return False

    # Destinatari
    cc_list = [x.strip() for x in CC_EMAILS.split(",") if x.strip()]
    bcc_list = [x.strip() for x in BCC_EMAILS.split(",") if x.strip()]
    all_rcpts = [TO_EMAIL] + cc_list + bcc_list

    # Messaggio
    msg = EmailMessage()
    msg["From"] = FROM_EMAIL
    msg["To"] = TO_EMAIL
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg["Subject"] = EMAIL_SUBJECT
    msg["Date"] = datetime.now(ZoneInfo("Europe/Rome")).strftime("%a, %d %b %Y %H:%M:%S %z")
    msg.set_content(EMAIL_BODY)

    with open(path, "rb") as f:
        data = f.read()
    filename = os.path.basename(path)
    msg.add_attachment(data, maintype="image", subtype="png", filename=filename)

    context = ssl.create_default_context()
    print(f"[INFO] Invio email: from={FROM_EMAIL} to={all_rcpts} via {SMTP_HOST}:{SMTP_PORT}")

    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=60) as s:
                if DEBUG_SMTP:
                    s.set_debuglevel(1)
                s.login(SMTP_USER, SMTP_PASS)
                refused = s.sendmail(FROM_EMAIL, all_rcpts, msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60) as s:
                if DEBUG_SMTP:
                    s.set_debuglevel(1)
                s.starttls(context=context)
                s.login(SMTP_USER, SMTP_PASS)
                refused = s.sendmail(FROM_EMAIL, all_rcpts, msg.as_string())

        if refused:
            print(f"[WARN] Alcuni destinatari rifiutati dal server SMTP: {refused}")
        else:
            print("[OK] Il server SMTP ha accettato il messaggio per tutti i destinatari.")
        return True
    except Exception as e:
        print(f"[ERROR] Invio email fallito: {e}")
        return False


# ---- Main --------------------------------------------------------------------
def main():
    if not guard_by_rome_hour():
        return

    today_rome = datetime.now(ZoneInfo("Europe/Rome")).strftime("%Y-%m-%d")
    base = os.path.splitext(SCREENSHOT_NAME)[0]
    out_path = f"{base}_{today_rome}.png"

    # Screenshot
    capture_screenshot_domani(out_path)
    print(f"[OK] Screenshot salvato: {out_path}")

    # Copia anche un file 'latest' per l'upload artifact stabile
    try:
        shutil.copyfile(out_path, "bollettino_domani_latest.png")
        print("[INFO] Copia latest creata: bollettino_domani_latest.png")
    except Exception as e:
        print(f"[WARN] Impossibile creare copia latest: {e}")

    # Invio email (non blocca il workflow in caso di errore)
    sent = send_email_with_attachment(out_path)
    if sent:
        print(f"[OK] Email inviata a {TO_EMAIL} con allegato {out_path}")
    else:
        print("[WARN] Email non inviata. Vedi log sopra per dettagli.")


if __name__ == "__main__":
    main()
