Necesito implementar integración de pagos con Mercado Pago Checkout Pro en una app de reservas de canchas.

Contexto del proyecto:

- Backend: Django + Django REST Framework.
- App de reservas de canchas de tenis.
- Mercado Pago se usará con Checkout Pro para generar links de pago.
- No se usará caja/POS de Mercado Pago para los links, porque Checkout Pro no permite imputar pagos a una caja POS específica.
- Para que Rentas identifique los pagos en Mercado Pago, el título del item debe empezar con "TENIS" y la referencia externa debe tener formato claro.

Objetivo general:
Implementar un sistema de pagos parciales o totales asociados a una reserva.

Regla principal:
Una reserva puede tener uno o varios pagos asociados. Cada pago confirmado por Mercado Pago suma al total pagado de la reserva. La reserva pasa a estado "paid" solamente cuando la suma de pagos aprobados sea mayor o igual al monto total de la reserva.

Ejemplos:

- Reserva 1v1 total: $24.000.
  - Jugador A paga $12.000.
  - Jugador B paga $12.000.
  - Total pagado: $24.000.
  - Reserva queda pagada.

- Reserva 1v1 total: $24.000.
  - Un jugador paga $24.000.
  - Total pagado: $24.000.
  - Reserva queda pagada.

- Reserva 1v1 total: $24.000.
  - Jugador A paga $12.000.
  - Total pagado: $12.000.
  - Reserva queda en estado "partial_payment" o "pending_payment".

Estados sugeridos para Reservation:

- pending_payment: reserva creada, sin pagos confirmados.
- partial_payment: tiene pagos aprobados, pero no alcanza el total.
- paid: la suma de pagos aprobados alcanza o supera el total.
- expired: venció el tiempo para pagar y no se completó el pago.
- cancelled: cancelada manualmente.
- rejected: todos los intentos de pago fueron rechazados o no hay pago válido.

Campos sugeridos para Reservation:

- total_amount: DecimalField
- paid_amount: DecimalField, default=0
- payment_status: pending_payment, partial_payment, paid, expired, cancelled, rejected
- payment_expires_at
- paid_at
- mp_external_reference_base, por ejemplo "TENIS-RESERVA-{reservation_id}"

Modelo sugerido PaymentTransaction:

- reservation FK
- player FK nullable, si el pago corresponde a una persona concreta de la reserva
- provider = "mercadopago"
- preference_id
- payment_id
- external_reference
- status
- status_detail
- amount
- payer_email nullable
- payment_url
- raw_response JSONField
- created_at
- updated_at
- paid_at

Modelo de personas/jugadores asociados a reserva:
Si ya existe un modelo de jugadores asociados a la reserva, permitir generar pagos:

1. Por jugador individual.
2. Por monto total de la reserva.
3. Por monto personalizado si corresponde.

Ejemplo de referencias externas:

- Pago total:
  "TENIS-RESERVA-123-TOTAL"
- Pago jugador 1:
  "TENIS-RESERVA-123-JUGADOR-45"
- Pago parcial:
  "TENIS-RESERVA-123-PARCIAL-1"

El objetivo es que cada link tenga una external_reference única, pero que todas puedan relacionarse con la misma reserva.

Configuración por .env:

- MP_ACCESS_TOKEN
- MP_PUBLIC_KEY, si hace falta
- MP_WEBHOOK_URL=https://sporturnos.com.ar/api/payments/webhook/
- FRONTEND_SUCCESS_URL=https://sporturnos.com.ar/pago/success
- FRONTEND_FAILURE_URL=https://sporturnos.com.ar/pago/failure
- FRONTEND_PENDING_URL=https://sporturnos.com.ar/pago/pending
- PAYMENT_EXPIRATION_MINUTES=60

Servicio Mercado Pago:
Crear archivo:
payments/services/mercadopago_service.py

Debe tener una función:
create_checkout_preference_for_reservation_payment(reservation, amount, player=None, payment_type="partial")

