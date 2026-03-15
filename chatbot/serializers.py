from rest_framework import serializers
from .models import ChatConversation, ChatMessage, MedicineRequest, PharmacyResponse, Pharmacy, Pharmacist


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
            'location_suburb', 'status', 'created_at', 'expires_at'
        ]


class PharmacySerializer(serializers.ModelSerializer):
    class Meta:
        model = Pharmacy
        fields = [
            'pharmacy_id', 'name', 'address', 'latitude', 'longitude',
            'phone', 'email', 'is_active', 'created_at'
        ]


class PharmacistSerializer(serializers.ModelSerializer):
    pharmacy = PharmacySerializer(read_only=True)
    pharmacy_id = serializers.CharField(write_only=True, required=False)
    full_name = serializers.CharField(read_only=True)
    
    class Meta:
        model = Pharmacist
        fields = [
            'pharmacist_id', 'pharmacy', 'pharmacy_id', 'first_name', 'last_name',
            'full_name', 'email', 'phone', 'license_number', 'is_active', 'created_at'
        ]


class PharmacyResponseSerializer(serializers.ModelSerializer):
    pharmacist_name = serializers.SerializerMethodField()
    pharmacy_name = serializers.SerializerMethodField()
    pharmacy_id = serializers.SerializerMethodField()
    pharmacist_id = serializers.SerializerMethodField()
    
    class Meta:
        model = PharmacyResponse
        fields = [
            'response_id', 'pharmacy_id', 'pharmacy_name', 'pharmacist_id', 'pharmacist_name',
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


class ChatRequestSerializer(serializers.Serializer):
    """Serializer for chat API requests"""
    message = serializers.CharField(required=True)
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
    start_new_search = serializers.BooleanField(
        required=False,
        default=False,
        help_text="When true, creates a new session - user sees only results for this search (no previous results)"
    )


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
