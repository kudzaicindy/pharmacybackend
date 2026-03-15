"""
OCR Service for reading prescription images
Uses Google Gemini Vision API for better accuracy with medical text
"""
import os
from typing import Dict, List, Optional
from PIL import Image
import io
import base64

# Lazy import to avoid protobuf compatibility issues
_genai = None

def _get_genai():
    """Lazy import google.generativeai"""
    global _genai
    if _genai is None:
        try:
            import google.generativeai as genai
            _genai = genai
        except Exception as e:
            raise ImportError(f"Failed to import google.generativeai: {e}")
    return _genai


class OCRService:
    """Service for extracting text from prescription images"""
    
    def __init__(self):
        api_key = os.getenv('GEMINI_API_KEY')
        if not api_key:
            raise ValueError("GEMINI_API_KEY not found in environment variables")
        
        genai = _get_genai()
        genai.configure(api_key=api_key)
        # Using gemini-1.5-flash for vision tasks
        self.model = genai.GenerativeModel('gemini-1.5-flash')
    
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
            
            # Generate content with image
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
        # Common medicine name patterns
        medicines = []
        
        # Use Gemini to extract medicine names more accurately
        try:
            genai = _get_genai()
            extraction_prompt = f"""From this prescription text, extract ONLY the medicine/drug names. 
Return them as a comma-separated list. If no medicines are found, return "none".

Prescription text:
{text}

Medicine names:"""
            
            response = self.model.generate_content(extraction_prompt)
            result = response.text.strip().lower()
            
            if result and result != "none":
                medicines = [m.strip() for m in result.split(',') if m.strip()]
        except:
            # Fallback: simple pattern matching
            import re
            # Common medicine patterns
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
        import re
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