La preference debe enviarse a:
POST https://api.mercadopago.com/checkout/preferences

Headers:
Authorization: Bearer {MP_ACCESS_TOKEN}
Content-Type: application/json

Body para pago total:
{
"items": [
{
"title": "TENIS - Reserva cancha {court_name}",
"description": "Reserva {reservation_id} - Cancha {court_name} - {start_time}",
"quantity": 1,
"currency_id": "ARS",
"unit_price": amount
}
],
"external_reference": "TENIS-RESERVA-{reservation_id}-TOTAL",
"metadata": {
"area": "tenis",
"reservation_id": reservation_id,
"payment_type": "total",
"court_id": court_id,
"court_name": court_name,
"start_time": start_time_iso,
"end_time": end_time_iso
},
"notification_url": MP_WEBHOOK_URL,
"back_urls": {
"success": FRONTEND_SUCCESS_URL,
"failure": FRONTEND_FAILURE_URL,
"pending": FRONTEND_PENDING_URL
},
"auto_return": "approved",
"expires": true,
"expiration_date_from": now_iso,
"expiration_date_to": expiration_iso
}

Body para pago individual por jugador:
{
"items": [
{
"title": "TENIS - Reserva jugador",
"description": "Reserva {reservation_id} - {player_name} - Cancha {court_name}",
"quantity": 1,
"currency_id": "ARS",
"unit_price": amount
}
],
"external_reference": "TENIS-RESERVA-{reservation_id}-JUGADOR-{player_id}",
"metadata": {
"area": "tenis",
"reservation_id": reservation_id,
"payment_type": "player",
"player_id": player_id,
"player_name": player_name,
"court_id": court_id,
"court_name": court_name,
"start_time": start_time_iso,
"end_time": end_time_iso
},
"notification_url": MP_WEBHOOK_URL,
"back_urls": {
"success": FRONTEND_SUCCESS_URL,
"failure": FRONTEND_FAILURE_URL,
"pending": FRONTEND_PENDING_URL
},
"auto_return": "approved",
"expires": true,
"expiration_date_from": now_iso,
"expiration_date_to": expiration_iso
}

Endpoint backend:
POST /api/reservations/{id}/payments/create-link/

Body posible:
{
"amount": 12000,
"payment_type": "player",
"player_id": 45
}

O para pago total:
{
"amount": 24000,
"payment_type": "total"
}

El endpoint debe:

1. Buscar la reserva.
2. Validar que la reserva no esté cancelled, expired o paid.
3. Validar que el monto sea mayor a 0.
4. Validar que el monto no supere de forma absurda el saldo pendiente, salvo que se decida permitir sobrepago.
5. Crear una PaymentTransaction en estado pending.
6. Crear la preference en Mercado Pago.
7. Guardar preference_id, payment_url, external_reference, amount.
8. Devolver:
   {
   "reservation_id": id,
   "payment_transaction_id": id,
   "payment_url": "...",
   "preference_id": "...",
   "amount": 12000,
   "reservation_total_amount": 24000,
   "reservation_paid_amount": 0,
   "reservation_remaining_amount": 24000,
   "expires_at": "..."
   }

Webhook:
POST /api/payments/webhook/

Mercado Pago puede enviar distintos formatos. Contemplar:

- query param type/payment id
- body con data.id
- topic/payment

Flujo:

1. Obtener payment_id desde request.
2. Si no hay payment_id, responder 200 y loguear.
3. Consultar:
   GET https://api.mercadopago.com/v1/payments/{payment_id}
   Authorization: Bearer {MP_ACCESS_TOKEN}
4. Leer:

- id
- status
- status_detail
- external_reference
- transaction_amount
- metadata
- payer

5. Buscar PaymentTransaction por external_reference.
6. Si no existe, intentar resolver reservation_id desde external_reference.
7. Validar que el monto coincida con la PaymentTransaction.
8. Evitar duplicados:
   - Si ya existe una PaymentTransaction con ese payment_id aprobado, no volver a sumar.
   - El webhook debe ser idempotente.

