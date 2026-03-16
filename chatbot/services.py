"""
AI Chatbot Service - Handles interactions with OpenRouter API
"""
import os
from typing import List, Dict, Optional
import json
import re
from PIL import Image
import io
import base64
import requests


class OCRService:
    """Service for extracting text from prescription images using Google Gemini API"""
    
    def __init__(self):
        api_key = os.getenv('GEMINI_API_KEY')
        if not api_key:
            raise ValueError("GEMINI_API_KEY not found in environment variables")
        
        # Use the new Google genai library (google.genai)
        # Fallback to old library (google.generativeai) if new one not available
        try:
            import google.genai as genai
            self.client = genai.Client(api_key=api_key)
            # Using gemini-2.5-flash-lite for vision tasks (supports image processing, better free tier)
            self.model_name = "gemini-2.5-flash-lite"
            self.model = None  # New API uses client, not model directly
        except ImportError:
            # Fallback to old library if new one not available
            try:
                import google.generativeai as genai_old
                genai_old.configure(api_key=api_key)
                self.client = None
                self.model = genai_old.GenerativeModel('gemini-2.5-flash-lite')
                self.model_name = "gemini-2.5-flash-lite"
            except Exception as e:
                raise ValueError(f"Failed to initialize Gemini API: {e}. Please install google-generativeai: pip install google-generativeai")
    
    def extract_prescription_text(self, image_file) -> Dict:
        """
        Extract medicine names and dosages from prescription image
        
        Args:
            image_file: Django UploadedFile or file-like object
            
        Returns:
            Dict with 'medicines', 'dosages', 'raw_text', 'confidence'
        """
        try:
             # Read image
            image = Image.open(image_file)
            
            # Convert to bytes if needed
            if hasattr(image_file, 'read'):
                image_file.seek(0)
                image_bytes = image_file.read()
            else:
                buffer = io.BytesIO()
                image.save(buffer, format='PNG')
                image_bytes = buffer.getvalue()
            
            # Use Gemini Vision API to extract prescription information
            prompt = """Analyze this prescription image and extract:
1. All medicine/drug names
2. Dosages for each medicine
3. Frequency (how many times per day)
4. Duration (how many days)
5. Any special instructions

Return the information in a structured format. If you cannot read something clearly, indicate that.

Focus on extracting medicine names accurately as this is critical for patient safety."""
            
            # Generate content with image using new or old API
            if self.client:
                # New API: google.genai.Client
                import base64
                image_base64 = base64.b64encode(image_bytes).decode('utf-8')
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                    config={
                        "response_mime_type": "text/plain"
                    }
                )
                # For images, we need to use a different approach with the new API
                # The new API might have different image handling
                try:
                    # Try the new API format for images
                    response = self.client.models.generate_content(
                        model=self.model_name,
                        contents=[
                            {"text": prompt},
                            {"inline_data": {"mime_type": "image/png", "data": image_base64}}
                        ]
                    )
                    extracted_text = response.text.strip()
                except Exception:
                    # If new API format fails, fall back to old API
                    import google.generativeai as genai_old
                    genai_old.configure(api_key=os.getenv('GEMINI_API_KEY'))
                    model_old = genai_old.GenerativeModel(self.model_name)
                    response = model_old.generate_content([
                        prompt,
                        {"mime_type": "image/png", "data": image_bytes}
                    ])
                    extracted_text = response.text.strip()
            else:
                # Old API: google.generativeai.GenerativeModel
                response = self.model.generate_content([
                    prompt,
                    {
                        "mime_type": "image/png",
                        "data": image_bytes
                    }
                ])
                extracted_text = response.text.strip()
            
            # Parse the response to extract structured data
            medicines = self._extract_medicine_names(extracted_text)
            dosages = self._extract_dosages(extracted_text)
            
            return {
                'medicines': medicines,
                'dosages': dosages,
                'raw_text': extracted_text,
                'confidence': 'high' if medicines else 'low'
            }
            
        except Exception as e:
            return {
                'medicines': [],
                'dosages': {},
                'raw_text': '',
                'confidence': 'low',
                'error': str(e)
            }
    
    def _extract_medicine_names(self, text: str) -> List[str]:
        """Extract medicine names from OCR text"""
        medicines = []
        
        # Use Gemini to extract medicine names more accurately
        try:
            extraction_prompt = f"""From this prescription text, extract ONLY the medicine/drug names. 
Return them as a comma-separated list. If no medicines are found, return "none".

Prescription text:
{text}

Medicine names:"""
            
            if self.client:
                # New API: google.genai.Client
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=extraction_prompt
                )
                result = response.text.strip().lower()
            else:
                # Old API: google.generativeai.GenerativeModel
                response = self.model.generate_content(extraction_prompt)
                result = response.text.strip().lower()
            
            if result and result != "none":
                medicines = [m.strip() for m in result.split(',') if m.strip()]
        except:
            # Fallback: simple pattern matching
            patterns = [
                r'\b([A-Z][a-z]+(?:[-\s][A-Z][a-z]+)*)\s*(?:tablet|tab|capsule|cap|mg|ml|g)\b',
                r'\b(paracetamol|ibuprofen|amoxicillin|aspirin|penicillin)\b',
            ]
            
            for pattern in patterns:
                matches = re.findall(pattern, text, re.IGNORECASE)
                medicines.extend(matches)
        
        # Remove duplicates and clean
        medicines = list(set([m.lower().strip() for m in medicines if len(m) > 2]))
        return medicines
    
    def _extract_dosages(self, text: str) -> Dict[str, str]:
        """Extract dosage information for each medicine"""
        dosages = {}
        
        # Simple extraction - can be enhanced
        # Look for patterns like "500mg", "2x daily", etc.
        dosage_patterns = [
            r'(\d+)\s*(?:mg|ml|g)',
            r'(\d+)\s*(?:times?|x)\s*(?:daily|per day)',
            r'(\d+)\s*(?:tablets?|capsules?)',
        ]
        
        for pattern in dosage_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            if matches:
                dosages['dosage'] = ' '.join(matches)
                break
        
        return dosages


