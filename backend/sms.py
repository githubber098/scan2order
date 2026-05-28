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


def send_otp(phone: str, code: str) -> bool:
    """Send a 6-digit OTP to *phone*. Returns True on success."""
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    auth_token  = os.environ.get("TWILIO_AUTH_TOKEN", "")
    from_number = os.environ.get("TWILIO_FROM_NUMBER", "")

    if not all([account_sid, auth_token, from_number]):
        # Dev fallback: log the code so you can test without Twilio
        print(f"[sms] TWILIO not configured — OTP for {phone}: {code}")
        return True   # return True so the auth flow still works locally

    try:
        from twilio.rest import Client
        client = Client(account_sid, auth_token)
        client.messages.create(
            body=f"Your scan2order code is {code}. Valid for 10 minutes.",
            from_=from_number,
            to=phone,
        )
        print(f"[sms] OTP sent to {phone[:4]}****{phone[-2:]}")
        return True
    except Exception as e:
        print(f"[sms] send failed to {phone[:4]}*****: {e}")
        return False