9. Si status == "approved":
   - Marcar PaymentTransaction como approved.
   - Guardar payment_id, status, status_detail, raw_response, paid_at.
   - Recalcular el total pagado de la reserva sumando PaymentTransaction approved.
   - Actualizar reservation.paid_amount.
   - Si paid_amount >= total_amount:
     reservation.payment_status = "paid"
     reservation.paid_at = now
     Si paid_amount > 0 pero paid_amount < total_amount:
     reservation.payment_status = "partial_payment"

10. Si status in ["rejected", "cancelled"]:

- Marcar PaymentTransaction con ese estado.
- No sumar al total pagado.
- Si la reserva no tiene pagos aprobados, mantener pending_payment o rejected según corresponda.

11. Responder siempre 200 para evitar reintentos innecesarios.

Liberación automática:
Implementar comando Django:
python manage.py expire_pending_reservations

Debe:

- Buscar reservas con payment_status in ["pending_payment", "partial_payment"]
- payment_expires_at < now()
- paid_amount < total_amount
- Marcarlas como "expired" según regla de negocio.
- Liberar el turno si no se completó el pago.

Importante:
Definir qué hacer si hubo pago parcial y venció:
Opción A:

- La reserva queda expired y se debe gestionar devolución/manual.
  Opción B:
- No se permite vencimiento si hubo pago parcial y se avisa al admin.
  Implementar inicialmente opción B o dejarlo configurable:
  Si paid_amount > 0 y paid_amount < total_amount al vencer:
- payment_status = "partial_payment"
- marcar requires_admin_review = true
- no liberar automáticamente sin revisión.
  Si paid_amount == 0 al vencer:
- payment_status = "expired"
- liberar turno.

Validación de disponibilidad:
Actualizar la lógica de disponibilidad para que bloqueen el turno:

- paid
- pending_payment no vencida
- partial_payment no vencida o con revisión pendiente

No bloquean:

- expired
- cancelled
- rejected

Frontend Angular:
Flujo sugerido:

1. Usuario crea reserva.
2. Backend devuelve reserva con total_amount y jugadores asociados.
3. Front muestra opciones:
   - Pagar total: $24.000
   - Pagar mi parte: $12.000
   - Generar link para otro jugador: $12.000

4. Al elegir opción, llamar:
   POST /api/reservations/{id}/payments/create-link/
5. Backend devuelve payment_url.
6. Redirigir:
   window.location.href = payment_url

Pantalla de reserva:
Mostrar:

- total_amount
- paid_amount
- remaining_amount
- payment_status
- lista de pagos:
  - jugador
  - monto
  - estado
  - fecha
  - payment_id MP

Pantallas:

- /pago/success: mostrar "Pago recibido. Estamos confirmando la reserva."
- /pago/failure: mostrar "El pago fue rechazado o cancelado."
- /pago/pending: mostrar "El pago quedó pendiente. Te avisaremos cuando se confirme."
  La confirmación real debe depender del webhook, no solamente del redirect del frontend.

Reporte para Rentas:
El sistema debe poder exportar pagos de Tenis con:

- fecha
- reserva
- cancha
- horario
- jugador si corresponde
- monto pagado
- estado
- payment_id de Mercado Pago
- external_reference
- operación de Mercado Pago si está disponible

Importante para visualización en Mercado Pago:

- El title debe empezar con "TENIS - ..."
- Para pago total: "TENIS - Reserva cancha"
- Para pago individual: "TENIS - Reserva jugador"
- La description debe incluir reserva, cancha, horario y jugador si corresponde.
- No usar centavos tipo ,19 para pagos generados por la app.
- La identificación principal será external_reference y el reporte propio del sistema.

Criterios de aceptación:

