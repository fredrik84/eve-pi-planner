"""Notification channel implementations.

Adding a new channel: subclass BaseNotifier, add to _CHANNEL_MAP.
"""
import json as _json
import os
import urllib.error
import urllib.parse
import urllib.request


class BaseNotifier:
    def send(self, title: str, body: str, url: str | None = None) -> None:
        raise NotImplementedError


class PushoverNotifier(BaseNotifier):
    _API = "https://api.pushover.net/1/messages.json"

    def __init__(self, config: dict):
        self._user_key = config.get("user_key", "")
        # App token: user-supplied takes precedence, then env var.
        self._app_token = config.get("app_token") or os.environ.get("PUSHOVER_APP_TOKEN", "")

    def send(self, title: str, body: str, url: str | None = None) -> None:
        if not self._user_key or not self._app_token:
            raise ValueError(
                "Pushover requires user_key and app_token "
                "(or set PUSHOVER_APP_TOKEN env var for a shared app token)"
            )
        payload = {
            "token": self._app_token,
            "user": self._user_key,
            "title": title,
            "message": body,
        }
        if url:
            payload["url"] = url
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(self._API, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = _json.loads(resp.read())
        if result.get("status") != 1:
            errors = result.get("errors", [result])
            raise RuntimeError(f"Pushover error: {errors}")


class NtfyNotifier(BaseNotifier):
    def __init__(self, config: dict):
        self._topic = config.get("topic", "")
        self._server = config.get("server", "https://ntfy.sh").rstrip("/")

    def send(self, title: str, body: str, url: str | None = None) -> None:
        if not self._topic:
            raise ValueError("ntfy requires a topic")
        endpoint = f"{self._server}/{urllib.parse.quote(self._topic, safe='')}"
        headers: dict[str, str] = {
            "Title": title,
            "Content-Type": "text/plain; charset=utf-8",
        }
        if url:
            headers["Click"] = url
        data = body.encode("utf-8")
        req = urllib.request.Request(endpoint, data=data, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=10):
            pass


class DiscordNotifier(BaseNotifier):
    def __init__(self, config: dict):
        self._webhook_url = config.get("webhook_url", "")

    def send(self, title: str, body: str, url: str | None = None) -> None:
        if not self._webhook_url:
            raise ValueError("Discord requires webhook_url")
        content = f"**{title}**\n{body}"
        if url:
            content += f"\n<{url}>"
        payload = _json.dumps({"content": content}).encode("utf-8")
        req = urllib.request.Request(
            self._webhook_url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "DiscordBot (https://eve-pi.failed.name, 1)",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10):
                pass  # Discord returns 204 No Content on success
        except urllib.error.HTTPError as exc:
            body_text = ""
            try:
                body_text = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise RuntimeError(f"Discord webhook returned HTTP {exc.code}: {body_text}") from exc


_CHANNEL_MAP: dict[str, type] = {
    "pushover": PushoverNotifier,
    "ntfy": NtfyNotifier,
    "discord": DiscordNotifier,
}

CHANNEL_LABELS = {
    "pushover": "Pushover",
    "ntfy": "ntfy.sh",
    "discord": "Discord webhook",
}


def make_notifier(channel: str, config: dict) -> BaseNotifier:
    cls = _CHANNEL_MAP.get(channel)
    if cls is None:
        raise ValueError(f"Unknown notification channel: {channel!r}")
    return cls(config)


import logging as _logging
import threading as _threading

_admin_log = _logging.getLogger(__name__)


def notify_admin_discord(title: str, body: str) -> None:
    """Fire a plain-text admin alert to the server Discord webhook (DISCORD_WEBHOOK env var).
    Runs in a daemon thread so it never blocks the request."""
    url = os.environ.get("DISCORD_WEBHOOK", "")
    if not url:
        return

    def _send():
        try:
            DiscordNotifier({"webhook_url": url}).send(title, body)
        except Exception as exc:
            _admin_log.warning("Admin Discord notify failed: %s", exc)

    _threading.Thread(target=_send, daemon=True).start()