class ChatbotService:
    """Service for handling AI chatbot interactions using OpenRouter API or Gemini (fallback)"""
    
    def __init__(self):
        self.api_key = os.getenv('OPENROUTER_API_KEY')
        self.gemini_key = os.getenv('GEMINI_API_KEY')
        self.backend = None
        if self.api_key:
            self.backend = 'openrouter'
        elif self.gemini_key:
            self.backend = 'gemini'
            print("[INFO] Chatbot using GEMINI_API_KEY (OpenRouter not set)")
        if not self.backend:
            raise ValueError(
                "Neither OPENROUTER_API_KEY nor GEMINI_API_KEY found. "
                "Add one to your .env: OPENROUTER_API_KEY from https://openrouter.ai/keys or GEMINI_API_KEY from Google AI."
            )
        self.api_url = "https://openrouter.ai/api/v1/chat/completions"
        self.model = "google/gemini-2.5-flash-lite"
        
        # System prompt for healthcare chatbot
        self.system_prompt = """You are a helpful healthcare assistant for a pharmacy connection platform in Zimbabwe. 
Your role is to:
1. Help patients find medicines by understanding their symptoms or prescription needs
2. Guide users through the process step-by-step
3. Provide general medication information (NOT medical diagnosis)
4. Always remind users to consult healthcare professionals for medical advice

SYMPTOM DESCRIPTION FLOW (CRITICAL - follow EXACTLY, in this order):
Step 1 - When patient describes symptoms (e.g., "I have fever", "I have a headache", "headache and body pains"):
  • NEVER ask for location in this step. NEVER say "where are you" or "what is your location".
  • Analyze symptoms and suggest specific medicines with rationale, e.g.:
    "Based on your symptoms, you might need:
    • Paracetamol – for fever and headache
    • Ibuprofen – for pain relief
    Would you like to search for these medicines? You can say yes or tell me which ones you want."
  • End with the question "Would you like to search for these medicines?" - do NOT ask for location.

Step 2 - ONLY after patient confirms (e.g., "Yes", "I want paracetamol", "All of them"):
  • Acknowledge their selection
  • Then say: "To find pharmacies near you, I need your location. Please share your area or use your current location."

Step 3 - When patient provides location:
  • Confirm: "I'll send your request to nearby pharmacies. They will respond with availability, prices, and distance."

DIRECT MEDICINE SEARCH:
- If patient says "I am looking for [medicine name]" or mentions specific medicine:
  → Ask: "Do you have a prescription? Please upload your prescription image so pharmacies can check availability."
  → If no prescription: Ask for location and proceed.

Important guidelines:
- Be friendly, empathetic, and clear
- For symptom descriptions: ALWAYS suggest medicines first, then ask for confirmation, then ask for location (in that order)
- Never provide medical diagnosis
- Always include disclaimers about consulting healthcare professionals
- Support English, Shona, and Ndebele languages when possible
- Mention medicine names clearly so they can be extracted (paracetamol, ibuprofen, etc.)"""
    
    def _call_gemini(self, system_content: str, history: List[Dict[str, str]], user_message: str) -> str:
        """Generate chat response using Google Gemini (fallback when OpenRouter not set)."""
        try:
            import google.generativeai as genai
            genai.configure(api_key=self.gemini_key)
            model = genai.GenerativeModel('gemini-2.0-flash-lite')
            parts = [system_content]
            for msg in history:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role == "user":
                    parts.append(f"User: {content}")
                else:
                    parts.append(f"Assistant: {content}")
            parts.append(f"User: {user_message}")
            prompt = "\n\n".join(parts)
            response = model.generate_content(prompt)
            if response and response.text:
                return response.text.strip()
            return "I couldn't generate a response. Please try again."
        except Exception as e:
            print(f"[ERROR] Gemini chat fallback failed: {e}")
            raise
    
    def process_message(
        self,
        user_message: str,
        conversation_history: List[Dict[str, str]],
        context: Optional[Dict] = None,
        preferred_language: Optional[str] = None
    ) -> Dict:
        """
        Process user message and generate AI response
        
        Args:
            user_message: User's input message
            conversation_history: Previous messages in format [{"role": "user", "content": "..."}, ...]
            context: Additional context (extracted entities, user location, etc.)
            preferred_language: 'en' (English), 'sn' (Shona), 'nd' (Ndebele) - AI responds in this language
        
        Returns:
            Dict with 'response', 'intent', 'entities', 'requires_location', 'suggested_medicines'
        """
        try:
            # Build language instruction for system prompt
            lang_map = {'en': 'English', 'sn': 'Shona', 'nd': 'Ndebele'}
            lang_name = lang_map.get((preferred_language or '').lower(), None)
            language_instruction = ""
            if lang_name and lang_name != 'English':
                language_instruction = f"\n\nLANGUAGE: You MUST respond in {lang_name} only. All your messages must be in {lang_name}."

            # Build conversation context
            messages = []
            
            # Add system prompt as first message
            messages.append({
                "role": "user",
                "content": self.system_prompt + language_instruction
            })
            
            # Add conversation history
            for msg in conversation_history[-8:]:  # Keep last 8 turns for context
                messages.append(msg)
            
            # Add current user message
            messages.append({
                "role": "user",
                "content": user_message
            })
            
            # Generate response using OpenRouter API
            try:
                # Format messages for OpenRouter API
                openrouter_messages = []
                
                # Add system prompt with language instruction
                openrouter_messages.append({
                    "role": "system",
                    "content": self.system_prompt + language_instruction
                })
                
                # Add conversation history (last 8 messages)
                for msg in conversation_history[-8:]:
                    openrouter_messages.append({
                        "role": msg.get("role", "user"),
                        "content": msg.get("content", "")
                    })
                
                # Add current user message
                openrouter_messages.append({
                    "role": "user",
                    "content": user_message
                })
                
                if self.backend == 'gemini':
                    ai_response = self._call_gemini(
                        system_content=self.system_prompt + language_instruction,
                        history=conversation_history[-8:],
                        user_message=user_message
                    )
                else:
                    # Make API request to OpenRouter
                    headers = {
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://pharmacybackend.com",
                        "X-Title": "Pharmacy Chatbot"
                    }
                    payload = {
                        "model": self.model,
                        "messages": openrouter_messages,
                        "temperature": 0.7,
                        "max_tokens": 500
                    }
                    response = requests.post(self.api_url, headers=headers, json=payload, timeout=30)
                    response.raise_for_status()
                    result = response.json()
                    ai_response = result['choices'][0]['message']['content'].strip()
                
            except requests.exceptions.RequestException as api_error:
                error_msg = str(api_error)
                print(f"[ERROR] OpenRouter API call failed: {error_msg}")
                if hasattr(api_error, 'response') and api_error.response is not None:
                    try:
                        error_detail = api_error.response.json()
                        print(f"[ERROR] API Error Details: {error_detail}")
                    except:
                        print(f"[ERROR] API Error Response: {api_error.response.text}")
                # Re-raise to be caught by outer exception handler
                raise api_error
            except (KeyError, IndexError) as parse_error:
                error_msg = f"Failed to parse OpenRouter API response: {str(parse_error)}"
                print(f"[ERROR] {error_msg}")
                if 'result' in locals():
                    print(f"[ERROR] Response structure: {result}")
                raise Exception(error_msg)
            
            # Extract intent and entities
            intent = self._classify_intent(user_message, ai_response)
            entities = self._extract_entities(user_message)
            requires_location = self._check_location_requirement(user_message, ai_response)
            suggested_medicines = self._extract_medicine_suggestions(ai_response)
            # For symptom intent, add symptom-based suggestions (per platform guide)
            if intent == 'symptom_description':
                symptom_suggestions = self._suggest_medicines_from_symptoms(user_message, entities)
                for m in symptom_suggestions:
                    if m not in suggested_medicines:
                        suggested_medicines.append(m)
            # For medicine_selection: extract which medicines user selected
            selected_medicines = []
            if intent == 'medicine_selection':
                selected_medicines = self._extract_selected_medicines(user_message, context)
                if not selected_medicines and context and context.get('suggested_medicines'):
                    # User said "yes" or "all" - use previously suggested medicines
                    selected_medicines = context.get('suggested_medicines', [])
            
            result = {
                'response': ai_response,
                'intent': intent,
                'entities': entities,
                'requires_location': requires_location,
                'suggested_medicines': suggested_medicines,
                'confidence': 0.85
            }
            if selected_medicines:
                result['selected_medicines'] = selected_medicines
            return result
            
        except Exception as e:
            error_str = str(e)
            error_type = type(e).__name__
            
            # Log full error details for debugging
            print(f"[ERROR] ChatbotService.process_message failed")
            print(f"[ERROR] Error type: {error_type}")
            print(f"[ERROR] Error message: {error_str}")
            import traceback
            print(f"[ERROR] Traceback:\n{traceback.format_exc()}")
            
            # Check for specific API errors
            if 'quota' in error_str.lower() or '429' in error_str or 'rate limit' in error_str.lower():
                error_response = (
                    "I'm currently experiencing high demand. The API quota may have been reached. "
                    "Please try again in a few moments."
                )
            elif '401' in error_str or 'unauthorized' in error_str.lower():
                error_response = (
                    "There's an authentication issue with the AI service. Please contact support."
                )
            elif '404' in error_str or 'not found' in error_str.lower() or 'NotFound' in error_type:
                error_response = (
                    "The AI model is temporarily unavailable. Please try again later."
                )
            elif 'InvalidArgument' in error_type or 'invalid' in error_str.lower():
                error_response = (
                    "There was an issue with the request format. Please try rephrasing your message."
                )
            else:
                error_response = (
                    "I apologize, but I'm having trouble processing your request right now. "
                    "Please try again or contact support."
                )
            
            return {
                'response': error_response,
                'intent': 'error',
                'entities': {},
                'requires_location': False,
                'suggested_medicines': [],
                'confidence': 0.0,
                'error': error_str,
                'error_type': error_type
            }
    
    def _classify_intent(self, user_message: str, ai_response: str) -> str:
        """Classify user intent from message"""
        message_lower = user_message.lower().strip()
        
        if any(word in message_lower for word in ['location', 'where', 'address']) and not any(s in message_lower for s in ['headache', 'fever', 'pain', 'cough']):
            return 'location_provided'
        if any(word in message_lower for word in ['prescription', 'upload', 'doctor']):
            return 'prescription_upload'
        if any(word in message_lower for word in ['looking for', 'need', 'want', 'search', 'find']) and not any(s in message_lower for s in ['headache', 'fever', 'pain', 'symptom']):
            return 'medicine_search'
        # Medicine selection: user confirming/selecting after we suggested medicines
        selection_keywords = ['yes', 'yeah', 'yep', 'ok', 'okay', 'sure', 'all of them', 'those', "i'll take", "i want", 'confirm', 'proceed', 'go ahead']
        if any(kw in message_lower for kw in selection_keywords):
            # Check if previous AI response suggested medicines (asked "would you like to search")
            if 'would you like' in ai_response.lower() or 'search for these' in ai_response.lower() or 'paracetamol' in ai_response.lower() or 'ibuprofen' in ai_response.lower():
                return 'medicine_selection'
        # User names specific medicines - likely selecting from our suggestions
        if any(m in message_lower for m in ['paracetamol', 'panadol', 'ibuprofen', 'aspirin', 'antihistamine', 'antacid']):
            if any(s in message_lower for s in ['and', 'or', 'want', 'take', 'need', 'both']):
                return 'medicine_selection'
        if any(word in message_lower for word in ['headache', 'pain', 'fever', 'symptom', 'feeling', 'body pain', 'body ache', 'muscle']):
            return 'symptom_description'
        else:
            return 'general_inquiry'
    
    def _extract_entities(self, message: str) -> Dict:
        """Extract medical entities from message"""
        entities = {
            'medicines': [],
            'symptoms': [],
            'dosages': []
        }
        
        # Common medicine patterns
        medicine_patterns = [
            r'\b(?:paracetamol|panadol|aspirin|ibuprofen|amoxicillin|penicillin)\b',
            r'\b(?:tablet|capsule|syrup|injection)\b'
        ]
        
        # Symptom patterns (include body pains, myalgia for "body pains")
        symptom_keywords = [
            'headache', 'fever', 'pain', 'pains', 'cough', 'cold', 'flu', 'nausea', 'dizziness',
            'sore throat', 'stomach ache', 'stomach', 'diarrhea', 'diarrhoea', 'runny nose',
            'stuffy nose', 'body ache', 'body pain', 'body pains', 'muscle ache', 'myalgia', 'upset stomach'
        ]
        
        message_lower = message.lower()
        
        # Extract symptoms
        for symptom in symptom_keywords:
            if symptom in message_lower:
                entities['symptoms'].append(symptom)
        
        # Extract dosages
        dosage_pattern = r'\d+\s*(?:mg|ml|g|tablets?|capsules?)'
        dosages = re.findall(dosage_pattern, message_lower)
        entities['dosages'] = dosages
        
        return entities
    
    def _check_location_requirement(self, user_message: str, ai_response: str) -> bool:
        """Check if location is required based on conversation"""
        message_lower = user_message.lower()
        response_lower = ai_response.lower()
        
        # AI is asking for location
        if 'location' in response_lower or 'where are you' in response_lower or 'share your area' in response_lower:
            return True
        
        # Check if user is asking for medicine (direct search)
        if any(word in message_lower for word in ['medicine', 'medication', 'drug', 'pharmacy']):
            location_indicators = ['location', 'address', 'where', 'near', 'close']
            if not any(indicator in message_lower for indicator in location_indicators):
                return True
        
        return False
    
    def _extract_medicine_suggestions(self, ai_response: str) -> List[str]:
        """Extract medicine names from AI response"""
        import re
        medicines = []
        seen = set()
        
        # Common medicine names to look for (include ORS, loperamide, buscopan for diarrhoea/stomach)
        common_medicines = [
            'paracetamol', 'panadol', 'aspirin', 'ibuprofen', 'amoxicillin',
            'penicillin', 'cough syrup', 'antihistamine', 'antacid', 'omeprazole',
            'loperamide', 'oral rehydration salts', 'ors', 'buscopan',
            'diclofenac', 'dextromethorphan', 'meclizine', 'decongestant'
        ]
        
        response_lower = ai_response.lower()
        for medicine in common_medicines:
            if medicine in response_lower and medicine not in seen:
                # Use canonical form (ORS -> oral rehydration salts)
                canonical = 'oral rehydration salts' if medicine == 'ors' else medicine
                medicines.append(canonical)
                seen.add(medicine)
                seen.add(canonical)
        
        # Also parse AI bullet format: "**Medicine Name**" or "• Medicine –" or "Medicine –"
        bullet_patterns = [
            r'\*\*([^*]+?)\*\*\s*[–\-]',  # **Oral Rehydration Salts (ORS)** –
            r'[•\-\*]\s+\*\*([^*]+?)\*\*',  # • **Loperamide**
            r'[•\-\*]\s+([A-Za-z][A-Za-z\s]+?)(?:\s+[–\-]|\s*$)',  # • Loperamide –
        ]
        for pattern in bullet_patterns:
            for match in re.finditer(pattern, ai_response):
                name = match.group(1).strip()
                name_lower = name.lower()
                # Filter out non-medicines (instructions, common words)
                blocklist = {'minutes', 'before', 'eating', 'location', 'drug', 'would', 'like', 'search', 'these', 'take', 'use'}
                if len(name) > 2 and name_lower not in blocklist and not any(b in name_lower for b in ['minute', 'before', 'eating']):
                    if name_lower not in seen:
                        medicines.append(name_lower)
                        seen.add(name_lower)
        
        return medicines

    def _suggest_medicines_from_symptoms(self, message: str, entities: Dict) -> List[str]:
        """
        Suggest medicines based on symptom keywords in user message (per platform guide).
        Used to populate suggested_medicines for symptom_description intent.
        """
        symptom_medicines = {
            'headache': ['paracetamol', 'ibuprofen', 'aspirin'],
            'fever': ['paracetamol', 'ibuprofen'],
            'pain': ['paracetamol', 'ibuprofen', 'diclofenac'],
            'cough': ['cough syrup', 'dextromethorphan'],
            'cold': ['paracetamol', 'antihistamine'],
            'flu': ['paracetamol', 'antihistamine', 'cough syrup'],
            'nausea': ['antacid', 'omeprazole'],
            'dizziness': ['antihistamine', 'meclizine'],
            'sore throat': ['paracetamol', 'amoxicillin', 'throat lozenges'],
            'stomach': ['antacid', 'omeprazole'],
            'stomach ache': ['antacid', 'omeprazole', 'paracetamol'],
            'running stomach': ['oral rehydration salts', 'loperamide'],
            'running stomacg': ['oral rehydration salts', 'loperamide'],
            'runny stomach': ['oral rehydration salts', 'loperamide'],
            'diarrhea': ['oral rehydration salts', 'loperamide'],
            'diarrhoea': ['oral rehydration salts', 'loperamide'],
            'runny nose': ['antihistamine', 'decongestant'],
            'stuffy nose': ['decongestant', 'antihistamine'],
            'body ache': ['paracetamol', 'ibuprofen'],
            'body pain': ['paracetamol', 'ibuprofen'],
            'body pains': ['paracetamol', 'ibuprofen'],
            'muscle ache': ['paracetamol', 'ibuprofen', 'diclofenac'],
            'myalgia': ['paracetamol', 'ibuprofen'],
        }
        message_lower = message.lower()
        suggested = []
        seen = set()
        # Check extracted symptoms first
        for symptom in entities.get('symptoms', []):
            if symptom in symptom_medicines:
                for m in symptom_medicines[symptom]:
                    if m not in seen:
                        suggested.append(m)
                        seen.add(m)
        # Also scan message for symptom keywords
        for symptom, meds in symptom_medicines.items():
            if symptom in message_lower:
                for m in meds:
                    if m not in seen:
                        suggested.append(m)
                        seen.add(m)
        return suggested

    def _extract_selected_medicines(self, message: str, context: Optional[Dict] = None) -> List[str]:
        """Extract medicine names from user's selection message (e.g., 'I want paracetamol and ibuprofen')"""
        common_medicines = [
            'paracetamol', 'panadol', 'aspirin', 'ibuprofen', 'amoxicillin',
            'penicillin', 'cough syrup', 'antihistamine', 'antacid', 'omeprazole',
            'diclofenac', 'dextromethorphan', 'oral rehydration salts', 'loperamide',
            'throat lozenges', 'decongestant'
        ]
        message_lower = message.lower()
        selected = []
        for m in common_medicines:
            if m in message_lower:
                selected.append(m)
        if selected:
            return selected
        # Fallback: if user said "yes" or "all", use context's suggested_medicines
        if context and context.get('suggested_medicines'):
            return context.get('suggested_medicines', [])
        return []
    
    def suggest_alternatives(self, unavailable_medicine: str, symptoms: List[str] = None) -> List[str]:
        """
        Suggest alternative medicines using AI and therapeutic category matching
        
        Uses multiple approaches:
        1. AI-powered suggestions via Gemini (primary)
        2. Therapeutic category matching (fallback)
        3. Hardcoded common alternatives (last resort)
        """
        if symptoms is None:
            symptoms = []
        
        # Approach 1: Use AI to suggest alternatives based on medicine and symptoms
        try:
            prompt = f"""You are a healthcare assistant. A patient needs alternatives to {unavailable_medicine}.
            
            Patient symptoms: {', '.join(symptoms) if symptoms else 'Not specified'}
            
            Suggest 2-3 safe alternative medicines that:
            1. Treat similar conditions/symptoms
            2. Are commonly available in Zimbabwe
            3. Have similar therapeutic effects
            
            Only suggest medicines that are safe alternatives. Do not suggest medicines that require different medical conditions.
            Return only the medicine names, one per line, without explanations.
            
            If you cannot suggest safe alternatives, return "NO_SAFE_ALTERNATIVES"."""
            
            # Use OpenRouter API for alternatives
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.7,
                "max_tokens": 200
            }
            
            response = requests.post(self.api_url, headers=headers, json=payload, timeout=30)
            response.raise_for_status()
            result = response.json()
            ai_response_text = result['choices'][0]['message']['content'].strip()
            ai_suggestions = ai_response_text.split('\n')
            
            # Filter and clean suggestions
            alternatives = []
            for suggestion in ai_suggestions:
                suggestion = suggestion.strip()
                # Remove numbering, bullets, etc.
                suggestion = re.sub(r'^[\d\.\-\*]\s*', '', suggestion)
                if suggestion and suggestion.upper() != 'NO_SAFE_ALTERNATIVES' and len(suggestion) > 2:
                    alternatives.append(suggestion)
            
            if alternatives:
                return alternatives[:3]  # Return top 3
        
        except Exception as e:
            # Fall back to other methods if AI fails
            pass
        
        # Approach 2: Therapeutic category matching (basic)
        therapeutic_alternatives = {
            # Pain relievers / Analgesics
            'paracetamol': ['ibuprofen', 'aspirin', 'diclofenac'],
            'panadol': ['paracetamol', 'ibuprofen', 'aspirin'],
            'ibuprofen': ['paracetamol', 'aspirin', 'naproxen'],
            'aspirin': ['paracetamol', 'ibuprofen'],
            
            # Antibiotics
            'amoxicillin': ['penicillin', 'azithromycin', 'cephalexin'],
            'penicillin': ['amoxicillin', 'azithromycin'],
            'azithromycin': ['amoxicillin', 'erythromycin'],
            
            # Antacids
            'antacid': ['omeprazole', 'ranitidine', 'calcium carbonate'],
            'omeprazole': ['ranitidine', 'antacid', 'lansoprazole'],
            
            # Cough medicines
            'cough syrup': ['dextromethorphan', 'guaifenesin', 'codeine'],
            
            # Antihistamines
            'antihistamine': ['cetirizine', 'loratadine', 'chlorpheniramine'],
            'cetirizine': ['loratadine', 'antihistamine'],
        }
        
        medicine_lower = unavailable_medicine.lower()
        
        # Check direct matches
        if medicine_lower in therapeutic_alternatives:
            return therapeutic_alternatives[medicine_lower]
        
        # Check partial matches (e.g., "paracetamol 500mg" matches "paracetamol")
        for key, alternatives_list in therapeutic_alternatives.items():
            if key in medicine_lower or medicine_lower in key:
                return alternatives_list
        
        # Approach 3: Symptom-based suggestions (if symptoms provided)
        if symptoms:
            symptom_medicines = {
                'headache': ['paracetamol', 'ibuprofen', 'aspirin'],
                'fever': ['paracetamol', 'ibuprofen'],
                'pain': ['paracetamol', 'ibuprofen', 'diclofenac'],
                'cough': ['cough syrup', 'dextromethorphan', 'guaifenesin'],
                'cold': ['paracetamol', 'antihistamine', 'decongestant'],
                'nausea': ['antacid', 'omeprazole'],
            }
            
            for symptom in symptoms:
                symptom_lower = symptom.lower()
                if symptom_lower in symptom_medicines:
                    return symptom_medicines[symptom_lower]
        
        return []


