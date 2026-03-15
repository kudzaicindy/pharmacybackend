"""
URL configuration for pharmacybackend project.
"""
from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse
from datetime import datetime

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
    path('api/chatbot/', include('chatbot.urls')),
]
