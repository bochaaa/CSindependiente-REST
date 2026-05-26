from datetime import date

from django.contrib.auth import get_user_model
from django.db.models import Prefetch
from django.db import OperationalError, transaction
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer, TokenRefreshSerializer
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .models import (
    BlockedSlot,
    CancellationRequest,
    ClubSchedule,
    Court,
    PriceRule,
    RecurringReservationRule,
    Reservation,
    ReservationPlayer,
    SpecialSchedule,
)
from .permissions import IsAdminOrReadOnly
from .serializers import (
    AvailabilityResponseSerializer,
    AuthUserSerializer,
    BlockedSlotSerializer,
    CancelReservationSerializer,
    CancellationRequestCreateSerializer,
    CancellationRequestResolveSerializer,
    CancellationRequestSerializer,
    ClubScheduleSerializer,
    CourtSerializer,
    GenerateRecurringReservationsResponseSerializer,
    PriceRuleSerializer,
    ReservationPaymentStatusSerializer,
    RecurringRuleDeactivateResponseSerializer,
    RecurringRuleDeactivateSerializer,
    RecurringReservationRuleSerializer,
    ReservationCreateSerializer,
    ReservationSerializer,
    SpecialScheduleSerializer,
)
from .services import (
    cancel_reservation_by_admin,
    deactivate_recurring_rule,
    generate_availability_for_date,
    generate_recurring_reservations,
    set_reservation_payment_status,
)

User = get_user_model()


@extend_schema_view(
    list=extend_schema(description="List courts. Public read endpoint."),
    create=extend_schema(description="Create court. Admin only."),
    retrieve=extend_schema(description="Get a court by id."),
    partial_update=extend_schema(description="Update court fields. Admin only."),
    destroy=extend_schema(description="Delete court. Admin only."),
)
class CourtViewSet(viewsets.ModelViewSet):
    queryset = Court.objects.all().order_by("name")
    serializer_class = CourtSerializer
    permission_classes = (IsAdminOrReadOnly,)


@extend_schema_view(
    list=extend_schema(description="List price rules."),
    create=extend_schema(description="Create a price rule. Admin only."),
    retrieve=extend_schema(description="Get a price rule."),
    partial_update=extend_schema(description="Update a price rule. Admin only."),
    destroy=extend_schema(description="Delete a price rule. Admin only."),
)
class PriceRuleViewSet(viewsets.ModelViewSet):
    queryset = PriceRule.objects.all().order_by("-valid_from", "game_mode", "player_type")
    serializer_class = PriceRuleSerializer
    permission_classes = (IsAdminOrReadOnly,)


@extend_schema_view(
    list=extend_schema(description="List weekly club schedules."),
    create=extend_schema(description="Create a weekly schedule. Admin only."),
    retrieve=extend_schema(description="Get a schedule row."),
    partial_update=extend_schema(description="Update a schedule row. Admin only."),
    destroy=extend_schema(description="Delete a schedule row. Admin only."),
)
class ClubScheduleViewSet(viewsets.ModelViewSet):
    queryset = ClubSchedule.objects.all().order_by("day_of_week")
    serializer_class = ClubScheduleSerializer
    permission_classes = (IsAdminOrReadOnly,)


@extend_schema_view(
    list=extend_schema(description="List date-based special schedules."),
    create=extend_schema(description="Create a special schedule. Admin only."),
    retrieve=extend_schema(description="Get a special schedule."),
    partial_update=extend_schema(description="Update a special schedule. Admin only."),
    destroy=extend_schema(description="Delete a special schedule. Admin only."),
)
class SpecialScheduleViewSet(viewsets.ModelViewSet):
    queryset = SpecialSchedule.objects.all().order_by("date")
    serializer_class = SpecialScheduleSerializer
    permission_classes = (IsAdminOrReadOnly,)


