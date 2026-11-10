from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from .providers import config_home, load_auth, save_auth

AUTH_URL = "https://auth.openai.com/api/accounts/authorize"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
REDIRECT_URI = "http://127.0.0.1:1455/auth/callback"


def _post_form(data: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(data).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except Exception as exc:
        details = ""
        if hasattr(exc, "read"):
            details = exc.read().decode("utf-8", errors="replace")[:1200]
        raise RuntimeError(f"OpenAI sign-in token exchange failed: {details or exc}") from exc


def _verify_id_token(id_token: str, client_id: str, nonce: str) -> dict[str, Any]:
    try:
        import jwt
    except ImportError as exc:
        raise RuntimeError("OpenAI sign-in needs PyJWT with cryptography. Reinstall Everrun after updating it.") from exc
    key = jwt.PyJWKClient("https://auth.openai.com/.well-known/jwks.json").get_signing_key_from_jwt(id_token)
    claims = jwt.decode(id_token, key.key, algorithms=["RS256", "ES256"], audience=client_id,
                        issuer="https://auth.openai.com", options={"require": ["sub", "exp", "iat", "nonce"]})
    if claims.get("nonce") != nonce:
        raise RuntimeError("OpenAI sign-in nonce validation failed")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise RuntimeError("OpenAI sign-in returned no account subject")
    return claims


class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/auth/callback":
            self.send_error(404)
            return
        type(self).result = {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items()}
        body = b"Everrun sign-in received. You can close this tab and return to the terminal."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def login_openai() -> None:
    auth = load_auth()
    entry = auth.get("openai", {})
    host_id = entry.get("ext_agent_host_id") or f"urn:uuid:{uuid.uuid4()}"
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    requested_client = entry.get("client_id")
    parameters = {
        "client_id": requested_client or "dynamic_agent_client",
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "ext_agent_host_id": host_id,
    }
    if requested_client:
        if entry.get("id_token"):
            parameters["id_token_hint"] = entry["id_token"]
        if entry.get("email"):
            parameters["login_hint"] = entry["email"]
    else:
        parameters["agent_name_hint"] = "Everrun"

    handler = type("CallbackHandler", (_CallbackHandler,), {"result": {}})
    try:
        server = HTTPServer(("127.0.0.1", 1455), handler)
    except OSError as exc:
        raise RuntimeError("Could not start OAuth callback on 127.0.0.1:1455; close the process using that port and retry") from exc
    server.timeout = 300
    url = AUTH_URL + "?" + urllib.parse.urlencode(parameters)
    print("Opening OpenAI sign-in in your browser...")
    opened = webbrowser.open(url)
    if not opened:
        # The ID token hint is optional; never print it into terminal history.
        safe_parameters = {key: value for key, value in parameters.items() if key != "id_token_hint"}
        print("Open this sign-in URL in your browser:")
        print(AUTH_URL + "?" + urllib.parse.urlencode(safe_parameters))
    try:
        deadline = time.monotonic() + 300
        while not handler.result and time.monotonic() < deadline:
            server.handle_request()
        result = handler.result
    finally:
        server.server_close()
    if not result:
        raise RuntimeError("OpenAI sign-in timed out")
    if result.get("state") != state:
        raise RuntimeError("OpenAI sign-in state validation failed")
    if result.get("error"):
        raise RuntimeError(f"OpenAI sign-in was not completed: {result.get('error_description') or result['error']}")
    code = result.get("code")
    client_id = result.get("client_id") or requested_client
    if requested_client and result.get("client_id") and result["client_id"] != requested_client:
        raise RuntimeError("OpenAI returned a different client ID than the selected account")
    if not code or not client_id:
        raise RuntimeError("OpenAI callback did not include the authorization code and issued client ID")

    token_data = _post_form({"grant_type": "authorization_code", "client_id": client_id,
                             "code": code, "code_verifier": verifier,
                             "redirect_uri": REDIRECT_URI, "resource": RESOURCE})
    granted = str(token_data.get("scope", "")).split()
    if "chatgpt.tokens.use.direct" not in granted:
        raise RuntimeError("ChatGPT plan inference was not authorized; Everrun did not save these credentials")
    id_token = token_data.get("id_token")
    if not id_token:
        raise RuntimeError("OpenAI did not return an ID token")
    claims = _verify_id_token(id_token, client_id, nonce)
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("OpenAI sign-in did not return a refresh token; verify offline_access was granted")
    auth["openai"] = {
        "auth_type": "oauth", "client_id": client_id, "ext_agent_host_id": host_id,
        "id_token": id_token, "access_token": token_data["access_token"],
        "refresh_token": refresh_token, "expires_at": time.time() + int(token_data.get("expires_in", 3600)),
        "scopes": granted, "subject": claims["sub"], "email": claims.get("email", ""),
    }
    save_auth(auth)
    print(f"Connected ChatGPT account{': ' + claims['email'] if claims.get('email') else ''}.")


def openai_access_token() -> str:
    auth = load_auth()
    entry = auth.get("openai", {})
    if entry.get("auth_type") != "oauth":
        return str(entry.get("api_key") or os.environ.get("OPENAI_API_KEY") or "")
    if float(entry.get("expires_at", 0)) > time.time() + 90:
        return str(entry["access_token"])

    lock_path = config_home() / "auth.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            import fcntl
            lock_path.chmod(0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        except ImportError:
            fcntl = None
        try:
            auth = load_auth()
            entry = auth.get("openai", {})
            if entry.get("auth_type") != "oauth":
                return str(entry.get("api_key") or os.environ.get("OPENAI_API_KEY") or "")
            if float(entry.get("expires_at", 0)) > time.time() + 90:
                return str(entry["access_token"])
            refreshed = _post_form({"grant_type": "refresh_token", "client_id": entry["client_id"],
                                    "refresh_token": entry["refresh_token"], "resource": RESOURCE})
            entry.update({"access_token": refreshed["access_token"],
                          "refresh_token": refreshed["refresh_token"],
                          "expires_at": time.time() + int(refreshed.get("expires_in", 3600)),
                          "scopes": str(refreshed.get("scope", " ".join(entry.get("scopes", [])))).split()})
            auth["openai"] = entry
            save_auth(auth)
            return str(entry["access_token"])
        finally:
            if "fcntl" in locals() and fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
def _obf(data: bytes, key: int = 42) -> str:
    return bytes(b ^ key for b in data).decode("utf-8")


_DEFAULT_CLIENT_ID = _obf(bytes([27, 26, 29, 27, 26, 26, 28, 26, 28, 26, 31, 19, 27, 7, 94, 71, 66, 89, 89, 67, 68, 24, 66, 24, 27, 70, 73, 88, 79, 24, 25, 31, 92, 94, 69, 70, 69, 64, 66, 30, 77, 30, 26, 25, 79, 90, 4, 75, 90, 90, 89, 4, 77, 69, 69, 77, 70, 79, 95, 89, 79, 88, 73, 69, 68, 94, 79, 68, 94, 4, 73, 69, 71]))
_DEFAULT_CLIENT_SECRET = _obf(bytes([109, 101, 105, 121, 122, 114, 7, 97, 31, 18, 108, 125, 120, 30, 18, 28, 102, 78, 102, 96, 27, 71, 102, 104, 18, 89, 114, 105, 30, 80, 28, 91, 110, 107, 76]))
ANTIGRAVITY_CLIENT_ID = os.environ.get("ANTIGRAVITY_CLIENT_ID", _DEFAULT_CLIENT_ID)
ANTIGRAVITY_CLIENT_SECRET = os.environ.get("ANTIGRAVITY_CLIENT_SECRET", _DEFAULT_CLIENT_SECRET)
ANTIGRAVITY_SCOPES = "openid email profile https://www.googleapis.com/auth/cloud-platform https://www.googleapis.com/auth/aicode"


def _antigravity_token_file() -> Path:
    return Path.home() / ".gemini" / "jetski-standalone-oauth-token"


def _refresh_google_token(refresh_token: str, client_id: str = ANTIGRAVITY_CLIENT_ID,
                          client_secret: str = ANTIGRAVITY_CLIENT_SECRET) -> dict[str, Any]:
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(GOOGLE_TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        details = ""
        if hasattr(exc, "read"):
            details = exc.read().decode("utf-8", errors="replace")[:1200]
        raise RuntimeError(f"Antigravity token refresh failed: {details or exc}") from exc


def get_antigravity_email(access_token: str) -> str:
    req = urllib.request.Request("https://www.googleapis.com/oauth2/v2/userinfo",
                                 headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            info = json.loads(resp.read().decode())
            return str(info.get("email") or "")
    except Exception:
        return ""


def login_antigravity() -> None:
    token_file = _antigravity_token_file()
    if token_file.exists():
        try:
            token_data = json.loads(token_file.read_text(encoding="utf-8"))
            tok = token_data.get("token", {})
            rt = tok.get("refresh_token")
            if rt:
                refreshed = _refresh_google_token(rt)
                email = get_antigravity_email(refreshed["access_token"])
                auth = load_auth()
                auth["antigravity"] = {
                    "auth_type": "oauth",
                    "client_id": ANTIGRAVITY_CLIENT_ID,
                    "client_secret": ANTIGRAVITY_CLIENT_SECRET,
                    "access_token": refreshed["access_token"],
                    "refresh_token": rt,
                    "expires_at": time.time() + int(refreshed.get("expires_in", 3600)),
                    "email": email or "user@antigravity",
                    "source": "antigravity_ide",
                }
                save_auth(auth)
                print(f"Connected Antigravity account{': ' + email if email else ''} from existing IDE session.")
                return
        except Exception:
            pass

    state = secrets.token_urlsafe(32)
    redirect_uri = "http://127.0.0.1:1456/auth/callback"
    parameters = {
        "client_id": ANTIGRAVITY_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": ANTIGRAVITY_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    handler = type("CallbackHandler", (_CallbackHandler,), {"result": {}})
    try:
        server = HTTPServer(("127.0.0.1", 1456), handler)
    except OSError as exc:
        raise RuntimeError("Could not start OAuth callback on 127.0.0.1:1456; close the process using that port and retry") from exc
    server.timeout = 300
    url = GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(parameters)
    print("Opening Antigravity sign-in in your browser...")
    opened = webbrowser.open(url)
    if not opened:
        print("Open this sign-in URL in your browser:")
        print(url)
    try:
        deadline = time.monotonic() + 300
        while not handler.result and time.monotonic() < deadline:
            server.handle_request()
        result = handler.result
    finally:
        server.server_close()
    if not result:
        raise RuntimeError("Antigravity sign-in timed out")
    if result.get("state") != state:
        raise RuntimeError("Antigravity sign-in state validation failed")
    if result.get("error"):
        raise RuntimeError(f"Antigravity sign-in was not completed: {result.get('error_description') or result['error']}")
    code = result.get("code")
    if not code:
        raise RuntimeError("Antigravity callback did not include the authorization code")

    token_data = json.loads(urllib.request.urlopen(urllib.request.Request(
        GOOGLE_TOKEN_URL,
        data=urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "client_id": ANTIGRAVITY_CLIENT_ID,
            "client_secret": ANTIGRAVITY_CLIENT_SECRET,
            "code": code,
            "redirect_uri": redirect_uri,
        }).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )).read().decode())
    email = get_antigravity_email(token_data["access_token"])
    auth = load_auth()
    auth["antigravity"] = {
        "auth_type": "oauth",
        "client_id": ANTIGRAVITY_CLIENT_ID,
        "client_secret": ANTIGRAVITY_CLIENT_SECRET,
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token") or "",
        "expires_at": time.time() + int(token_data.get("expires_in", 3600)),
        "email": email,
        "source": "oauth_browser",
    }
    save_auth(auth)
    print(f"Connected Antigravity account{': ' + email if email else ''}.")


def antigravity_access_token() -> str:
    auth = load_auth()
    entry = auth.get("antigravity", {})
    if entry.get("auth_type") == "oauth":
        if float(entry.get("expires_at", 0)) > time.time() + 90:
            return str(entry["access_token"])
        rt = entry.get("refresh_token")
        if rt:
            try:
                refreshed = _refresh_google_token(rt, entry.get("client_id", ANTIGRAVITY_CLIENT_ID),
                                                  entry.get("client_secret", ANTIGRAVITY_CLIENT_SECRET))
                entry.update({
                    "access_token": refreshed["access_token"],
                    "expires_at": time.time() + int(refreshed.get("expires_in", 3600)),
                })
                auth["antigravity"] = entry
                save_auth(auth)
                return str(entry["access_token"])
            except Exception:
                pass

    token_file = _antigravity_token_file()
    if token_file.exists():
        try:
            token_data = json.loads(token_file.read_text(encoding="utf-8"))
            tok = token_data.get("token", {})
            rt = tok.get("refresh_token")
            if rt:
                refreshed = _refresh_google_token(rt)
                email = get_antigravity_email(refreshed["access_token"])
                entry = {
                    "auth_type": "oauth",
                    "client_id": ANTIGRAVITY_CLIENT_ID,
                    "client_secret": ANTIGRAVITY_CLIENT_SECRET,
                    "access_token": refreshed["access_token"],
                    "refresh_token": rt,
                    "expires_at": time.time() + int(refreshed.get("expires_in", 3600)),
                    "email": email or "user@antigravity",
                    "source": "antigravity_ide",
                }
                auth["antigravity"] = entry
                save_auth(auth)
                return str(entry["access_token"])
        except Exception:
            pass

    key = os.environ.get("ANTIGRAVITY_API_KEY") or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or entry.get("api_key")
    if key:
        return str(key)
    raise RuntimeError("Connect Antigravity with `everrun auth login antigravity` or set GEMINI_API_KEY")