class LocationService:
    """Service for handling location-related operations"""
    
    @staticmethod
    def geocode_address(address: str, country: str = "Zimbabwe") -> tuple:
        """
        Convert a text address to coordinates using OpenStreetMap Nominatim geocoding.
        Returns (latitude, longitude) or (None, None) if geocoding fails.
        
        Args:
            address: Text address to geocode (e.g., "183 21 Crescent, Glen View 1, Harare")
            country: Country to limit search (default: "Zimbabwe")
        
        Returns:
            Tuple of (latitude, longitude) or (None, None) if not found
        """
        if not address or not address.strip():
            return (None, None)
        
        try:
            # Clean and normalize address
            address = address.strip()
            
            # Common Zimbabwean cities/towns to detect
            zimbabwe_locations = [
                'Harare', 'Bulawayo', 'Gweru', 'Mutare', 'Kwekwe', 'Chitungwiza',
                'Glen View', 'Avondale', 'Belvedere', 'Mbare', 'Highfield', 'Epworth',
                'Hatfield', 'Waterfalls', 'Borrowdale', 'Mount Pleasant', 'Greendale'
            ]
            
            # Check if address already contains city/country
            address_lower = address.lower()
            has_city = any(city.lower() in address_lower for city in zimbabwe_locations)
            has_country = 'zimbabwe' in address_lower or 'zw' in address_lower
            
            # Build search queries with different levels of context
            search_queries = []
            
            # Query 1: Original address with country
            if not has_country:
                search_queries.append(f"{address}, {country}")
            
            # Query 2: Add Harare if no city detected (most pharmacies are in Harare)
            if not has_city:
                search_queries.append(f"{address}, Harare, {country}")
            
            # Query 3: Original address as-is (in case it already has full context)
            search_queries.append(address)
            
            # Use Nominatim geocoding API (free, no API key required)
            url = "https://nominatim.openstreetmap.org/search"
            headers = {
                "User-Agent": "PharmacyBackend/1.0"  # Required by Nominatim
            }
            
            # Try each search query until we find a match
            for search_query in search_queries:
                params = {
                    "q": search_query,
                    "format": "json",
                    "limit": 1,
                    "addressdetails": 1,
                    "countrycodes": "zw"  # Limit to Zimbabwe
                }
                
                try:
                    response = requests.get(url, params=params, headers=headers, timeout=15)
                    
                    if response.status_code == 200:
                        data = response.json()
                        if data and len(data) > 0:
                            result = data[0]
                            lat = float(result.get("lat", 0))
                            lon = float(result.get("lon", 0))
                            
                            # Validate coordinates are in reasonable range for Zimbabwe
                            # Zimbabwe is roughly: lat -22.0 to -15.0, lon 25.0 to 33.0
                            if -90 <= lat <= 90 and -180 <= lon <= 180:
                                # Check if coordinates are reasonable for Zimbabwe
                                if -25.0 <= lat <= -15.0 and 25.0 <= lon <= 35.0:
                                    print(f"[INFO] Geocoded address '{address}' to coordinates: {lat}, {lon} (using query: {search_query})")
                                    return (lat, lon)
                                elif -90 <= lat <= 90 and -180 <= lon <= 180:
                                    # Accept coordinates even if outside Zimbabwe range (might be geocoded incorrectly)
                                    print(f"[WARN] Geocoded address '{address}' to coordinates: {lat}, {lon} (may be outside Zimbabwe)")
                                    return (lat, lon)
                    
                    # Rate limiting: wait between requests
                    import time
                    time.sleep(1)  # Be respectful to Nominatim API
                    
                except requests.exceptions.Timeout:
                    print(f"[WARN] Geocoding timeout for query: {search_query}")
                    continue
                except Exception as e:
                    print(f"[WARN] Geocoding error for query '{search_query}': {str(e)}")
                    continue
            
            print(f"[WARN] Failed to geocode address: {address} (tried {len(search_queries)} variations)")
            return (None, None)
            
        except Exception as e:
            print(f"[ERROR] Geocoding error for '{address}': {str(e)}")
            return (None, None)
    
    @staticmethod
    def calculate_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """
        Calculate distance between two coordinates using Haversine formula
        Returns distance in kilometers
        """
        from math import radians, sin, cos, sqrt, atan2
        
        R = 6371  # Earth's radius in kilometers
        
        lat1_rad = radians(lat1)
        lat2_rad = radians(lat2)
        delta_lat = radians(lat2 - lat1)
        delta_lon = radians(lon2 - lon1)
        
        a = sin(delta_lat / 2) ** 2 + cos(lat1_rad) * cos(lat2_rad) * sin(delta_lon / 2) ** 2
        c = 2 * atan2(sqrt(a), sqrt(1 - a))
        
        distance = R * c
        return round(distance, 2)
    
    @staticmethod
    def estimate_travel_time(distance_km: float, context: str = 'urban') -> int:
        """
        Estimate travel time in minutes based on distance and context
        """
        if context == 'urban':
            # Urban: average speed ~30 km/h (traffic, stops)
            speed_kmh = 30
        else:
            # Rural: average speed ~50 km/h (less traffic)
            speed_kmh = 50
        
        time_hours = distance_km / speed_kmh
        time_minutes = int(time_hours * 60)
        
        # Add buffer time
        time_minutes += 5
        
        return time_minutes