1. Se puede crear una reserva con total_amount.
2. Se puede generar un link de pago por el total de la reserva.
3. Se puede generar un link de pago por jugador/persona asociada.
4. Cada link crea una PaymentTransaction pendiente.
5. Al recibir webhook approved, se suma el pago a la reserva.
6. Si la suma de pagos aprobados alcanza el total, la reserva pasa a paid.
7. Si la suma no alcanza, la reserva queda partial_payment.
8. El webhook es idempotente y no duplica pagos si Mercado Pago reintenta.
9. El turno se bloquea mientras la reserva está pendiente o parcialmente paga según reglas.
10. El sistema puede liberar reservas vencidas sin pagos.
11. Si hay pago parcial vencido, queda marcado para revisión admin y no se libera automáticamente sin decisión.
12. Rentas puede identificar visualmente los movimientos porque el title inicia con "TENIS - ...".
13. El sistema puede generar reporte de pagos por reserva y por área Tenis.
14. El código debe estar separado en servicios, modelos, serializers, views y commands.
15. Agregar logs claros para creación de links, webhooks, pagos aprobados, pagos rechazados y expiraciones.

Regla especial de identificación para Rentas:
Además de usar title y external_reference, todos los links de pago generados para Tenis deben incluir un decimal identificador de $0,19.

Ejemplos:

- Si la reserva total vale $24.000, el link de pago total debe generarse por $24.000,19.
- Si la reserva se paga entre 2 jugadores y cada uno paga $12.000, cada link individual debe generarse por $12.000,19.
- Si hay 4 jugadores y cada uno paga $6.000, cada link individual debe generarse por $6.000,19.

Este decimal de $0,19 se usa solamente para que Rentas pueda filtrar visualmente o por movimientos en Mercado Pago. No representa un valor real del turno.

Implementar separación entre:

- base_amount: monto real que corresponde al pago de la reserva.
- mp_amount: monto enviado a Mercado Pago, incluyendo el decimal identificador.
- identification_decimal: decimal usado para identificar área Tenis, por defecto 0.19.

Ejemplo:
base_amount = 12000.00
identification_decimal = 0.19
mp_amount = 12000.19

En la creación de la preferencia de Mercado Pago, usar:
unit_price = mp_amount

Pero para calcular si la reserva está completamente pagada, sumar solamente los base_amount de los pagos aprobados.

Ejemplo:
Reserva total_amount = 24000.00

Jugador A:
base_amount = 12000.00
mp_amount = 12000.19

Jugador B:
base_amount = 12000.00
mp_amount = 12000.19

Mercado Pago registra total cobrado:
24000.38

El sistema debe computar como pagado para la reserva:
12000.00 + 12000.00 = 24000.00

Cuando paid_base_amount >= reservation.total_amount, la reserva pasa a paid.

Campos sugeridos en PaymentTransaction:

- base_amount: DecimalField
- identification_decimal: DecimalField, default=0.19
- mp_amount: DecimalField
- amount_received: DecimalField nullable, monto real informado por Mercado Pago
- external_reference
- payment_id
- status
- raw_response

Validaciones:

- mp_amount debe ser base_amount + identification_decimal.
- Para pagos de Tenis, identification_decimal debe ser 0.19.
- No permitir crear links con montos enteros sin el decimal identificador, salvo que se configure explícitamente.
- En webhook, validar que transaction_amount recibido desde Mercado Pago coincida con mp_amount.
- Si coincide y el pago está approved, sumar base_amount a la reserva.
- Guardar también amount_received para conciliación.

Visualización para Rentas:

- El title debe empezar con "TENIS - ...".
- El monto en Mercado Pago debe terminar en ,19.
- external_reference debe tener formato claro:
  - TENIS-RESERVA-{reservation_id}-TOTAL
  - TENIS-RESERVA-{reservation_id}-JUGADOR-{player_id}
  - TENIS-RESERVA-{reservation_id}-PARCIAL-{payment_transaction_id}

Reporte propio:
El reporte debe mostrar ambos montos:

- Monto real aplicado a la reserva: base_amount.
- Monto cobrado por Mercado Pago: mp_amount.
- Decimal identificador: 0.19.
