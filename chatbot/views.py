from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone
from django.db.models import F
from datetime import timedelta
import os
import uuid

from .models import (
    ChatConversation, ChatMessage, MedicineRequest, PharmacyResponse, Pharmacy, Pharmacist,
    PharmacistDecline, PharmacyInventory, PharmacyRating, Reservation,
    PatientProfile, SavedMedicine, PatientNotification,
)
from .serializers import (
    ChatRequestSerializer, ChatResponseSerializer,
    ChatConversationSerializer, MedicineRequestSerializer,
    PharmacyResponseSerializer, PharmacistSerializer, PharmacistLoginSerializer,
    PharmacyRegistrationSerializer, PharmacistRegistrationSerializer,
    PharmacySerializer
)
from .services import LocationService, OCRService, RankingEngine, DrugInteractionService
from django.core.files.storage import default_storage
from django.conf import settings

# Lazy import to avoid protobuf issues on startup
_chatbot_service = None

def get_chatbot_service():
    """Lazy load chatbot service to avoid import errors"""
    global _chatbot_service
    if _chatbot_service is None:
        try:
            from .services import ChatbotService
            _chatbot_service = ChatbotService()
            print("[OK] Chatbot service initialized successfully")
        except ValueError as e:
            # API key missing or invalid
            error_msg = str(e)
            print(f"[ERROR] Chatbot service initialization failed: {error_msg}")
            if "OPENROUTER_API_KEY" in error_msg or "GEMINI_API_KEY" in error_msg:
                print("[INFO] Add OPENROUTER_API_KEY or GEMINI_API_KEY to your .env file")
                print("[INFO] OpenRouter: https://openrouter.ai/keys | Gemini: https://aistudio.google.com/apikey")
            return None
        except ImportError as e:
            # google.generativeai not installed
            print(f"[ERROR] Failed to import google.generativeai: {e}")
            print("[INFO] Install with: pip install google-generativeai")
            return None
        except Exception as e:
            # Other errors
            print(f"[ERROR] Chatbot service unavailable: {e}")
            print(f"[ERROR] Error type: {type(e).__name__}")
            return None
    return _chatbot_service