class RankingEngine:
    """
    MCDA-based ranking engine per platform guide.
    Ranks pharmacy responses using weighted scoring with context-aware weights
    (urban vs rural - urban: price-sensitive, rural: distance-critical).
    """
    
    @staticmethod
    def calculate_pharmacy_density(center_lat: float, center_lon: float, radius_km: float = 5) -> int:
        """
        Count pharmacies within radius to determine urban (>=3) vs rural context.
        """
        try:
            from .models import Pharmacy
            count = 0
            for p in Pharmacy.objects.filter(is_active=True):
                if p.latitude and p.longitude:
                    d = LocationService.calculate_distance(center_lat, center_lon, p.latitude, p.longitude)
                    if d <= radius_km:
                        count += 1
            return count
        except Exception:
            return 0
    
    @staticmethod
    def get_context_weights(pharmacy_density: int) -> dict:
        """
        Return weight vector based on context (urban vs rural) per guide.
        """
        if pharmacy_density >= 3:
            return {
                'price': 0.35,
                'distance': 0.25,
                'rating': 0.25,
                'reliability': 0.15
            }
        return {
            'price': 0.20,
            'distance': 0.45,
            'rating': 0.20,
            'reliability': 0.15
        }
    
    @staticmethod
    def normalize_price(price: float, min_price: float, max_price: float) -> float:
        """Lower price = higher score (0-1)"""
        if max_price == min_price or max_price <= 0:
            return 1.0
        return max(0, (max_price - price) / (max_price - min_price))
    
    @staticmethod
    def normalize_distance(distance: float, min_dist: float, max_dist: float) -> float:
        """Closer = higher score (0-1)"""
        if max_dist == min_dist or max_dist <= 0:
            return 1.0
        return max(0, (max_dist - distance) / (max_dist - min_dist))
    
    @staticmethod
    def normalize_rating(rating: float, min_rating: float, max_rating: float) -> float:
        """Higher rating = higher score (0-1)"""
        if max_rating == min_rating:
            return 1.0 if rating > 0 else 0.5
        return max(0, (rating - min_rating) / (max_rating - min_rating))
    
    @staticmethod
    def normalize_reliability(rate: float, min_rate: float, max_rate: float) -> float:
        """Higher reliability = higher score (0-1)"""
        if max_rate == min_rate:
            return 1.0 if rate > 0 else 0.5
        return max(0, (rate - min_rate) / (max_rate - min_rate))
    
    @staticmethod
    def rank_responses(responses: list, patient_lat: float = None, patient_lon: float = None,
                       center_lat: float = None, center_lon: float = None) -> tuple:
        """
        Rank pharmacy responses using MCDA.
        Returns (ranked_list, weights_used, context).
        Each item in ranked_list includes score, score_breakdown, and original response data.
        """
        if not responses:
            return [], {}, 'unknown'
        
        # Determine context from pharmacy density
        center_lat = center_lat or patient_lat
        center_lon = center_lon or patient_lon
        density = 0
        if center_lat and center_lon:
            density = RankingEngine.calculate_pharmacy_density(center_lat, center_lon)
        context = 'urban' if density >= 3 else 'rural'
        weights = RankingEngine.get_context_weights(density)
        
        # Collect min/max for normalization
        prices = [float(r.get('price') or r.get('total_price') or 0) for r in responses]
        distances = [float(r.get('distance_km') or 0) for r in responses]
        ratings = [float(r.get('pharmacy_rating') or 0) for r in responses]
        reliability = [float(r.get('pharmacy_response_rate') or 100) for r in responses]
        
        min_p, max_p = (min(prices), max(prices)) if prices else (0, 1)
        min_d, max_d = (min(distances), max(distances)) if distances else (0, 1)
        min_r, max_r = (min(ratings), max(ratings)) if ratings else (0, 5)
        min_rel, max_rel = (min(reliability), max(reliability)) if reliability else (0, 100)
        
        scored = []
        for r in responses:
            price = float(r.get('price') or r.get('total_price') or 0)
            dist = float(r.get('distance_km') or 0)
            rating_val = float(r.get('pharmacy_rating') or 0)
            rel = float(r.get('pharmacy_response_rate') or 100)
            
            norm_price = RankingEngine.normalize_price(price, min_p, max_p)
            norm_dist = RankingEngine.normalize_distance(dist, min_d, max_d)
            norm_rating = RankingEngine.normalize_rating(rating_val, min_r, max_r)
            norm_rel = RankingEngine.normalize_reliability(rel, min_rel, max_rel)
            
            score = (
                weights['price'] * norm_price +
                weights['distance'] * norm_dist +
                weights['rating'] * norm_rating +
                weights['reliability'] * norm_rel
            )
            breakdown = {
                'price': round(norm_price, 4),
                'distance': round(norm_dist, 4),
                'rating': round(norm_rating, 4),
                'reliability': round(norm_rel, 4)
            }
            scored.append({
                'response': r,
                'score': round(score, 4),
                'score_breakdown': breakdown,
                'weights_used': weights,
                'context': context
            })
        
        scored.sort(key=lambda x: x['score'], reverse=True)
        return scored, weights, context


