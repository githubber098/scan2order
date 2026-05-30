"""
sms.py — Send OTP SMS via Twilio.

Required env vars (set in .env):
  TWILIO_ACCOUNT_SID  — from console.twilio.com
  TWILIO_AUTH_TOKEN   — from console.twilio.com
  TWILIO_FROM_NUMBER  — your Twilio phone number in E.164, e.g. +12015551234

If any var is missing, send_otp() falls back to printing the code to the server
log so you can still test locally without a Twilio account.
"""

import os


def _friendly_sms_error(e) -> str:
    """Turn a Twilio exception into a short, user-appropriate message.

    The full error is logged separately; this is what the end user sees.
    """
    msg = (getattr(e, "msg", "") or str(e)).strip()
    code = getattr(e, "code", None)
    if code == 21608 or "unverified" in msg.lower():
        # Twilio trial: can only SMS numbers verified in the console.
        return ("This number isn't approved for SMS yet (our SMS sender is in "
                "trial mode). Please sign in with email instead.")
    if code == 21211 or "not a valid phone" in msg.lower():
        return "That doesn't look like a valid mobile number."
    return ("Couldn't send the SMS code: " + msg[:160]) if msg else "Couldn't send the SMS code."


def send_otp(phone: str, code: str) -> str | None:
    """Send a 6-digit OTP to *phone*.

    Returns None on success, or a short human-readable error string on failure
    (the full provider error is always logged server-side).
    """
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    auth_token  = os.environ.get("TWILIO_AUTH_TOKEN", "")
    from_number = os.environ.get("TWILIO_FROM_NUMBER", "")

    if not all([account_sid, auth_token, from_number]):
        # Dev fallback: log the code so you can test without Twilio
        print(f"[sms] TWILIO not configured — OTP for {phone}: {code}")
        return None   # treat as success so the auth flow still works locally

    try:
        from twilio.rest import Client
        client = Client(account_sid, auth_token)
        client.messages.create(
            body=f"Your scan2order code is {code}. Valid for 10 minutes.",
            from_=from_number,
            to=phone,
        )
        print(f"[sms] OTP sent to {phone[:4]}****{phone[-2:]}")
        return None
    except Exception as e:
        print(f"[sms] send failed to {phone[:4]}*****: {e}")
        return _friendly_sms_error(e)
