from rest_framework import serializers
from .models import (
    ChatConversation, ChatMessage, MedicineRequest, PharmacyResponse, Pharmacy, Pharmacist,
    PharmacySettings, PharmacySettingsHistory,
)
from .admin_analytics import compute_effective_pharmacy_response_rates_for_ids


class ChatMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatMessage
        fields = ['message_id', 'role', 'content', 'created_at', 'metadata']


class ChatConversationSerializer(serializers.ModelSerializer):
    messages = ChatMessageSerializer(many=True, read_only=True)
    message_count = serializers.IntegerField(source='messages.count', read_only=True)
    
    class Meta:
        model = ChatConversation
        fields = [
            'conversation_id', 'session_id', 'created_at', 'updated_at',
            'status', 'context_metadata', 'messages', 'message_count'
        ]


class MedicineRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = MedicineRequest
        fields = [
            'request_id', 'request_type', 'medicine_names', 'symptoms',
            'location_latitude', 'location_longitude', 'location_address',
            'location_suburb', 'status', 'created_at', 'expires_at',
            'prescription_review_snapshot',
            'prescription_image',
        ]


class PharmacyListSerializer(serializers.ListSerializer):
    """
    When serializing many pharmacies, compute response_rate in one pass: patient requests
    responded to ÷ patient requests in range (see admin_analytics), not N DB queries.
    """

    def to_representation(self, data):
        iterable = list(data) if data is not None else []
        ids = [getattr(p, 'pharmacy_id', None) for p in iterable]
        ids = [i for i in ids if i]
        rates = compute_effective_pharmacy_response_rates_for_ids(ids) if ids else {}
        self.context['response_rate_by_pharmacy_id'] = rates
        return super().to_representation(data)


class PharmacySerializer(serializers.ModelSerializer):
    """response_rate = patient requests this pharmacy responded to ÷ patient requests in range (computed)."""

    response_rate = serializers.SerializerMethodField()

    class Meta:
        model = Pharmacy
        list_serializer_class = PharmacyListSerializer
        fields = [
            'pharmacy_id', 'name', 'address', 'latitude', 'longitude',
            'phone', 'email', 'tax_number', 'whatsapp', 'website', 'description',
            'is_active',
            'rating', 'rating_count', 'response_rate',
            'pharmacy_type', 'verification_status', 'last_inventory_sync_at',
            'created_at', 'updated_at',
        ]

    def get_response_rate(self, obj):
        m = self.context.get('response_rate_by_pharmacy_id')
        if m is not None:
            return m.get(str(obj.pharmacy_id), float(obj.response_rate or 100))
        m = compute_effective_pharmacy_response_rates_for_ids([obj.pharmacy_id])
        return m.get(str(obj.pharmacy_id), float(obj.response_rate or 100))


class PharmacistSerializer(serializers.ModelSerializer):
    pharmacy = PharmacySerializer(read_only=True)
    pharmacy_id = serializers.CharField(write_only=True, required=False)
    full_name = serializers.CharField(read_only=True)
    
    class Meta:
        model = Pharmacist
        fields = [
            'pharmacist_id', 'pharmacy', 'pharmacy_id', 'first_name', 'last_name',
            'full_name', 'display_name', 'email', 'phone', 'license_number', 'is_active', 'created_at'
        ]


