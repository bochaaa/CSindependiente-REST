# Frontend API Contract (CSI Tenis)

Documento de referencia para el equipo frontend con los endpoints disponibles y las reglas de negocio vigentes en backend.

## 1) Base y autenticacion

- Base URL local: `http://127.0.0.1:8000`
- Swagger UI: `GET /api/docs/`
- OpenAPI schema: `GET /api/schema/`
- Auth admin: JWT Bearer token
- Header para endpoints admin: `Authorization: Bearer <access_token>`

### JWT endpoints

- `POST /api/token/`
- `POST /api/token/refresh/`
- `GET /api/auth/me/` (usuario autenticado)
- `GET /api/auth/users/{id}/` (admin autenticado)

Request `POST /api/token/`:

```json
{
  "username": "admin",
  "password": "csindependiente2026"
}
```

Response 200:

```json
{
  "refresh": "<jwt_refresh>",
  "access": "<jwt_access>"
}
```

## 2) Throttling (rate limit)

- `GET /api/availability/` -> `20/min`
- `POST /api/reservations/` -> `20/min`
- `POST /api/reservations/{id}/request-cancellation/` -> `20/min`
- `POST /api/token/` -> `10/min`
- `POST /api/token/refresh/` -> `10/min`

## 3) Endpoints disponibles

## 3.1 Publicos (sin login)

- `GET /api/courts/`
- `GET /api/courts/{id}/`
- `GET /api/prices/`
- `GET /api/prices/{id}/`
- `GET /api/schedules/`
- `GET /api/schedules/{id}/`
- `GET /api/special-schedules/`
- `GET /api/special-schedules/{id}/`
- `GET /api/blocked-slots/`
- `GET /api/blocked-slots/{id}/`
- `GET /api/availability/?date=YYYY-MM-DD`
- `POST /api/reservations/`
- `POST /api/reservations/{id}/request-cancellation/`

## 3.2 Admin (requiere JWT admin)

- `GET /api/auth/me/`
- `GET /api/auth/users/{id}/`
- `POST /api/courts/`
- `PATCH /api/courts/{id}/`
- `PUT /api/courts/{id}/`
- `DELETE /api/courts/{id}/`
- `POST /api/prices/`
- `PATCH /api/prices/{id}/`
- `PUT /api/prices/{id}/`
- `DELETE /api/prices/{id}/`
- `POST /api/schedules/`
- `PATCH /api/schedules/{id}/`
- `PUT /api/schedules/{id}/`
- `DELETE /api/schedules/{id}/`
- `POST /api/special-schedules/`
- `PATCH /api/special-schedules/{id}/`
- `PUT /api/special-schedules/{id}/`
- `DELETE /api/special-schedules/{id}/`
- `POST /api/blocked-slots/`
- `DELETE /api/blocked-slots/{id}/`
- `GET /api/reservations/?date=YYYY-MM-DD`
- `GET /api/reservations/{id}/`
- `PATCH /api/reservations/{id}/cancel/`
- `GET /api/cancellation-requests/`
- `GET /api/cancellation-requests/{id}/`
- `PATCH /api/cancellation-requests/{id}/resolve/`
- `GET /api/recurring-rules/`
- `POST /api/recurring-rules/`
- `GET /api/recurring-rules/{id}/`
- `PATCH /api/recurring-rules/{id}/`
- `PUT /api/recurring-rules/{id}/`
- `DELETE /api/recurring-rules/{id}/`
- `PATCH /api/recurring-rules/{id}/deactivate/`
- `POST /api/recurring-rules/generate/?days_ahead=90`
- `GET /api/notification-devices/`
- `POST /api/notification-devices/`
- `DELETE /api/notification-devices/{id}/`
- `POST /api/notification-devices/unregister/`

## 4) Payloads clave

## 4.1 Crear reserva normal (publico)

`POST /api/reservations/`

