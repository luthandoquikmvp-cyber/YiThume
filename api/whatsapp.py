# whatsapp.py — Meta WhatsApp Cloud API sender (env-gated, zero-config safe).
#
# Set WHATSAPP_TOKEN + WHATSAPP_PHONE_NUMBER_ID to send real messages
# (works with a free Meta developer test number — no business verification).
# When unset, messages are logged to the console and the app keeps working
# on the wa.me link fallback.

import os
import json
import urllib.request
import urllib.error

WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "").strip()
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()
GRAPH_API_VERSION = os.environ.get("WHATSAPP_API_VERSION", "v20.0")


def enabled():
    return bool(WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID)


def send_text(to, text):
    """Send a plain-text WhatsApp message. Returns True if sent via the
    Cloud API, False otherwise (caller should fall back gracefully)."""
    to = str(to or "").strip()
    if not to:
        return False
    if not enabled():
        print(f"[whatsapp:console] to={to}\n{text}\n", flush=True)
        return False
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {WHATSAPP_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            body = res.read().decode("utf-8", "replace")
            ok = res.status in (200, 201)
            if not ok:
                print(f"[whatsapp:error] status={res.status} body={body}", flush=True)
            return ok
    except urllib.error.HTTPError as e:
        print(f"[whatsapp:error] {e.code} {e.read().decode('utf-8', 'replace')}", flush=True)
        return False
    except Exception as e:
        print(f"[whatsapp:error] {e}", flush=True)
        return False