class PharmacyResponseSerializer(serializers.ModelSerializer):
    pharmacist_name = serializers.SerializerMethodField()
    pharmacy_name = serializers.SerializerMethodField()
    pharmacy_id = serializers.SerializerMethodField()
    pharmacist_id = serializers.SerializerMethodField()
    pharmacy_contact = serializers.SerializerMethodField()
    pharmacist_contact = serializers.SerializerMethodField()

    class Meta:
        model = PharmacyResponse
        fields = [
            'response_id', 'pharmacy_id', 'pharmacy_name', 'pharmacist_id', 'pharmacist_name',
            'pharmacy_contact',
            'pharmacist_contact',
            'medicine_available', 'price', 'quantity', 'expiry_date', 'preparation_time', 'distance_km',
            'estimated_travel_time', 'alternative_medicines', 'medicine_responses', 'notes', 'submitted_at'
        ]
    
    def get_pharmacist_name(self, obj):
        if obj.pharmacist:
            return obj.pharmacist.full_name
        return obj.pharmacist_name or 'Unknown'
    
    def get_pharmacy_name(self, obj):
        if obj.pharmacy:
            return obj.pharmacy.name
        return obj.pharmacy_name or 'Unknown'
    
    def get_pharmacy_id(self, obj):
        return obj.pharmacy_id  # Uses the property from model
    
    def get_pharmacist_id(self, obj):
        return obj.pharmacist_id  # Uses the property from model

    def get_pharmacy_contact(self, obj):
        """Branch contact for patients (quotes / ranked pharmacy rows)."""
        ph = getattr(obj, 'pharmacy', None)
        if ph is None:
            return None

        def _s(val, maxlen):
            if val is None:
                return None
            if isinstance(val, (dict, list, tuple, set)):
                return None
            x = str(val).strip()
            if not x:
                return None
            return x[:maxlen] if maxlen else x

        return {
            'address': _s(ph.address, 500),
            'phone': _s(ph.phone, 20),
            'email': _s(ph.email, 254),
            'whatsapp': _s(getattr(ph, 'whatsapp', None), 40),
            'website': _s(ph.website, 512),
        }

    def get_pharmacist_contact(self, obj):
        """Respondent pharmacist direct contact when present (fallback if branch fields are blank)."""
        pf = getattr(obj, 'pharmacist', None)
        if pf is None:
            return None
        name = (pf.display_name or '').strip()
        if not name:
            name = pf.full_name
        body = {'name': name}
        if (pf.phone or '').strip():
            body['phone'] = pf.phone.strip()
        if (pf.email or '').strip():
            body['email'] = pf.email.strip()
        return body


class ChatRequestSerializer(serializers.Serializer):
    """Serializer for chat API requests"""

    prescription_image_only = serializers.BooleanField(
        required=False,
        default=False,
        help_text='Broadcast prescription image for pharmacist manual read without an OCR-derived medicine list.',
    )
    ocr_failed = serializers.BooleanField(
        required=False,
        default=False,
        help_text='Client indicates prior OCR failure; use with prescription_image_only and location.',
    )
    message = serializers.CharField(
        required=False,
        allow_blank=True,
        default='',
        help_text='User message body; omit when prescription_image_only (server stubs a synthetic line).',
    )
    session_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    conversation_id = serializers.UUIDField(required=False, allow_null=True)
    location_latitude = serializers.FloatField(required=False, allow_null=True)
    location_longitude = serializers.FloatField(required=False, allow_null=True)
    location_address = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    location_suburb = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    language = serializers.CharField(required=False, allow_blank=True, allow_null=True,
        help_text="Preferred language: 'en' (English), 'sn' (Shona), 'nd' (Ndebele)")
    selected_medicines = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        allow_empty=True,
        help_text="Medicines patient explicitly selected (e.g., from symptom flow)"
    )
    suggested_medicines = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        allow_empty=True,
        help_text="Medicines suggested by AI (frontend may echo this when sending location)"
    )
    medicines = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        allow_empty=True,
        help_text=(
            "Full prescription/interaction list from the client. "
            "Use when sending location/confirm so the broadcast matches OCR (same key many UIs already send)."
        ),
    )
    start_new_search = serializers.BooleanField(
        required=False,
        default=False,
        help_text="When true, creates a new session - user sees only results for this search (no previous results)"
    )
    notify_patient_request_email = serializers.BooleanField(
        required=False,
        default=True,
        help_text="When true (default), send patient request confirmation email if addresses are available.",
    )
    patient_request_email = serializers.EmailField(
        required=False,
        allow_blank=True,
        help_text="Optional extra recipient for the patient request email (e.g. from client profile).",
    )

    def validate(self, attrs):
        stripped = str(attrs.get('message') or '').strip()
        if not stripped:
            if attrs.get('prescription_image_only'):
                attrs['message'] = '[Prescription image: broadcast to pharmacies for manual review]'
            else:
                raise serializers.ValidationError(
                    {'message': 'Message is required unless prescription_image_only is true.'},
                )
        return attrs


class ChatResponseSerializer(serializers.Serializer):
    """Serializer for chat API responses"""
    response = serializers.CharField()
    conversation_id = serializers.UUIDField()
    message_id = serializers.UUIDField()
    intent = serializers.CharField()
    requires_location = serializers.BooleanField()
    suggested_medicines = serializers.ListField(child=serializers.CharField())
    medicine_request_id = serializers.UUIDField(required=False, allow_null=True)


