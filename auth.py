import os
import time
import base64

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


PROD_BASE = "https://trading-api.kalshi.com/trade-api/v2"
DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"


def _get_base_url():
    env = os.getenv("KALSHI_ENV", "demo").lower()
    return PROD_BASE if env == "prod" else DEMO_BASE


def _load_private_key():
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    if not key_path or not os.path.exists(key_path):
        raise FileNotFoundError(f"Private key not found at: {key_path}")
    with open(key_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


_private_key = None


def _get_private_key():
    global _private_key
    if _private_key is None:
        _private_key = _load_private_key()
    return _private_key


def _sign_request(method: str, path: str, timestamp_ms: int) -> str:
    """Sign a request using RSA-PSS per Kalshi v2 spec."""
    key = _get_private_key()
    message = f"{timestamp_ms}{method}{path}".encode()
    signature = key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


def authenticated_request(method: str, path: str, params=None, json_body=None):
    """Make an authenticated request to the Kalshi API.

    Args:
        method: HTTP method (GET, POST, DELETE)
        path: API path starting with /trade-api/v2/...
        params: Query parameters for GET requests
        json_body: JSON body for POST requests

    Returns:
        Response JSON as dict
    """
    base_url = _get_base_url()
    api_key = os.getenv("KALSHI_API_KEY", "")
    timestamp_ms = int(time.time() * 1000)

    # Path for signing must be the full path (without base domain)
    signature = _sign_request(method.upper(), path, timestamp_ms)

    headers = {
        "KALSHI-ACCESS-KEY": api_key,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    url = base_url.rstrip("/").rsplit("/trade-api/v2", 1)[0] + path

    resp = requests.request(
        method=method.upper(),
        url=url,
        headers=headers,
        params=params,
        json=json_body,
        timeout=10,
    )
    resp.raise_for_status()

    if resp.status_code == 204 or not resp.text:
        return {}
    return resp.json()
