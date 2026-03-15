from django.contrib import admin
from .models import ChatConversation, ChatMessage, MedicineRequest, PharmacyResponse, Pharmacy, Pharmacist, PharmacyRating


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


@admin.register(MedicineRequest)
class MedicineRequestAdmin(admin.ModelAdmin):
    list_display = ['request_id', 'request_type', 'status', 'created_at']
    list_filter = ['request_type', 'status', 'created_at']
    search_fields = ['medicine_names', 'symptoms']


@admin.register(PharmacyResponse)
class PharmacyResponseAdmin(admin.ModelAdmin):
    list_display = ['response_id', 'pharmacy_name', 'medicine_available', 'price', 'submitted_at']
    list_filter = ['medicine_available', 'submitted_at']
    search_fields = ['pharmacy_name', 'notes']


@admin.register(Pharmacy)
class PharmacyAdmin(admin.ModelAdmin):
    list_display = ['pharmacy_id', 'name', 'address', 'phone', 'email', 'is_active', 'created_at']
    list_filter = ['is_active', 'created_at']
    search_fields = ['name', 'pharmacy_id', 'address', 'email']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(PharmacyRating)
class PharmacyRatingAdmin(admin.ModelAdmin):
    list_display = ['pharmacy', 'rating', 'created_at']
    list_filter = ['rating', 'created_at']
    search_fields = ['pharmacy__name']


@admin.register(Pharmacist)
class PharmacistAdmin(admin.ModelAdmin):
    list_display = ['pharmacist_id', 'full_name', 'pharmacy', 'email', 'license_number', 'is_active', 'created_at']
    list_filter = ['is_active', 'pharmacy', 'created_at']
    search_fields = ['first_name', 'last_name', 'email', 'license_number', 'pharmacy__name']
    readonly_fields = ['pharmacist_id', 'created_at', 'updated_at']
    raw_id_fields = ['pharmacy', 'user']