```json
{
  "court": 1,
  "date": "2026-05-20",
  "start_time": "18:00",
  "game_mode": "SINGLES",
  "contact_name": "Pedro Rodriguez",
  "contact_phone": "2302123456",
  "players": [
    {
      "first_name": "Pedro",
      "last_name": "Rodriguez",
      "is_member": true
    },
    {
      "first_name": "Santi",
      "last_name": "Fernandez",
      "is_member": false
    }
  ],
  "notes": "Reserva desde frontend"
}
```

Respuesta 201 incluye:

- `reservation_type`
- `game_mode`
- `start_datetime`
- `end_datetime`
- `status`
- `total_price`
- `players[].price_applied`

## 4.2 Solicitar cancelacion (publico)

`POST /api/reservations/{id}/request-cancellation/`

```json
{
  "requester_name": "Pedro Rodriguez",
  "requester_phone": "2302123456",
  "reason": "No podemos asistir"
}
```

## 4.3 Resolver solicitud de cancelacion (admin)

`PATCH /api/cancellation-requests/{id}/resolve/`

Aprobar:

```json
{
  "status": "APPROVED",
  "cancellation_reason": "Aprobada por admin"
}
```

Rechazar:

```json
{
  "status": "REJECTED"
}
```

## 4.4 Disponibilidad por rangos (publico)

`GET /api/availability/?date=2026-05-20`

Respuesta (estructura):

```json
{
  "date": "2026-05-20",
  "reservation_duration_minutes": 90,
  "courts": [
    {
      "id": 1,
      "name": "Cancha 1",
      "available_ranges": [
        {
          "start_time": "08:00:00",
          "end_time": "16:00:00",
          "duration_minutes": 480,
          "can_book_90_min": true,
          "can_start_until": "14:30:00"
        }
      ],
      "unavailable_ranges": [
        {
          "start_time": "16:00:00",
          "end_time": "17:00:00",
          "reason": "RESERVATION",
          "reservation_type": "CLASS",
          "reservation_contact_name": null,
          "class_title": "Clases de Pedrito",
          "block_reason": null
        }
      ]
    }
  ]
}
```

Campos opcionales en `unavailable_ranges`:

- `reservation_type`: `NORMAL` o `CLASS` cuando `reason=RESERVATION`.
- `reservation_contact_name`: se informa cuando el tramo ocupado corresponde a reserva normal.
- `class_title`: se informa cuando el tramo ocupado corresponde a clase.
- `block_reason`: se informa cuando el tramo ocupado corresponde a bloqueo.

## 4.5 Registrar dispositivo para notificaciones (admin)

El frontend debe llamar este endpoint cuando un admin activa notificaciones o cuando Firebase/Capacitor entrega un token nuevo.

`POST /api/notification-devices/`

Headers:

- `Authorization: Bearer <access_token>`

Request web PWA:

```json
{
  "platform": "web",
  "provider": "fcm",
  "token": "TOKEN_FCM_WEB",
  "device_id": "browser-opcional"
}
```

Request Android Capacitor:

```json
{
  "platform": "android",
  "provider": "fcm",
  "token": "TOKEN_FCM_ANDROID",
  "device_id": "android-device-id-opcional"
}
```

Response 201:

```json
{
  "id": 7,
  "user_id": 12,
  "platform": "web",
  "provider": "fcm",
  "token": "TOKEN_FCM_WEB",
  "device_id": "browser-opcional",
  "enabled": true,
  "last_seen": "2026-06-29T10:30:00-03:00",
  "created_at": "2026-06-29T10:30:00-03:00",
  "updated_at": "2026-06-29T10:30:00-03:00"
}
```

El endpoint es idempotente por `token`: si el mismo token se registra otra vez, actualiza `platform`, `device_id`, `user`, `enabled=true` y `last_seen`.

Para logout o baja de permisos:

`POST /api/notification-devices/unregister/`

```json
{
  "token": "TOKEN_FCM_WEB"
}
```

Alternativa por id:

`DELETE /api/notification-devices/{id}/`

Ambos desactivan el dispositivo (`enabled=false`), no lo borran fisicamente.

Payload previsto para push cuando se crea una reserva:

```json
{
  "title": "Nueva reserva",
  "body": "Cancha 2 - 18:00 hs",
  "data": {
    "type": "reservation_created",
    "reservation_id": "123",
    "url": "/admin/reservations?date=2026-06-29"
  }
}
```

