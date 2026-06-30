from __future__ import annotations

import json
import logging

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)

_firebase_app = None


class PushNotificationsNotConfigured(Exception):
    pass


class InvalidPushTokenError(Exception):
    pass


def _import_firebase_admin():
    try:
        import firebase_admin
        from firebase_admin import credentials, messaging
    except ImportError as exc:
        raise PushNotificationsNotConfigured(
            "firebase-admin is not installed. Install requirements.txt in production."
        ) from exc
    return firebase_admin, credentials, messaging


def _get_firebase_app():
    global _firebase_app
    if _firebase_app is not None:
        return _firebase_app

    credentials_path = getattr(settings, "FIREBASE_CREDENTIALS_PATH", "")
    credentials_json = getattr(settings, "FIREBASE_CREDENTIALS_JSON", "")
    if not credentials_path and not credentials_json:
        raise PushNotificationsNotConfigured(
            "FIREBASE_CREDENTIALS_PATH or FIREBASE_CREDENTIALS_JSON is required."
        )

    firebase_admin, credentials, _ = _import_firebase_admin()
    try:
        _firebase_app = firebase_admin.get_app()
        return _firebase_app
    except ValueError:
        pass

    if credentials_json:
        try:
            certificate_data = json.loads(credentials_json)
        except json.JSONDecodeError as exc:
            raise ImproperlyConfigured("FIREBASE_CREDENTIALS_JSON must be valid JSON.") from exc
        certificate = credentials.Certificate(certificate_data)
    else:
        certificate = credentials.Certificate(credentials_path)

    _firebase_app = firebase_admin.initialize_app(certificate)
    return _firebase_app


def _stringify_data(data: dict | None) -> dict:
    return {str(key): str(value) for key, value in (data or {}).items()}


def send_firebase_push(token: str, payload: dict) -> str:
    _, _, messaging = _import_firebase_admin()
    app = _get_firebase_app()
    notification = messaging.Notification(
        title=payload.get("title", ""),
        body=payload.get("body", ""),
    )
    message = messaging.Message(
        token=token,
        notification=notification,
        data=_stringify_data(payload.get("data")),
    )
    try:
        return messaging.send(message, app=app)
    except Exception as exc:
        if exc.__class__.__name__ in {"UnregisteredError", "SenderIdMismatchError"}:
            raise InvalidPushTokenError(str(exc)) from exc
        logger.warning("Firebase push failed for token=%s: %s", token[:12], exc)
        raise
