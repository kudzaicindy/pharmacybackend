from django.db import models
from django.contrib.auth.models import User
from django.core.validators import MinLengthValidator
import uuid


class ChatConversation(models.Model):
    """Stores conversation sessions between patients and AI chatbot"""
    conversation_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    session_id = models.CharField(max_length=255, unique=True, help_text="Session identifier for anonymous users")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    status = models.CharField(
        max_length=20,
        choices=[
            ('active', 'Active'),
            ('completed', 'Completed'),
            ('abandoned', 'Abandoned')
        ],
        default='active'
    )
    context_metadata = models.JSONField(default=dict, help_text="Stores conversation context, extracted entities, etc.")
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"Conversation {self.conversation_id} - {self.status}"


class ChatMessage(models.Model):
    """Individual messages within a conversation"""
    message_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(ChatConversation, on_delete=models.CASCADE, related_name='messages')
    role = models.CharField(
        max_length=20,
        choices=[
            ('user', 'User'),
            ('assistant', 'Assistant'),
            ('system', 'System')
        ]
    )
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    metadata = models.JSONField(
        default=dict,
        help_text="Stores extracted entities, confidence scores, intent classification, etc."
    )
    
    class Meta:
        ordering = ['created_at']
    
    def __str__(self):
        return f"{self.role}: {self.content[:50]}..."


class MedicineRequest(models.Model):
    """Medicine requests created through chatbot interactions"""
    request_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(ChatConversation, on_delete=models.CASCADE, related_name='medicine_requests')
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    
    # Request details
    request_type = models.CharField(
        max_length=20,
        choices=[
            ('symptom', 'Symptom Description'),
            ('prescription', 'Prescription Upload'),
            ('direct', 'Direct Medicine Search')
        ]
    )
    medicine_names = models.JSONField(default=list, help_text="List of medicine names requested")
    symptoms = models.TextField(blank=True, help_text="Symptom description if request_type is 'symptom'")
    
    # Location
    location_latitude = models.FloatField(null=True, blank=True)
    location_longitude = models.FloatField(null=True, blank=True)
    location_address = models.CharField(max_length=500, blank=True)
    location_suburb = models.CharField(max_length=100, blank=True)
    
    # Status - per guide lifecycle
    status = models.CharField(
        max_length=20,
        choices=[
            ('created', 'Created'),
            ('validated', 'Validated'),
            ('broadcasting', 'Broadcasting'),
            ('awaiting_responses', 'Awaiting Responses'),
            ('partial', 'Partial'),
            ('timeout', 'Timeout'),
            ('responses_received', 'Responses Received'),
            ('ranking', 'Ranking'),
            ('completed', 'Completed'),
            ('expired', 'Expired')
        ],
        default='created'
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"Request {self.request_id} - {self.request_type}"


class MedicineRequestRankingSnapshot(models.Model):
    """
    Audit trail of ranked pharmacy rows actually returned to the patient.
    Populated when the patient hits GET .../ranked/ or the patient portal request detail.
    """
    snapshot_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(
        MedicineRequest,
        on_delete=models.CASCADE,
        related_name='ranking_snapshots',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    source = models.CharField(
        max_length=32,
        db_index=True,
        help_text="ranked_api | patient_portal | chat_assistant",
    )
    limit_applied = models.PositiveSmallIntegerField(default=0)
    ranked_items = models.JSONField(
        default=list,
        help_text="Ordered list as returned to the client (rank, scores, pharmacy ids, flags).",
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['request', '-created_at']),
        ]

    def __str__(self):
        return f"Ranking snapshot {self.snapshot_id} for request {self.request_id}"


class Pharmacy(models.Model):
    """Pharmacy information"""
    pharmacy_id = models.CharField(max_length=255, unique=True, primary_key=True, validators=[MinLengthValidator(3)])
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=500)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    is_active = models.BooleanField(default=True)
    rating = models.FloatField(default=0, blank=True, help_text="Average rating 0-5")
    rating_count = models.IntegerField(default=0, blank=True, help_text="Number of ratings received")
    response_rate = models.FloatField(default=100, blank=True, help_text="Response rate percentage (0-100)")
    pharmacy_type = models.CharField(
        max_length=50,
        blank=True,
        default='',
        help_text="e.g. retail, chain, hospital (admin registry)",
    )
    verification_status = models.CharField(
        max_length=20,
        choices=[
            ('verified', 'Verified'),
            ('pending_review', 'Pending review'),
            ('suspended', 'Suspended'),
        ],
        default='verified',
        help_text="Registry verification pill; inactive pharmacies still show as suspended in API.",
    )
    last_inventory_sync_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Optional official inventory sync timestamp (otherwise max inventory.updated_at is used).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['name']
        verbose_name_plural = 'Pharmacies'
    
    def __str__(self):
        return self.name