## 5) Reglas de negocio vigentes

## 5.1 Duracion y tipos

- Reserva normal (`reservation_type=NORMAL`): duracion fija 90 minutos.
- Clase (`reservation_type=CLASS`): duracion fija 60 minutos.
- `SINGLES` exige 2 jugadores.
- `DOUBLES` exige 4 jugadores.

## 5.2 Precios

- Precio por jugador segun `game_mode` + `is_member`.
- Se guarda historico por jugador en `ReservationPlayer.price_applied`.
- `total_price` se calcula en backend como suma de `price_applied`.
- Si no hay precio activo para una combinacion requerida, la reserva falla.

## 5.3 Solapamientos

- No se permite solapar con:
- Reservas activas (`CONFIRMED` o `CANCELLATION_REQUESTED`).
- Reservas de clase `CLASS`.
- Bloqueos (`BlockedSlot`).
- Regla de solapamiento: `nuevo_inicio < existente_fin` y `nuevo_fin > existente_inicio`.
- Reservas `CANCELLED` no bloquean.

## 5.4 Horarios del club

- Se usa `SpecialSchedule` por fecha con prioridad sobre `ClubSchedule`.
- Si no hay horario o el dia esta cerrado, no se puede reservar.
- La reserva debe entrar completa dentro del horario abierto.

## 5.5 Cancelaciones

- Solo admin puede cancelar directamente reserva (`PATCH /cancel/`).
- Usuario comun solo solicita cancelacion.
- La solicitud se permite solo hasta 3 horas antes del inicio del turno.
- Al solicitar cancelacion, la reserva pasa a `CANCELLATION_REQUESTED`.
- Al aprobar solicitud (`APPROVED`), reserva pasa a `CANCELLED`.
- Al rechazar solicitud (`REJECTED`), reserva vuelve a `CONFIRMED` si estaba en `CANCELLATION_REQUESTED`.
- No se elimina fisicamente la reserva.

## 5.6 Clases recurrentes

- `RecurringReservationRule` define plantilla semanal.
- Al crear o editar una regla, backend dispara generacion automatica de clases concretas `CLASS` para los proximos 90 dias.
- Tambien se puede forzar manualmente por endpoint admin `POST /api/recurring-rules/generate/`.
- Se evitan duplicados de clases por `recurring_rule + court + start_datetime`.
- Para "eliminar" una clase recurrente sin perder historial, usar `PATCH /api/recurring-rules/{id}/deactivate/`.
- `deactivate` pone `active=false` en la regla y cancela (`status=CANCELLED`) todas las clases futuras generadas por esa regla.

## 5.7 Notificaciones

- El admin registra tokens FCM con `POST /api/notification-devices/`.
- Al crear una reserva normal, backend crea `NotificationLog` `PUSH` pendiente para cada dispositivo activo de admins.
- Si `PUSH_NOTIFICATIONS_ENABLED=True` y Firebase esta configurado en backend, el backend envia el push real por FCM y marca el log como `SENT`.
- Si Firebase no esta configurado, el log queda como `PENDING` o `FAILED` segun la configuracion, sin romper la creacion de la reserva.
- No hay envio real de WhatsApp/email en esta etapa.

## 6) Enums utiles para frontend

- `reservation_type`: `NORMAL`, `CLASS`
- `reservation_status`: `CONFIRMED`, `CANCELLED`, `CANCELLATION_REQUESTED`
- `game_mode`: `SINGLES`, `DOUBLES`
- `player_type` (PriceRule): `MEMBER`, `NON_MEMBER`
- `cancellation_request_status`: `PENDING`, `APPROVED`, `REJECTED`
- `block_type`: `TOURNAMENT`, `MAINTENANCE`, `OTHER`
- `day_of_week`: `MONDAY`, `TUESDAY`, `WEDNESDAY`, `THURSDAY`, `FRIDAY`, `SATURDAY`, `SUNDAY`
- `notification_platform`: `web`, `android`
- `notification_provider`: `fcm`