class PharmacyRegistrationSerializer(serializers.Serializer):
    """Serializer for pharmacy registration"""
    pharmacy_id = serializers.CharField(required=True, min_length=3, help_text="Unique pharmacy identifier")
    name = serializers.CharField(required=True, max_length=255)
    address = serializers.CharField(required=True, max_length=500)
    latitude = serializers.FloatField(required=False, allow_null=True)
    longitude = serializers.FloatField(required=False, allow_null=True)
    phone = serializers.CharField(required=False, allow_blank=True, max_length=20)
    email = serializers.EmailField(required=False, allow_blank=True)


class PharmacistRegistrationSerializer(serializers.Serializer):
    """Serializer for pharmacist registration"""
    pharmacy_id = serializers.CharField(required=True, help_text="ID of the pharmacy this pharmacist belongs to")
    first_name = serializers.CharField(required=True, max_length=100)
    last_name = serializers.CharField(required=True, max_length=100)
    email = serializers.EmailField(required=True)
    phone = serializers.CharField(required=False, allow_blank=True, max_length=20)
    license_number = serializers.CharField(required=False, allow_blank=True, max_length=100)
    # User account creation
    username = serializers.CharField(required=True, max_length=150, help_text="Django username for authentication")
    password = serializers.CharField(required=True, min_length=8, write_only=True, help_text="Password for authentication")


class PharmacistLoginSerializer(serializers.Serializer):
    """Serializer for pharmacist login"""
    email = serializers.EmailField(required=True)
    password = serializers.CharField(required=True, write_only=True)


