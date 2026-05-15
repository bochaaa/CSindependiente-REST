# CSI Tenis Backend

Backend para administrar reservas de canchas de tenis del Club Sportivo Independiente.

## Tecnologías

- Python 3.14
- Django 6.0.5
- Django REST Framework 3.17.1

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py runserver
```

## Documentacion API (Swagger)

- OpenAPI schema: `http://127.0.0.1:8000/api/schema/`
- Swagger UI: `http://127.0.0.1:8000/api/docs/`

## Endpoints principales

- `POST /api/token/`
- `POST /api/token/refresh/`
- `GET/POST/PATCH/DELETE /api/courts/`
- `GET/POST/PATCH/DELETE /api/prices/`
- `GET/POST/PATCH/DELETE /api/schedules/`
- `GET/POST/PATCH/DELETE /api/special-schedules/`
- `GET /api/availability/?date=YYYY-MM-DD`
- `GET/POST /api/reservations/`
- `GET /api/reservations/{id}/`
- `PATCH /api/reservations/{id}/cancel/`
- `POST /api/reservations/{id}/request-cancellation/`
- `GET /api/cancellation-requests/`
- `GET /api/cancellation-requests/{id}/`
- `PATCH /api/cancellation-requests/{id}/resolve/`
- `GET/POST/PATCH/DELETE /api/recurring-rules/`
- `POST /api/recurring-rules/generate/?days_ahead=90`
- `GET/POST/DELETE /api/blocked-slots/`

## JWT rapido

1. Pedir tokens en `POST /api/token/` con:
   - `username`
   - `password`
2. Usar el `access` token en header:
   - `Authorization: Bearer <token>`
3. Renovar con `POST /api/token/refresh/` usando `refresh`.

## Seed inicial

Ejecutar:

```powershell
.\.venv\Scripts\python.exe manage.py seed_initial_data
```

Esto crea/actualiza:
- 5 canchas: `Cancha 1` a `Cancha 5` (activas).
- Horario semanal: `08:00` a `21:00`.
- Precios activos desde hoy:
  - `SINGLES + MEMBER = 8000.00`
  - `SINGLES + NON_MEMBER = 13000.00`
  - `DOUBLES + MEMBER = 4000.00`
  - `DOUBLES + NON_MEMBER = 6500.00`

## Reglas publicas extra

- Throttling en endpoints publicos:
  - `GET /api/availability/` -> `20/min`
  - `POST /api/reservations/` -> `20/min`
  - `POST /api/reservations/{id}/request-cancellation/` -> `20/min`
  - `POST /api/token/` -> `10/min`
  - `POST /api/token/refresh/` -> `10/min`
- Solicitud de cancelacion:
  - Solo se permite hasta 3 horas antes del turno.
  - Si faltan menos de 3 horas, se rechaza la solicitud.
  - Al solicitar cancelacion, la reserva pasa a `CANCELLATION_REQUESTED`.

## Postman

Archivos listos para importar:
- `docs/postman/CSI-Tenis.postman_collection.json`
- `docs/postman/CSI-Tenis.local.postman_environment.json`

Contrato para frontend:
- `docs/frontend_api_contract.md`

Flujo sugerido:
1. Importar coleccion + environment.
2. Ejecutar `Auth -> POST /api/token/`.
3. Probar endpoints `Public`.
4. Probar endpoints `Admin` (usan `Bearer {{access_token}}` automaticamente).

## Estructura inicial

- `csitenis/`: configuración del proyecto
- `reservations/`: app para administrar reservas de canchas
