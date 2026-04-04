from django.urls import re_path

from . import consumers

websocket_urlpatterns = [
    re_path(r"ws/chatbot/(?P<request_id>[^/]+)/$", consumers.ChatbotConsumer.as_asgi()),
]

