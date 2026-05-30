"""
email_sender.py — Send OTP codes by email via SMTP.

Required env vars (set in .env) for real delivery:
  SMTP_HOST   e.g. smtp.gmail.com
  SMTP_PORT   e.g. 587            (default: 587, STARTTLS)
  SMTP_USER   SMTP username / login email
  SMTP_PASS   SMTP password or app-password
  SMTP_FROM   From address        (default: SMTP_USER)

Works with any SMTP provider (Gmail app password, Fastmail, SES SMTP, etc.).
If SMTP_HOST/USER/PASS are not all set, send_otp() falls back to printing the
code to the server log so the flow is testable locally without a mail server.
"""

import os
import smtplib
import ssl
from email.message import EmailMessage


def send_otp(email: str, code: str) -> bool:
    """Email a 6-digit OTP to *email*. Returns True on success."""
    host = os.environ.get("SMTP_HOST", "")
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASS", "")
    port = int(os.environ.get("SMTP_PORT", "587") or "587")
    from_addr = os.environ.get("SMTP_FROM", "") or user or "no-reply@scan2order"

    if not all([host, user, password]):
        # Dev fallback: log the code so the flow works without an SMTP server.
        print(f"[email] SMTP not configured — OTP for {email}: {code}")
        return True

    msg = EmailMessage()
    msg["Subject"] = "Your scan2order code"
    msg["From"] = from_addr
    msg["To"] = email
    msg.set_content(
        f"Your scan2order verification code is {code}.\n\n"
        f"It is valid for 10 minutes. If you didn't request this, ignore this email."
    )

    try:
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=15) as server:
                server.login(user, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.starttls(context=context)
                server.login(user, password)
                server.send_message(msg)
        # Don't log the full address — just enough to correlate.
        local = email.split("@")[0]
        print(f"[email] OTP sent to {local[:2]}***@{email.split('@')[-1]}")
        return True
    except Exception as e:
        print(f"[email] send failed to {email.split('@')[-1]}: {e}")
        return False
