from channels.generic.websocket import AsyncJsonWebsocketConsumer


class ChatbotConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.request_id = self.scope["url_route"]["kwargs"]["request_id"]
        self.group_name = f"chat_request_{self.request_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def chatbot_update(self, event):
        data = event.get("data", {})
        await self.send_json(data)