@extend_schema(
    description=(
        "Get court availability ranges for a specific date. "
        "Returns available and unavailable continuous ranges. "
        "For available ranges, includes whether a 90-minute booking fits and the last valid start time. "
        "Public endpoint with 20/min throttling."
    ),
    parameters=[
        OpenApiParameter(
            name="date",
            type=str,
            required=True,
            location=OpenApiParameter.QUERY,
            description="Date in YYYY-MM-DD format.",
        )
    ],
    responses={200: AvailabilityResponseSerializer},
)
class AvailabilityAPIView(APIView):
    permission_classes = (AllowAny,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "public_availability"

    def get(self, request):
        date_value = request.query_params.get("date")
        if not date_value:
            return Response({"detail": "date query param is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            parsed_date = date.fromisoformat(date_value)
        except ValueError:
            return Response({"detail": "date must use YYYY-MM-DD format."}, status=status.HTTP_400_BAD_REQUEST)
        data = generate_availability_for_date(parsed_date)
        serializer = AvailabilityResponseSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.data)


@extend_schema_view(
    list=extend_schema(
        description=(
            "List reservations. Admin only. Optional filters: "
            "?date=YYYY-MM-DD, ?is_paid=true|false, ?unpaid=true|false"
        )
    ),
    retrieve=extend_schema(description="Retrieve one reservation. Admin only."),
    create=extend_schema(description="Create a NORMAL reservation (90 minutes). Public endpoint."),
)
class ReservationViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    queryset = Reservation.objects.select_related("court", "recurring_rule").prefetch_related(
        Prefetch("players", queryset=ReservationPlayer.objects.order_by("id"))
    )

    throttle_classes = (ScopedRateThrottle,)

    def get_permissions(self):
        if self.action in ("create", "request_cancellation"):
            return [AllowAny()]
        if self.action in ("cancel", "mark_payment", "list", "retrieve"):
            return [IsAdminUser()]
        return super().get_permissions()

    def get_throttles(self):
        if self.action == "create":
            self.throttle_scope = "public_reservation_create"
        elif self.action == "request_cancellation":
            self.throttle_scope = "public_reservation_cancellation_request"
        else:
            self.throttle_scope = None
        return super().get_throttles()

    def get_queryset(self):
        queryset = super().get_queryset()
        date_filter = self.request.query_params.get("date")
        is_paid_filter = self.request.query_params.get("is_paid")
        unpaid_filter = self.request.query_params.get("unpaid")
        if date_filter:
            try:
                parsed = date.fromisoformat(date_filter)
                queryset = queryset.filter(start_datetime__date=parsed)
            except ValueError:
                return queryset.none()
        if is_paid_filter is not None:
            parsed_bool = self._parse_bool_query_param(is_paid_filter)
            if parsed_bool is None:
                return queryset.none()
            queryset = queryset.filter(is_paid=parsed_bool)
        if unpaid_filter is not None:
            parsed_bool = self._parse_bool_query_param(unpaid_filter)
            if parsed_bool is None:
                return queryset.none()
            if parsed_bool:
                queryset = queryset.filter(is_paid=False)
            else:
                queryset = queryset.filter(is_paid=True)
        return queryset.order_by("start_datetime")

    @staticmethod
    def _parse_bool_query_param(value: str) -> bool | None:
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes"):
            return True
        if normalized in ("false", "0", "no"):
            return False
        return None

    def get_serializer_class(self):
        if self.action == "create":
            return ReservationCreateSerializer
        return ReservationSerializer

    @extend_schema(
        description="Cancel a reservation. Admin only. Soft cancellation via status=CANCELLED.",
        request=CancelReservationSerializer,
        responses={200: ReservationSerializer},
    )
    @action(detail=True, methods=("patch",), url_path="cancel")
    def cancel(self, request, pk=None):
        reservation = self.get_object()
        serializer = CancelReservationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        cancelled = cancel_reservation_by_admin(
            reservation=reservation,
            cancelled_by=request.user,
            cancellation_reason=serializer.validated_data.get("cancellation_reason", ""),
        )
        return Response(ReservationSerializer(cancelled).data)

    @extend_schema(
        description=(
            "Confirm or revert payment status for a reservation. Admin only. "
            "Use is_paid=true to confirm payment, is_paid=false to mark unpaid."
        ),
        request=ReservationPaymentStatusSerializer,
        responses={200: ReservationSerializer},
    )
    @action(detail=True, methods=("patch",), url_path="payment")
    def mark_payment(self, request, pk=None):
        reservation = self.get_object()
        serializer = ReservationPaymentStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updated = set_reservation_payment_status(
            reservation=reservation,
            is_paid=serializer.validated_data["is_paid"],
            confirmed_by=request.user,
        )
        return Response(ReservationSerializer(updated).data)

    @extend_schema(
        description=(
            "Create a cancellation request for a reservation. Public endpoint. "
            "The reservation remains CONFIRMED until admin decision. "
            "It is only allowed until 3 hours before start time. Public endpoint with 20/min throttling."
        ),
        request=CancellationRequestCreateSerializer,
        responses={201: OpenApiResponse(description="Cancellation request created.")},
    )
    @action(detail=True, methods=("post",), url_path="request-cancellation")
    def request_cancellation(self, request, pk=None):
        reservation = self.get_object()
        serializer = CancellationRequestCreateSerializer(data=request.data, context={"reservation": reservation})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"detail": "Cancellation request created."}, status=status.HTTP_201_CREATED)


