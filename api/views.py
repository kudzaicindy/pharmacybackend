from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status


@api_view(['GET'])
def api_root(request):
    """API root endpoint"""
    return Response({
        'message': 'Pharmacy API',
        'endpoints': {
            'health': '/health/',
            'root': '/',
        }
    })
