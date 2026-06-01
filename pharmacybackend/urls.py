"""
URL configuration for pharmacybackend project.
"""
from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse
from datetime import datetime

from chatbot.views import admin_dashboard_widgets

def health_check(request):
    """Health check endpoint"""
    return JsonResponse({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'message': 'Pharmacy Backend API is running'
    })

def root(request):
    """Root endpoint"""
    return JsonResponse({
        'message': 'Pharmacy Backend API',
        'status': 'success',
        'version': '1.0.0'
    })

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', root, name='root'),
    path('health/', health_check, name='health'),
    path('api/', include('api.urls')),
    # More specific than include('chatbot.urls') — ensures this route resolves even if chatbot.urls is stale.
    path(
        'api/chatbot/admin/dashboard/widgets/',
        admin_dashboard_widgets,
        name='admin-dashboard-widgets',
    ),
    path('api/chatbot/', include('chatbot.urls')),
]