@extend_schema_view(
    list=extend_schema(description="List cancellation requests. Admin only."),
    retrieve=extend_schema(description="Retrieve cancellation request. Admin only."),
)
class CancellationRequestViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    queryset = CancellationRequest.objects.select_related("reservation", "resolved_by").order_by("-created_at")
    serializer_class = CancellationRequestSerializer
    permission_classes = (IsAdminUser,)

    def get_queryset(self):
        queryset = super().get_queryset()
        status_filter = self.request.query_params.get("status")
        reservation_filter = self.request.query_params.get("reservation")
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        if reservation_filter:
            queryset = queryset.filter(reservation_id=reservation_filter)
        return queryset

    @extend_schema(
        description=(
            "Resolve cancellation request. Admin only. "
            "APPROVED cancels reservation (soft cancel), REJECTED keeps reservation unchanged."
        ),
        request=CancellationRequestResolveSerializer,
        responses={200: CancellationRequestSerializer},
    )
    @action(detail=True, methods=("patch",), url_path="resolve")
    def resolve(self, request, pk=None):
        cancellation_request = self.get_object()
        serializer = CancellationRequestResolveSerializer(
            instance=cancellation_request,
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        resolved = serializer.save()
        return Response(CancellationRequestSerializer(resolved).data)


@extend_schema_view(
    list=extend_schema(description="List recurring class rules. Admin only."),
    create=extend_schema(description="Create recurring class rule (60-minute classes). Admin only."),
    retrieve=extend_schema(description="Get recurring class rule. Admin only."),
    partial_update=extend_schema(description="Update recurring class rule. Admin only."),
    destroy=extend_schema(description="Delete recurring class rule. Admin only."),
)
class RecurringReservationRuleViewSet(viewsets.ModelViewSet):
    queryset = RecurringReservationRule.objects.select_related("court").order_by("court_id", "start_time")
    serializer_class = RecurringReservationRuleSerializer
    permission_classes = (IsAdminUser,)

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)
        self._schedule_recurring_generation()

    def perform_update(self, serializer):
        serializer.save()
        self._schedule_recurring_generation()

    def _schedule_recurring_generation(self):
        # In SQLite dev mode, immediate heavy writes can hit "database is locked".
        # Run generation after commit and swallow transient lock errors.
        def _run_generation():
            try:
                generate_recurring_reservations(days_ahead=90)
            except OperationalError:
                # Non-fatal for request flow. Admin can re-run via /recurring-rules/generate/.
                return

        transaction.on_commit(_run_generation, robust=True)

    @extend_schema(
        description=(
            "Deactivate recurring rule and cancel all future generated CLASS reservations "
            "for this rule. Admin only."
        ),
        request=RecurringRuleDeactivateSerializer,
        responses={200: RecurringRuleDeactivateResponseSerializer},
    )
    @action(detail=True, methods=("patch",), url_path="deactivate")
    def deactivate(self, request, pk=None):
        recurring_rule = self.get_object()
        serializer = RecurringRuleDeactivateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reason = serializer.validated_data.get("cancellation_reason", "") or "Regla recurrente desactivada por admin."
        rule, cancelled_count = deactivate_recurring_rule(
            recurring_rule=recurring_rule,
            deactivated_by=request.user,
            cancellation_reason=reason,
        )
        response_data = {
            "rule": RecurringReservationRuleSerializer(rule).data,
            "cancelled_future_classes": cancelled_count,
        }
        return Response(response_data)