class Pharmacist(models.Model):
    """Pharmacist information - each pharmacy can have multiple pharmacists"""
    pharmacist_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    pharmacy = models.ForeignKey(Pharmacy, on_delete=models.CASCADE, related_name='pharmacists')
    user = models.OneToOneField(User, on_delete=models.CASCADE, null=True, blank=True, help_text="Link to Django User for authentication")
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=20, blank=True)
    license_number = models.CharField(max_length=100, blank=True, help_text="Pharmacist license/registration number")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['last_name', 'first_name']
        unique_together = ['pharmacy', 'email']
    
    def __str__(self):
        return f"{self.first_name} {self.last_name} - {self.pharmacy.name}"
    
    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"


class PharmacyResponse(models.Model):
    """Responses from pharmacists to medicine requests"""
    response_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(MedicineRequest, on_delete=models.CASCADE, related_name='pharmacy_responses')
    pharmacy = models.ForeignKey(Pharmacy, on_delete=models.CASCADE, related_name='responses', null=True, blank=True)
    pharmacist = models.ForeignKey(Pharmacist, on_delete=models.SET_NULL, related_name='responses', null=True, blank=True)
    # Legacy fields for backward compatibility (when pharmacy/pharmacist FK is not set)
    pharmacy_name = models.CharField(max_length=255, blank=True, help_text="Legacy field - use pharmacy FK when possible")
    pharmacist_name = models.CharField(max_length=255, blank=True, help_text="Legacy field - use pharmacist FK when possible")
    
    # Response details
    medicine_available = models.BooleanField(default=False)
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    preparation_time = models.IntegerField(help_text="Time in minutes to prepare medicine", default=0)
    distance_km = models.FloatField(null=True, blank=True, help_text="Distance from patient location")
    estimated_travel_time = models.IntegerField(null=True, blank=True, help_text="Estimated travel time in minutes")
    
    # Per-medicine responses (for multi-medicine requests)
    # Format: [{"medicine": "name", "available": true/false, "price": "2.50", "quantity": 100, "expiry": "2026-08", "alternative": "alt_name"}]
    medicine_responses = models.JSONField(default=list, blank=True, help_text="Per-medicine availability, prices, quantity, expiry, alternatives")
    
    quantity = models.IntegerField(null=True, blank=True, help_text="Total quantity (e.g. capsules)")
    expiry_date = models.DateField(null=True, blank=True, help_text="Medicine expiry date")
    
    # Alternatives
    alternative_medicines = models.JSONField(default=list, help_text="List of alternative medicines suggested (legacy - use medicine_responses)")
    notes = models.TextField(blank=True)
    
    submitted_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['submitted_at']
    
    @property
    def pharmacy_id(self):
        """Get pharmacy_id from ForeignKey or legacy field"""
        if self.pharmacy:
            return self.pharmacy.pharmacy_id
        return None
    
    @property
    def pharmacist_id(self):
        """Get pharmacist_id from ForeignKey"""
        if self.pharmacist:
            return self.pharmacist.pharmacist_id
        return None
    
    def __str__(self):
        pharmacist_name = self.pharmacist.full_name if self.pharmacist else self.pharmacist_name or "Unknown"
        pharmacy_name = self.pharmacy.name if self.pharmacy else self.pharmacy_name or "Unknown"
        return f"Response from {pharmacist_name} ({pharmacy_name}) for {self.request.request_id}"


class PharmacistDecline(models.Model):
    """Tracks when a pharmacist declines to respond to a request"""
    request = models.ForeignKey(MedicineRequest, on_delete=models.CASCADE, related_name='pharmacist_declines')
    pharmacist = models.ForeignKey(Pharmacist, on_delete=models.CASCADE, related_name='declined_requests')
    declined_at = models.DateTimeField(auto_now_add=True)
    reason = models.CharField(max_length=255, blank=True)

    class Meta:
        unique_together = ['request', 'pharmacist']
        ordering = ['-declined_at']


class PharmacyInventory(models.Model):
    """Pharmacy medicine inventory - stock levels per medicine"""
    pharmacy = models.ForeignKey(Pharmacy, on_delete=models.CASCADE, related_name='inventory')
    medicine_name = models.CharField(max_length=255)
    quantity = models.IntegerField(default=0, help_text="Units in stock")
    reserved_quantity = models.IntegerField(default=0, help_text="Quantity reserved by patients (locks stock)")
    low_stock_threshold = models.IntegerField(default=10, help_text="Alert when quantity drops below this")
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="Price per unit for ranking")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ['pharmacy', 'medicine_name']
        ordering = ['medicine_name']
        verbose_name_plural = 'Pharmacy inventory'

    @property
    def available_quantity(self):
        """Stock available for sale (quantity minus reserved)."""
        return max(0, self.quantity - self.reserved_quantity)

    def __str__(self):
        status = "low" if self.quantity < self.low_stock_threshold else "ok"
        return f"{self.medicine_name} x{self.quantity} ({status})"


