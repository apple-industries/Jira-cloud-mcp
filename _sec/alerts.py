"""Lightweight email alerting for the snapshot job. Provider-agnostic SMTP via stdlib.
Reads config from .env (falls back to os.environ). If SMTP isn't configured, it logs and
skips (never crashes the caller). Keys:
  SNAPSHOT_SMTP_HOST      e.g. mail.yourprovider.com   (required to enable email)
  SNAPSHOT_SMTP_PORT      default 587 (STARTTLS); use 465 for implicit SSL
  SNAPSHOT_SMTP_USER      mailbox login (optional if relay allows unauth)
  SNAPSHOT_SMTP_PASSWORD  mailbox / app password
  SNAPSHOT_ALERT_TO       default jesseweisberg@appleindustries.com
  SNAPSHOT_ALERT_FROM     default = SMTP_USER (or TO)
"""
import os
import smtplib
import ssl
import socket
from datetime import datetime, timezone
from email.message import EmailMessage

REPO_ROOT = "/Users/jesseweisberg/dev/jira-cloud-mcp"
DEFAULT_TO = "jesseweisberg@appleindustries.com"


def _cfg():
    """Merge .env (lower precedence) with real environment (higher)."""
    vals = {}
    try:
        with open(os.path.join(REPO_ROOT, ".env")) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    vals.update({k: v for k, v in os.environ.items() if k.startswith("SNAPSHOT_")})
    return vals


def send_email(subject, body):
    c = _cfg()
    host = c.get("SNAPSHOT_SMTP_HOST", "").strip()
    if not host:
        print("[alert] SNAPSHOT_SMTP_HOST not set in .env — email alert skipped (logged only)")
        return False
    port = int(c.get("SNAPSHOT_SMTP_PORT", "587"))
    user = c.get("SNAPSHOT_SMTP_USER", "").strip() or None
    pw = c.get("SNAPSHOT_SMTP_PASSWORD", "").strip() or None
    to = c.get("SNAPSHOT_ALERT_TO", DEFAULT_TO).strip()
    frm = c.get("SNAPSHOT_ALERT_FROM", "").strip() or user or to

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = frm
    msg["To"] = to
    msg.set_content(body)

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            if user:
                s.login(user, pw)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.ehlo()
            s.starttls(context=ctx)
            if user:
                s.login(user, pw)
            s.send_message(msg)
    print(f"[alert] email sent to {to} via {host}:{port}")
    return True


def alert_failure(detail):
    """Email a failure notice. Never raises — a broken alert must not mask the real error."""
    when = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    subject = "[Jira] Quarterly theme/horizon snapshot FAILED"
    body = (
        "The quarterly initiative theme/horizon snapshot job failed to complete.\n\n"
        f"When:   {when}\n"
        f"Host:   {socket.gethostname()}\n"
        f"Script: {REPO_ROOT}/_sec/snapshot.py\n"
        f"Log:    {REPO_ROOT}/_sec/snapshot.log\n\n"
        "Error:\n"
        f"{detail}\n\n"
        "Re-run manually:\n"
        "  launchctl kickstart -k gui/$(id -u)/com.appleindustries.theme-horizon-snapshot\n"
    )
    try:
        if not send_email(subject, body):
            print("[alert] (email not configured) " + subject)
    except Exception as e:
        print(f"[alert] FAILED to send alert email: {type(e).__name__}: {e}")
