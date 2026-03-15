from django.urls import path
from . import views

urlpatterns = [
    # Patient endpoints
    path('chat/', views.chat, name='chatbot-chat'),
    path('conversation/<uuid:conversation_id>/', views.get_conversation, name='get-conversation'),
    path('request/<uuid:request_id>/responses/', views.get_pharmacy_responses, name='get-pharmacy-responses'),
    path('request/<uuid:request_id>/ranked/', views.get_ranked_responses, name='get-ranked-responses'),
    path('alternatives/', views.suggest_alternatives, name='suggest-alternatives'),
    path('upload-prescription/', views.upload_prescription, name='upload-prescription'),
    path('check-interactions/', views.check_drug_interactions, name='check-drug-interactions'),
    path('rate-pharmacy/', views.rate_pharmacy, name='rate-pharmacy'),
    path('record-purchase/', views.record_purchase, name='record-purchase'),
    path('reserve/', views.reserve_medicine, name='reserve-medicine'),
    
    # Registration endpoints
    path('register/pharmacy/', views.register_pharmacy, name='register-pharmacy'),
    path('register/pharmacist/', views.register_pharmacist, name='register-pharmacist'),
    path('register/patient/', views.register_patient, name='register-patient'),
    path('pharmacies/', views.list_pharmacies, name='list-pharmacies'),
    path('pharmacists/', views.list_pharmacists, name='list-pharmacists'),
    path('pharmacists/<str:pharmacy_id>/', views.list_pharmacists, name='list-pharmacists-by-pharmacy'),
    
    # Pharmacist endpoints
    path('pharmacist/login/', views.pharmacist_login, name='pharmacist-login'),
    path('pharmacist/<uuid:pharmacist_id>/', views.get_pharmacist_profile, name='get-pharmacist-profile'),
    path('pharmacist/requests/', views.get_pharmacist_requests, name='get-pharmacist-requests'),
    path('pharmacist/response/<uuid:request_id>/', views.submit_pharmacy_response, name='submit-pharmacy-response'),
    path('pharmacist/decline/<uuid:request_id>/', views.decline_pharmacy_request, name='decline-pharmacy-request'),
    path('pharmacist/inventory/', views.pharmacist_inventory, name='pharmacist-inventory'),
    path('pharmacist/reservations/', views.pharmacist_reservations_list, name='pharmacist-reservations-list'),
    path('pharmacist/reservations/<uuid:reservation_id>/confirm/', views.pharmacist_reservation_confirm, name='pharmacist-reservation-confirm'),
    path('pharmacist/reservations/<uuid:reservation_id>/complete/', views.pharmacist_reservation_complete, name='pharmacist-reservation-complete'),
    
    # Legacy pharmacy endpoints (backward compatibility)
    path('pharmacy/requests/', views.get_pharmacist_requests, name='get-pharmacy-requests'),
    path('pharmacy/response/<uuid:request_id>/', views.submit_pharmacy_response, name='submit-pharmacy-response-legacy'),

    # Patient dashboard (MediConnect)
    path('patient/dashboard/stats/', views.patient_dashboard_stats, name='patient-dashboard-stats'),
    path('patient/requests/', views.patient_my_requests, name='patient-my-requests'),
    path('patient/requests/<uuid:request_id>/', views.patient_request_detail, name='patient-request-detail'),
    path('patient/saved-medicines/', views.patient_saved_medicines, name='patient-saved-medicines'),
    path('patient/saved-medicines/remove/', views.patient_saved_medicine_remove, name='patient-saved-medicines-remove'),
    path('patient/saved-medicines/remove/<str:medicine_name>/', views.patient_saved_medicine_remove, name='patient-saved-medicines-remove-by-name'),
    path('patient/notifications/', views.patient_notifications_list, name='patient-notifications-list'),
    path('patient/notifications/mark-read/', views.patient_notifications_mark_read, name='patient-notifications-mark-read'),
    path('patient/profile/', views.patient_profile, name='patient-profile'),
]