class Reservation(models.Model):
    """Patient reservation: locks pharmacy stock for 2 hours until pickup or expiry."""
    reservation_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    pharmacy = models.ForeignKey(Pharmacy, on_delete=models.CASCADE, related_name='reservations')
    # Anonymous: use session_id/conversation; optional user later
    conversation = models.ForeignKey(
        ChatConversation, on_delete=models.CASCADE, related_name='reservations', null=True, blank=True
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    session_id = models.CharField(max_length=255, blank=True, help_text="For anonymous reservations")
    patient_name = models.CharField(max_length=255, blank=True)
    patient_phone = models.CharField(max_length=50, blank=True)

    medicine_name = models.CharField(max_length=255)
    quantity = models.IntegerField(default=1)
    price_at_reservation = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Price at time of reservation"
    )

    status = models.CharField(
        max_length=20,
        choices=[
            ('pending', 'Pending'),
            ('confirmed', 'Confirmed'),
            ('picked_up', 'Picked Up'),
            ('expired', 'Expired'),
            ('cancelled', 'Cancelled'),
        ],
        default='pending',
    )
    reserved_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(help_text="Reservation expires 2 hours after creation")
    confirmed_at = models.DateTimeField(null=True, blank=True)
    picked_up_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-reserved_at']

    def __str__(self):
        return f"{self.medicine_name} x{self.quantity} @ {self.pharmacy.name} ({self.status})"


class PharmacyRating(models.Model):
    """UC-P12: Patient ratings for pharmacy visits (anonymous or attributed)"""
    pharmacy = models.ForeignKey(Pharmacy, on_delete=models.CASCADE, related_name='ratings')
    response = models.ForeignKey(
        'PharmacyResponse', on_delete=models.SET_NULL, null=True, blank=True,
        help_text="Optional: link to the response/visit being rated"
    )
    rating = models.IntegerField(help_text="1-5 stars")
    notes = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.pharmacy.name} - {self.rating}/5"


class AdminAuditLog(models.Model):
    """Admin action trail for SPA control center (optional compliance / debugging)."""
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='admin_audit_logs')
    username = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=120)
    target_type = models.CharField(max_length=80, blank=True)
    target_id = models.CharField(max_length=500, blank=True)
    success = models.BooleanField(default=True)
    detail = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.username} {self.action}"


# ---- Patient dashboard (MediConnect) ----


class PatientProfile(models.Model):
    """Patient profile and preferences, keyed by session (anonymous) or user."""
    session_id = models.CharField(max_length=255, unique=True, null=True, blank=True, db_index=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name='patient_profiles')
    display_name = models.CharField(max_length=150, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    home_area = models.CharField(max_length=200, blank=True, help_text="e.g. Avondale, Harare")
    preferred_language = models.CharField(max_length=10, default='en')
    allergies = models.JSONField(default=list, help_text="e.g. ['Penicillin']")
    conditions = models.JSONField(default=list, help_text="e.g. ['Type 2 Diabetes', 'Hypertension']")
    max_search_radius_km = models.IntegerField(default=10, null=True, blank=True)
    sort_results_by = models.CharField(
        max_length=30, default='best_match',
        choices=[('best_match', 'Best Match (AI)'), ('nearest', 'Nearest First'), ('cheapest', 'Cheapest First')]
    )
    notify_pharmacy_responses = models.BooleanField(default=True)
    notify_request_expiry = models.BooleanField(default=True)
    notify_drug_interactions = models.BooleanField(default=True)
    notify_medibot_followup = models.BooleanField(default=False)
    notification_method = models.CharField(max_length=20, default='in_app')
    share_location_with_pharmacies = models.BooleanField(default=True)
    save_search_history = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return self.display_name or self.email or self.session_id or str(self.pk)


class SavedMedicine(models.Model):
    """Patient's saved medicine shortlist for quick reorder."""
    session_id = models.CharField(max_length=255, db_index=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name='saved_medicines')
    medicine_name = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255, blank=True, help_text="e.g. Paracetamol 500mg")
    last_searched_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = [['session_id', 'medicine_name']]

    def __str__(self):
        return f"{self.medicine_name} ({self.session_id})"


class PatientNotification(models.Model):
    """In-app notifications for patient: responses, alerts, fulfilled, expired."""
    NOTIFICATION_TYPES = [
        ('pharmacy_response', 'Pharmacy Response'),
        ('drug_alert', 'Drug Interaction Alert'),
        ('request_fulfilled', 'Request Fulfilled'),
        ('request_expired', 'Request Expired'),
        ('medibot_followup', 'MediBot Follow-up'),
        ('system', 'System'),
    ]
    session_id = models.CharField(max_length=255, db_index=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name='patient_notifications')
    notification_type = models.CharField(max_length=30, choices=NOTIFICATION_TYPES, default='system')
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    related_request_id = models.UUIDField(null=True, blank=True, db_index=True)
    related_response_id = models.UUIDField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.notification_type}: {self.title[:50]}"
