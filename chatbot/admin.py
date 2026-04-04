from django.contrib import admin
from django.db.models import Count, Q
from .models import (
    ChatConversation, ChatMessage, MedicineRequest, MedicineRequestRankingSnapshot,
    PharmacyResponse,
    Pharmacy, Pharmacist, PharmacyRating, PharmacyInventory, Reservation,
    PharmacistDecline, PatientProfile, PatientNotification, AdminAuditLog,
)


@admin.register(ChatConversation)
class ChatConversationAdmin(admin.ModelAdmin):
    list_display = ['conversation_id', 'session_id', 'status', 'created_at']
    list_filter = ['status', 'created_at']
    search_fields = ['session_id', 'conversation_id']


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ['message_id', 'conversation', 'role', 'created_at']
    list_filter = ['role', 'created_at']
    search_fields = ['content']


@admin.register(MedicineRequestRankingSnapshot)
class MedicineRequestRankingSnapshotAdmin(admin.ModelAdmin):
    list_display = ['snapshot_id', 'request', 'source', 'limit_applied', 'created_at']
    list_filter = ['source', 'created_at']
    search_fields = ['snapshot_id', 'request__request_id']
    readonly_fields = ['snapshot_id', 'created_at']


@admin.register(MedicineRequest)
class MedicineRequestAdmin(admin.ModelAdmin):
    change_list_template = 'admin/chatbot/dashboard_change_list.html'
    list_display = ['request_id', 'request_type', 'status', 'response_count', 'decline_count', 'created_at']
    list_filter = ['request_type', 'status', 'created_at']
    search_fields = ['medicine_names', 'symptoms']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(
            response_count_value=Count('pharmacy_responses', distinct=True),
            decline_count_value=Count('pharmacist_declines', distinct=True),
        )

    @admin.display(description='Responses', ordering='response_count_value')
    def response_count(self, obj):
        return obj.response_count_value

    @admin.display(description='Declines', ordering='decline_count_value')
    def decline_count(self, obj):
        return obj.decline_count_value

    def changelist_view(self, request, extra_context=None):
        if extra_context is None:
            extra_context = {}
        qs = self.get_queryset(request)
        extra_context['dashboard_stats'] = [
            {'label': 'Total Patient Requests', 'value': qs.count()},
            {'label': 'Awaiting Responses', 'value': qs.filter(status='awaiting_responses').count()},
            {'label': 'Completed Requests', 'value': qs.filter(status='completed').count()},
            {'label': 'Expired Requests', 'value': qs.filter(status='expired').count()},
        ]
        return super().changelist_view(request, extra_context=extra_context)


@admin.register(PharmacyResponse)
class PharmacyResponseAdmin(admin.ModelAdmin):
    list_display = ['response_id', 'pharmacy_name', 'medicine_available', 'price', 'submitted_at']
    list_filter = ['medicine_available', 'submitted_at']
    search_fields = ['pharmacy_name', 'notes']


@admin.register(Pharmacy)
class PharmacyAdmin(admin.ModelAdmin):
    change_list_template = 'admin/chatbot/dashboard_change_list.html'
    list_display = [
        'pharmacy_id',
        'name',
        'address',
        'phone',
        'email',
        'rating',
        'rating_count',
        'response_rate',
        'is_active',
        'pharmacists_count',
        'reservations_count',
        'active_reservations_count',
        'created_at',
    ]
    list_filter = ['is_active', 'verification_status', 'created_at']
    search_fields = ['name', 'pharmacy_id', 'address', 'email']
    readonly_fields = ['created_at', 'updated_at']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(
            pharmacists_count_value=Count('pharmacists', distinct=True),
            reservations_count_value=Count('reservations', distinct=True),
            active_reservations_count_value=Count(
                'reservations',
                filter=Q(reservations__status__in=['pending', 'confirmed']),
                distinct=True,
            ),
        )

    @admin.display(description='Pharmacists', ordering='pharmacists_count_value')
    def pharmacists_count(self, obj):
        return obj.pharmacists_count_value

    @admin.display(description='Reservations', ordering='reservations_count_value')
    def reservations_count(self, obj):
        return obj.reservations_count_value

    @admin.display(description='Active Reservations', ordering='active_reservations_count_value')
    def active_reservations_count(self, obj):
        return obj.active_reservations_count_value

    def changelist_view(self, request, extra_context=None):
        if extra_context is None:
            extra_context = {}
        qs = self.get_queryset(request)
        extra_context['dashboard_stats'] = [
            {'label': 'Registered Pharmacies', 'value': qs.count()},
            {'label': 'Active Pharmacies', 'value': qs.filter(is_active=True).count()},
            {'label': 'Total Pharmacists', 'value': Pharmacist.objects.count()},
            {'label': 'Platform Reservations', 'value': Reservation.objects.count()},
        ]
        return super().changelist_view(request, extra_context=extra_context)


