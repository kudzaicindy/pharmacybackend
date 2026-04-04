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
    # SPA admin auth (Django session; set VITE_ADMIN_LOGIN_PATH to /api/chatbot/admin/login)
    path('admin/login/', views.admin_login, name='admin-login'),
    path('admin/logout/', views.admin_logout, name='admin-logout'),
    path('admin/me/', views.admin_me, name='admin-me'),
    path('admin/dashboard/data/', views.admin_dashboard_data, name='admin-dashboard-data'),
    path('admin/control/center/', views.admin_control_center, name='admin-control-center'),
    path('admin/requests/<uuid:request_id>/', views.admin_request_detail, name='admin-request-detail'),
    # Admin access to patient dashboard data (by session_id)
    path('admin/patients/<str:session_id>/overview/', views.admin_patient_overview, name='admin-patient-overview'),
    path('admin/patients/<str:session_id>/profile/', views.admin_update_patient_profile, name='admin-update-patient-profile'),
    path('admin/patients/<str:session_id>/saved-medicines/', views.admin_patient_saved_medicines, name='admin-patient-saved-medicines'),
    path('admin/patients/<str:session_id>/saved-medicines/clear/', views.admin_clear_patient_saved_medicines, name='admin-clear-patient-saved-medicines'),
    path('admin/patients/<str:session_id>/notifications/', views.admin_patient_notifications, name='admin-patient-notifications'),
    path('admin/patients/<str:session_id>/notifications/clear/', views.admin_clear_patient_notifications, name='admin-clear-patient-notifications'),
    path('admin/pharmacies/', views.admin_create_pharmacy, name='admin-create-pharmacy'),
    path('admin/pharmacies/export/', views.admin_export_pharmacies_csv, name='admin-pharmacies-export'),
    path('admin/pharmacies/<str:pharmacy_id>/', views.admin_update_pharmacy, name='admin-update-pharmacy'),
    path('admin/pharmacies/<str:pharmacy_id>/delete/', views.admin_delete_pharmacy, name='admin-delete-pharmacy'),
    path('admin/pharmacists/', views.admin_create_pharmacist, name='admin-create-pharmacist'),
    path('admin/pharmacists/<uuid:pharmacist_id>/', views.admin_update_pharmacist, name='admin-update-pharmacist'),
    path('admin/pharmacists/<uuid:pharmacist_id>/delete/', views.admin_delete_pharmacist, name='admin-delete-pharmacist'),
    path('admin/requests/<uuid:request_id>/status/', views.admin_update_request_status, name='admin-update-request-status'),
    path('admin/reservations/<uuid:reservation_id>/status/', views.admin_update_reservation_status, name='admin-update-reservation-status'),
    path('admin/analytics/search-volume/', views.admin_search_analytics, name='admin-search-analytics'),
    path('admin/audit/logs/', views.admin_audit_logs, name='admin-audit-logs'),
    path('admin/users/', views.admin_users_list, name='admin-users-list'),
    path('admin/patients-list/', views.admin_patients_list, name='admin-patients-list'),
    path('admin/chatbot/logs/<uuid:conversation_id>/', views.admin_chatbot_conversation_logs, name='admin-chatbot-conversation-logs'),
    path('admin/chatbot/logs/', views.admin_chatbot_logs, name='admin-chatbot-logs'),

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
