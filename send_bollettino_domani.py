# ... (import e costanti invariati)
FORCE_SEND = os.getenv("FORCE_SEND", "false").strip().lower() == "true"

def guard_by_rome_hour():
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