class DrugInteractionService:
    """UC-P08/UC-S05: Check for drug interactions between medicines"""

    # Common known interactions (medicine pairs -> severity, description)
    KNOWN_INTERACTIONS = {
        ('warfarin', 'aspirin'): ('moderate', 'Increased bleeding risk'),
        ('warfarin', 'ibuprofen'): ('moderate', 'Increased bleeding risk'),
        ('warfarin', 'paracetamol'): ('mild', 'High doses may affect clotting'),
        ('aspirin', 'ibuprofen'): ('moderate', 'Increased stomach bleeding risk; avoid regular ibuprofen with aspirin'),
        ('aspirin', 'naproxen'): ('moderate', 'Increased stomach bleeding risk'),
        ('ibuprofen', 'naproxen'): ('moderate', 'Both NSAIDs - increased stomach/bleeding risk'),
        ('metformin', 'contrast'): ('moderate', 'Risk of lactic acidosis with contrast dyes'),
        ('maoi', 'tyramine'): ('severe', 'MAOIs with tyramine-rich foods - hypertensive crisis'),
        ('fluoxetine', 'maoi'): ('severe', 'Serotonin syndrome risk'),
        ('sertraline', 'maoi'): ('severe', 'Serotonin syndrome risk'),
    }

    @classmethod
    def normalize_medicine(cls, name: str) -> str:
        return name.lower().strip()

    @classmethod
    def check_interactions(cls, medicines: List[str]) -> List[Dict]:
        """Check all pairs of medicines for known interactions."""
        if len(medicines) < 2:
            return []
        norm = [cls.normalize_medicine(m) for m in medicines]
        results = []
        seen = set()
        for i, a in enumerate(norm):
            for j, b in enumerate(norm):
                if i >= j:
                    continue
                pair = tuple(sorted([a, b]))
                if pair in seen:
                    continue
                seen.add(pair)
                for (m1, m2), (sev, desc) in cls.KNOWN_INTERACTIONS.items():
                    if (m1 in a or a in m1) and (m2 in b or b in m2):
                        results.append({
                            'medicine_a': medicines[i],
                            'medicine_b': medicines[j],
                            'severity': sev,
                            'description': desc,
                        })
                    elif (m1 in b or b in m1) and (m2 in a or a in m2):
                        results.append({
                            'medicine_a': medicines[i],
                            'medicine_b': medicines[j],
                            'severity': sev,
                            'description': desc,
                        })
        return results