class AdminLoginSerializer(serializers.Serializer):
    """Staff/superuser login for SPA admin dashboard (Django session)."""
    username = serializers.CharField(required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    password = serializers.CharField(required=True, write_only=True)

    def validate(self, attrs):
        if not (attrs.get('username') or '').strip() and not (attrs.get('email') or '').strip():
            raise serializers.ValidationError('Provide username or email.')
        return attrs


class EmailOtpVerifySerializer(serializers.Serializer):
    """Complete LOGIN_EMAIL_2FA step after password check."""
    otp_challenge = serializers.CharField(min_length=8, max_length=64)
    code = serializers.CharField(min_length=4, max_length=8, trim_whitespace=True)


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()
    user_type = serializers.ChoiceField(choices=['admin', 'patient', 'pharmacist'], required=False)
    account_type = serializers.ChoiceField(choices=['admin', 'patient', 'pharmacist'], required=False)

    def validate(self, attrs):
        t = attrs.get('user_type') or attrs.get('account_type')
        if not t:
            raise serializers.ValidationError({'user_type': 'This field is required.'})
        attrs['account_type'] = t
        return attrs


class PasswordResetConfirmSerializer(serializers.Serializer):
    email = serializers.EmailField()
    user_type = serializers.ChoiceField(choices=['admin', 'patient', 'pharmacist'], required=False)
    account_type = serializers.ChoiceField(choices=['admin', 'patient', 'pharmacist'], required=False)
    code = serializers.CharField(min_length=4, max_length=8, trim_whitespace=True)
    new_password = serializers.CharField(min_length=8, max_length=128, write_only=True)

    def validate(self, attrs):
        t = attrs.get('user_type') or attrs.get('account_type')
        if not t:
            raise serializers.ValidationError({'user_type': 'This field is required.'})
        attrs['account_type'] = t
        return attrs


class MfaLoginCompleteSerializer(serializers.Serializer):
    """SPA second step after password (TOTP device or email OTP)."""
    user_type = serializers.ChoiceField(choices=['admin', 'patient', 'pharmacist'])
    mfa_token = serializers.CharField(required=False, allow_blank=True, min_length=8, max_length=64)
    mfa_challenge_token = serializers.CharField(required=False, allow_blank=True, min_length=8, max_length=64)
    otp_code = serializers.CharField(min_length=4, max_length=12, trim_whitespace=True)

    def validate(self, attrs):
        tok = (attrs.get('mfa_token') or attrs.get('mfa_challenge_token') or '').strip()
        if not tok:
            raise serializers.ValidationError('mfa_token or mfa_challenge_token is required.')
        attrs['mfa_token'] = tok
        return attrs


class AdminReportGenerateSerializer(serializers.Serializer):
    """Serializer for AI-generated admin PDF narrative."""

    report_type = serializers.CharField(required=False, allow_blank=True, default='dashboard_summary')
    title = serializers.CharField(required=False, allow_blank=True, default='Platform Report')
    timeframe = serializers.CharField(required=False, allow_blank=True, default='last_30_days')
    dashboard_snapshot = serializers.JSONField(required=True)
    custom_instruction = serializers.CharField(required=False, allow_blank=True, default='')
    tone = serializers.ChoiceField(
        choices=['executive', 'technical', 'neutral'],
        required=False,
        default='executive',
    )


class PharmacySettingsSerializer(serializers.ModelSerializer):
    pharmacy_id = serializers.CharField(source='pharmacy.pharmacy_id', read_only=True)
    pharmacy_name = serializers.CharField(source='pharmacy.name', read_only=True)
    license_number = serializers.CharField(source='pharmacist.license_number', read_only=True)
    address = serializers.CharField(source='pharmacy.address', read_only=True)
    phone = serializers.CharField(source='pharmacy.phone', read_only=True)
    email = serializers.CharField(source='pharmacy.email', read_only=True)
    opening_hours = serializers.DictField(required=False)
    ui_default_filters = serializers.DictField(required=False)

    class Meta:
        model = PharmacySettings
        fields = [
            'pharmacy_id', 'pharmacy_name', 'branch_name', 'license_number', 'address', 'city', 'geo_region', 'phone', 'email',
            'opening_hours', 'timezone', 'holiday_mode', 'auto_accept_reservations', 'max_reservation_window_minutes',
            'low_stock_threshold_default', 'out_of_stock_behavior', 'auto_substitute_enabled',
            'notify_new_request', 'notify_low_stock', 'notify_reservation_expiry',
            'notify_channel_sms', 'notify_channel_email', 'notify_channel_in_app',
            'preferred_profile',
            'disclaimer_visible', 'prescription_enforcement', 'audit_logging_enabled',
            'ui_dark_mode', 'ui_table_density', 'ui_default_page_size', 'ui_default_filters',
            'version', 'updated_by', 'updated_at',
        ]


class PharmacySettingsPatchSerializer(serializers.Serializer):
    branch_name = serializers.CharField(required=False, allow_blank=True, max_length=255)
    city = serializers.CharField(required=False, allow_blank=True, max_length=120)
    geo_region = serializers.CharField(required=False, allow_blank=True, max_length=120)
    opening_hours = serializers.DictField(required=False)
    timezone = serializers.CharField(required=False, allow_blank=True, max_length=64)
    holiday_mode = serializers.BooleanField(required=False)
    auto_accept_reservations = serializers.BooleanField(required=False)
    max_reservation_window_minutes = serializers.IntegerField(required=False, min_value=10, max_value=10080)
    low_stock_threshold_default = serializers.IntegerField(required=False, min_value=0, max_value=100000)
    out_of_stock_behavior = serializers.ChoiceField(required=False, choices=['hide', 'allow_backorder', 'notify_only'])
    auto_substitute_enabled = serializers.BooleanField(required=False)
    notify_new_request = serializers.BooleanField(required=False)
    notify_low_stock = serializers.BooleanField(required=False)
    notify_reservation_expiry = serializers.BooleanField(required=False)
    notify_channel_sms = serializers.BooleanField(required=False)
    notify_channel_email = serializers.BooleanField(required=False)
    notify_channel_in_app = serializers.BooleanField(required=False)
    preferred_profile = serializers.CharField(required=False, allow_blank=True, max_length=64)
    disclaimer_visible = serializers.BooleanField(required=False)
    prescription_enforcement = serializers.BooleanField(required=False)
    audit_logging_enabled = serializers.BooleanField(required=False)
    ui_dark_mode = serializers.BooleanField(required=False)
    ui_table_density = serializers.ChoiceField(required=False, choices=['compact', 'comfortable'])
    ui_default_page_size = serializers.IntegerField(required=False, min_value=5, max_value=200)
    ui_default_filters = serializers.DictField(required=False)
    version = serializers.IntegerField(required=False, min_value=1)
    updated_at = serializers.DateTimeField(required=False)


class PharmacySettingsHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = PharmacySettingsHistory
        fields = ['history_id', 'changed_by', 'action', 'payload', 'created_at']
