from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AvailabilityAPIView,
    BlockedSlotViewSet,
    CancellationRequestViewSet,
    ClubScheduleViewSet,
    CourtViewSet,
    GenerateRecurringReservationsAPIView,
    MercadoPagoReportCSVAPIView,
    NotificationDeviceViewSet,
    PaymentWebhookAPIView,
    PriceRuleViewSet,
    RecurringReservationRuleViewSet,
    ReservationViewSet,
    SpecialScheduleViewSet,
)

router = DefaultRouter()
router.register("courts", CourtViewSet, basename="court")
router.register("notification-devices", NotificationDeviceViewSet, basename="notification-device")
router.register("prices", PriceRuleViewSet, basename="price")
router.register("schedules", ClubScheduleViewSet, basename="schedule")
router.register("special-schedules", SpecialScheduleViewSet, basename="special-schedule")
router.register("reservations", ReservationViewSet, basename="reservation")
router.register("recurring-rules", RecurringReservationRuleViewSet, basename="recurring-rule")
router.register("blocked-slots", BlockedSlotViewSet, basename="blocked-slot")
router.register("cancellation-requests", CancellationRequestViewSet, basename="cancellation-request")

urlpatterns = [
    path("", include(router.urls)),
    path("availability/", AvailabilityAPIView.as_view(), name="availability"),
    path(
        "recurring-rules/generate/",
        GenerateRecurringReservationsAPIView.as_view(),
        name="generate-recurring-reservations",
    ),
    path("payments/webhook/", PaymentWebhookAPIView.as_view(), name="payment-webhook"),
    path(
        "payments/reports/mercadopago.csv/",
        MercadoPagoReportCSVAPIView.as_view(),
        name="mercadopago-report-csv",
    ),
]