@admin.register(PharmacyRating)
class PharmacyRatingAdmin(admin.ModelAdmin):
    list_display = ['pharmacy', 'rating', 'created_at']
    list_filter = ['rating', 'created_at']
    search_fields = ['pharmacy__name']


@admin.register(Pharmacist)
class PharmacistAdmin(admin.ModelAdmin):
    change_list_template = 'admin/chatbot/dashboard_change_list.html'
    list_display = [
        'pharmacist_id',
        'full_name',
        'pharmacy',
        'email',
        'license_number',
        'is_active',
        'responses_count',
        'declines_count',
        'created_at',
    ]
    list_filter = ['is_active', 'pharmacy', 'created_at']
    search_fields = ['first_name', 'last_name', 'email', 'license_number', 'pharmacy__name']
    readonly_fields = ['pharmacist_id', 'created_at', 'updated_at']
    raw_id_fields = ['pharmacy', 'user']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(
            responses_count_value=Count('responses', distinct=True),
            declines_count_value=Count('declined_requests', distinct=True),
        )

    @admin.display(description='Responses', ordering='responses_count_value')
    def responses_count(self, obj):
        return obj.responses_count_value

    @admin.display(description='Declines', ordering='declines_count_value')
    def declines_count(self, obj):
        return obj.declines_count_value

    def changelist_view(self, request, extra_context=None):
        if extra_context is None:
            extra_context = {}
        qs = self.get_queryset(request)
        extra_context['dashboard_stats'] = [
            {'label': 'Registered Pharmacists', 'value': qs.count()},
            {'label': 'Active Pharmacists', 'value': qs.filter(is_active=True).count()},
            {'label': 'Total Pharmacy Responses', 'value': PharmacyResponse.objects.count()},
            {'label': 'Total Request Declines', 'value': PharmacistDecline.objects.count()},
        ]
        return super().changelist_view(request, extra_context=extra_context)


@admin.register(PharmacyInventory)
class PharmacyInventoryAdmin(admin.ModelAdmin):
    list_display = ['pharmacy', 'medicine_name', 'quantity', 'reserved_quantity', 'low_stock_threshold', 'price', 'updated_at']
    list_filter = ['pharmacy', 'medicine_name']
    search_fields = ['medicine_name', 'pharmacy__name']
    list_editable = ['quantity', 'reserved_quantity', 'low_stock_threshold', 'price']


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    change_list_template = 'admin/chatbot/dashboard_change_list.html'
    list_display = [
        'reservation_id',
        'pharmacy',
        'patient_name',
        'patient_phone',
        'medicine_name',
        'quantity',
        'status',
        'reserved_at',
        'expires_at',
    ]
    list_filter = ['status', 'pharmacy']
    search_fields = ['medicine_name', 'pharmacy__name', 'session_id', 'patient_name', 'patient_phone']

    def changelist_view(self, request, extra_context=None):
        if extra_context is None:
            extra_context = {}
        qs = self.get_queryset(request)
        extra_context['dashboard_stats'] = [
            {'label': 'Total Reserved Through Platform', 'value': qs.count()},
            {'label': 'Pending', 'value': qs.filter(status='pending').count()},
            {'label': 'Confirmed', 'value': qs.filter(status='confirmed').count()},
            {'label': 'Picked Up', 'value': qs.filter(status='picked_up').count()},
            {'label': 'Expired or Cancelled', 'value': qs.filter(status__in=['expired', 'cancelled']).count()},
        ]
        return super().changelist_view(request, extra_context=extra_context)


@admin.register(PharmacistDecline)
class PharmacistDeclineAdmin(admin.ModelAdmin):
    list_display = ['request', 'pharmacist', 'declined_at', 'reason']
    list_filter = ['declined_at']


@admin.register(PatientProfile)
class PatientProfileAdmin(admin.ModelAdmin):
    list_display = ['session_id', 'display_name', 'email', 'max_search_radius_km', 'updated_at']
    search_fields = ['session_id', 'display_name', 'email']


@admin.register(PatientNotification)
class PatientNotificationAdmin(admin.ModelAdmin):
    list_display = ['session_id', 'notification_type', 'title', 'read_at', 'created_at']
    list_filter = ['notification_type', 'read_at']
    search_fields = ['title', 'body', 'session_id']


@admin.register(AdminAuditLog)
class AdminAuditLogAdmin(admin.ModelAdmin):
    list_display = [
        'created_at', 'username', 'action', 'target_type', 'target_id', 'success', 'ip_address',
    ]
    list_filter = ['success', 'action', 'created_at']
    search_fields = ['username', 'action', 'target_id']
    readonly_fields = [
        'user', 'username', 'action', 'target_type', 'target_id',
        'success', 'detail', 'ip_address', 'created_at',
    ]