@extend_schema_view(
    list=extend_schema(description="List blocked slots."),
    create=extend_schema(description="Create a blocked slot. Admin only."),
    retrieve=extend_schema(description="Get blocked slot."),
    destroy=extend_schema(description="Delete blocked slot. Admin only."),
)
class BlockedSlotViewSet(viewsets.ModelViewSet):
    queryset = BlockedSlot.objects.select_related("court", "created_by").order_by("start_datetime")
    serializer_class = BlockedSlotSerializer
    http_method_names = ("get", "post", "delete")
    permission_classes = (IsAdminOrReadOnly,)

    def get_queryset(self):
        queryset = super().get_queryset()
        date_filter = self.request.query_params.get("date")
        if date_filter:
            try:
                parsed = date.fromisoformat(date_filter)
                queryset = queryset.filter(start_datetime__date=parsed)
            except ValueError:
                return queryset.none()
        return queryset


@extend_schema(
    description=(
        "Generate concrete CLASS reservations from active recurring rules "
        "for the next N days. Admin only."
    ),
    parameters=[
        OpenApiParameter(
            name="days_ahead",
            type=int,
            required=False,
            location=OpenApiParameter.QUERY,
            description="How many days ahead to generate. Default: 90.",
        )
    ],
    request=None,
    responses={200: GenerateRecurringReservationsResponseSerializer},
)
class GenerateRecurringReservationsAPIView(APIView):
    permission_classes = (IsAdminUser,)
    serializer_class = GenerateRecurringReservationsResponseSerializer

    def post(self, request):
        days_ahead_raw = request.query_params.get("days_ahead", "90")
        try:
            days_ahead = int(days_ahead_raw)
        except ValueError:
            return Response({"detail": "days_ahead must be an integer."}, status=status.HTTP_400_BAD_REQUEST)
        created = generate_recurring_reservations(days_ahead=days_ahead)
        response_data = {"created": created, "days_ahead": days_ahead}
        serializer = self.serializer_class(data=response_data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.data)


@extend_schema(
    description=(
        "Obtain JWT access and refresh tokens. "
        "Use admin credentials when calling admin-only endpoints."
    ),
    request=TokenObtainPairSerializer,
    responses={200: OpenApiResponse(description="JWT token pair generated.")},
)
class AdminTokenObtainPairView(TokenObtainPairView):
    permission_classes = (AllowAny,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "auth_token_obtain"


@extend_schema(
    description="Refresh JWT access token using a valid refresh token.",
    request=TokenRefreshSerializer,
    responses={200: OpenApiResponse(description="JWT access token refreshed.")},
)
class AdminTokenRefreshView(TokenRefreshView):
    permission_classes = (AllowAny,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "auth_token_refresh"


@extend_schema(
    description="Return current authenticated user profile from JWT/session.",
    responses={200: AuthUserSerializer},
)
class AuthMeAPIView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        serializer = AuthUserSerializer(request.user)
        return Response(serializer.data)


@extend_schema(
    description="Return user profile by id. Admin only.",
    responses={200: AuthUserSerializer},
)
class AuthUserDetailAPIView(APIView):
    permission_classes = (IsAdminUser,)

    def get(self, request, user_id: int):
        user = User.objects.filter(id=user_id).first()
        if not user:
            return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = AuthUserSerializer(user)
        return Response(serializer.data)
