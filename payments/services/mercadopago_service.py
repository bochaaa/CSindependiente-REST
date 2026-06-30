from __future__ import annotations

import json
import logging
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone
from rest_framework import serializers

logger = logging.getLogger(__name__)

MERCADOPAGO_PREFERENCES_URL = "https://api.mercadopago.com/checkout/preferences"
MERCADOPAGO_PAYMENTS_URL = "https://api.mercadopago.com/v1/payments/{payment_id}"


def _get_access_token() -> str:
    access_token = getattr(settings, "MP_ACCESS_TOKEN", "")
    if not access_token:
        raise ImproperlyConfigured("MP_ACCESS_TOKEN is required to use Mercado Pago Checkout Pro.")
    return access_token


def _json_request(method: str, url: str, payload: dict | None = None) -> dict:
    data = None
    headers = {
        "Authorization": f"Bearer {_get_access_token()}",
        "Content-Type": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = Request(url=url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=15) as response:
            response_data = response.read().decode("utf-8")
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        logger.warning("Mercado Pago HTTP error %s: %s", exc.code, error_body)
        raise serializers.ValidationError({"detail": "Mercado Pago rechazo la solicitud."}) from exc
    except URLError as exc:
        logger.warning("Mercado Pago connection error: %s", exc)
        raise serializers.ValidationError({"detail": "No se pudo conectar con Mercado Pago."}) from exc
    return json.loads(response_data) if response_data else {}


def _decimal_as_mp_number(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def build_preference_payload(payment_transaction) -> dict:
    reservation = payment_transaction.reservation
    player = payment_transaction.player
    court = reservation.court
    expires_at = payment_transaction.expires_at

    if player:
        title = "TENIS - Reserva jugador"
        description = (
            f"Reserva {reservation.id} - {player.first_name} {player.last_name} - "
            f"Cancha {court.name}"
        )
    else:
        title = f"TENIS - Reserva cancha {court.name}"
        description = (
            f"Reserva {reservation.id} - Cancha {court.name} - "
            f"{timezone.localtime(reservation.start_datetime).strftime('%Y-%m-%d %H:%M')}"
        )

    metadata = {
        "area": "tenis",
        "reservation_id": reservation.id,
        "payment_transaction_id": payment_transaction.id,
        "payment_type": payment_transaction.payment_type,
        "court_id": court.id,
        "court_name": court.name,
        "start_time": reservation.start_datetime.isoformat(),
        "end_time": reservation.end_datetime.isoformat(),
        "base_amount": str(payment_transaction.base_amount),
        "identification_decimal": str(payment_transaction.identification_decimal),
    }
    if player:
        metadata["player_id"] = player.id
        metadata["player_name"] = f"{player.first_name} {player.last_name}"

    payload = {
        "items": [
            {
                "title": title,
                "description": description,
                "quantity": 1,
                "currency_id": "ARS",
                "unit_price": _decimal_as_mp_number(payment_transaction.mp_amount),
            }
        ],
        "external_reference": payment_transaction.external_reference,
        "metadata": metadata,
        "notification_url": settings.MP_WEBHOOK_URL,
        "back_urls": {
            "success": settings.FRONTEND_SUCCESS_URL,
            "failure": settings.FRONTEND_FAILURE_URL,
            "pending": settings.FRONTEND_PENDING_URL,
        },
        "auto_return": "approved",
    }
    if expires_at:
        payload.update(
            {
                "expires": True,
                "expiration_date_from": timezone.now().isoformat(),
                "expiration_date_to": expires_at.isoformat(),
            }
        )
    return payload


def create_checkout_preference_for_reservation_payment(payment_transaction) -> dict:
    payload = build_preference_payload(payment_transaction)
    return _json_request("POST", MERCADOPAGO_PREFERENCES_URL, payload)


def get_payment(payment_id: str) -> dict:
    return _json_request("GET", MERCADOPAGO_PAYMENTS_URL.format(payment_id=payment_id))