@api_view(['POST'])
@permission_classes([AllowAny])
def chat(request):
    """
    Main chatbot endpoint - handles user messages and returns AI responses
    """
    # Debug: Log incoming request data
    print(f"[DEBUG] Received request data: {request.data}")
    print(f"[DEBUG] Request content type: {request.content_type}")
    
    serializer = ChatRequestSerializer(data=request.data)
    if not serializer.is_valid():
        error_response = {
            'error': 'Validation failed',
            'details': serializer.errors,
            'received_data': dict(request.data) if hasattr(request.data, 'keys') else str(request.data)
        }
        print(f"[ERROR] Validation failed: {error_response}")
        return Response(error_response, status=status.HTTP_400_BAD_REQUEST)
    
    data = serializer.validated_data
    message = data['message']
    session_id = data.get('session_id') or str(uuid.uuid4())
    start_new_search = data.get('start_new_search', False)
    
    # When start_new_search=True, use fresh session so user sees only this search's results
    if start_new_search:
        session_id = f"{session_id}-{uuid.uuid4()}"[:64]  # New session = new conversation
    
    # Get or create conversation
    conversation, created = ChatConversation.objects.get_or_create(
        session_id=session_id,
        defaults={'status': 'active'}
    )
    
    # Save user message
    user_message = ChatMessage.objects.create(
        conversation=conversation,
        role='user',
        content=message,
        metadata={'session_id': session_id}
    )
    
    # Get conversation history (most recent messages first, limit to last 6-8 messages)
    # Use order_by('-created_at') to get newest first, then reverse to chronological order
    previous_messages = list(conversation.messages.order_by('-created_at')[:8])
    previous_messages.reverse()  # Reverse to chronological order for AI
    history = [
        {'role': msg.role, 'content': msg.content}
        for msg in previous_messages
    ]
    
    # Process message with AI
    chatbot_service = get_chatbot_service()
    if not chatbot_service:
        error_msg = (
            'Chatbot service is currently unavailable. '
            'Add OPENROUTER_API_KEY or GEMINI_API_KEY to your .env file. '
            'OpenRouter: https://openrouter.ai/keys | Gemini: https://aistudio.google.com/apikey'
        )
        print(f"[ERROR] {error_msg}")
        return Response({
            'error': error_msg,
            'setup_required': True,
            'api_key_url': 'https://openrouter.ai/keys'
        }, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    
    preferred_language = (data.get('language') or conversation.context_metadata.get('preferred_language') or '').strip().lower()
    if preferred_language:
        conversation.context_metadata['preferred_language'] = preferred_language
        conversation.save()
    ai_result = chatbot_service.process_message(
        user_message=message,
        conversation_history=history,
        context=conversation.context_metadata,
        preferred_language=preferred_language
    )
    
    # Save AI response
    ai_message = ChatMessage.objects.create(
        conversation=conversation,
        role='assistant',
        content=ai_result['response'],
        metadata={
            'intent': ai_result['intent'],
            'entities': ai_result['entities'],
            'requires_location': ai_result['requires_location'],
            'suggested_medicines': ai_result['suggested_medicines']
        }
    )

    # Read previous turn's context BEFORE updating (needed to detect "user just provided location after we asked for it")
    previous_requires_location = conversation.context_metadata.get('requires_location', False)
    previous_intent = conversation.context_metadata.get('last_intent', '')

    # Update conversation context with current response
    conversation.context_metadata.update({
        'last_intent': ai_result['intent'],
        'extracted_entities': ai_result['entities'],
        'requires_location': ai_result['requires_location'],
        'suggested_medicines': ai_result.get('suggested_medicines', []),
        'selected_medicines': ai_result.get('selected_medicines', conversation.context_metadata.get('selected_medicines', []))
    })
    if ai_result.get('selected_medicines'):
        conversation.context_metadata['selected_medicines'] = ai_result['selected_medicines']
    conversation.save()
    
    # Handle medicine request creation
    medicine_request_id = None
    pharmacy_responses = None
    
    # Determine if we should create a medicine request
    intent = ai_result.get('intent', 'general_inquiry')
    medicines = ai_result.get('suggested_medicines', [])
    message_lower = message.lower()
    ai_response_text = ai_result['response']
    
    # Check if message contains medicine/symptom keywords (even if AI failed)
    symptom_keywords = ['headache', 'pain', 'pains', 'fever', 'cough', 'cold', 'flu', 'nausea', 'dizziness', 'symptom',
                        'runny nose', 'stuffy nose', 'sore throat', 'body ache', 'body pain', 'body pains', 'muscle ache', 'sneezing',
                        'runny stomach', 'stomach', 'diarrhea', 'diarrhoea', 'upset stomach', 'stomach ache',
                        'vomiting', 'vomit', 'throwing up']
    medicine_keywords = ['medicine', 'medication', 'drug', 'pill', 'tablet', 'need', 'looking for', 'want', 'search']
    has_symptom = any(keyword in message_lower for keyword in symptom_keywords)
    has_medicine_intent = any(keyword in message_lower for keyword in medicine_keywords)
    
    # Check conversation history for previous symptom/medicine mentions
    # Use most recent messages (reverse order) to find latest symptoms
    # (previous_requires_location and previous_intent were read above, before context update)
    recent_user_messages = list(conversation.messages.filter(role='user').order_by('-created_at')[:5])
    previous_messages_text = ' '.join([msg.content.lower() for msg in recent_user_messages])
    has_previous_symptom = any(keyword in previous_messages_text for keyword in symptom_keywords)
    
    # Handle "yes" confirmation when location is provided (user confirming to proceed)
    confirmation_keywords = ['yes', 'yeah', 'yep', 'ok', 'okay', 'sure', 'proceed', 'go ahead', 'confirm']
    is_confirmation = message_lower.strip() in confirmation_keywords
    
    # User is checking for responses (no new search) - e.g. "any updates?", "got any?"
    # IMPORTANT: Only treat explicit follow-up phrases as "check for existing responses".
    # Bare confirmations like "yes", "ok" should NOT trigger fetching old responses.
    follow_up_check_phrases = ['any updates', 'any news', 'got any', 'any response', 'any responses', 'check', 'waiting']
    is_follow_up_check = any(p in message_lower for p in follow_up_check_phrases)
    
    # Also check AI response text for symptom mentions (e.g., "runny nose")
    ai_response_lower = ai_response_text.lower()
    ai_mentions_symptom = any(keyword in ai_response_lower for keyword in symptom_keywords)
    
    # Extract location coordinates from request data OR from message text OR from AI response
    location_lat = data.get('location_latitude')
    location_lon = data.get('location_longitude')
    
    import re
    
    # Try to extract from message text first
    if not location_lat or not location_lon:
        coord_pattern = r'(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)'
        coord_matches = re.findall(coord_pattern, message)
        
        if coord_matches:
            try:
                lat_str, lon_str = coord_matches[0]
                location_lat = float(lat_str)
                location_lon = float(lon_str)
                
                if -90 <= location_lat <= 90 and -180 <= location_lon <= 180:
                    print(f"[INFO] Extracted coordinates from user message: {location_lat}, {location_lon}")
                else:
                    location_lat = None
                    location_lon = None
            except (ValueError, IndexError):
                location_lat = None
                location_lon = None
    
    # If still no coordinates, try to extract from AI response text
    # This handles cases where AI confirms location like "Location: -17.8394, 31.0543"
    if not location_lat or not location_lon:
        coord_pattern = r'(?:Location:?\s*)?(-?\d+\.?\d*)\s*[,:]\s*(-?\d+\.?\d*)'
        coord_matches = re.findall(coord_pattern, ai_response_text)
        
        if coord_matches:
            try:
                lat_str, lon_str = coord_matches[0]
                location_lat = float(lat_str)
                location_lon = float(lon_str)
                
                if -90 <= location_lat <= 90 and -180 <= location_lon <= 180:
                    print(f"[INFO] Extracted coordinates from AI response: {location_lat}, {location_lon}")
                    # Mark that we found location in AI response
                    ai_message.metadata['location_extracted_from_response'] = True
                    ai_message.save(update_fields=['metadata'])
                else:
                    location_lat = None
                    location_lon = None
            except (ValueError, IndexError):
                location_lat = None
                location_lon = None
    
    # If still no coordinates, try geocoding the address
    # Check if message looks like an address or if location_address is provided
    if not location_lat or not location_lon:
        location_address = data.get('location_address') or ''
        address_to_geocode = location_address.strip() if isinstance(location_address, str) else ''
        
        # If no address in request body, check if message looks like an address
        # (contains numbers + street names, suburbs, etc.)
        if not address_to_geocode:
            # Check if message contains address-like patterns (numbers + street names)
            address_patterns = [
                r'\d+\s+[A-Za-z\s]+(?:street|st|road|rd|avenue|ave|crescent|cres|drive|dr|way|lane|ln)',
                r'(?:Glen View|Avondale|Belvedere|Mbare|Highfield|Epworth|Hatfield|Waterfalls|Borrowdale|Mount Pleasant|Greendale)\s*\d*',
                r'Harare|Bulawayo|Gweru|Mutare|Kwekwe|Chitungwiza',
            ]
            is_address_like = any(re.search(pattern, message, re.IGNORECASE) for pattern in address_patterns)
            
            if is_address_like:
                address_to_geocode = message
                print(f"[INFO] Detected address-like pattern in message: {message}")
        
        # If user is confirming ("yes", "okay") and we still don't have coordinates,
        # check recent conversation history for address messages
        if not address_to_geocode and is_confirmation and previous_requires_location:
            # Look for address in recent user messages (excluding current "yes"/"okay" message)
            for msg in recent_user_messages:
                if msg.content != message:  # Don't check the current confirmation message
                    is_msg_address = any(re.search(pattern, msg.content, re.IGNORECASE) for pattern in address_patterns)
                    if is_msg_address:
                        address_to_geocode = msg.content
                        print(f"[INFO] Found address in conversation history for confirmation: {address_to_geocode}")
                        break
        
        # Try geocoding if we have an address
        if address_to_geocode:
            location_lat, location_lon = LocationService.geocode_address(address_to_geocode)
            if location_lat and location_lon:
                print(f"[INFO] Successfully geocoded address to coordinates: {location_lat}, {location_lon}")
    
    # IMPORTANT: Medicine requests are ONLY created when location is provided
    # This is because:
    # 1. We need location to find nearby pharmacies
    # 2. Distance calculation requires coordinates
    # 3. Travel time estimation needs location
    # 
    # Flow: Patient provides location → Request created → Broadcast to pharmacies → Pharmacies respond
    
    # Debug: Log location extraction status
    print(f"[DEBUG] Location extraction - lat: {location_lat}, lon: {location_lon}, intent: {intent}, requires_location: {previous_requires_location}")
    
    pharmacy_responses = None
    medicine_request_id = None
    is_new_request = False
    
    # Check if there's an existing active request in this conversation
    # Use most recent active request so each query gets its own request's responses
    existing_request = MedicineRequest.objects.filter(
        conversation=conversation,
        status__in=['broadcasting', 'awaiting_responses', 'responses_received']
    ).order_by('-created_at').first()
    
    # Determine what symptoms/medicines are being requested (for matching with existing requests)
    # Get the most recent symptom message from conversation history (in reverse order - most recent first)
    # We need the LAST symptom mentioned before the location request
    all_user_messages = list(conversation.messages.filter(role='user').order_by('-created_at')[:10])
    symptom_messages_for_matching = []
    medicine_messages_for_matching = []
    
    # Extract medicine names from conversation history (look for medicine keywords)
    import re
    medicine_patterns = [
        r'\b(paracetamol|aspirin|ibuprofen|panadol|calpol|brufen|amoxicillin|penicillin|metformin|insulin)\b',
        # Add more medicine names as needed
    ]
    
    # Check conversation metadata for prescription-uploaded medicines
    # BUT only use them if this is a prescription-related query, not a symptom-based query
    prescription_medicines = conversation.context_metadata.get('prescription_medicines', [])
    
    # Determine if this is a symptom-based query by checking:
    # 1. Current message keywords
    # 2. Previous intent (from metadata)
    # 3. Previous messages in conversation
    previous_intent_from_metadata = conversation.context_metadata.get('last_intent', '')
    has_previous_symptom_in_messages = any(
        any(kw in msg.content.lower() for kw in symptom_keywords) 
        for msg in all_user_messages[:3]  # Check last 3 user messages
    )
    
    is_symptom_based_query = (
        any(kw in message_lower for kw in symptom_keywords) or
        intent == 'symptom_description' or
        previous_intent_from_metadata == 'symptom_description' or
        has_previous_symptom_in_messages or
        (any(kw in message_lower for kw in ['have', 'feeling', 'hurts', 'pain', 'ache']) and 
         not any(kw in message_lower for kw in ['medicine', 'prescription', 'looking for']))
    )
    if prescription_medicines and not is_symptom_based_query:
        medicine_messages_for_matching.extend(prescription_medicines)
        print(f"[INFO] Found prescription medicines in conversation metadata: {prescription_medicines}")
    elif prescription_medicines and is_symptom_based_query:
        print(f"[INFO] Ignoring prescription medicines from metadata - this is a symptom-based query, not prescription-based")
    
    for msg in all_user_messages:
        msg_lower = msg.content.lower()
        # Check for symptoms
        if any(kw in msg_lower for kw in symptom_keywords):
            symptom_messages_for_matching.append(msg.content)
        
        # Check for prescription upload messages: "Uploaded prescription with medicines: ..."
        if 'uploaded prescription with medicines:' in msg_lower:
            # Extract medicines from prescription upload message
            # Format: "Uploaded prescription with medicines: azelaic acid, hyaluronic acid, ..."
            try:
                medicines_part = msg.content.split('Uploaded prescription with medicines:')[1].strip()
                if medicines_part and medicines_part.lower() != 'unable to read':
                    # Split by comma and clean
                    extracted_meds = [m.strip() for m in medicines_part.split(',') if m.strip()]
                    medicine_messages_for_matching.extend(extracted_meds)
                    # Store in conversation metadata for future use
                    conversation.context_metadata['prescription_medicines'] = extracted_meds
                    conversation.save(update_fields=['context_metadata'])
                    print(f"[INFO] Extracted medicines from prescription upload message: {extracted_meds}")
            except (IndexError, AttributeError):
                pass
        
        # Check for medicines - look for common medicine names in the message
        for pattern in medicine_patterns:
            matches = re.findall(pattern, msg_lower, re.IGNORECASE)
            if matches:
                medicine_messages_for_matching.extend(matches)
        # Also check if message looks like a medicine search (e.g., "I need paracetamol", "looking for aspirin")
        if any(kw in msg_lower for kw in ['medicine', 'medication', 'drug', 'pill', 'tablet']) and not medicines:
            # Try to extract medicine name from the message
            words = msg.content.split()
            # Look for capitalized words that might be medicine names
            for word in words:
                if len(word) > 3 and word[0].isupper() and word.lower() not in ['I', 'Need', 'Want', 'Looking', 'For', 'Medicine']:
                    medicine_messages_for_matching.append(word.lower())
    
    # Use the most recent symptom (first in reverse-ordered list) or current message if no symptoms found
    current_query_symptoms = symptom_messages_for_matching[0] if symptom_messages_for_matching else message
    
    # Extract medicines: prioritize selected_medicines (from request or context), then suggested_medicines (frontend may send this)
    selected_from_request = data.get('selected_medicines') or data.get('suggested_medicines') or []
    selected_from_context = (
        conversation.context_metadata.get('selected_medicines') or
        conversation.context_metadata.get('suggested_medicines') or
        []
    )
    raw_selected = selected_from_request or selected_from_context
    
    # Blocklist: filter out non-medicine words (instructions, UI text, common false positives)
    _medicine_blocklist = {
        'minutes', 'before', 'eating', 'eatin', 'location', 'drug', 'dru', 'yes', 'would',
        'like', 'search', 'these', 'use', 'my', 'enter', 'manually', 'found', 'great', 'need',
        'medicine', 'medicines', 'tablet', 'tablets', 'take', 'help', 'near', 'you',
    }
    
    def _filter_valid_medicines(names: list) -> list:
        out = []
        for m in (names or []):
            s = (m.lower() if isinstance(m, str) else str(m).lower()).strip()
            if len(s) < 2:
                continue
            if s in _medicine_blocklist:
                continue
            if any(b in s for b in ['minute', 'before', 'eating', 'location']):
                continue
            out.append(s)
        return out
    
    selected_medicines = _filter_valid_medicines(raw_selected) if raw_selected else []

    if selected_medicines:
        current_query_medicines = [m.lower() if isinstance(m, str) else str(m).lower() for m in selected_medicines]
        medicines = current_query_medicines
    elif medicines:
        current_query_medicines = medicines
    elif medicine_messages_for_matching and not is_symptom_based_query:
        filtered_medicines = [
            m for m in medicine_messages_for_matching 
            if m.lower() not in [pm.lower() for pm in (prescription_medicines or [])] or not is_symptom_based_query
        ] if is_symptom_based_query else medicine_messages_for_matching
        current_query_medicines = _filter_valid_medicines(list(set([m.lower() if isinstance(m, str) else str(m).lower() for m in filtered_medicines])))
        medicines = current_query_medicines
    elif medicine_messages_for_matching and is_symptom_based_query:
        current_medicines = [
            m for m in medicine_messages_for_matching 
            if m.lower() not in [pm.lower() for pm in (prescription_medicines or [])]
        ]
        current_query_medicines = _filter_valid_medicines(list(set([m.lower() if isinstance(m, str) else str(m).lower() for m in current_medicines]))) if current_medicines else []
        medicines = current_query_medicines
    elif any(kw in message_lower for kw in medicine_keywords) or message_lower in ['paracetamol', 'aspirin', 'ibuprofen', 'panadol']:
        # Current message might be a medicine name
        current_query_medicines = [message_lower] if len(message_lower.split()) == 1 else []
        if current_query_medicines:
            medicines = current_query_medicines
    else:
        current_query_medicines = []
    
    # Check if existing request matches current query (to avoid showing responses for different symptoms/medicines)
    existing_request_matches = False
    if existing_request:
        # Get current query symptoms/medicines
        current_symptoms_lower = current_query_symptoms.lower()
        existing_symptoms_lower = (existing_request.symptoms or '').lower()
        
        # Check if symptoms match (for symptom_description requests)
        if existing_request.request_type == 'symptom':
            # Check for keyword overlap between current and existing symptoms
            current_symptom_keywords = [kw for kw in symptom_keywords if kw in current_symptoms_lower]
            existing_symptom_keywords = [kw for kw in symptom_keywords if kw in existing_symptoms_lower]
            existing_request_matches = (
                bool(set(current_symptom_keywords) & set(existing_symptom_keywords)) or
                current_symptoms_lower == existing_symptoms_lower
            )
            # If both have no symptom keywords, don't match (let it create new request)
            if not current_symptom_keywords and not existing_symptom_keywords:
                existing_request_matches = False
        # Check if medicines match (for direct medicine searches)
        elif existing_request.request_type == 'direct':
            existing_medicines = [m.lower() for m in existing_request.medicine_names or []]
            current_medicines = [m.lower() for m in current_query_medicines or []]
            
            # Only match if both have medicines AND they overlap
            # If one has medicines and the other doesn't, they don't match
            # If both are empty, they don't match (different requests)
            if len(existing_medicines) > 0 and len(current_medicines) > 0:
                # Both have medicines - check if they overlap
                common_medicines = set(existing_medicines) & set(current_medicines)
                # Match only if there's significant overlap (at least one common medicine)
                existing_request_matches = len(common_medicines) > 0
            else:
                # One or both have no medicines - don't match (different queries)
                existing_request_matches = False
                print(f"[INFO] Medicine mismatch: existing={existing_medicines}, current={current_medicines} - NOT matching")
    
    # Create request if:
    # 1. Location is provided (REQUIRED) AND
    # 2. (Intent indicates medicine search/symptom/medicine_selection/location_provided OR we have medicines/symptoms)
    should_create_request = False  # Default; only True when we have location and meet criteria
    if location_lat and location_lon:
        # If requires_location was true (we asked for location), create request when location is now provided
        # This handles cases where AI asks for location with intent='general_inquiry' but previous turn had symptoms
        if previous_requires_location:
            # Previous turn asked for location - NOW we have location coordinates
            should_create_request = (
                has_previous_symptom or
                has_symptom or
                ai_mentions_symptom or
                previous_intent in ['medicine_search', 'symptom_description', 'medicine_selection'] or
                intent in ['medicine_search', 'symptom_description', 'medicine_selection', 'location_provided'] or
                len(medicines) > 0 or
                len(selected_medicines) > 0 or
                is_confirmation
            )
            print(f"[INFO] previous_requires_location=True: should_create_request={should_create_request}, previous_intent={previous_intent}")
        elif intent == 'location_provided' and (len(medicines) > 0 or len(selected_medicines) > 0 or has_previous_symptom or ai_mentions_symptom):
            # User explicitly provided location and we have medicine/symptom context - always create
            should_create_request = True
            print(f"[INFO] location_provided with medicines/symptoms - forcing create")
        else:
            # Normal flow - create if intent, medicines, or keywords match
            # Also include 'location_provided' intent (user just provided location)
            should_create_request = (
                intent in ['medicine_search', 'symptom_description', 'medicine_selection', 'location_provided'] or
                len(medicines) > 0 or
                len(selected_medicines) > 0 or
                has_symptom or
                has_medicine_intent or
                ai_mentions_symptom or
                has_previous_symptom
            )
        
        # Only use existing request if it matches current query, otherwise create new one
        # Also check if existing request is recent (created in last 5 minutes) - don't reuse old requests
        from django.utils import timezone
        from datetime import timedelta
        
        is_recent_request = False
        if existing_request:
            time_since_creation = timezone.now() - existing_request.created_at
            is_recent_request = time_since_creation < timedelta(minutes=30)  # Increased to 30 minutes for checking responses
        
        # Only return existing request's responses when user EXPLICITLY asks for updates (e.g. "any updates?", "check").
        # Do NOT return on bare "yes"/"ok" - that can be a new search; returning would show "old" responses.
        if existing_request and is_follow_up_check and is_recent_request and not (location_lat and location_lon):
            response_count = existing_request.pharmacy_responses.count()
            if response_count > 0:
                medicine_request_id = existing_request.request_id
                ranked_responses = get_ranked_pharmacy_responses(existing_request, limit=3)
                if ranked_responses:
                    pharmacy_responses = ranked_responses
                    print(f"[INFO] User asked for updates - returning {len(pharmacy_responses)} responses for existing request {medicine_request_id}")
                    should_create_request = False  # Don't create new request
                else:
                    print(f"[INFO] User asked for updates but no ranked responses found for request {medicine_request_id}")
        
        if existing_request and should_create_request and existing_request_matches and is_recent_request:
            # Use existing request only if it's recent (within last 30 minutes) AND has no responses yet
            # If it has responses and user is NOT just checking, mark as completed to create new request
            medicine_request_id = existing_request.request_id
            response_count = existing_request.pharmacy_responses.count()
            
            if response_count > 0 and not is_follow_up_check:
                # Existing request already has responses AND user is not explicitly asking for updates - treat as new search
                print(f"[INFO] Existing request {medicine_request_id} has {response_count} responses and user wants new search, creating new request")
                existing_request.status = 'completed'
                existing_request.save(update_fields=['status'])
                existing_request = None  # Will create new request below
            elif response_count > 0 and is_follow_up_check:
                # User explicitly asked for updates - handled above; here as fallback
                ranked_responses = get_ranked_pharmacy_responses(existing_request, limit=3)
                if ranked_responses:
                    pharmacy_responses = ranked_responses
                    print(f"[INFO] Found {len(pharmacy_responses)} ranked responses for existing request {medicine_request_id}")
                    should_create_request = False  # Don't create new request
            else:
                # No responses yet - can reuse existing request
                ranked_responses = get_ranked_pharmacy_responses(existing_request, limit=3)
                if ranked_responses:
                    pharmacy_responses = ranked_responses
                    print(f"[INFO] Found {len(pharmacy_responses)} ranked responses for existing request {medicine_request_id}")
        elif existing_request and should_create_request and (not existing_request_matches or not is_recent_request):
            # Existing request doesn't match OR is too old - only return its responses if user explicitly asked for updates
            if is_follow_up_check and is_recent_request:
                response_count = existing_request.pharmacy_responses.count()
                if response_count > 0:
                    medicine_request_id = existing_request.request_id
                    ranked_responses = get_ranked_pharmacy_responses(existing_request, limit=3)
                    if ranked_responses:
                        pharmacy_responses = ranked_responses
                        print(f"[INFO] User asked for updates - returning {len(pharmacy_responses)} responses for existing request {medicine_request_id}")
                        should_create_request = False  # Don't create new request
                    else:
                        print(f"[INFO] User asked for updates but no responses yet for request {medicine_request_id}, keeping request active")
                        should_create_request = False  # Don't create new request
                else:
                    print(f"[INFO] User asked for updates but no responses yet for request {existing_request.request_id}, keeping request active")
                    should_create_request = False  # Don't create new request
            else:
                # Existing request doesn't match OR is too old - mark old one as completed and create new one
                if not is_recent_request:
                    print(f"[INFO] Existing request {existing_request.request_id} is too old (created {time_since_creation}), creating new request")
                existing_request.status = 'completed'
                existing_request.save(update_fields=['status'])
                print(f"[INFO] Marked existing request {existing_request.request_id} as completed (different query or too old)")
                existing_request = None  # Treat as if no existing request
        elif existing_request and should_create_request and not existing_request_matches:
            existing_request.status = 'completed'
            existing_request.save(update_fields=['status'])
            print(f"[INFO] Marked existing request {existing_request.request_id} as completed (different query)")
            existing_request = None  # Will create new request in block below
    
    # Create new request when we should and don't have an active one to reuse
    # (Note: use separate if, not elif - we may have just set existing_request=None above)
    if should_create_request and not existing_request:
            # Create new request only if none exists
            # Determine request type and content
            # If location was extracted from AI response, use previous conversation context for symptoms
            if intent == 'error' and (has_symptom or has_previous_symptom):
                # AI failed but we detected symptoms - treat as symptom description
                request_intent = 'symptom_description'
                # Use the most recent symptom message from conversation history
                symptoms_text = current_query_symptoms if current_query_symptoms and current_query_symptoms != message else message
            elif intent == 'error' and has_medicine_intent:
                # AI failed but we detected medicine intent
                request_intent = 'medicine_search'
                symptoms_text = ''
            elif previous_intent in ['medicine_search', 'symptom_description', 'medicine_selection'] and (has_previous_symptom or has_symptom or selected_medicines):
                # Symptom flow: medicine_selection -> treat as symptom_description for request type
                request_intent = 'symptom_description' if previous_intent in ['symptom_description', 'medicine_selection'] else previous_intent
                symptoms_text = current_query_symptoms if request_intent == 'symptom_description' and current_query_symptoms else ''
            elif (intent == 'general_inquiry' and previous_requires_location and 
                  (has_previous_symptom or has_symptom or ai_mentions_symptom)):
                # AI asked for location with general_inquiry, but symptoms were mentioned
                request_intent = 'symptom_description'
                # Use the most recent symptom message from conversation history
                symptoms_text = current_query_symptoms if current_query_symptoms else message
            else:
                request_intent = intent if intent in ['medicine_search', 'symptom_description'] else 'symptom_description' if (has_symptom or has_previous_symptom or ai_mentions_symptom or selected_medicines) else 'medicine_search'
                # Ensure symptoms_text is always set if this is a symptom request
                if request_intent == 'symptom_description':
                    # Prioritize: current_query_symptoms > message > empty
                    symptoms_text = current_query_symptoms if current_query_symptoms else message
                    # If still empty, try to get from conversation history
                    if not symptoms_text or symptoms_text.strip() == '':
                        if symptom_messages_for_matching:
                            symptoms_text = symptom_messages_for_matching[0]
                        elif has_previous_symptom:
                            # Get most recent user message with symptom keyword
                            for msg in all_user_messages:
                                if any(kw in msg.content.lower() for kw in symptom_keywords):
                                    symptoms_text = msg.content
                                    break
                else:
                    symptoms_text = ''
            
            # Final validation: if request_type is symptom but symptoms_text is empty, use current message
            if request_intent == 'symptom_description' and (not symptoms_text or symptoms_text.strip() == ''):
                symptoms_text = message if message else 'Symptom description requested'
            
            print(f"[INFO] Creating medicine request: intent={request_intent}, medicines={medicines}, symptoms={symptoms_text[:50] if symptoms_text else 'None'}")
            
            # For symptom-based requests: use selected_medicines (from symptom flow) or AI-extracted
            if request_intent == 'symptom_description':
                if selected_medicines:
                    medicines_to_use = selected_medicines
                    print(f"[INFO] Symptom flow - using patient-selected medicines: {medicines_to_use}")
                elif medicines:
                    medicines_to_use = medicines
                elif symptoms_text and symptoms_text.strip():
                    # Derive suggested medicines from symptoms so pharmacist sees what to look for
                    try:
                        from .services import ChatbotService
                        suggested = ChatbotService()._suggest_medicines_from_symptoms(symptoms_text, {})
                        medicines_to_use = suggested if suggested else []
                        if medicines_to_use:
                            print(f"[INFO] Symptom-based - derived medicines from symptoms: {medicines_to_use}")
                    except Exception as e:
                        print(f"[WARNING] Could not derive medicines from symptoms: {e}")
                        medicines_to_use = []
                else:
                    medicines_to_use = []
                    print(f"[INFO] Symptom-based request - no medicines selected, pharmacies will suggest for '{symptoms_text[:50] if symptoms_text else 'unknown'}'")
            else:
                medicines_to_use = medicines if medicines else current_query_medicines if current_query_medicines else []
            
            # Final filter: remove any non-medicine garbage before creating request
            medicines_to_use = _filter_valid_medicines(medicines_to_use) if medicines_to_use else []
            
            # LIVE INVENTORY PATH: when we have location + medicines, query live inventory first (implementation guide flow)
            live_results = []
            if medicines_to_use and location_lat and location_lon:
                live_results = get_live_inventory_ranked(location_lat, location_lon, medicines_to_use, limit=10)
                if not live_results:
                    print(f"[INFO] Live inventory: 0 results for {medicines_to_use} at ({location_lat},{location_lon}); falling back to request + pharmacist responses")
            if live_results:
                pharmacy_responses = list(live_results)
                # Merge in pharmacist responses (symptom/prescription requests): include pharmacies that responded
                # with availability or alternatives so patient sees both live stock and pharmacist suggestions
                if existing_request and existing_request.pharmacy_responses.exists():
                    pharmacist_responses = get_ranked_pharmacy_responses(existing_request, limit=10)
                    live_pharmacy_ids = {r.get('pharmacy_id') for r in pharmacy_responses if r.get('pharmacy_id')}
                    for pr in pharmacist_responses:
                        pid = pr.get('pharmacy_id')
                        if pid and pid not in live_pharmacy_ids:
                            pr['from_pharmacist_response'] = True
                            pr['from_live_inventory'] = False
                            pharmacy_responses.append(pr)
                            live_pharmacy_ids.add(pid)
                    if len(pharmacist_responses) > 0:
                        print(f"[INFO] Merged {len(pharmacist_responses)} pharmacist response(s) with {len(live_results)} live inventory result(s)")
                print(f"[INFO] Live inventory: found {len(live_results)} pharmacies with stock; showing ranked results (40/30/20/10)")
                # Do not create a request; patient sees results immediately and can reserve
            else:
                print(f"[INFO] Creating medicine request: intent={request_intent}, medicines={medicines_to_use}, symptoms={symptoms_text[:50] if symptoms_text else 'None'}")
                medicine_request = create_medicine_request(
                    conversation=conversation,
                    user=request.user if request.user.is_authenticated else None,
                    intent=request_intent,
                    medicines=medicines_to_use,
                    symptoms=symptoms_text,
                    latitude=location_lat,
                    longitude=location_lon,
                    address=data.get('location_address', ''),
                    suburb=data.get('location_suburb', '')
                )
                medicine_request_id = medicine_request.request_id
                is_new_request = True
                print(f"[INFO] Medicine request created: {medicine_request_id} (intent: {request_intent}, status: {medicine_request.status})")
                pharmacy_responses = None
                print(f"[INFO] New request {medicine_request_id} created and broadcasting - waiting for pharmacists to respond")
    elif intent in ['medicine_search', 'symptom_description', 'medicine_selection'] or has_symptom or has_medicine_intent:
        # Location not provided - AI will ask for it (symptom flow: suggest → confirm → location)
        print(f"[INFO] Medicine request pending - waiting for location (intent: {intent})")
    
    # When user sends any message (including follow-ups without location), check for pharmacy responses.
    # For symptom/prescription requests, merge pharmacist responses with live inventory so patient sees both.
    should_fetch_responses = is_follow_up_check
    if not pharmacy_responses and existing_request and should_fetch_responses:
        response_count = existing_request.pharmacy_responses.count()
        if response_count > 0:
            medicine_request_id = existing_request.request_id
            pharmacist_list = get_ranked_pharmacy_responses(existing_request, limit=10)
            req_meds = existing_request.medicine_names or []
            req_lat = existing_request.location_latitude
            req_lon = existing_request.location_longitude
            if req_meds and req_lat and req_lon:
                live_list = get_live_inventory_ranked(req_lat, req_lon, req_meds, limit=10)
                live_ids = {r.get('pharmacy_id') for r in live_list if r.get('pharmacy_id')}
                pharmacy_responses = list(live_list)
                for pr in pharmacist_list:
                    pid = pr.get('pharmacy_id')
                    if pid and pid not in live_ids:
                        pr['from_pharmacist_response'] = True
                        pr['from_live_inventory'] = False
                        pharmacy_responses.append(pr)
                        live_ids.add(pid)
                if live_list:
                    print(f"[INFO] Follow-up: merged {len(pharmacist_list)} pharmacist + {len(live_list)} live = {len(pharmacy_responses)} total")
            else:
                pharmacy_responses = pharmacist_list
            print(f"[INFO] Fetched {len(pharmacy_responses)} pharmacy responses for follow-up check")
    
    # IMPORTANT: Only return pharmacy responses if:
    # 1. It's a NEW request with responses (first time showing)
    # 2. OR it's an existing request and we haven't shown responses before (check conversation messages)
    # This prevents duplicate notifications
    
    # Check if we've already shown responses to this conversation
    already_shown_responses = False
    if existing_request and not is_new_request and pharmacy_responses:
        # Check recent messages to see if we already sent pharmacy responses
        # Look for assistant messages that contain pharmacy_responses in response data
        recent_ai_messages = ChatMessage.objects.filter(
            conversation=conversation,
            role='assistant'
        ).order_by('-created_at')[:5]  # Check last 5 messages
        
        for msg in recent_ai_messages:
            # Check if metadata indicates we showed responses
            if msg.metadata and isinstance(msg.metadata, dict):
                if msg.metadata.get('pharmacy_responses_shown'):
                    # We've already shown responses - only show if new ones were added
                    last_response_time = msg.created_at
                    new_responses_count = existing_request.pharmacy_responses.filter(
                        submitted_at__gt=last_response_time
                    ).count()
                    
                    if new_responses_count == 0:
                        already_shown_responses = True
                        pharmacy_responses = None  # Don't show again
                        print(f"[INFO] Responses already shown for request {existing_request.request_id}, no new responses")
                    else:
                        print(f"[INFO] Found {new_responses_count} new responses since last shown")
                    break
    
    # If we have pharmacy responses and haven't shown them yet, return them instead of AI response
    if pharmacy_responses and not already_shown_responses:
        # Generate recommendation for best pharmacy
        best_pharmacy = pharmacy_responses[0] if pharmacy_responses else None
        recommendation = None
        
        if best_pharmacy:
            reasons = []
            if best_pharmacy.get('medicine_available'):
                reasons.append("medicine is available")
            if best_pharmacy.get('ranking_score', 1000) < 100:
                if best_pharmacy.get('distance_km'):
                    reasons.append(f"only {best_pharmacy['distance_km']:.1f}km away")
                if best_pharmacy.get('total_time_minutes'):
                    reasons.append(f"ready in {best_pharmacy['total_time_minutes']} minutes")
                if best_pharmacy.get('price'):
                    reasons.append(f"best price: ${best_pharmacy['price']}")
            
            reason_text = ", ".join(reasons[:3]) if reasons else "best overall option"
            recommendation = {
                'recommended_pharmacy': best_pharmacy.get('pharmacy_name'),
                'pharmacy_id': best_pharmacy.get('pharmacy_id'),
                'reason': f"I recommend **{best_pharmacy.get('pharmacy_name')}** because {reason_text}.",
                'ranking_score': best_pharmacy.get('ranking_score')
            }
        
        from_live = bool(pharmacy_responses and pharmacy_responses[0].get('from_live_inventory'))
        req_id_for_short = medicine_request_id if not from_live else None
        short_req_id = str(req_id_for_short).replace('-', '')[:8].upper() if req_id_for_short else None
        response_data = {
            'response': f"✅ I found {len(pharmacy_responses)} {'pharmacies' if len(pharmacy_responses) != 1 else 'pharmacy'} with live stock. Here are the top ranked options (distance, price, availability, rating):" if from_live else f"✅ Your request has been sent to nearby pharmacies! I found {len(pharmacy_responses)} top {'pharmacies' if len(pharmacy_responses) != 1 else 'pharmacy'} with available options. Here are the top ranked responses:",
            'conversation_id': conversation.conversation_id,
            'message_id': ai_message.message_id,
            'intent': 'medicine_search',
            'requires_location': False,
            'suggested_medicines': medicines,
            'medicine_request_id': req_id_for_short,
            'short_request_id': short_req_id,
            'pharmacy_responses': pharmacy_responses,
            'recommendation': recommendation,
            'request_sent_to_pharmacies': True,
            'total_responses': len(pharmacy_responses),
            'results_for_request_id': str(req_id_for_short) if req_id_for_short else None,
            'from_live_inventory': from_live,
            'live_results_note': 'Results are from current stock. Other pharmacies may have added stock; search again or refresh to see the latest.' if from_live else None,
        }
        
        # Mark in message metadata that we've shown responses
        ai_message.metadata = {'pharmacy_responses_shown': True, 'total_responses': len(pharmacy_responses)}
        ai_message.save(update_fields=['metadata'])
        # Persist suggested_medicines to conversation so Reserve can use them when frontend sends only conversation_id + pharmacy_id
        if medicines:
            conversation.context_metadata['suggested_medicines'] = list(medicines)
            conversation.save(update_fields=['context_metadata'])
    elif medicine_request_id:
        # Request created - check if we have responses
        try:
            medicine_request = MedicineRequest.objects.get(request_id=medicine_request_id)
            response_count = medicine_request.pharmacy_responses.count()
            request_status = medicine_request.status
            
            # Update status if we have responses but status is still 'awaiting_responses'
            if response_count > 0 and request_status == 'awaiting_responses':
                medicine_request.status = 'responses_received'
                medicine_request.save(update_fields=['status'])
                request_status = 'responses_received'
            
            if response_count > 0:
                # We have responses - get ranked or chronological (depending on 2-min delay)
                ranked_responses = get_ranked_pharmacy_responses(medicine_request, limit=3)
                ranking_pending = ranked_responses[0].get('ranking_pending', False) if ranked_responses else False
                if ranking_pending:
                    msg = f"✅ {response_count} {'pharmacies have' if response_count > 1 else 'pharmacy has'} responded. More may respond. Final ranking in {RANKING_DELAY_MINUTES} minutes."
                else:
                    msg = f"✅ Great news! {response_count} {'pharmacies have' if response_count > 1 else 'pharmacy has'} responded. Here are the top ranked options."
                short_req_id = str(medicine_request_id).replace('-', '')[:8].upper()
                response_data = {
                    'response': msg,
                    'conversation_id': conversation.conversation_id,
                    'message_id': ai_message.message_id,
                    'intent': 'medicine_search',
                    'requires_location': False,
                    'suggested_medicines': medicines,
                    'medicine_request_id': medicine_request_id,
                    'short_request_id': short_req_id,
                    'pharmacy_responses': ranked_responses,
                    'request_sent_to_pharmacies': True,
                    'total_responses': response_count,
                    'status': request_status,
                    'ranking_pending': ranking_pending,
                    'results_for_request_id': str(medicine_request_id),
                }
                # Persist so Reserve can use when frontend sends only conversation_id + pharmacy_id
                to_save = medicine_request.medicine_names or medicines or []
                if to_save:
                    conversation.context_metadata['suggested_medicines'] = list(to_save)
                    conversation.save(update_fields=['context_metadata'])
            else:
                # No responses yet - include poll hint so frontend can check for responses without user sending another message
                conversation_id_str = str(conversation.conversation_id)
                poll_url = f"/api/chatbot/request/{medicine_request_id}/ranked/?conversation_id={conversation_id_str}&limit=3"
                short_req_id = str(medicine_request_id).replace('-', '')[:8].upper()
                response_data = {
                    'response': "✅ Request has been sent. Waiting for pharmacies to respond. Responses will appear as soon as pharmacies reply.",
                    'conversation_id': conversation.conversation_id,
                    'message_id': ai_message.message_id,
                    'intent': 'medicine_search',
                    'requires_location': False,
                    'suggested_medicines': medicines,
                    'medicine_request_id': medicine_request_id,
                    'short_request_id': short_req_id,
                    'pharmacy_responses': [],
                    'request_sent_to_pharmacies': True,
                    'total_responses': 0,
                    'status': request_status,
                    'poll_url': poll_url,
                    'poll_interval_seconds': 10,
                    'polling_enabled': True,
                }
        except MedicineRequest.DoesNotExist:
            # Request doesn't exist (shouldn't happen, but handle gracefully)
            short_req_id = str(medicine_request_id).replace('-', '')[:8].upper() if medicine_request_id else None
            response_data = {
                'response': ai_result['response'],
                'conversation_id': conversation.conversation_id,
                'message_id': ai_message.message_id,
                'intent': ai_result['intent'],
                'requires_location': ai_result['requires_location'],
                'suggested_medicines': ai_result['suggested_medicines'],
                'medicine_request_id': medicine_request_id,
                'short_request_id': short_req_id,
                'request_sent_to_pharmacies': False
            }
    else:
        # When AI returned "error" but user clearly asked for medicine/symptoms, ask for location instead of generic error
        if (intent == 'error' and (has_medicine_intent or has_symptom) and
            (medicines or current_query_medicines or has_symptom or has_previous_symptom)):
            med_list = list(medicines) if medicines else list(current_query_medicines) if current_query_medicines else []
            if med_list:
                med_text = ', '.join(med_list)
                response_data = {
                    'response': f"I can help you find **{med_text}**. To show pharmacies near you with availability and prices, please share your location (e.g. area name or use your current location).",
                    'conversation_id': conversation.conversation_id,
                    'message_id': ai_message.message_id,
                    'intent': 'medicine_search',
                    'requires_location': True,
                    'suggested_medicines': med_list,
                    'medicine_request_id': None,
                    'request_sent_to_pharmacies': False,
                }
            else:
                response_data = {
                    'response': "To find pharmacies near you, please share your location (e.g. area name or use your current location).",
                    'conversation_id': conversation.conversation_id,
                    'message_id': ai_message.message_id,
                    'intent': 'medicine_search',
                    'requires_location': True,
                    'suggested_medicines': [],
                    'medicine_request_id': None,
                    'request_sent_to_pharmacies': False,
                }
        else:
            # Normal AI response
            response_data = {
                'response': ai_result['response'],
                'conversation_id': conversation.conversation_id,
                'message_id': ai_message.message_id,
                'intent': ai_result['intent'],
                'requires_location': ai_result['requires_location'],
                'suggested_medicines': ai_result['suggested_medicines'],
                'medicine_request_id': medicine_request_id,
                'request_sent_to_pharmacies': False
            }
    
    return Response(response_data, status=status.HTTP_200_OK)


def create_medicine_request(
    conversation, user, intent, medicines, symptoms, latitude, longitude, address, suburb=''
):
    """
    Create a medicine request and broadcast to nearby pharmacies.
    
    This function is ONLY called when patient provides location coordinates.
    The request is immediately set to 'broadcasting' status and made available
    to pharmacies via the dashboard.
    
    Flow:
    1. Patient provides location → This function is called
    2. Request created with status='broadcasting'
    3. Nearby pharmacies are notified (via query in dashboard)
    4. Pharmacies see request in their dashboard
    5. Pharmacies submit responses via API
    """
    request_type = 'symptom' if intent == 'symptom_description' else 'direct'
    
    # Urban (≥3 pharmacies within 5km): 30min timeout; rural: 2hr
    density = RankingEngine.calculate_pharmacy_density(latitude, longitude) if (latitude and longitude) else 0
    timeout_minutes = 30 if density >= 3 else 120
    expires_at = timezone.now() + timedelta(minutes=timeout_minutes)

    medicine_request = MedicineRequest.objects.create(
        conversation=conversation,
        user=user,
        request_type=request_type,
        medicine_names=medicines,
        symptoms=symptoms,
        location_latitude=latitude,
        location_longitude=longitude,
        location_address=address,
        location_suburb=suburb or '',
        status='broadcasting',  # Immediately available to pharmacies
        expires_at=expires_at
    )
    
    # Broadcast to nearby pharmacies
    # In production, this would:
    # 1. Query nearby pharmacies from database (within X km radius)
    # 2. Send push notifications/emails/SMS to pharmacies
    # 3. Pharmacies see request in dashboard and respond via API
    broadcast_to_pharmacies(medicine_request)

    # Only simulate responses if explicitly enabled (set AUTO_SIMULATE_RESPONSES=true in .env for demos)
    # Default: wait for real pharmacists to respond via dashboard
    if os.environ.get('AUTO_SIMULATE_RESPONSES', '').lower() == 'true':
        simulate_pharmacy_responses(medicine_request)
        print(f"[INFO] Auto-simulate enabled: simulated pharmacy responses for request {medicine_request.request_id}")

    print(f"[INFO] Medicine request {medicine_request.request_id} created and broadcasted to pharmacies")
    return medicine_request


def broadcast_to_pharmacies(medicine_request):
    """
    Broadcast medicine request to nearby pharmacies.
    
    This function:
    1. Queries pharmacies within a reasonable distance (e.g., 10km radius)
    2. Makes the request visible in pharmacy dashboard
    3. Optionally sends notifications (push, email, SMS)
    
    Note: The request is already visible in dashboard via status='broadcasting'
    This function can be extended to add notification logic.
    """
    from .models import Pharmacy
    from .services import LocationService
    
    # Query nearby pharmacies (within 10km radius)
    # This is a simple implementation - can be optimized with geospatial queries
    nearby_pharmacies = []
    
    if medicine_request.location_latitude and medicine_request.location_longitude:
        all_pharmacies = Pharmacy.objects.filter(is_active=True)
        
        for pharmacy in all_pharmacies:
            if pharmacy.latitude and pharmacy.longitude:
                distance = LocationService.calculate_distance(
                    medicine_request.location_latitude,
                    medicine_request.location_longitude,
                    pharmacy.latitude,
                    pharmacy.longitude
                )
                
                # Include pharmacies within 10km radius
                if distance <= 10.0:
                    nearby_pharmacies.append({
                        'pharmacy': pharmacy,
                        'distance_km': distance
                    })
        
        print(f"[INFO] Found {len(nearby_pharmacies)} nearby pharmacies for request {medicine_request.request_id}")
        
        # TODO: Send notifications to pharmacies
        # - Push notifications via FCM/APNS
        # - Email notifications
        # - SMS notifications
        # - In-app notifications
    
    return nearby_pharmacies


RANKING_DELAY_MINUTES = 2  # Apply MCDA ranking only after this many minutes from request creation


def get_ranked_pharmacy_responses(medicine_request, limit=3):
    """
    Get pharmacy responses for a medicine request.
    - Before 2 min: return responses in chronological order (as they arrive) - show immediately
    - After 2 min: apply MCDA ranking and return ranked list
    - Availability is most important (unavailable = +1000 penalty)
    - Time factor: total_time * 2 (preparation + travel time)
    - Price factor: price * 10 (lower is better)
    - Distance factor: distance_km * 5 (closer is better)
    - Lower total score = better rank
    """
    import re
    from .services import LocationService
    
    responses = medicine_request.pharmacy_responses.all()
    
    if not responses.exists():
        return []
    
    ranked_responses = []
    for response in responses:
        # ALWAYS calculate or recalculate distance and travel time if we have coordinates
        # This ensures distance is always calculated using the best algorithm (Haversine)
        if medicine_request.location_latitude and medicine_request.location_longitude:
            pharmacy_lat = None
            pharmacy_lon = None
            
            # Method 1: Try to get coordinates from pharmacy FK (primary method)
            if response.pharmacy:
                if response.pharmacy.latitude and response.pharmacy.longitude:
                    pharmacy_lat = response.pharmacy.latitude
                    pharmacy_lon = response.pharmacy.longitude
                elif response.pharmacy.address:
                    # Fallback: geocode when pharmacy has address but no coordinates
                    try:
                        lat, lon = LocationService.geocode_address(response.pharmacy.address)
                        if lat and lon:
                            response.pharmacy.latitude = lat
                            response.pharmacy.longitude = lon
                            response.pharmacy.save(update_fields=['latitude', 'longitude'])
                            pharmacy_lat, pharmacy_lon = lat, lon
                            print(f"[INFO] Geocoded pharmacy {response.pharmacy.pharmacy_id} to {lat}, {lon}")
                    except Exception as ge:
                        print(f"[WARNING] Could not geocode pharmacy: {ge}")
            
            # Method 2: If FK not set, try to look up pharmacy by pharmacy_id (legacy support)
            if not pharmacy_lat or not pharmacy_lon:
                pharmacy_id = None
                if response.pharmacy:
                    pharmacy_id = response.pharmacy.pharmacy_id
                else:
                    # Try to get pharmacy_id from property (which might return None if FK not set)
                    pharmacy_id = response.pharmacy_id
                    # If still None, try looking up by pharmacy_name as fallback
                    if not pharmacy_id and hasattr(response, 'pharmacy_name') and response.pharmacy_name:
                        try:
                            from .models import Pharmacy
                            # Try exact name match first
                            pharmacy = Pharmacy.objects.filter(name=response.pharmacy_name).first()
                            if pharmacy:
                                pharmacy_id = pharmacy.pharmacy_id
                        except Exception as e:
                            print(f"[WARNING] Error looking up pharmacy by name: {e}")
                            pass
                
                if pharmacy_id:
                    try:
                        from .models import Pharmacy
                        pharmacy_obj = Pharmacy.objects.get(pharmacy_id=pharmacy_id)
                        if pharmacy_obj.latitude and pharmacy_obj.longitude:
                            pharmacy_lat = pharmacy_obj.latitude
                            pharmacy_lon = pharmacy_obj.longitude
                        elif pharmacy_obj.address:
                            # Fallback: geocode address when pharmacy has no coordinates
                            try:
                                lat, lon = LocationService.geocode_address(pharmacy_obj.address)
                                if lat and lon:
                                    pharmacy_obj.latitude = lat
                                    pharmacy_obj.longitude = lon
                                    pharmacy_obj.save(update_fields=['latitude', 'longitude'])
                                    pharmacy_lat, pharmacy_lon = lat, lon
                                    print(f"[INFO] Geocoded pharmacy {pharmacy_id} address to {lat}, {lon}")
                            except Exception as ge:
                                print(f"[WARNING] Could not geocode pharmacy {pharmacy_id}: {ge}")
                        # Link FK if not set
                        if not response.pharmacy:
                            response.pharmacy = pharmacy_obj
                            response.save(update_fields=['pharmacy'])
                            print(f"[INFO] Linked pharmacy FK to {pharmacy_id} for response {response.response_id}")
                    except Pharmacy.DoesNotExist:
                        print(f"[WARNING] Pharmacy {pharmacy_id} not found in database")
                        pass
                    except Exception as e:
                        print(f"[WARNING] Error looking up pharmacy {pharmacy_id}: {e}")
                        pass
            
            # Calculate distance using Haversine formula (best algorithm for shortest distance)
            if pharmacy_lat and pharmacy_lon:
                distance_km = LocationService.calculate_distance(
                    medicine_request.location_latitude,
                    medicine_request.location_longitude,
                    float(pharmacy_lat),
                    float(pharmacy_lon)
                )
                # Estimate travel time based on distance (urban context)
                travel_time = LocationService.estimate_travel_time(distance_km, 'urban')
                
                # Always update response with calculated values (recalculate to ensure accuracy)
                response.distance_km = distance_km
                response.estimated_travel_time = travel_time
                response.save(update_fields=['distance_km', 'estimated_travel_time'])
                print(f"[INFO] Calculated distance: {distance_km:.2f}km, travel time: {travel_time}min for pharmacy {response.pharmacy_id}")
            else:
                print(f"[WARNING] Cannot calculate distance for response {response.response_id}: missing pharmacy coordinates")
                print(f"[DEBUG] Request location: lat={medicine_request.location_latitude}, lon={medicine_request.location_longitude}")
                print(f"[DEBUG] Pharmacy location: lat={pharmacy_lat}, lon={pharmacy_lon}")
                print(f"[DEBUG] Pharmacy FK set: {response.pharmacy is not None}")
                if response.pharmacy:
                    print(f"[DEBUG] Pharmacy has coordinates: {response.pharmacy.latitude is not None and response.pharmacy.longitude is not None}")
        
        # Ensure distance and travel time are in response_data (even if None)
        # This ensures frontend always gets these fields
        
        # Calculate total time
        total_time = response.preparation_time
        if response.estimated_travel_time:
            total_time += response.estimated_travel_time

        # Pharmacy rating and response_rate for MCDA
        pharmacy_rating = 0.0
        pharmacy_response_rate = 100.0
        if response.pharmacy:
            pharmacy_rating = float(getattr(response.pharmacy, 'rating', 0) or 0)
            pharmacy_response_rate = float(getattr(response.pharmacy, 'response_rate', 100) or 100)
        
        # Get pharmacy/pharmacist info using serializer method
        serializer = PharmacyResponseSerializer(response)
        response_data = serializer.data
        
        # Get requested medicines from the medicine request
        requested_medicines = medicine_request.medicine_names or []
        requested_medicines_lower = [m.lower() for m in requested_medicines]
        
        # ALWAYS check inventory first (source of truth - decreased when patients buy or pharmacists edit)
        inventory_by_medicine = {}
        if response.pharmacy:
            for inv in PharmacyInventory.objects.filter(pharmacy=response.pharmacy, quantity__gt=F('reserved_quantity')):
                inventory_by_medicine[inv.medicine_name.lower()] = inv.quantity - inv.reserved_quantity
        
        # Determine availability: ALWAYS check inventory first (source of truth)
        if requested_medicines and inventory_by_medicine:
            found_in_inv = False
            for req_med in requested_medicines:
                req_lower = req_med.lower()
                if req_lower in inventory_by_medicine:
                    found_in_inv = True
                    break
                for inv_name in inventory_by_medicine:
                    if req_lower in inv_name or inv_name in req_lower:
                        found_in_inv = True
                        break
                if found_in_inv:
                    break
            response_data['medicine_available'] = found_in_inv
        # Fallback: parse notes only when inventory has no data (e.g. "paracetamol $3")
        if not response_data.get('medicine_available') and not response_data.get('price') and getattr(response, 'notes', ''):
            price_match = re.search(r'\$?\s*(\d+(?:\.\d{1,2})?)\s*(?:dollars?|usd)?', response.notes, re.IGNORECASE)
            if price_match:
                try:
                    parsed = float(price_match.group(1))
                    if parsed > 0:
                        response_data['medicine_available'] = True
                        response_data['price'] = str(parsed)
                except (ValueError, TypeError):
                    pass
        
        response_data['total_time_minutes'] = total_time
        response_data['pharmacy_rating'] = pharmacy_rating
        response_data['pharmacy_response_rate'] = pharmacy_response_rate
        
        # Create per-medicine breakdown for display
        # Format: [{"medicine": "pantoprazole", "available": true, "price": "2.25"}, ...]
        medicines_breakdown = []
        alternatives_by_medicine = {}  # Track which alternatives are for which medicine
        
        # Add pharmacy name and map alternatives to requested medicines
        if response_data.get('alternative_medicines'):
            pharmacy_name = response_data.get('pharmacy_name', 'Unknown Pharmacy')
            pharmacy_id = response_data.get('pharmacy_id')
            
            # Format alternatives with context about which medicine they're for
            formatted_alternatives = []
            alternatives_list = response_data['alternative_medicines']
            
            # Handle both string list (legacy) and object list (new format)
            for alt in alternatives_list:
                if isinstance(alt, str):
                    # Legacy format: just a string, need to determine which medicine it's for
                    alt_name = alt
                    # Try to match to unavailable medicines
                    # Since medicine_available is a boolean, we assume alternatives are for medicines
                    # that are either explicitly unavailable or not mentioned as available
                    for_medicine = None
                    
                    # If there's only one requested medicine, map the alternative to it
                    if len(requested_medicines) == 1:
                        for_medicine = requested_medicines[0]
                    # Otherwise, try to find the most likely match based on therapeutic category
                    # For now, mark as generic alternative if multiple medicines requested
                    elif len(requested_medicines) > 1:
                        # Could be alternative for any unavailable medicine
                        # We'll mark it as a general alternative
                        for_medicine = None  # Will be shown as "general alternative"
                    
                    formatted_alternatives.append({
                        'medicine': alt_name,
                        'for_medicine': for_medicine,  # None if not specific
                        'suggested_by': pharmacy_name,
                        'pharmacy_id': pharmacy_id
                    })
                elif isinstance(alt, dict):
                    # New format: already an object, add missing fields if needed
                    formatted_alt = {
                        'medicine': alt.get('medicine', alt.get('name', '')),
                        'for_medicine': alt.get('for_medicine', alt.get('for', None)),
                        'suggested_by': alt.get('suggested_by', pharmacy_name),
                        'pharmacy_id': alt.get('pharmacy_id', pharmacy_id)
                    }
                    formatted_alternatives.append(formatted_alt)
            
            # If we have requested medicines and alternatives but no for_medicine mapping,
            # try to intelligently map them based on therapeutic categories
            if formatted_alternatives and requested_medicines and len(requested_medicines) > 1:
                # Only do intelligent matching if we have multiple medicines and unmapped alternatives
                unmapped_alternatives = [alt for alt in formatted_alternatives if alt.get('for_medicine') is None]
                
                if unmapped_alternatives:
                    from .services import ChatbotService
                    try:
                        chatbot_service = ChatbotService()
                        
                        # Create a mapping of requested medicines to their suggested alternatives
                        medicine_to_alternatives = {}
                        for req_med in requested_medicines:
                            suggested_alts = chatbot_service.suggest_alternatives(req_med, [])
                            medicine_to_alternatives[req_med.lower()] = [s.lower() for s in suggested_alts]
                        
                        # Match each unmapped alternative to a requested medicine
                        # Each alternative can only be matched to one medicine
                        matched_alternatives = set()
                        for req_med in requested_medicines:
                            req_med_lower = req_med.lower()
                            suggested_alts_lower = medicine_to_alternatives.get(req_med_lower, [])
                            
                            # Find best matching alternative for this medicine
                            for alt_obj in unmapped_alternatives:
                                alt_medicine_lower = alt_obj.get('medicine', '').lower()
                                if alt_medicine_lower not in matched_alternatives and alt_medicine_lower in suggested_alts_lower:
                                    alt_obj['for_medicine'] = req_med
                                    matched_alternatives.add(alt_medicine_lower)
                                    break
                    except Exception as e:
                        # If ChatbotService fails, continue without intelligent matching
                        print(f"[WARNING] Could not perform intelligent alternative matching: {e}")
                        pass
            
            response_data['alternative_medicines'] = formatted_alternatives
            
            # Build alternatives_by_medicine mapping for per-medicine breakdown
            for alt in formatted_alternatives:
                for_med = alt.get('for_medicine')
                if for_med:
                    if for_med.lower() not in alternatives_by_medicine:
                        alternatives_by_medicine[for_med.lower()] = []
                    alternatives_by_medicine[for_med.lower()].append(alt.get('medicine'))
        
        # Create per-medicine breakdown
        # If requested_medicines exist, show breakdown per medicine
        # Otherwise, show general availability
        if requested_medicines:
            for req_med in requested_medicines:
                req_med_lower = req_med.lower()
                medicine_status = {
                    'medicine': req_med,
                    'available': False,
                    'price': None,
                    'alternative': None
                }
                
                # Use effective availability (includes notes parsing, inventory check)
                effective_available = response_data.get('medicine_available', response.medicine_available)
                effective_price = response_data.get('price') or (str(response.price) if response.price else None)
                # Check if this medicine has alternatives (suggesting it's unavailable)
                if req_med_lower in alternatives_by_medicine:
                    medicine_status['available'] = False
                    medicine_status['alternative'] = alternatives_by_medicine[req_med_lower][0]
                elif effective_available:
                    medicine_status['available'] = True
                    if len(requested_medicines) == 1:
                        medicine_status['price'] = effective_price
                    else:
                        medicine_status['price'] = None
                else:
                    # Check inventory - source of truth (decreased on purchase/edit)
                    in_inv = req_med_lower in inventory_by_medicine
                    if not in_inv:
                        for inv_name in inventory_by_medicine:
                            if req_med_lower in inv_name or inv_name in req_lower:
                                in_inv = True
                                break
                    medicine_status['available'] = in_inv
                    if in_inv and len(requested_medicines) == 1 and effective_price:
                        medicine_status['price'] = effective_price
                
                medicines_breakdown.append(medicine_status)
        else:
            # No specific medicines requested (symptom-based search)
            # Show general availability
            medicines_breakdown.append({
                'medicine': 'Requested medicines',
                'available': response.medicine_available,
                'price': str(response.price) if response.price and response.medicine_available else None,
                'alternative': None
            })
        
        # Add per-medicine breakdown to response
        response_data['medicines'] = medicines_breakdown
        
        # Also add requested medicines to response for frontend reference
        response_data['requested_medicines'] = requested_medicines
        
        ranked_responses.append(response_data)

    # Before 2 min: show responses as they arrive (chronological), no ranking
    # After 2 min: apply MCDA ranking
    time_since_creation = timezone.now() - medicine_request.created_at
    ranking_ready = time_since_creation >= timedelta(minutes=RANKING_DELAY_MINUTES)

    if not ranking_ready:
        # Chronological order (submitted_at) - show responses immediately as they arrive
        ranked_responses.sort(key=lambda r: r.get('submitted_at', ''))
        for i, r in enumerate(ranked_responses, 1):
            r['rank'] = i
            r['ranking_pending'] = True
            r['ranking_score'] = None
        return ranked_responses[:limit]

    # MCDA ranking: split by availability, rank each group
    available = [r for r in ranked_responses if r.get('medicine_available')]
    unavailable = [r for r in ranked_responses if not r.get('medicine_available')]
    patient_lat = medicine_request.location_latitude
    patient_lon = medicine_request.location_longitude

    def apply_mcda(items):
        if not items:
            return []
        scored, weights, context = RankingEngine.rank_responses(
            items, patient_lat=patient_lat, patient_lon=patient_lon
        )
        out = []
        for s in scored:
            r = s['response']
            r['ranking_score'] = s['score']
            r['score_breakdown'] = s['score_breakdown']
            r['weights_used'] = s['weights_used']
            r['mcda_context'] = s['context']
            r['ranking_pending'] = False
            out.append(r)
        return out

    ranked_available = apply_mcda(available)
    ranked_unavailable = apply_mcda(unavailable)
    ranked_responses = ranked_available + ranked_unavailable

    for i, r in enumerate(ranked_responses, 1):
        r['rank'] = i

    return ranked_responses[:limit]


def get_live_inventory_ranked(latitude, longitude, medicine_names, limit=10, max_distance_km=50):
    """
    Query LIVE inventory: find pharmacies with requested medicines in stock (available = quantity - reserved).
    Rank by 40% distance, 30% price, 20% availability, 10% rating.
    Returns list of dicts in same shape as pharmacy_responses for chat/frontend.
    """
    from decimal import Decimal
    if not medicine_names or not latitude or not longitude:
        return []
    medicine_names_lower = [m.lower().strip() for m in medicine_names if m and str(m).strip()]
    if not medicine_names_lower:
        return []

    # All inventory rows with stock (available > 0), for active pharmacies
    inv_qs = PharmacyInventory.objects.filter(
        pharmacy__is_active=True,
        quantity__gt=F('reserved_quantity')
    ).select_related('pharmacy')

    # Filter to rows where medicine name matches any requested (contains or equals)
    matching_inv = []
    for inv in inv_qs:
        inv_name_lower = inv.medicine_name.lower()
        for req in medicine_names_lower:
            if req in inv_name_lower or inv_name_lower in req or req == inv_name_lower:
                matching_inv.append(inv)
                break

    # Group by pharmacy
    by_pharmacy = {}
    for inv in matching_inv:
        ph = inv.pharmacy
        pid = ph.pharmacy_id
        if pid not in by_pharmacy:
            by_pharmacy[pid] = {
                'pharmacy': ph,
                'items': [],
                'total_price': 0,
                'total_available': 0,
            }
        avail = inv.quantity - inv.reserved_quantity
        price_val = float(inv.price) if inv.price is not None else 0
        by_pharmacy[pid]['items'].append({
            'inv': inv,
            'available': avail,
            'price': inv.price,
            'price_float': price_val,
        })
        by_pharmacy[pid]['total_price'] += price_val * 1  # per-unit display; could use quantity
        by_pharmacy[pid]['total_available'] += avail

    # Build result per pharmacy with distance and ranking
    results = []
    for pid, data in by_pharmacy.items():
        ph = data['pharmacy']
        if not ph.latitude or not ph.longitude:
            continue
        distance_km = LocationService.calculate_distance(
            latitude, longitude, float(ph.latitude), float(ph.longitude)
        )
        if distance_km > max_distance_km:
            continue
        travel_time = LocationService.estimate_travel_time(distance_km, 'urban')
        prep_time = 0
        total_time = travel_time + prep_time
        rating = float(getattr(ph, 'rating', 0) or 0)
        total_price = data['total_price']
        total_available = data['total_available']

        medicines_breakdown = []
        for it in data['items']:
            inv = it['inv']
            medicines_breakdown.append({
                'medicine': inv.medicine_name,
                'available': True,
                'price': str(inv.price) if inv.price is not None else None,
                'quantity': it['available'],
            })

        # Single display price (sum or first item)
        display_price = data['items'][0]['price_float'] if data['items'] else 0
        if len(data['items']) > 1:
            display_price = total_price  # or first; frontend can show breakdown

        results.append({
            'pharmacy_id': pid,
            'pharmacy_name': ph.name,
            'address': ph.address or '',
            'distance_km': round(distance_km, 2),
            'estimated_travel_time': travel_time,
            'preparation_time': prep_time,
            'total_time_minutes': total_time,
            'price': str(display_price) if display_price else None,
            'medicine_available': True,
            'medicines_breakdown': medicines_breakdown,
            'pharmacy_rating': rating,
            'total_available': total_available,
            'ranking_score': None,  # set below
            'from_live_inventory': True,
            'submitted_at': None,
        })

    if not results:
        return []

    # Normalize and rank: 40% distance, 30% price, 20% availability, 10% rating (higher = better)
    distances = [r['distance_km'] for r in results]
    prices = [float(r['price']) if r.get('price') else 999 for r in results]
    availabilities = [r.get('total_available', 0) for r in results]
    ratings = [r.get('pharmacy_rating', 0) for r in results]

    max_d = max(distances) if distances else 1
    max_p = max(prices) if prices else 1
    max_a = max(availabilities) if availabilities else 1

    for r in results:
        norm_dist = 1 - (r['distance_km'] / max_d) if max_d else 1
        price_val = float(r['price']) if r.get('price') else 999
        norm_price = 1 - (price_val / max_p) if max_p else 1
        norm_avail = min((r.get('total_available', 0) or 0) / 100, 1.0) if max_a else 0
        norm_rating = (r.get('pharmacy_rating', 0) or 0) / 5.0
        r['ranking_score'] = round(0.40 * norm_dist + 0.30 * norm_price + 0.20 * norm_avail + 0.10 * norm_rating, 4)

    results.sort(key=lambda x: x['ranking_score'], reverse=True)
    for i, r in enumerate(results[:limit], 1):
        r['rank'] = i
    return results[:limit]


def simulate_pharmacy_responses(medicine_request):
    """Simulate pharmacy responses for demonstration"""
    # In production, this would query actual pharmacies and send notifications
    
    # Simulate 3 pharmacy responses
    pharmacies = [
        {
            'id': 'ph-001',
            'name': 'HealthFirst Pharmacy',
            'lat': -17.8095,
            'lon': 31.0452,
            'available': True,
            'price': 4.50,
            'prep_time': 15
        },
        {
            'id': 'ph-002',
            'name': 'City Care Pharmacy',
            'lat': -17.8245,
            'lon': 31.0389,
            'available': True,
            'price': 3.80,
            'prep_time': 30
        },
        {
            'id': 'ph-003',
            'name': 'Wellness Pharmacy',
            'lat': -17.8068,
            'lon': 31.0501,
            'available': True,
            'price': 5.20,
            'prep_time': 10
        }
    ]
    
    for pharm in pharmacies:
        distance = LocationService.calculate_distance(
            medicine_request.location_latitude,
            medicine_request.location_longitude,
            pharm['lat'],
            pharm['lon']
        )
        
        travel_time = LocationService.estimate_travel_time(distance, 'urban')
        
        # Try to get pharmacy from database, or use legacy fields
        pharmacy = None
        try:
            pharmacy = Pharmacy.objects.get(pharmacy_id=pharm['id'])
        except Pharmacy.DoesNotExist:
            pass  # Use legacy pharmacy_name field
        
        PharmacyResponse.objects.create(
            request=medicine_request,
            pharmacy=pharmacy,
            pharmacy_name=pharm['name'] if not pharmacy else '',
            medicine_available=pharm['available'],
            price=pharm['price'],
            preparation_time=pharm['prep_time'],
            distance_km=distance,
            estimated_travel_time=travel_time
        )
    
    medicine_request.status = 'responses_received'
    medicine_request.save()


@api_view(['GET'])
@permission_classes([AllowAny])
def get_pharmacy_responses(request, request_id):
    """
    Get pharmacy responses for a medicine request
    
    SECURITY: Requires conversation_id or session_id to verify ownership
    Each request can only be accessed by the user who created it.
    """
    try:
        medicine_request = MedicineRequest.objects.get(request_id=request_id)
        
        # SECURITY: Verify ownership - require conversation_id or session_id
        conversation_id = request.query_params.get('conversation_id')
        session_id = request.query_params.get('session_id')
        
        if not conversation_id and not session_id:
            return Response(
                {'error': 'conversation_id or session_id is required to verify ownership'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Verify the request belongs to the provided conversation/session
        if conversation_id:
            try:
                conversation = ChatConversation.objects.get(conversation_id=conversation_id)
                if medicine_request.conversation != conversation:
                    return Response(
                        {'error': 'Medicine request does not belong to this conversation'},
                        status=status.HTTP_403_FORBIDDEN
                    )
            except ChatConversation.DoesNotExist:
                return Response(
                    {'error': 'Conversation not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
        elif session_id:
            if medicine_request.conversation.session_id != session_id:
                return Response(
                    {'error': 'Medicine request does not belong to this session'},
                    status=status.HTTP_403_FORBIDDEN
                )
        
        # Only return responses for this specific request
        responses = medicine_request.pharmacy_responses.all()
        
        # ALWAYS calculate distance and travel time if missing
        from .services import LocationService
        
        for response in responses:
            # Calculate distance and travel time if missing
            if not response.distance_km and medicine_request.location_latitude and medicine_request.location_longitude:
                pharmacy_lat = None
                pharmacy_lon = None
                
                # Try to get coordinates from pharmacy FK
                if response.pharmacy and response.pharmacy.latitude and response.pharmacy.longitude:
                    pharmacy_lat = response.pharmacy.latitude
                    pharmacy_lon = response.pharmacy.longitude
                
                if pharmacy_lat and pharmacy_lon:
                    try:
                        distance_km = LocationService.calculate_distance(
                            medicine_request.location_latitude,
                            medicine_request.location_longitude,
                            pharmacy_lat,
                            pharmacy_lon
                        )
                        travel_time = LocationService.estimate_travel_time(distance_km, 'urban')
                        
                        # Update response with calculated values
                        response.distance_km = distance_km
                        response.estimated_travel_time = travel_time
                        response.save(update_fields=['distance_km', 'estimated_travel_time'])
                        print(f"[INFO] Calculated distance: {distance_km:.2f}km, travel time: {travel_time}min for response {response.response_id}")
                    except Exception as e:
                        print(f"[WARNING] Error calculating distance/time for response {response.response_id}: {e}")
            
            # Calculate total time (preparation + travel)
            total_time = response.preparation_time
            if response.estimated_travel_time:
                total_time += response.estimated_travel_time
            response.total_time_minutes = total_time
        
        # Sort by total time
        sorted_responses = sorted(
            responses,
            key=lambda x: getattr(x, 'total_time_minutes', 999)
        )
        
        serializer = PharmacyResponseSerializer(sorted_responses, many=True)
        response_data = serializer.data
        
        # Add total_time_minutes to each response (not in serializer fields)
        for i, response in enumerate(sorted_responses):
            response_data[i]['total_time_minutes'] = getattr(response, 'total_time_minutes', response.preparation_time)
        
        return Response(response_data, status=status.HTTP_200_OK)
    
    except MedicineRequest.DoesNotExist:
        return Response(
            {'error': 'Medicine request not found'},
            status=status.HTTP_404_NOT_FOUND
        )


@api_view(['GET'])
@permission_classes([AllowAny])
def get_conversation(request, conversation_id):
    """Get conversation history"""
    try:
        conversation = ChatConversation.objects.get(conversation_id=conversation_id)
        serializer = ChatConversationSerializer(conversation)
        return Response(serializer.data, status=status.HTTP_200_OK)
    except ChatConversation.DoesNotExist:
        return Response(
            {'error': 'Conversation not found'},
            status=status.HTTP_404_NOT_FOUND
        )


@api_view(['POST'])
@permission_classes([AllowAny])
def rate_pharmacy(request):
    """
    UC-P12: Patient submits rating for a pharmacy (anonymous or after visit).
    POST /api/chatbot/rate-pharmacy/
    Body: { "pharmacy_id": "simed-01", "rating": 5, "response_id": "uuid" (optional), "notes": "" (optional) }
    """
    pharmacy_id = request.data.get('pharmacy_id')
    rating_val = request.data.get('rating')
    response_id = request.data.get('response_id')
    notes = request.data.get('notes', '')[:500]

    if not pharmacy_id:
        return Response({'error': 'pharmacy_id required'}, status=status.HTTP_400_BAD_REQUEST)
    if rating_val is None:
        return Response({'error': 'rating required (1-5)'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        rating_val = int(rating_val)
        if rating_val < 1 or rating_val > 5:
            raise ValueError()
    except (ValueError, TypeError):
        return Response({'error': 'rating must be 1-5'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        pharmacy = Pharmacy.objects.get(pharmacy_id=pharmacy_id)
    except Pharmacy.DoesNotExist:
        return Response({'error': 'Pharmacy not found'}, status=status.HTTP_404_NOT_FOUND)

    response_obj = None
    if response_id:
        try:
            response_obj = PharmacyResponse.objects.get(response_id=response_id)
            if response_obj.pharmacy != pharmacy:
                return Response({'error': 'response_id does not match pharmacy'}, status=status.HTTP_400_BAD_REQUEST)
        except PharmacyResponse.DoesNotExist:
            pass

    PharmacyRating.objects.create(
        pharmacy=pharmacy,
        response=response_obj,
        rating=rating_val,
        notes=notes,
    )
    # Update pharmacy running average
    from django.db.models import Avg, Count
    agg = PharmacyRating.objects.filter(pharmacy=pharmacy).aggregate(avg=Avg('rating'), cnt=Count('id'))
    pharmacy.rating = round(agg['avg'] or 0, 2)
    pharmacy.rating_count = agg['cnt']
    pharmacy.save(update_fields=['rating', 'rating_count'])

    return Response({
        'message': 'Thank you for your rating',
        'pharmacy_id': pharmacy_id,
        'pharmacy_name': pharmacy.name,
        'rating': pharmacy.rating,
        'rating_count': pharmacy.rating_count,
    }, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([AllowAny])
def check_drug_interactions(request):
    """
    UC-P08/UC-S05: Check for drug interactions between medicines.
    POST /api/chatbot/check-interactions/
    Body: { "medicines": ["aspirin", "ibuprofen", "warfarin"] }
    """
    medicines = request.data.get('medicines', [])
    if isinstance(medicines, str):
        medicines = [m.strip() for m in medicines.split(',') if m.strip()]
    if not medicines:
        return Response({'error': 'medicines list required'}, status=status.HTTP_400_BAD_REQUEST)
    if len(medicines) < 2:
        return Response({
            'medicines': medicines,
            'interactions': [],
            'message': 'At least 2 medicines needed to check interactions',
        }, status=status.HTTP_200_OK)

    interactions = DrugInteractionService.check_interactions(medicines)
    severity_order = {'severe': 3, 'moderate': 2, 'mild': 1}
    interactions.sort(key=lambda x: severity_order.get(x['severity'], 0), reverse=True)

    return Response({
        'medicines': medicines,
        'interactions': interactions,
        'has_interactions': len(interactions) > 0,
        'disclaimer': 'This is not a substitute for professional medical advice. Consult a healthcare provider.',
    }, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([AllowAny])
def suggest_alternatives(request):
    """Suggest alternative medicines"""
    unavailable_medicine = request.data.get('medicine')
    symptoms = request.data.get('symptoms', [])
    
    if not unavailable_medicine:
        return Response(
            {'error': 'Medicine name required'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    chatbot_service = get_chatbot_service()
    if not chatbot_service:
        return Response({
            'error': 'Chatbot service is currently unavailable'
        }, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    
    alternatives = chatbot_service.suggest_alternatives(unavailable_medicine, symptoms)
    
    return Response({
        'unavailable_medicine': unavailable_medicine,
        'alternatives': alternatives
    }, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([AllowAny])
def upload_prescription(request):
    """
    Upload prescription image and extract medicine information using OCR
    """
    if 'prescription_image' not in request.FILES:
        return Response(
            {'error': 'No prescription image provided'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    image_file = request.FILES['prescription_image']
    
    # Validate image
    if not image_file.content_type.startswith('image/'):
        return Response(
            {'error': 'File must be an image'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    try:
        # Initialize OCR service
        ocr_service = OCRService()
        
        # Extract prescription text
        ocr_result = ocr_service.extract_prescription_text(image_file)
        
        # Get or create conversation
        session_id = request.data.get('session_id') or str(uuid.uuid4())
        conversation, created = ChatConversation.objects.get_or_create(
            session_id=session_id,
            defaults={'status': 'active'}
        )
        
        # Save user message about prescription upload
        medicines_list = ocr_result['medicines'] if ocr_result['medicines'] else []
        user_message = ChatMessage.objects.create(
            conversation=conversation,
            role='user',
            content=f"Uploaded prescription with medicines: {', '.join(medicines_list) if medicines_list else 'Unable to read'}"
        )
        
        # Store extracted medicines in conversation metadata for later use
        if medicines_list:
            conversation.context_metadata['prescription_medicines'] = medicines_list
            conversation.save(update_fields=['context_metadata'])
            print(f"[INFO] Stored prescription medicines in conversation metadata: {medicines_list}")
        
        # Create medicine request if medicines were extracted
        medicine_request_id = None
        if ocr_result['medicines']:
            # Get location from request
            latitude = request.data.get('location_latitude')
            longitude = request.data.get('location_longitude')
            address = request.data.get('location_address', '')
            
            if latitude and longitude:
                medicine_request = create_medicine_request(
                    conversation=conversation,
                    user=request.user if request.user.is_authenticated else None,
                    intent='prescription',
                    medicines=ocr_result['medicines'],
                    symptoms='',
                    latitude=float(latitude),
                    longitude=float(longitude),
                    address=address,
                    suburb=request.data.get('location_suburb', '')
                )
                medicine_request_id = medicine_request.request_id
        
        return Response({
            'medicines': ocr_result['medicines'],
            'dosages': ocr_result['dosages'],
            'raw_text': ocr_result['raw_text'],
            'confidence': ocr_result['confidence'],
            'conversation_id': conversation.conversation_id,
            'medicine_request_id': medicine_request_id,
            'message': 'Prescription processed successfully' if ocr_result['medicines'] else 'Could not extract medicines from prescription. Please try again or enter medicines manually.'
        }, status=status.HTTP_200_OK)
    
    except Exception as e:
        return Response({
            'error': f'Error processing prescription: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([AllowAny])
def get_pharmacist_requests(request):
    """
    Get all medicine requests for a pharmacist (pharmacist dashboard)
    Query params: pharmacist_id (required)
    """
    pharmacist_id = request.query_params.get('pharmacist_id')
    
    if not pharmacist_id:
        return Response(
            {'error': 'pharmacist_id query parameter is required'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    try:
        pharmacist = Pharmacist.objects.get(pharmacist_id=pharmacist_id)
    except Pharmacist.DoesNotExist:
        return Response(
            {'error': 'Pharmacist not found'},
            status=status.HTTP_404_NOT_FOUND
        )
    
    # Get all requests for this pharmacist:
    # 1. Active requests (broadcasting, awaiting responses, or received responses)
    # 2. Completed/expired requests where this pharmacist has responded (for history)
    from django.db.models import Q
    
    # Use a single query with Q objects to combine conditions
    all_requests = MedicineRequest.objects.filter(
        Q(status__in=['broadcasting', 'awaiting_responses', 'responses_received']) |
        Q(status__in=['completed', 'expired'], pharmacy_responses__pharmacist=pharmacist)
    ).distinct().order_by('-created_at')
    
    # If pharmacist's pharmacy has location, filter nearby requests
    # Use a reasonable radius for Zimbabwe (50km - covers most urban areas)
    from .services import LocationService
    MAX_DISTANCE_KM = 50.0  # Maximum distance to show ACTIVE requests (50km radius)
    # Note: Completed requests are shown regardless of distance (they're history)
    nearby_requests = []
    
    if pharmacist.pharmacy and pharmacist.pharmacy.latitude and pharmacist.pharmacy.longitude:
        pharmacy_lat = pharmacist.pharmacy.latitude
        pharmacy_lon = pharmacist.pharmacy.longitude
        
        for req in all_requests:
            # For active requests, filter by distance
            # For completed requests (history), show regardless of distance
            is_completed = req.status in ['completed', 'expired']
            
            # Only show requests with location
            if req.location_latitude and req.location_longitude:
                distance = LocationService.calculate_distance(
                    req.location_latitude,
                    req.location_longitude,
                    pharmacy_lat,
                    pharmacy_lon
                )
                # Show if: (1) within MAX_DISTANCE_KM OR (2) completed/expired (history)
                if distance <= MAX_DISTANCE_KM or is_completed:
                    nearby_requests.append((req, distance))
                else:
                    print(f"[INFO] Active request {req.request_id} is {distance:.2f}km away (beyond {MAX_DISTANCE_KM}km limit), not showing to pharmacist {pharmacist_id}")
            else:
                # Include requests without location (show all)
                # These requests don't have coordinates, so we can't filter by distance
                nearby_requests.append((req, None))
    else:
        # Pharmacy has no location - show all requests
        # This allows pharmacies without coordinates to still see all requests
        nearby_requests = [(req, None) for req in all_requests]
        if pharmacist.pharmacy:
            print(f"[WARNING] Pharmacy {pharmacist.pharmacy.pharmacy_id} has no coordinates - showing all requests")
    
    # Check which requests this pharmacist has responded to or declined
    request_data = []
    for req, distance in nearby_requests:
        has_responded = PharmacyResponse.objects.filter(
            request=req,
            pharmacist=pharmacist
        ).exists()
        has_declined = PharmacistDecline.objects.filter(
            request=req,
            pharmacist=pharmacist
        ).exists()
        
        # Get response count for this request
        response_count = req.pharmacy_responses.count()
        
        short_id = str(req.request_id).replace('-', '')[:8].upper()
        request_data.append({
            'request_id': str(req.request_id),
            'short_request_id': short_id,
            'request_type': req.request_type,
            'medicine_names': req.medicine_names,
            'symptoms': req.symptoms,
            'location_address': req.location_address,
            'location_suburb': req.location_suburb or '',
            'location_latitude': req.location_latitude,
            'location_longitude': req.location_longitude,
            'created_at': req.created_at,
            'expires_at': req.expires_at,
            'status': req.status,
            'has_responded': has_responded,
            'has_declined': has_declined,
            'response_count': response_count,
            'distance_km': round(distance, 2) if distance else None
        })
    
    print(f"[INFO] Returning {len(request_data)} requests for pharmacist {pharmacist_id}")
    return Response(request_data, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([AllowAny])
def submit_pharmacy_response(request, request_id):
    """
    Submit pharmacist response to a medicine request
    Requires pharmacist_id (or can use pharmacy_id for backward compatibility)
    """
    try:
        medicine_request = MedicineRequest.objects.get(request_id=request_id)
    except MedicineRequest.DoesNotExist:
        return Response(
            {'error': 'Medicine request not found'},
            status=status.HTTP_404_NOT_FOUND
        )
    
    # Get pharmacist_id (preferred) or pharmacy_id (backward compatibility)
    pharmacist_id = request.data.get('pharmacist_id')
    pharmacy_id = request.data.get('pharmacy_id')
    
    pharmacist = None
    pharmacy = None
    
    if pharmacist_id:
        try:
            pharmacist = Pharmacist.objects.get(pharmacist_id=pharmacist_id)
            pharmacy = pharmacist.pharmacy
        except Pharmacist.DoesNotExist:
            return Response(
                {'error': 'Pharmacist not found'},
                status=status.HTTP_404_NOT_FOUND
            )
    elif pharmacy_id:
        try:
            pharmacy = Pharmacy.objects.get(pharmacy_id=pharmacy_id)
        except Pharmacy.DoesNotExist:
            return Response(
                {'error': 'Pharmacy not found'},
                status=status.HTTP_404_NOT_FOUND
            )
    else:
        return Response(
            {'error': 'pharmacist_id or pharmacy_id is required'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    medicine_available = request.data.get('medicine_available', False)
    price_val = request.data.get('price')
    notes_text = request.data.get('notes', '') or ''
    
    # Parse notes when structured fields are empty: e.g. "paracetamol $2" or "available $3.50"
    if not medicine_available and not price_val and notes_text.strip():
        import re
        price_match = re.search(r'\$?\s*(\d+(?:\.\d{1,2})?)\s*(?:dollars?|usd)?', notes_text, re.IGNORECASE)
        if price_match:
            try:
                parsed_price = float(price_match.group(1))
                if parsed_price > 0:
                    medicine_available = True
                    price_val = str(parsed_price)
                    request.data['price'] = price_val
                    request.data['medicine_available'] = True
                    print(f"[INFO] Parsed price ${parsed_price} from notes for response")
            except (ValueError, TypeError):
                pass
    
    # Check if this pharmacist already responded
    existing_response = None
    if pharmacist:
        existing_response = PharmacyResponse.objects.filter(
            request=medicine_request,
            pharmacist=pharmacist
        ).first()
    elif pharmacy:
        # Backward compatibility: check by pharmacy ForeignKey
        existing_response = PharmacyResponse.objects.filter(
            request=medicine_request,
            pharmacy=pharmacy
        ).first()
    
    # ALWAYS calculate distance and travel time from patient location to pharmacy location
    # This ensures distance and time are always populated for ranking
    pharmacy_lat = request.data.get('pharmacy_latitude') or (pharmacy.latitude if pharmacy else None)
    pharmacy_lon = request.data.get('pharmacy_longitude') or (pharmacy.longitude if pharmacy else None)
    
    distance_km = None
    travel_time = None
    
    # Calculate distance and travel time if we have both patient and pharmacy coordinates
    if pharmacy_lat and pharmacy_lon and medicine_request.location_latitude and medicine_request.location_longitude:
        try:
            distance_km = LocationService.calculate_distance(
                medicine_request.location_latitude,
                medicine_request.location_longitude,
                float(pharmacy_lat),
                float(pharmacy_lon)
            )
            travel_time = LocationService.estimate_travel_time(distance_km, 'urban')
            print(f"[INFO] Calculated distance: {distance_km:.2f}km, travel time: {travel_time}min from patient to {pharmacy.name if pharmacy else 'pharmacy'}")
        except Exception as e:
            print(f"[WARNING] Error calculating distance/time: {e}")
            # Continue without distance/time if calculation fails
    
    # Handle per-medicine responses (if provided)
    medicine_responses = request.data.get('medicine_responses', [])
    
    # If medicine_responses is provided, calculate overall availability and price from it
    if medicine_responses:
        # Calculate overall medicine_available (true if ANY medicine is available)
        calculated_available = any(
            item.get('available', False) for item in medicine_responses 
            if isinstance(item, dict)
        )
        # Use calculated availability if provided medicine_available is not explicitly set
        if 'medicine_available' not in request.data or request.data.get('medicine_available') is None:
            medicine_available = calculated_available
        
        # Calculate total price from per-medicine prices if overall price not provided
        if not request.data.get('price'):
            total_price = sum(
                float(item.get('price', 0) or 0) 
                for item in medicine_responses 
                if isinstance(item, dict) and item.get('available', False)
            )
            if total_price > 0:
                request.data['price'] = str(total_price)

    expiry_date = None
    expiry_raw = request.data.get('expiry_date')
    if expiry_raw:
        try:
            from datetime import datetime
            if isinstance(expiry_raw, str):
                expiry_date = datetime.strptime(expiry_raw[:10], '%Y-%m-%d').date()
            elif hasattr(expiry_raw, 'year'):
                expiry_date = expiry_raw
        except (ValueError, TypeError):
            pass
    
    if existing_response:
        # Update existing response - ALWAYS update distance and travel time if calculated
        existing_response.medicine_available = medicine_available
        existing_response.price = request.data.get('price')
        existing_response.preparation_time = request.data.get('preparation_time', 0)
        existing_response.quantity = request.data.get('quantity')
        existing_response.expiry_date = expiry_date
        existing_response.medicine_responses = medicine_responses
        existing_response.alternative_medicines = request.data.get('alternative_medicines', [])
        existing_response.notes = request.data.get('notes', '')
        # Update distance and travel time if calculated (or keep existing if not calculated)
        if distance_km is not None:
            existing_response.distance_km = distance_km
        if travel_time is not None:
            existing_response.estimated_travel_time = travel_time
        existing_response.save()
        response_obj = existing_response
    else:
        # Create new response
        response_obj = PharmacyResponse.objects.create(
            request=medicine_request,
            pharmacy=pharmacy,
            pharmacist=pharmacist,
            pharmacy_name=pharmacy.name if pharmacy else request.data.get('pharmacy_name', ''),
            pharmacist_name=pharmacist.full_name if pharmacist else request.data.get('pharmacist_name', ''),
            medicine_available=medicine_available,
            price=request.data.get('price'),
            preparation_time=request.data.get('preparation_time', 0),
            quantity=request.data.get('quantity'),
            expiry_date=expiry_date,
            distance_km=distance_km,
            estimated_travel_time=travel_time,
            medicine_responses=medicine_responses,
            alternative_medicines=request.data.get('alternative_medicines', []),
            notes=request.data.get('notes', '')
        )
    
    # Update request status if needed
    if medicine_request.status == 'broadcasting':
        medicine_request.status = 'awaiting_responses'
    
    # Update status to 'responses_received' when first response comes in
    if medicine_request.status == 'awaiting_responses':
        medicine_request.status = 'responses_received'
    
    medicine_request.save(update_fields=['status'])
    
    serializer = PharmacyResponseSerializer(response_obj)

    # Create patient notification when a pharmacy responds
    try:
        conversation = medicine_request.conversation
        session_id = conversation.session_id
        short_req_id = str(medicine_request.request_id).replace('-', '')[:8].upper()
        # Build a human-friendly medicine label, e.g. "Ibuprofen 400mg" or "your request"
        med_names = medicine_request.medicine_names or []
        if isinstance(med_names, list) and med_names:
            first_med = str(med_names[0])
        else:
            first_med = 'your request'
        # Use response price if available
        price_str = None
        if response_obj.price:
            try:
                price_str = f"${float(response_obj.price):.2f}"
            except (ValueError, TypeError):
                price_str = str(response_obj.price)
        title = f"{pharmacy.name if pharmacy else response_obj.pharmacy_name} responded to your request #{short_req_id}"
        if price_str and first_med:
            body = f"{first_med} available for {price_str}."
        elif first_med:
            body = f"{first_med} availability update."
        else:
            body = "New pharmacy response to your request."
        PatientNotification.objects.create(
            session_id=session_id,
            notification_type='pharmacy_response',
            title=title,
            body=body,
            related_request_id=medicine_request.request_id,
            related_response_id=response_obj.response_id,
        )
    except Exception as e:
        # Do not fail the main response flow if notification creation fails
        print(f"[WARNING] Failed to create patient notification for response {response_obj.response_id}: {e}")

    return Response(serializer.data, status=status.HTTP_201_CREATED if not existing_response else status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([AllowAny])
def decline_pharmacy_request(request, request_id):
    """
    Pharmacist declines to respond to a medicine request
    POST body: { "pharmacist_id": "uuid", "reason": "optional" }
    """
    try:
        medicine_request = MedicineRequest.objects.get(request_id=request_id)
    except MedicineRequest.DoesNotExist:
        return Response({'error': 'Request not found'}, status=status.HTTP_404_NOT_FOUND)

    pharmacist_id = request.data.get('pharmacist_id')
    if not pharmacist_id:
        return Response({'error': 'pharmacist_id is required'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        pharmacist = Pharmacist.objects.get(pharmacist_id=pharmacist_id)
    except Pharmacist.DoesNotExist:
        return Response({'error': 'Pharmacist not found'}, status=status.HTTP_404_NOT_FOUND)

    if PharmacyResponse.objects.filter(request=medicine_request, pharmacist=pharmacist).exists():
        return Response({'error': 'Already responded to this request'}, status=status.HTTP_400_BAD_REQUEST)

    decline_obj, created = PharmacistDecline.objects.get_or_create(
        request=medicine_request,
        pharmacist=pharmacist,
        defaults={'reason': request.data.get('reason', '')}
    )
    if not created:
        return Response({'message': 'Already declined', 'declined_at': decline_obj.declined_at}, status=status.HTTP_200_OK)

    return Response({
        'message': 'Request declined',
        'request_id': str(request_id),
        'declined_at': decline_obj.declined_at
    }, status=status.HTTP_201_CREATED)


@api_view(['GET', 'POST'])
@permission_classes([AllowAny])
def pharmacist_inventory(request):
    """
    GET: List inventory for pharmacist's pharmacy
        ?pharmacist_id=uuid
    POST: Update inventory (bulk)
        { "pharmacist_id": "uuid", "items": [{ "medicine_name": "paracetamol", "quantity": 100, "low_stock_threshold": 10 }, ...] }
    """
    pharmacist_id = request.query_params.get('pharmacist_id') if request.method == 'GET' else request.data.get('pharmacist_id')
    if not pharmacist_id:
        return Response({'error': 'pharmacist_id is required'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        pharmacist = Pharmacist.objects.get(pharmacist_id=pharmacist_id)
        pharmacy = pharmacist.pharmacy
    except Pharmacist.DoesNotExist:
        return Response({'error': 'Pharmacist not found'}, status=status.HTTP_404_NOT_FOUND)
    if not pharmacy:
        return Response({'error': 'Pharmacist has no pharmacy'}, status=status.HTTP_400_BAD_REQUEST)

    if request.method == 'GET':
        items = list(PharmacyInventory.objects.filter(pharmacy=pharmacy).order_by('medicine_name'))
        in_stock = sum(1 for i in items if i.quantity >= i.low_stock_threshold)
        low_stock = sum(1 for i in items if 0 < i.quantity < i.low_stock_threshold)
        out_of_stock = sum(1 for i in items if i.quantity <= 0)

        return Response({
            'pharmacy_id': pharmacy.pharmacy_id,
            'pharmacy_name': pharmacy.name,
            'summary': {
                'total_medicines': len(items),
                'in_stock': in_stock,
                'low_stock': low_stock,
                'out_of_stock': out_of_stock,
            },
            'items': [
                {
                    'medicine_name': i.medicine_name,
                    'quantity': i.quantity,
                    'reserved_quantity': i.reserved_quantity,
                    'available_quantity': i.quantity - i.reserved_quantity,
                    'low_stock_threshold': i.low_stock_threshold,
                    'price': str(i.price) if i.price is not None else None,
                    'price_missing': i.price is None,
                    'status': 'out_of_stock' if i.quantity <= 0 else ('low_stock' if i.quantity < i.low_stock_threshold else 'in_stock'),
                    'updated_at': i.updated_at,
                }
                for i in items
            ],
        }, status=status.HTTP_200_OK)

    # POST - update inventory (price required so patients see and rank by price)
    items_data = request.data.get('items', [])
    if not items_data:
        return Response({'error': 'items array is required'}, status=status.HTTP_400_BAD_REQUEST)

    updated = []
    for item in items_data:
        medicine_name = (item.get('medicine_name') or '').strip()
        if not medicine_name:
            continue
        quantity = int(item.get('quantity', 0))
        low_threshold = int(item.get('low_stock_threshold', 10))

        # Price is required: used for ranking and display; keep updated for accuracy
        if 'price' not in item:
            return Response(
                {'error': f'Each item must include "price" (number). Medicine "{medicine_name}" is missing price. Prices should be kept updated for patient display and ranking.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        try:
            price_val = float(item.get('price'))
            if price_val < 0:
                price_val = 0
        except (TypeError, ValueError):
            return Response(
                {'error': f'Invalid "price" for "{medicine_name}". Must be a number (e.g. 5.00).'},
                status=status.HTTP_400_BAD_REQUEST
            )

        defaults = {
            'quantity': max(0, quantity),
            'low_stock_threshold': max(1, low_threshold),
            'price': price_val,
        }
        inv, created = PharmacyInventory.objects.update_or_create(
            pharmacy=pharmacy,
            medicine_name=medicine_name.lower(),
            defaults=defaults
        )
        updated.append({
            'medicine_name': inv.medicine_name,
            'quantity': inv.quantity,
            'low_stock_threshold': inv.low_stock_threshold,
            'price': str(inv.price) if inv.price is not None else None,
        })

    return Response({
        'message': 'Inventory updated',
        'updated_count': len(updated),
        'items': updated,
    }, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([AllowAny])
def pharmacist_reservations_list(request):
    """
    GET: List pending reservations for pharmacist's pharmacy.
    Query: ?pharmacist_id=uuid
    """
    pharmacist_id = request.query_params.get('pharmacist_id')
    if not pharmacist_id:
        return Response({'error': 'pharmacist_id is required'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        pharmacist = Pharmacist.objects.get(pharmacist_id=pharmacist_id)
        pharmacy = pharmacist.pharmacy
    except Pharmacist.DoesNotExist:
        return Response({'error': 'Pharmacist not found'}, status=status.HTTP_404_NOT_FOUND)
    now = timezone.now()
    pending = Reservation.objects.filter(
        pharmacy=pharmacy,
        status__in=['pending', 'confirmed'],
        expires_at__gt=now,
    ).order_by('reserved_at')
    out = []
    for r in pending:
        out.append({
            'reservation_id': str(r.reservation_id),
            'medicine_name': r.medicine_name,
            'quantity': r.quantity,
            'price_at_reservation': str(r.price_at_reservation) if r.price_at_reservation else None,
            'status': r.status,
            'reserved_at': r.reserved_at.isoformat(),
            'expires_at': r.expires_at.isoformat(),
            'patient_phone': r.patient_phone or '',
        })
    return Response({'pharmacy_id': pharmacy.pharmacy_id, 'reservations': out}, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([AllowAny])
def pharmacist_reservation_confirm(request, reservation_id):
    """
    Pharmacist confirms reservation (medicine ready for pickup).
    POST body: { "pharmacist_id": "uuid" }
    """
    pharmacist_id = request.data.get('pharmacist_id')
    if not pharmacist_id:
        return Response({'error': 'pharmacist_id is required'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        pharmacist = Pharmacist.objects.get(pharmacist_id=pharmacist_id)
        pharmacy = pharmacist.pharmacy
    except Pharmacist.DoesNotExist:
        return Response({'error': 'Pharmacist not found'}, status=status.HTTP_404_NOT_FOUND)
    try:
        reservation = Reservation.objects.get(reservation_id=reservation_id, pharmacy=pharmacy)
    except Reservation.DoesNotExist:
        return Response({'error': 'Reservation not found'}, status=status.HTTP_404_NOT_FOUND)
    if reservation.status not in ('pending', 'confirmed'):
        return Response({'error': f'Reservation is {reservation.status}'}, status=status.HTTP_400_BAD_REQUEST)
    if reservation.expires_at <= timezone.now():
        reservation.status = 'expired'
        reservation.save(update_fields=['status'])
        return Response({'error': 'Reservation has expired'}, status=status.HTTP_400_BAD_REQUEST)
    reservation.status = 'confirmed'
    reservation.confirmed_at = timezone.now()
    reservation.save(update_fields=['status', 'confirmed_at'])
    return Response({
        'success': True,
        'reservation_id': str(reservation.reservation_id),
        'message': 'Reservation confirmed. Patient can pick up.',
    }, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([AllowAny])
def pharmacist_reservation_complete(request, reservation_id):
    """
    Mark reservation as picked up: decrement stock, release reserved quantity, mark complete.
    POST body: { "pharmacist_id": "uuid" }
    """
    from django.db import transaction
    pharmacist_id = request.data.get('pharmacist_id')
    if not pharmacist_id:
        return Response({'error': 'pharmacist_id is required'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        pharmacist = Pharmacist.objects.get(pharmacist_id=pharmacist_id)
        pharmacy = pharmacist.pharmacy
    except Pharmacist.DoesNotExist:
        return Response({'error': 'Pharmacist not found'}, status=status.HTTP_404_NOT_FOUND)
    try:
        reservation = Reservation.objects.get(reservation_id=reservation_id, pharmacy=pharmacy)
    except Reservation.DoesNotExist:
        return Response({'error': 'Reservation not found'}, status=status.HTTP_404_NOT_FOUND)
    if reservation.status not in ('pending', 'confirmed'):
        return Response({'error': f'Reservation is {reservation.status}'}, status=status.HTTP_400_BAD_REQUEST)
    if reservation.expires_at <= timezone.now():
        reservation.status = 'expired'
        reservation.save(update_fields=['status'])
        inv = PharmacyInventory.objects.filter(
            pharmacy=pharmacy,
            medicine_name__iexact=reservation.medicine_name
        ).first()
        if inv:
            inv.reserved_quantity = max(0, inv.reserved_quantity - reservation.quantity)
            inv.save(update_fields=['reserved_quantity'])
        return Response({'error': 'Reservation had expired; reserved stock released.'}, status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        inv = PharmacyInventory.objects.filter(
            pharmacy=pharmacy,
            medicine_name__iexact=reservation.medicine_name
        ).select_for_update().first()
        if not inv:
            return Response({'error': 'Inventory item not found'}, status=status.HTTP_404_NOT_FOUND)
        inv.quantity = max(0, inv.quantity - reservation.quantity)
        inv.reserved_quantity = max(0, inv.reserved_quantity - reservation.quantity)
        inv.save(update_fields=['quantity', 'reserved_quantity'])
        reservation.status = 'picked_up'
        reservation.picked_up_at = timezone.now()
        reservation.save(update_fields=['status', 'picked_up_at'])

    return Response({
        'success': True,
        'reservation_id': str(reservation.reservation_id),
        'message': 'Pick-up completed. Stock decremented.',
    }, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([AllowAny])
def reserve_medicine(request):
    """
    Reserve medicine at a pharmacy (locks stock for 2 hours).
    Body: { "pharmacy_id": "str", "medicine_name": "str (optional if conversation_id provided)", "quantity": 1, "conversation_id": "uuid" or "session_id": "str", "patient_phone": "optional" }
    If medicine_name is omitted but conversation_id is sent, the first suggested_medicine from that conversation is used.
    Concurrency-safe: uses select_for_update so simultaneous reservations see correct available count.
    """
    from django.db import transaction
    pharmacy_id = request.data.get('pharmacy_id')
    medicine_name = (request.data.get('medicine_name') or '').strip()
    quantity = max(1, int(request.data.get('quantity', 1)))
    conversation_id = request.data.get('conversation_id')
    session_id = request.data.get('session_id', '')
    patient_phone = (request.data.get('patient_phone') or '').strip()

    if not pharmacy_id:
        return Response(
            {'error': 'pharmacy_id is required'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        pharmacy = Pharmacy.objects.get(pharmacy_id=pharmacy_id)
    except Pharmacy.DoesNotExist:
        return Response({'error': 'Pharmacy not found'}, status=status.HTTP_404_NOT_FOUND)

    conversation = None
    if conversation_id:
        try:
            conversation = ChatConversation.objects.get(conversation_id=conversation_id)
            if not session_id:
                session_id = conversation.session_id
            # Derive medicine_name from conversation if not provided (e.g. frontend only sent pharmacy_id + conversation_id)
            if not medicine_name and conversation.context_metadata:
                suggested = conversation.context_metadata.get('suggested_medicines') or []
                if isinstance(suggested, list) and len(suggested) > 0:
                    first = suggested[0]
                    medicine_name = (first if isinstance(first, str) else str(first)).strip()
        except ChatConversation.DoesNotExist:
            pass

    if not medicine_name:
        return Response(
            {'error': 'medicine_name is required. Include it in the request body, or ensure the conversation has suggested_medicines (e.g. from the last search).'},
            status=status.HTTP_400_BAD_REQUEST
        )

    with transaction.atomic():
        # Match inventory by pharmacy and medicine name (case-insensitive)
        inv_qs = PharmacyInventory.objects.filter(
            pharmacy=pharmacy,
            medicine_name__iexact=medicine_name
        ).select_for_update()
        inv = inv_qs.first()
        if not inv:
            return Response(
                {'error': f'Medicine "{medicine_name}" not found at this pharmacy'},
                status=status.HTTP_404_NOT_FOUND
            )
        available = inv.quantity - inv.reserved_quantity
        if available < quantity:
            return Response(
                {'error': f'Only {available} available (you requested {quantity})'},
                status=status.HTTP_400_BAD_REQUEST
            )
        expires_at = timezone.now() + timedelta(hours=2)
        reservation = Reservation.objects.create(
            pharmacy=pharmacy,
            conversation=conversation,
            session_id=session_id or str(uuid.uuid4()),
            patient_phone=patient_phone,
            medicine_name=inv.medicine_name,
            quantity=quantity,
            price_at_reservation=inv.price,
            status='pending',
            expires_at=expires_at,
        )
        inv.reserved_quantity += quantity
        inv.save(update_fields=['reserved_quantity'])

    return Response({
        'success': True,
        'reservation_id': str(reservation.reservation_id),
        'expires_at': expires_at.isoformat(),
        'message': f'Reservation confirmed. Please pick up within 2 hours. {quantity} x {inv.medicine_name} at {pharmacy.name}.',
        'pharmacy_name': pharmacy.name,
        'medicine_name': inv.medicine_name,
        'quantity': quantity,
    }, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([AllowAny])
def record_purchase(request):
    """
    Record a sale so pharmacy inventory is decremented.
    Call this when a user buys medicine from a pharmacy (e.g. after they collect in-store or complete order).
    Body: { "pharmacy_id": "uuid", "items": [{ "medicine_name": "paracetamol", "quantity": 2 }, ...] }
    Optional: "response_id" or "medicine_request_id" for audit.
    """
    pharmacy_id = request.data.get('pharmacy_id')
    if not pharmacy_id:
        return Response({'error': 'pharmacy_id is required'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        pharmacy = Pharmacy.objects.get(pharmacy_id=pharmacy_id)
    except Pharmacy.DoesNotExist:
        return Response({'error': 'Pharmacy not found'}, status=status.HTTP_404_NOT_FOUND)

    items_data = request.data.get('items', [])
    if not items_data:
        return Response({'error': 'items array is required (e.g. [{ "medicine_name": "paracetamol", "quantity": 2 }])'}, status=status.HTTP_400_BAD_REQUEST)

    results = []
    for item in items_data:
        medicine_name = (item.get('medicine_name') or '').strip()
        if not medicine_name:
            continue
        quantity_sold = max(0, int(item.get('quantity', 1)))
        if quantity_sold <= 0:
            continue
        medicine_key = medicine_name.lower()
        try:
            inv = PharmacyInventory.objects.get(pharmacy=pharmacy, medicine_name=medicine_key)
        except PharmacyInventory.DoesNotExist:
            results.append({
                'medicine_name': medicine_key,
                'quantity_sold': quantity_sold,
                'new_quantity': 0,
                'message': 'No inventory record; not decremented (add stock via pharmacist inventory first).',
            })
            continue
        old_qty = inv.quantity
        new_qty = max(0, old_qty - quantity_sold)
        inv.quantity = new_qty
        inv.save(update_fields=['quantity'])
        results.append({
            'medicine_name': inv.medicine_name,
            'quantity_sold': quantity_sold,
            'previous_quantity': old_qty,
            'new_quantity': new_qty,
        })

    return Response({
        'message': 'Purchase recorded; inventory decremented.',
        'pharmacy_id': str(pharmacy.pharmacy_id),
        'items': results,
    }, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([AllowAny])
def get_ranked_responses(request, request_id):
    """
    Get ranked pharmacy responses for a medicine request
    Ranking based on: availability, total time (prep + travel), price, distance
    Returns top 3 by default, or specify limit query parameter
    
    SECURITY: Requires conversation_id or session_id to verify ownership
    Each request can only be accessed by the user who created it.
    """
    try:
        medicine_request = MedicineRequest.objects.get(request_id=request_id)
        
        # SECURITY: Verify ownership - require conversation_id or session_id
        conversation_id = request.query_params.get('conversation_id')
        session_id = request.query_params.get('session_id')
        
        if not conversation_id and not session_id:
            return Response(
                {'error': 'conversation_id or session_id is required to verify ownership'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Verify the request belongs to the provided conversation/session
        if conversation_id:
            try:
                conversation = ChatConversation.objects.get(conversation_id=conversation_id)
                if medicine_request.conversation != conversation:
                    return Response(
                        {'error': 'Medicine request does not belong to this conversation'},
                        status=status.HTTP_403_FORBIDDEN
                    )
            except ChatConversation.DoesNotExist:
                return Response(
                    {'error': 'Conversation not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
        elif session_id:
            if medicine_request.conversation.session_id != session_id:
                return Response(
                    {'error': 'Medicine request does not belong to this session'},
                    status=status.HTTP_403_FORBIDDEN
                )
        
        # Get limit from query params (default: 3)
        limit = int(request.query_params.get('limit', 3))
        
        # Responses from pharmacists who replied to this request
        ranked_responses = get_ranked_pharmacy_responses(medicine_request, limit=limit * 2)
        
        # Also include pharmacies that have live inventory (updated after request was sent) so patient sees them on poll
        seen_pharmacy_ids = {r.get('pharmacy_id') for r in ranked_responses if r.get('pharmacy_id')}
        if medicine_request.location_latitude and medicine_request.location_longitude and (medicine_request.medicine_names or []):
            live_results = get_live_inventory_ranked(
                medicine_request.location_latitude,
                medicine_request.location_longitude,
                medicine_request.medicine_names,
                limit=limit * 2,
            )
            for r in live_results:
                pid = r.get('pharmacy_id')
                if pid and pid not in seen_pharmacy_ids:
                    r['from_live_inventory'] = True
                    ranked_responses.append(r)
                    seen_pharmacy_ids.add(pid)
        
        # Sort: MCDA responses use lower score = better; live inventory uses 0-1 higher = better. Normalize to "higher = better".
        def _score_key(x):
            s = x.get('ranking_score')
            if s is None:
                return 0
            s = float(s)
            if x.get('from_live_inventory'):
                return s  # already 0-1, higher better
            return 1 / (1 + s) if s >= 0 else 0  # MCDA: lower better -> convert to higher better
        ranked_responses.sort(key=_score_key, reverse=True)
        
        return Response(ranked_responses[:limit], status=status.HTTP_200_OK)
    
    except MedicineRequest.DoesNotExist:
        return Response(
            {'error': 'Medicine request not found'},
            status=status.HTTP_404_NOT_FOUND
        )
    except ValueError:
        return Response(
            {'error': 'Invalid limit parameter. Must be a number.'},
            status=status.HTTP_400_BAD_REQUEST
        )


@api_view(['POST'])
@permission_classes([AllowAny])
def pharmacist_login(request):
    """
    Pharmacist login endpoint
    Returns pharmacist information if credentials are valid
    """
    serializer = PharmacistLoginSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    email = serializer.validated_data['email']
    password = serializer.validated_data['password']
    
    try:
        pharmacist = Pharmacist.objects.get(email=email, is_active=True)
        
        # If pharmacist has a linked user account, authenticate with Django
        if pharmacist.user:
            from django.contrib.auth import authenticate
            user = authenticate(username=pharmacist.user.username, password=password)
            if not user:
                return Response(
                    {'error': 'Invalid credentials'},
                    status=status.HTTP_401_UNAUTHORIZED
                )
        else:
            # For pharmacists without user accounts, you might want to implement
            # a different authentication method (e.g., API key, token)
            # For now, we'll return an error
            return Response(
                {'error': 'Pharmacist account not linked to user. Please contact administrator.'},
                status=status.HTTP_401_UNAUTHORIZED
            )
        
        # Return pharmacist information
        pharmacist_serializer = PharmacistSerializer(pharmacist)
        return Response({
            'pharmacist': pharmacist_serializer.data,
            'message': 'Login successful'
        }, status=status.HTTP_200_OK)
    
    except Pharmacist.DoesNotExist:
        return Response(
            {'error': 'Invalid credentials'},
            status=status.HTTP_401_UNAUTHORIZED
        )


@api_view(['GET'])
@permission_classes([AllowAny])
def get_pharmacist_profile(request, pharmacist_id):
    """
    Get pharmacist profile information
    """
    try:
        pharmacist = Pharmacist.objects.get(pharmacist_id=pharmacist_id, is_active=True)
        serializer = PharmacistSerializer(pharmacist)
        return Response(serializer.data, status=status.HTTP_200_OK)
    except Pharmacist.DoesNotExist:
        return Response(
            {'error': 'Pharmacist not found'},
            status=status.HTTP_404_NOT_FOUND
        )


@api_view(['POST'])
@permission_classes([AllowAny])
def register_pharmacy(request):
    """
    Register a new pharmacy
    Automatically geocodes the address if coordinates are not provided
    """
    from .services import LocationService
    
    serializer = PharmacyRegistrationSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    data = serializer.validated_data
    
    # Check if pharmacy_id already exists
    if Pharmacy.objects.filter(pharmacy_id=data['pharmacy_id']).exists():
        return Response(
            {'error': f"Pharmacy with ID '{data['pharmacy_id']}' already exists"},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    # Get latitude and longitude
    latitude = data.get('latitude')
    longitude = data.get('longitude')
    
    # If coordinates not provided, try to geocode the address
    if not latitude or not longitude:
        address = data['address']
        print(f"[INFO] Geocoding pharmacy address: {address}")
        geocoded_lat, geocoded_lon = LocationService.geocode_address(address)
        
        if geocoded_lat and geocoded_lon:
            latitude = geocoded_lat
            longitude = geocoded_lon
            print(f"[INFO] Successfully geocoded pharmacy address to: {latitude}, {longitude}")
        else:
            print(f"[WARNING] Could not geocode pharmacy address: {address}. Pharmacy will be registered without coordinates.")
    
    # Create pharmacy
    pharmacy = Pharmacy.objects.create(
        pharmacy_id=data['pharmacy_id'],
        name=data['name'],
        address=data['address'],
        latitude=latitude,
        longitude=longitude,
        phone=data.get('phone', ''),
        email=data.get('email', ''),
        is_active=True
    )
    
    serializer_response = PharmacySerializer(pharmacy)
    return Response({
        'message': 'Pharmacy registered successfully',
        'pharmacy': serializer_response.data
    }, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([AllowAny])
def register_pharmacist(request):
    """
    Register a new pharmacist for a pharmacy
    Creates a Django User account for authentication
    """
    serializer = PharmacistRegistrationSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    data = serializer.validated_data
    
    # Check if pharmacy exists
    try:
        pharmacy = Pharmacy.objects.get(pharmacy_id=data['pharmacy_id'], is_active=True)
    except Pharmacy.DoesNotExist:
        return Response(
            {'error': f"Pharmacy with ID '{data['pharmacy_id']}' not found or inactive"},
            status=status.HTTP_404_NOT_FOUND
        )
    
    # Check if email already exists
    if Pharmacist.objects.filter(email=data['email']).exists():
        return Response(
            {'error': 'A pharmacist with this email already exists'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    # Check if username already exists
    from django.contrib.auth.models import User
    if User.objects.filter(username=data['username']).exists():
        return Response(
            {'error': 'A user with this username already exists'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    # Create Django User account
    user = User.objects.create_user(
        username=data['username'],
        email=data['email'],
        password=data['password'],
        first_name=data['first_name'],
        last_name=data['last_name']
    )
    
    # Create pharmacist profile
    pharmacist = Pharmacist.objects.create(
        pharmacy=pharmacy,
        user=user,
        first_name=data['first_name'],
        last_name=data['last_name'],
        email=data['email'],
        phone=data.get('phone', ''),
        license_number=data.get('license_number', ''),
        is_active=True
    )
    
    serializer_response = PharmacistSerializer(pharmacist)
    return Response({
        'message': 'Pharmacist registered successfully',
        'pharmacist': serializer_response.data
    }, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([AllowAny])
def register_patient(request):
    """
    Register or update a patient profile (anonymous, keyed by session).
    POST /api/chatbot/register/patient/
    Body: optional session_id or conversation_id; optional display_name, email, phone, date_of_birth,
    home_area, preferred_language, allergies, conditions, and preference flags.
    If session_id and conversation_id are omitted, a new session_id is generated and returned.
    """
    session_id = (
        request.data.get('session_id')
        or request.query_params.get('session_id')
    )
    conversation_id = (
        request.data.get('conversation_id')
        or request.query_params.get('conversation_id')
    )
    if not session_id and conversation_id:
        try:
            conv = ChatConversation.objects.get(conversation_id=conversation_id)
            session_id = conv.session_id
        except ChatConversation.DoesNotExist:
            return Response(
                {'error': 'Conversation not found'},
                status=status.HTTP_404_NOT_FOUND
            )
    if not session_id:
        session_id = 'session_' + uuid.uuid4().hex

    profile, created = PatientProfile.objects.get_or_create(
        session_id=session_id,
        defaults={}
    )
    allowed = {
        'display_name', 'email', 'phone', 'date_of_birth', 'home_area', 'preferred_language',
        'allergies', 'conditions', 'max_search_radius_km', 'sort_results_by',
        'notify_pharmacy_responses', 'notify_request_expiry', 'notify_drug_interactions',
        'notify_medibot_followup', 'notification_method', 'share_location_with_pharmacies', 'save_search_history',
    }
    updates = {k: request.data[k] for k in allowed if k in request.data}
    if 'date_of_birth' in updates and updates['date_of_birth']:
        from datetime import datetime
        try:
            if isinstance(updates['date_of_birth'], str):
                updates['date_of_birth'] = datetime.strptime(updates['date_of_birth'], '%Y-%m-%d').date()
        except ValueError:
            updates.pop('date_of_birth', None)
    for key, value in updates.items():
        setattr(profile, key, value)
    if updates:
        profile.save(update_fields=list(updates.keys()))

    return Response({
        'message': 'Patient registered successfully' if created else 'Patient profile updated',
        'session_id': session_id,
        'profile': {
            'display_name': profile.display_name,
            'email': profile.email,
            'phone': profile.phone,
            'date_of_birth': str(profile.date_of_birth) if profile.date_of_birth else None,
            'home_area': profile.home_area,
            'preferred_language': profile.preferred_language,
        },
    }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([AllowAny])
def list_pharmacies(request):
    """
    List all active pharmacies
    """
    pharmacies = Pharmacy.objects.filter(is_active=True).order_by('name')
    serializer = PharmacySerializer(pharmacies, many=True)
    return Response(serializer.data, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([AllowAny])
def list_pharmacists(request, pharmacy_id=None):
    """
    List pharmacists
    If pharmacy_id is provided, list pharmacists for that pharmacy only
    """
    if pharmacy_id:
        try:
            pharmacy = Pharmacy.objects.get(pharmacy_id=pharmacy_id)
            pharmacists = Pharmacist.objects.filter(pharmacy=pharmacy, is_active=True)
        except Pharmacy.DoesNotExist:
            return Response(
                {'error': 'Pharmacy not found'},
                status=status.HTTP_404_NOT_FOUND
            )
    else:
        pharmacists = Pharmacist.objects.filter(is_active=True)
    
    serializer = PharmacistSerializer(pharmacists, many=True)
    return Response(serializer.data, status=status.HTTP_200_OK)


# ---------- Patient dashboard (MediConnect) ----------

def _patient_session_from_request(request):
    """Resolve session_id from request (query param or body). Required for patient dashboard APIs."""
    session_id = request.query_params.get('session_id') or (request.data.get('session_id') if hasattr(request, 'data') else None)
    conversation_id = request.query_params.get('conversation_id') or (request.data.get('conversation_id') if hasattr(request, 'data') else None)
    if conversation_id:
        try:
            conv = ChatConversation.objects.get(conversation_id=conversation_id)
            return conv.session_id, None
        except ChatConversation.DoesNotExist:
            return None, Response({'error': 'Conversation not found'}, status=status.HTTP_404_NOT_FOUND)
    if not session_id:
        return None, Response({'error': 'session_id or conversation_id is required'}, status=status.HTTP_400_BAD_REQUEST)
    return session_id, None


@api_view(['GET'])
@permission_classes([AllowAny])
def patient_dashboard_stats(request):
    """
    GET /api/chatbot/patient/dashboard/stats/?session_id=... or ?conversation_id=...
    Returns: active_requests, fulfilled_count, expired_count, (optional) avg_savings, time_saved_hrs.
    """
    session_id, err = _patient_session_from_request(request)
    if err:
        return err
    conversations = ChatConversation.objects.filter(session_id=session_id).values_list('pk', flat=True)
    requests_qs = MedicineRequest.objects.filter(conversation_id__in=conversations)
    active_statuses = ('broadcasting', 'awaiting_responses', 'responses_received', 'ranking', 'partial')
    active = requests_qs.filter(status__in=active_statuses).count()
    fulfilled = requests_qs.filter(status='completed').count()
    expired = requests_qs.filter(status__in=('expired', 'timeout')).count()
    from django.db.models import Avg, Min
    best_prices = PharmacyResponse.objects.filter(request__in=requests_qs.filter(status='completed'), price__isnull=False).values('request').annotate(min_price=Min('price'))
    avg_savings = None  # placeholder: could compare min_price to a baseline
    return Response({
        'active_requests': active,
        'fulfilled_count': fulfilled,
        'expired_count': expired,
        'avg_savings': avg_savings,
        'time_saved_hrs': None,
    }, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([AllowAny])
def patient_my_requests(request):
    """
    GET /api/chatbot/patient/requests/?session_id=... or ?conversation_id=...&status=all|active|fulfilled|expired
    List medicine requests for this patient with response count, best price, status.
    """
    session_id, err = _patient_session_from_request(request)
    if err:
        return err
    status_filter = request.query_params.get('status', 'all')
    conversations = ChatConversation.objects.filter(session_id=session_id).values_list('pk', flat=True)
    requests_qs = MedicineRequest.objects.filter(conversation_id__in=conversations).select_related('conversation').prefetch_related('pharmacy_responses').order_by('-created_at')
    if status_filter == 'active':
        requests_qs = requests_qs.filter(status__in=('broadcasting', 'awaiting_responses', 'responses_received', 'ranking', 'partial'))
    elif status_filter == 'fulfilled':
        requests_qs = requests_qs.filter(status='completed')
    elif status_filter == 'expired':
        requests_qs = requests_qs.filter(status__in=('expired', 'timeout'))
    limit = min(int(request.query_params.get('limit', 50)), 100)
    requests_qs = requests_qs[:limit]
    from django.db.models import Min, Count
    results = []
    for req in requests_qs:
        responses = req.pharmacy_responses.all()
        response_count = responses.count()
        best_price = None
        best_pharmacy_id = None
        best_pharmacy_name = None
        best_medicine_name = None
        if responses.exists():
            with_price = responses.filter(price__isnull=False).order_by('price').first()
            if with_price:
                best_price = str(with_price.price)
                best_pharmacy_id = with_price.pharmacy.pharmacy_id if with_price.pharmacy else None
                best_pharmacy_name = with_price.pharmacy.name if with_price.pharmacy else with_price.pharmacy_name
                # Use first requested medicine as the label (e.g. Ibuprofen 400mg)
                meds = req.medicine_names or []
                if isinstance(meds, list) and meds:
                    best_medicine_name = str(meds[0])
        pharmacy_names = list(responses.values_list('pharmacy__name', flat=True))[:3]
        pharmacy_names = [n for n in pharmacy_names if n]
        short_id = str(req.request_id).replace('-', '')[:8].upper()
        results.append({
            'request_id': str(req.request_id),
            'short_request_id': short_id,
            'medicine_names': req.medicine_names or [],
            'symptoms': req.symptoms or '',
            'location_address': req.location_address or req.location_suburb or '',
            'submitted_at': req.created_at.isoformat() if req.created_at else None,
            'response_count': response_count,
            'pharmacy_names': pharmacy_names,
            'best_price': best_price,
            'best_pharmacy_id': best_pharmacy_id,
            'best_pharmacy_name': best_pharmacy_name,
            'best_medicine_name': best_medicine_name,
            'status': req.status,
        })
    return Response(results, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([AllowAny])
def patient_request_detail(request, request_id):
    """
    GET /api/chatbot/patient/requests/<request_id>/?session_id=... or conversation_id=...
    Single request with ranked pharmacy responses.
    """
    session_id, err = _patient_session_from_request(request)
    if err:
        return err
    try:
        medicine_request = MedicineRequest.objects.get(request_id=request_id)
    except MedicineRequest.DoesNotExist:
        return Response({'error': 'Request not found'}, status=status.HTTP_404_NOT_FOUND)
    if medicine_request.conversation.session_id != session_id:
        return Response({'error': 'Not your request'}, status=status.HTTP_403_FORBIDDEN)
    ranked = get_ranked_pharmacy_responses(medicine_request, limit=10)
    return Response({
        'request_id': str(medicine_request.request_id),
        'short_request_id': str(medicine_request.request_id).replace('-', '')[:8].upper(),
        'medicine_names': medicine_request.medicine_names or [],
        'symptoms': medicine_request.symptoms or '',
        'location_address': medicine_request.location_address or medicine_request.location_suburb or '',
        'status': medicine_request.status,
        'submitted_at': medicine_request.created_at.isoformat() if medicine_request.created_at else None,
        'pharmacy_responses': ranked,
    }, status=status.HTTP_200_OK)


@api_view(['GET', 'POST'])
@permission_classes([AllowAny])
def patient_saved_medicines(request):
    """
    GET /api/chatbot/patient/saved-medicines/?session_id=...
    POST body: { "session_id": "...", "medicine_name": "...", "display_name": "Paracetamol 500mg" (optional) }
    """
    session_id, err = _patient_session_from_request(request)
    if err:
        return err
    if request.method == 'GET':
        saved = SavedMedicine.objects.filter(session_id=session_id).order_by('-created_at')
        results = [{
            'id': s.id,
            'medicine_name': s.medicine_name,
            'display_name': s.display_name or s.medicine_name,
            'last_searched_at': s.last_searched_at.isoformat() if s.last_searched_at else None,
            'created_at': s.created_at.isoformat() if s.created_at else None,
        } for s in saved]
        return Response(results, status=status.HTTP_200_OK)
    medicine_name = (request.data.get('medicine_name') or '').strip()
    if not medicine_name:
        return Response({'error': 'medicine_name is required'}, status=status.HTTP_400_BAD_REQUEST)
    display_name = (request.data.get('display_name') or '').strip() or medicine_name
    obj, created = SavedMedicine.objects.get_or_create(
        session_id=session_id,
        medicine_name=medicine_name.lower(),
        defaults={'display_name': display_name},
    )
    if not created:
        obj.display_name = display_name
        obj.save(update_fields=['display_name'])
    return Response({
        'id': obj.id,
        'medicine_name': obj.medicine_name,
        'display_name': obj.display_name or obj.medicine_name,
        'created': created,
    }, status=status.HTTP_201_CREATED)


@api_view(['POST', 'DELETE'])
@permission_classes([AllowAny])
def patient_saved_medicine_remove(request, medicine_name=None):
    """
    POST/DELETE /api/chatbot/patient/saved-medicines/remove/?session_id=... body: { "medicine_name": "paracetamol" }
    or .../remove/paracetamol/?session_id=...
    """
    session_id, err = _patient_session_from_request(request)
    if err:
        return err
    name = medicine_name or (request.data.get('medicine_name') or '').strip()
    if not name:
        return Response({'error': 'medicine_name is required'}, status=status.HTTP_400_BAD_REQUEST)
    deleted, _ = SavedMedicine.objects.filter(session_id=session_id, medicine_name=name.lower()).delete()
    return Response({'removed': deleted > 0}, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([AllowAny])
def patient_notifications_list(request):
    """
    GET /api/chatbot/patient/notifications/?session_id=...&type=all|pharmacy_response|...&unread_only=false
    """
    session_id, err = _patient_session_from_request(request)
    if err:
        return err
    unread_only = request.query_params.get('unread_only', 'false').lower() == 'true'
    type_filter = request.query_params.get('type', 'all')
    qs = PatientNotification.objects.filter(session_id=session_id)
    if unread_only:
        qs = qs.filter(read_at__isnull=True)
    if type_filter != 'all':
        qs = qs.filter(notification_type=type_filter)
    qs = qs.order_by('-created_at')[:50]
    results = [{
        'id': n.id,
        'notification_type': n.notification_type,
        'title': n.title,
        'body': n.body,
        'related_request_id': str(n.related_request_id) if n.related_request_id else None,
        'related_response_id': str(n.related_response_id) if n.related_response_id else None,
        'read': n.read_at is not None,
        'read_at': n.read_at.isoformat() if n.read_at else None,
        'created_at': n.created_at.isoformat() if n.created_at else None,
    } for n in qs]
    return Response(results, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([AllowAny])
def patient_notifications_mark_read(request):
    """
    POST /api/chatbot/patient/notifications/mark-read/?session_id=... body: { "id": 1 } or { "ids": [1,2] } or omit to mark all
    """
    session_id, err = _patient_session_from_request(request)
    if err:
        return err
    now = timezone.now()
    ids = request.data.get('ids') or ([request.data.get('id')] if request.data.get('id') is not None else None)
    if ids:
        updated = PatientNotification.objects.filter(session_id=session_id, id__in=ids, read_at__isnull=True).update(read_at=now)
    else:
        updated = PatientNotification.objects.filter(session_id=session_id, read_at__isnull=True).update(read_at=now)
    return Response({'marked': updated}, status=status.HTTP_200_OK)


@api_view(['GET', 'PATCH'])
@permission_classes([AllowAny])
def patient_profile(request):
    """
    GET/PATCH /api/chatbot/patient/profile/?session_id=...
    PATCH body: partial profile fields (display_name, email, phone, home_area, allergies, conditions, preferences, etc.)
    """
    session_id, err = _patient_session_from_request(request)
    if err:
        return err
    profile, _ = PatientProfile.objects.get_or_create(session_id=session_id, defaults={})
    if request.method == 'GET':
        return Response({
            'display_name': profile.display_name,
            'email': profile.email,
            'phone': profile.phone,
            'date_of_birth': str(profile.date_of_birth) if profile.date_of_birth else None,
            'home_area': profile.home_area,
            'preferred_language': profile.preferred_language,
            'allergies': profile.allergies,
            'conditions': profile.conditions,
            'max_search_radius_km': profile.max_search_radius_km,
            'sort_results_by': profile.sort_results_by,
            'notify_pharmacy_responses': profile.notify_pharmacy_responses,
            'notify_request_expiry': profile.notify_request_expiry,
            'notify_drug_interactions': profile.notify_drug_interactions,
            'notify_medibot_followup': profile.notify_medibot_followup,
            'notification_method': profile.notification_method,
            'share_location_with_pharmacies': profile.share_location_with_pharmacies,
            'save_search_history': profile.save_search_history,
        }, status=status.HTTP_200_OK)
    allowed = {
        'display_name', 'email', 'phone', 'date_of_birth', 'home_area', 'preferred_language',
        'allergies', 'conditions', 'max_search_radius_km', 'sort_results_by',
        'notify_pharmacy_responses', 'notify_request_expiry', 'notify_drug_interactions',
        'notify_medibot_followup', 'notification_method', 'share_location_with_pharmacies', 'save_search_history',
    }
    updates = {k: request.data[k] for k in allowed if k in request.data}
    if 'date_of_birth' in updates and updates['date_of_birth']:
        from datetime import datetime
        try:
            if isinstance(updates['date_of_birth'], str):
                updates['date_of_birth'] = datetime.strptime(updates['date_of_birth'], '%Y-%m-%d').date()
        except ValueError:
            del updates['date_of_birth']
    for key, value in updates.items():
        setattr(profile, key, value)
    if updates:
        profile.save(update_fields=list(updates.keys()))
    return Response({
        'display_name': profile.display_name,
        'email': profile.email,
        'home_area': profile.home_area,
        'preferred_language': profile.preferred_language,
    }, status=status.HTTP_200_OK)
