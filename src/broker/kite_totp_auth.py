"""
Headless daily Zerodha Kite authentication using TOTP.

Usage: venv/bin/python src/broker/kite_totp_auth.py
Required .env variables:
  KITE_API_KEY
  KITE_API_SECRET
  KITE_USER_ID       (your Zerodha login ID, e.g. AB1234)
  KITE_USER_PASSWORD (your Zerodha password)
  KITE_TOTP_SECRET   (base32 TOTP secret from Zerodha 2FA setup)
"""

import os
import json
import logging
import requests
import pyotp
from datetime import datetime
from dotenv import load_dotenv

logger = logging.getLogger(__name__)
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)

# Zerodha Kite web endpoints for headless login
KITE_BASE_URL     = "https://kite.zerodha.com"
LOGIN_URL         = f"{KITE_BASE_URL}/api/login"
TWOFA_URL         = f"{KITE_BASE_URL}/api/twofa"
SESSION_FILE      = "kite_session.json"


def _datetime_handler(x):
    if hasattr(x, 'isoformat'):
        return x.isoformat()
    raise TypeError(f"Unknown type: {type(x)}")


def headless_login(
    api_key: str,
    api_secret: str,
    user_id: str,
    password: str,
    totp_secret: str,
) -> bool:
    """
    Performs a fully headless Zerodha login using credentials + TOTP.
    Saves the session to kite_session.json on success.
    Returns True on success, False on failure.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": KITE_BASE_URL,
    })

    # ── Step 1: Password login ────────────────────────────────────────────────
    logger.info(f"Step 1: Logging in as {user_id}...")
    try:
        resp = session.post(LOGIN_URL, data={
            "user_id": user_id,
            "password": password,
        })
        resp.raise_for_status()
        login_data = resp.json()

        if login_data.get("status") != "success":
            logger.error(f"Password login failed: {login_data.get('message', 'Unknown error')}")
            return False

        request_id = login_data["data"]["request_id"]
        logger.info(f"Password login successful. request_id: {request_id}")
    except Exception as e:
        logger.error(f"Login request failed: {e}")
        return False

    # ── Step 2: TOTP 2FA ─────────────────────────────────────────────────────
    logger.info("Step 2: Generating TOTP code...")
    try:
        totp = pyotp.TOTP(totp_secret)
        totp_code = totp.now()
        logger.info(f"Generated TOTP: {totp_code}")

        resp2 = session.post(TWOFA_URL, data={
            "user_id": user_id,
            "request_id": request_id,
            "twofa_value": totp_code,
            "twofa_type": "totp",
        })
        resp2.raise_for_status()
        twofa_data = resp2.json()

        if twofa_data.get("status") != "success":
            logger.error(f"TOTP verification failed: {twofa_data.get('message', 'Unknown error')}")
            return False

        logger.info("TOTP verification successful.")
    except Exception as e:
        logger.error(f"TOTP step failed: {e}")
        return False

    # ── Step 3: Extract request_token from redirect ───────────────────────────
    logger.info("Step 3: Extracting request_token...")
    try:
        # After 2FA, look for the request_token in the redirect URL
        login_resp = session.get(
            f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3",
            allow_redirects=True
        )
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(login_resp.url)
        params = parse_qs(parsed.query)
        request_token = params.get("request_token", [None])[0]

        if not request_token:
            logger.error(f"Could not extract request_token. Final URL: {login_resp.url}")
            return False

        logger.info(f"request_token: {request_token}")
    except Exception as e:
        logger.error(f"Failed to extract request_token: {e}")
        return False

    # ── Step 4: Generate access token via KiteConnect ────────────────────────
    logger.info("Step 4: Generating access token...")
    try:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=api_key)
        session_data = kite.generate_session(request_token, api_secret=api_secret)

        with open(SESSION_FILE, "w") as f:
            json.dump(session_data, f, default=_datetime_handler)

        logger.info(f"✅ Access token saved to {SESSION_FILE}. Login time: {session_data.get('login_time')}")
        return True
    except Exception as e:
        logger.error(f"Failed to generate/save access token: {e}")
        return False


def run_totp_auth():
    """Main entry point. Reads credentials from .env and authenticates."""
    load_dotenv()
    api_key      = os.getenv("KITE_API_KEY")
    api_secret   = os.getenv("KITE_API_SECRET")
    user_id      = os.getenv("KITE_USER_ID")
    password     = os.getenv("KITE_USER_PASSWORD")
    totp_secret  = os.getenv("KITE_TOTP_SECRET")

    missing = [k for k, v in {
        "KITE_API_KEY": api_key,
        "KITE_API_SECRET": api_secret,
        "KITE_USER_ID": user_id,
        "KITE_USER_PASSWORD": password,
        "KITE_TOTP_SECRET": totp_secret,
    }.items() if not v or "your_" in str(v)]

    if missing:
        logger.error(f"Missing .env variables: {missing}")
        logger.info("Add these to your .env file to enable headless TOTP authentication.")
        return False

    return headless_login(api_key, api_secret, user_id, password, totp_secret)


if __name__ == "__main__":
    success = run_totp_auth()
    if not success:
        print("\n⚠️  TOTP auth failed. Falling back to manual browser auth: run src/broker/kite_auth.py")
    else:
        print("\n✅ Headless TOTP authentication successful!")
