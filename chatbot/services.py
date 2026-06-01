"""
AI Chatbot Service - Handles interactions with OpenRouter API
"""
import os
from typing import List, Dict, Optional, Any, Tuple
import json
import re
from PIL import Image
import io
import base64
import requests


def _gemini_candidate_text(response) -> str:
    """Best-effort plain text from Gemini SDK responses (generativeai + genai client shapes)."""
    if response is None:
        return ''
    try:
        t = getattr(response, 'text', None)
        if t:
            return str(t).strip()
    except Exception:
        pass
    # google.genai / some SDK versions: candidates[].content.parts[].text
    try:
        cands = getattr(response, 'candidates', None) or []
        if not cands:
            return ''
        parts = getattr(cands[0].content, 'parts', None) or []
        return ''.join(getattr(p, 'text', '') or '' for p in parts).strip()
    except Exception:
        pass
    # google.genai: parsed / alternative shapes
    try:
        pr = getattr(response, 'parsed', None)
        if pr is not None:
            if isinstance(pr, str) and pr.strip():
                return pr.strip()
            if isinstance(pr, dict):
                return json.dumps(pr, ensure_ascii=False)
    except Exception:
        pass
    return ''


def _gemini_response_diagnostic(response) -> str:
    """Non-empty hint when .text is empty (blocked, no candidates, etc.)."""
    if response is None:
        return 'no response object'
    parts: List[str] = []
    try:
        pf = getattr(response, 'prompt_feedback', None)
        if pf is not None:
            br = getattr(pf, 'block_reason', None)
            if br is not None:
                parts.append(f'prompt_feedback.block_reason={br}')
    except Exception:
        pass
    try:
        cands = getattr(response, 'candidates', None) or []
        if not cands:
            parts.append('candidates=[]')
        else:
            c0 = cands[0]
            fr = getattr(c0, 'finish_reason', None)
            if fr is not None:
                parts.append(f'finish_reason={fr}')
            sr = getattr(c0, 'safety_ratings', None)
            if sr:
                parts.append('safety_ratings=present')
    except Exception as exc:
        parts.append(f'candidate_parse_error={exc}')
    return '; '.join(parts) if parts else 'unknown_empty_response'


def _image_mime_type(image_file, pil_image: Image.Image, image_bytes: bytes) -> str:
    ct = (getattr(image_file, 'content_type', None) or '').split(';')[0].strip().lower()
    if ct in ('image/jpeg', 'image/png', 'image/webp', 'image/gif'):
        return ct
    fmt = (pil_image.format or '').upper()
    return {
        'JPEG': 'image/jpeg',
        'JPG': 'image/jpeg',
        'PNG': 'image/png',
        'WEBP': 'image/webp',
        'GIF': 'image/gif',
    }.get(fmt, 'image/png')


def normalize_gemini_api_model(model_name: str, *, default: str = 'gemini-2.0-flash') -> str:
    """
    Map retired Google AI Studio model IDs to ones that still support generateContent.

    ``gemini-1.5-flash`` / ``gemini-1.5-pro`` (and versioned aliases) commonly return HTTP 404
    on ``v1beta``; override with ``GEMINI_VISION_MODEL`` or ``GEMINI_CHAT_MODEL``.
    """
    retired = {
        'gemini-1.5-flash': default,
        'gemini-1.5-flash-001': default,
        'gemini-1.5-flash-002': default,
        'gemini-1.5-flash-latest': default,
        'gemini-1.5-flash-8b': default,
        'gemini-1.5-pro': default,
        'gemini-1.5-pro-001': default,
        'gemini-1.5-pro-002': default,
        'gemini-1.5-pro-latest': default,
    }
    name = (model_name or '').strip()
    if not name:
        return default
    return retired.get(name.lower(), name)


def build_ocr_vision_model_chain() -> List[str]:
    """
    Ordered list of Gemini models to try for prescription vision.
    Override primary via GEMINI_* env; extend with GEMINI_VISION_MODEL_FALLBACK (comma-separated).
    Without FALLBACK env, sensible alternates are appended so transient quota on one Flash variant
    can still succeed on another.
    """
    resolved = (
        os.getenv('GEMINI_VISION_MODEL')
        or os.getenv('GEMINI_OCR_MODEL')
        or os.getenv('GEMINI_CHAT_MODEL')
        or 'gemini-2.0-flash'
    ).strip()
    primary = normalize_gemini_api_model(resolved)
    chain: List[str] = [primary]
    fb_raw = (os.getenv('GEMINI_VISION_MODEL_FALLBACK') or '').strip()
    if fb_raw:
        for part in fb_raw.split(','):
            m = normalize_gemini_api_model(part.strip())
            if m and m not in chain:
                chain.append(m)
    else:
        for m in (
            'gemini-2.5-flash-lite',
            'gemini-2.5-flash',
            'gemini-2.0-flash-lite',
        ):
            nm = normalize_gemini_api_model(m)
            if nm not in chain:
                chain.append(nm)
    return chain


def gemini_exception_is_quota_or_rate_limit(exc: BaseException) -> bool:
    tn = type(exc).__name__
    if 'ResourceExhausted' in tn:
        return True
    msg = str(exc).lower()
    trimmed = msg.strip()
    if trimmed.startswith('429') or ' 429 ' in f' {msg} ':
        return True
    if 'quota exceeded' in msg or 'quota' in msg and 'limit' in msg and 'exceed' in msg:
        return True
    if 'rate limit' in msg or 'too many requests' in msg:
        return True
    return False


def gemini_exception_is_transient_model_rejection(exc: BaseException) -> bool:
    """
    True when trying the next model in the chain might help (429, model not enabled, etc.).
    """
    if gemini_exception_is_quota_or_rate_limit(exc):
        return True
    msg = str(exc).lower()
    if '404' in msg and ('not found' in msg or 'not supported' in msg):
        return True
    return False


def retry_after_seconds_from_gemini_exception(exc: BaseException) -> Optional[int]:
    import math

    s = str(exc)
    m = re.search(r'retry in (\d+(?:\.\d+)?)\s*s', s, re.IGNORECASE)
    if m:
        return max(1, int(math.ceil(float(m.group(1)))))
    m2 = re.search(r'seconds:\s*(\d+)', s)
    if m2:
        return max(1, int(m2.group(1)))
    return None


def prescription_ocr_error_for_client(exc: BaseException, *, attempted_models: Optional[List[str]] = None) -> dict:
    """Short, SPA-friendly Gemini error payload (truncates verbose SDK/proto text)."""
    raw_full = str(exc).strip()
    raw = raw_full[:1800]

    quota = gemini_exception_is_quota_or_rate_limit(exc)
    retry_sec = retry_after_seconds_from_gemini_exception(exc)

    attempts = ''
    if attempted_models:
        attempts = '; tried: ' + ', '.join(attempted_models[:8])

    if quota:
        friendly = (
            'Prescription OCR is temporarily unavailable: your Google Gemini project hit a '
            'rate limit or has no usable quota for the models tried. '
            'If you see "limit: 0" on the free tier, enable billing or pick another model '
            '(set GEMINI_VISION_MODEL / GEMINI_VISION_MODEL_FALLBACK in .env). '
            'Limits: https://ai.google.dev/gemini-api/docs/rate-limits'
        )
        if retry_sec:
            friendly += f' Estimated retry-after: ~{retry_sec}s.'
        friendly += attempts
        return {
            'error_code': 'gemini_quota_rate_limit',
            'error': friendly,
            'error_detail': raw,
            **({'retry_after_seconds': retry_sec} if retry_sec is not None else {}),
            **({'attempted_models': attempted_models} if attempted_models else {}),
        }

    friendly = raw if len(raw) < 900 else raw[:900] + '…'
    if attempts:
        friendly += attempts

    out: Dict[str, Any] = {
        'error_code': 'gemini_prescription_error',
        'error': friendly,
        'error_detail': raw_full[:4000] if len(raw_full) > len(raw) else '',
    }
    if attempted_models:
        out['attempted_models'] = attempted_models
    return out


def _strip_json_code_fences(s: str) -> str:
    s = (s or '').strip()
    if not s.startswith('```'):
        return s
    lines = s.split('\n')
    if lines and lines[0].strip().startswith('```'):
        lines = lines[1:]
    if lines and lines[-1].strip() == '```':
        lines = lines[:-1]
    return '\n'.join(lines).strip()


def _extract_json_object(s: str) -> Optional[dict]:
    """Parse a JSON object from model output; tolerate fences and trailing junk."""
    s = _strip_json_code_fences(s)
    if not s:
        return None
    try:
        out = json.loads(s)
        return out if isinstance(out, dict) else None
    except Exception:
        pass
    i = s.find('{')
    if i == -1:
        return None
    depth = 0
    for j in range(i, len(s)):
        if s[j] == '{':
            depth += 1
        elif s[j] == '}':
            depth -= 1
            if depth == 0:
                try:
                    out = json.loads(s[i : j + 1])
                    return out if isinstance(out, dict) else None
                except Exception:
                    return None
    return None


def _ocr_dose_parts(item: dict) -> List[str]:
    parts: List[str] = []
    for k in ('strength', 'dose', 'frequency', 'duration', 'instructions'):
        v = (item.get(k) or '').strip() if isinstance(item, dict) else ''
        if v:
            parts.append(v)
    return parts


def _format_prescription_summary_markdown(items: List[Dict[str, Any]], confidence_percent: int) -> str:
    """Patient-facing block (Markdown) aligned with product copy."""
    lines = [
        "I've processed your prescription!",
        "",
        "**Medicines found:**",
    ]
    for i, it in enumerate(items, 1):
        name = (it.get('name') or '').strip()
        if name:
            lines.append(f"{i}. {name}")
    lines.extend(["", "**Dosages:**"])
    for it in items:
        name = (it.get('name') or '').strip()
        dose_bits = _ocr_dose_parts(it)
        dose_s = ' — '.join(dose_bits) if dose_bits else 'Not read clearly — confirm with your pharmacist.'
        lines.append(f"- **{name}:** {dose_s}")
    lines.extend(["", f"**Confidence:** {int(confidence_percent)}%"])
    return '\n'.join(lines)


OCR_VISION_JSON_PROMPT = """You are reading a prescription image for a pharmacy availability app.

Return ONLY a single JSON object (no markdown code fences, no commentary before or after) with this exact structure:
{
  "items": [
    {
      "name": "medicine or product name as on the script",
      "strength": "e.g. 40 mg or 10 mg/ml — use empty string if absent",
      "dose": "e.g. 1 tablet or 1 sachet — use empty string if absent",
      "frequency": "e.g. twice daily — use empty string if absent",
      "duration": "e.g. 5 days — use empty string if absent",
      "instructions": "e.g. before meals — use empty string if absent"
    }
  ],
  "confidence_percent": 85,
  "reading_notes": "short note if handwriting is unclear, otherwise empty string"
}

Rules:
- Include one object per distinct line/medicine on the prescription (including ORS sachets, suspensions, etc.).
- Copy names faithfully; do not invent medicines not visible on the image.
- confidence_percent: integer 0–100 reflecting how sure you are about names and strengths.
- Use "" for any field you cannot read."""


OCR_VISION_PROSE_PROMPT = """Analyze this prescription image and extract:
1. All medicine/drug names
2. Dosages for each medicine
3. Frequency (how many times per day)
4. Duration (how many days)
5. Any special instructions

Return the information in a structured format. If you cannot read something clearly, indicate that.

Focus on extracting medicine names accurately as this is critical for patient safety."""


class OCRService:
    """Service for extracting text from prescription images using Google Gemini API"""
    
    def __init__(self):
        api_key = os.getenv('GEMINI_API_KEY')
        if not api_key:
            raise ValueError("GEMINI_API_KEY not found in environment variables")

        self._api_key = api_key
        self._vision_model_chain = build_ocr_vision_model_chain()
        self.model_name = self._vision_model_chain[0]

        # Vision/OCR always uses google.generativeai (matches requirements.txt). The newer
        # `google.genai` client returns a different response shape; using it here often yielded
        # empty `raw_text` without raising. Chat may still use other stacks separately.
        try:
            import google.generativeai as genai_old

            genai_old.configure(api_key=api_key)
            self.model = genai_old.GenerativeModel(self.model_name)
            print('[INFO] OCR Gemini model chain: ' + ' -> '.join(self._vision_model_chain))
        except Exception as e:
            raise ValueError(
                f"Failed to initialize Gemini API for OCR: {e}. "
                'Install with: pip install google-generativeai'
            ) from e

    def _bind_generative_model(self, model_id: str) -> None:
        """Align follow-up text calls with the vision model that returned content."""
        import google.generativeai as genai_old

        genai_old.configure(api_key=self._api_key)
        self.model_name = model_id
        self.model = genai_old.GenerativeModel(model_id)

    def _generate_text_prescription(self, prompt: str) -> Optional[str]:
        """generateContent retries across the vision chain when the API returns quota / unsupported model."""
        import google.generativeai as genai_old

        genai_old.configure(api_key=self._api_key)
        last_exc: Optional[BaseException] = None
        for mn in self._vision_model_chain:
            try:
                model = genai_old.GenerativeModel(mn)
                response = model.generate_content(prompt)
                out = _gemini_candidate_text(response)
                if out:
                    self._bind_generative_model(mn)
                    return out
            except Exception as e:
                last_exc = e
                if not gemini_exception_is_transient_model_rejection(e):
                    raise
                print(f'[WARN] OCR text pass model={mn}: {e}')
        if last_exc:
            raise last_exc
        return None

    def _generate_vision(
        self, prompt: str, pil_for_gemini: Image.Image, image_bytes: bytes, mime_type: str
    ) -> str:
        import google.generativeai as genai_old

        genai_old.configure(api_key=self._api_key)
        last_quota_like: Optional[BaseException] = None

        for mn in self._vision_model_chain:
            model = genai_old.GenerativeModel(mn)
            pil_saw_transient = False

            try:
                response = model.generate_content([prompt, pil_for_gemini])
                out = _gemini_candidate_text(response)
                if out:
                    self._bind_generative_model(mn)
                    return out
                diag = _gemini_response_diagnostic(response)
                print(
                    f'[WARN] OCR vision returned empty PIL text model={mn} ({diag}); '
                    f'retrying raw bytes + {mime_type}'
                )
            except Exception as e:
                if not gemini_exception_is_transient_model_rejection(e):
                    raise
                pil_saw_transient = True
                if gemini_exception_is_quota_or_rate_limit(e):
                    last_quota_like = e
                print(f'[WARN] OCR vision PIL model={mn}: {e}')

            try:
                response = model.generate_content(
                    [prompt, {'mime_type': mime_type, 'data': image_bytes}]
                )
                out = _gemini_candidate_text(response)
                if out:
                    self._bind_generative_model(mn)
                    return out
                if not out:
                    print(
                        '[WARN] OCR vision bytes path empty model='
                        f'{mn}: {_gemini_response_diagnostic(response)}'
                    )
            except Exception as e:
                if gemini_exception_is_transient_model_rejection(e):
                    if gemini_exception_is_quota_or_rate_limit(e):
                        last_quota_like = e
                    print(f'[WARN] OCR vision bytes model={mn}: {e}')
                    continue

                raise

            if pil_saw_transient:
                continue

            return ''

        if last_quota_like:
            raise last_quota_like

        return ''

    @staticmethod
    def _items_from_json_payload(data: Optional[dict]) -> Tuple[List[Dict[str, Any]], Dict[str, str], int, str]:
        """Build structured items, per-medicine dosage map (lower-case key), confidence, notes."""
        if not isinstance(data, dict):
            return [], {}, 0, ''
        items_out: List[Dict[str, Any]] = []
        seen_lower: set[str] = set()
        for raw in data.get('items') or []:
            if not isinstance(raw, dict):
                continue
            name = (raw.get('name') or '').strip()
            if len(name) < 2:
                continue
            nl = name.lower()
            if nl in seen_lower:
                continue
            seen_lower.add(nl)
            item = {
                'name': name,
                'strength': str(raw.get('strength') or '').strip(),
                'dose': str(raw.get('dose') or '').strip(),
                'frequency': str(raw.get('frequency') or '').strip(),
                'duration': str(raw.get('duration') or '').strip(),
                'instructions': str(raw.get('instructions') or '').strip(),
            }
            items_out.append(item)
        try:
            cp = int(float(data.get('confidence_percent', 0)))
        except (TypeError, ValueError):
            cp = 70 if items_out else 0
        cp = max(0, min(100, cp))
        if items_out and cp == 0:
            cp = 65
        notes = str(data.get('reading_notes') or '').strip()
        dosages: Dict[str, str] = {}
        for it in items_out:
            k = it['name'].lower().strip()
            line = ' — '.join(_ocr_dose_parts(it))
            dosages[k] = line
        return items_out, dosages, cp, notes

    def _try_structure_prescription_text(
        self, transcript: str, medicine_names: List[str]
    ) -> Optional[dict]:
        """Second-pass text-only JSON to attach dosages to an ordered medicine list."""
        if not (transcript or '').strip() or not medicine_names:
            return None
        prompt = (
            'The following text was read from a prescription image.\n'
            f'Medicines already identified (in this order): {json.dumps(medicine_names)}\n\n'
            f'Prescription text:\n{transcript[:12000]}\n\n'
            'Return ONLY valid JSON (no markdown fences) with this shape:\n'
            '{"items": [{"name": "...", "strength": "", "dose": "", "frequency": "", '
            '"duration": "", "instructions": ""}], "confidence_percent": 0-100, "reading_notes": ""}\n'
            'Use one item per medicine in the same order; fill fields from the text when possible, else "".'
        )
        text = self._generate_text_prescription(prompt)
        return _extract_json_object(text or '') if text else None

    def extract_prescription_text(self, image_file) -> Dict:
        """
        Extract medicine names and per-line dosages from a prescription image.

        Returns:
            Dict with medicines, items (structured), dosages (name_lower -> dose line),
            raw_text, confidence ('high'|'low'), confidence_percent (0–100),
            reading_notes, summary_markdown (patient-facing).
        """
        try:
            if hasattr(image_file, 'read'):
                image_file.seek(0)
                image_bytes = image_file.read()
            else:
                image_bytes = b''
            if not image_bytes:
                return {
                    'medicines': [],
                    'items': [],
                    'dosages': {},
                    'raw_text': '',
                    'confidence': 'low',
                    'confidence_percent': 0,
                    'reading_notes': '',
                    'summary_markdown': '',
                    'error': 'Empty image upload',
                }

            image = Image.open(io.BytesIO(image_bytes))
            try:
                image.load()
            except Exception:
                pass

            mime_type = _image_mime_type(image_file, image, image_bytes)
            pil_for_gemini = image
            if pil_for_gemini.mode not in ('RGB', 'RGBA', 'L'):
                pil_for_gemini = pil_for_gemini.convert('RGB')

            raw_text = ''
            reading_notes = ''

            t_json = self._generate_vision(
                OCR_VISION_JSON_PROMPT, pil_for_gemini, image_bytes, mime_type
            )
            raw_text = t_json
            data = _extract_json_object(t_json)
            items_out, dosages, cp, reading_notes = self._items_from_json_payload(data)

            if not items_out:
                t_prose = self._generate_vision(
                    OCR_VISION_PROSE_PROMPT, pil_for_gemini, image_bytes, mime_type
                )
                if t_prose:
                    raw_text = t_prose
                meds = self._extract_medicine_names(t_prose)
                if meds:
                    enriched = self._try_structure_prescription_text(t_prose, meds)
                    if enriched:
                        items_out, dosages, cp2, n2 = self._items_from_json_payload(enriched)
                        reading_notes = reading_notes or n2
                        cp = max(cp, cp2, 55)
                    else:
                        items_out = [
                            {
                                'name': m,
                                'strength': '',
                                'dose': '',
                                'frequency': '',
                                'duration': '',
                                'instructions': '',
                            }
                            for m in meds
                        ]
                        dosages = {m.lower().strip(): '' for m in meds}
                        cp = max(cp, 55)

            medicines = [it['name'] for it in items_out]
            if not cp and medicines:
                cp = 70
            summary_md = (
                _format_prescription_summary_markdown(items_out, cp) if items_out else ''
            )
            conf_label = 'high' if medicines else 'low'

            out: Dict[str, Any] = {
                'medicines': medicines,
                'items': items_out,
                'dosages': dosages,
                'raw_text': raw_text,
                'confidence': conf_label,
                'confidence_percent': cp if medicines else 0,
                'reading_notes': reading_notes,
                'summary_markdown': summary_md,
            }
            if not medicines:
                rt = (raw_text or '').strip()
                if not rt:
                    out['error'] = (
                        f'Vision returned no text (model={self.model_name}). '
                        'Confirm GEMINI_API_KEY is loaded, billing/quota applies for Gemini API, '
                        'and tweak GEMINI_VISION_MODEL / GEMINI_VISION_MODEL_FALLBACK. '
                        'See server logs for [WARN] OCR.'
                    )
                else:
                    out['error'] = (
                        'Could not parse medicines from model output; inspect raw_text or adjust prompts.'
                    )
            return out

        except Exception as e:
            print(f'[WARN] OCR extract_prescription_text failed: {e}')
            extras = prescription_ocr_error_for_client(
                e, attempted_models=list(self._vision_model_chain)
            )
            return {
                'medicines': [],
                'items': [],
                'dosages': {},
                'raw_text': '',
                'confidence': 'low',
                'confidence_percent': 0,
                'reading_notes': '',
                'summary_markdown': '',
                **extras,
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
            
            text_out = self._generate_text_prescription(extraction_prompt)
            result = (text_out or '').lower()
            
            if result and result != "none":
                raw = [m.strip() for m in result.split(',') if m.strip()]
                seen: set[str] = set()
                for m in raw:
                    ml = m.lower().strip()
                    if len(ml) > 2 and ml not in seen:
                        seen.add(ml)
                        medicines.append(ml)
        except Exception as e:
            # Quota / exhausted model chain: propagate so upload-prescription JSON can explain billing/limits.
            if gemini_exception_is_quota_or_rate_limit(e):
                raise
            # Odd model/network errors — still try heuristic extraction from prose OCR text.
            patterns = [
                r'\b([A-Z][a-z]+(?:[-\s][A-Z][a-z]+)*)\s*(?:tablet|tab|capsule|cap|mg|ml|g)\b',
                r'\b(paracetamol|ibuprofen|amoxicillin|aspirin|penicillin)\b',
            ]
            
            for pattern in patterns:
                matches = re.findall(pattern, text, re.IGNORECASE)
                medicines.extend(matches)
        
        # Dedupe by lowercase, preserve first-seen order
        seen_lo: set[str] = set()
        ordered: List[str] = []
        for m in medicines:
            ml = m.lower().strip()
            if len(ml) <= 2 or ml in seen_lo:
                continue
            seen_lo.add(ml)
            ordered.append(ml)
        return ordered
    
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


def _chatbot_policy_prompt_suffix(policy: dict) -> str:
    """
    Append instructions derived from PlatformAdminSettings.chatbot_policy (merged with defaults).
    Only adds lines for enabled flags so admins can relax rules by turning toggles off.
    """
    lines: list[str] = []
    if policy.get('medical_disclaimer_all_responses', True):
        lines.append(
            'End substantive replies with a short reminder that you are not a doctor and the user should consult a '
            'healthcare professional for diagnosis and treatment.'
        )
    if policy.get('restrict_dosage_advice', True):
        lines.append(
            'Do not give specific dosing (mg, ml, tablets per dose, or duration). Direct users to a pharmacist or '
            'doctor for dosing. General OTC class mentions are OK without numbers.'
        )
    if policy.get('paediatric_warning_under_5', True):
        lines.append(
            'If the user mentions an infant or child under 5, emphasise that a clinician or pharmacist must advise '
            'before giving any medicine; do not suggest paediatric doses.'
        )
    if policy.get('emergency_symptom_detection', True):
        lines.append(
            'If the user describes possible emergency symptoms (e.g. chest pain, trouble breathing, severe bleeding, '
            'stroke-like symptoms, loss of consciousness), tell them to seek emergency medical services immediately '
            'and do not rely on chat for urgent care.'
        )
    if policy.get('prescription_only_flag', False):
        lines.append(
            'When naming medicines that are typically prescription-only, note that a valid prescription may be '
            'required and pharmacies on the platform will confirm.'
        )
    if not lines:
        return ''
    return '\n\nPLATFORM SAFETY RULES (must follow):\n' + '\n'.join(f'• {x}' for x in lines)


class ChatbotService:
    """Service for handling AI chatbot interactions using OpenRouter API or Gemini (fallback)"""
    
    def __init__(self):
        self.api_key = (os.getenv('OPENROUTER_API_KEY') or '').strip()
        self.gemini_key = (os.getenv('GEMINI_API_KEY') or '').strip()
        use_gemini = os.getenv('USE_GEMINI_FOR_CHAT', '').lower() in ('1', 'true', 'yes')
        self.backend = None
        # Prefer Gemini when USE_GEMINI_FOR_CHAT is set or when only GEMINI_API_KEY is set
        if use_gemini and self.gemini_key:
            self.backend = 'gemini'
            print("[INFO] Chatbot using GEMINI_API_KEY (USE_GEMINI_FOR_CHAT=true)")
        elif self.gemini_key and not self.api_key:
            self.backend = 'gemini'
            print("[INFO] Chatbot using GEMINI_API_KEY (OpenRouter not set)")
        elif self.api_key:
            self.backend = 'openrouter'
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
Step 1 - When patient describes ANY symptoms (e.g. "I have fever", "I have a headache", "I have sore legs", "sore leg", "leg pain", "runny stomach", "body pains", "muscle ache"):
  • NEVER ask for location in Step 1. NEVER say "where are you", "share your location", or "I need your location" in this step.
  • First: analyze the symptoms and suggest 2–4 specific medicine names with a short reason for each.
  • Use exact medicine names so they can be detected: paracetamol, ibuprofen, diclofenac, loperamide, oral rehydration salts, antacid, cough syrup, etc.
  • Example: "Based on your symptoms (sore legs / leg pain), you might need: • Paracetamol – for pain • Ibuprofen – for pain and inflammation • Diclofenac – for muscle/leg pain. Would you like to search for these medicines? You can say yes or tell me which ones you want."
  • End Step 1 with: "Would you like to search for these medicines?" Do NOT ask for location yet.

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
- Mention medicine names clearly so they can be extracted (paracetamol, ibuprofen, etc.)
- Paracetamol is appropriate for sore throat (pain and fever relief); also suggest throat lozenges and, if relevant, cough syrup or amoxicillin. For sore throat give 2–4 options including paracetamol and at least one throat-specific option (e.g. throat lozenges)."""
    
    def _effective_system_prompt(self, language_instruction: str = '') -> str:
        from .admin_analytics import merge_chatbot_policy

        policy = merge_chatbot_policy()
        return self.system_prompt + language_instruction + _chatbot_policy_prompt_suffix(policy)
    
    def _call_gemini(self, system_content: str, history: List[Dict[str, str]], user_message: str) -> str:
        """Generate chat response using Google Gemini (fallback when OpenRouter not set)."""
        try:
            import google.generativeai as genai
            genai.configure(api_key=self.gemini_key)
            # Prefer a model that still exists on v1beta generateContent; override with GEMINI_CHAT_MODEL.
            chat_model_raw = os.getenv('GEMINI_CHAT_MODEL', 'gemini-2.0-flash')
            chat_model = normalize_gemini_api_model(chat_model_raw.strip())
            if chat_model_raw.strip().lower() != chat_model.lower():
                print(f'[INFO] Chat Gemini: mapped env model {chat_model_raw!r} -> {chat_model!r}')
            model = genai.GenerativeModel(chat_model)
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

    def _call_openrouter_text(self, system_prompt: str, user_prompt: str, max_tokens: int = 900) -> str:
        """Call OpenRouter directly for non-chatbot narratives."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://pharmacybackend.com",
            "X-Title": "Pharmacy Admin Reports",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
            "max_tokens": max_tokens,
        }
        response = requests.post(self.api_url, headers=headers, json=payload, timeout=45)
        response.raise_for_status()
        result = response.json()
        return (result.get('choices', [{}])[0].get('message', {}).get('content') or '').strip()

    def generate_admin_report_narrative(
        self,
        dashboard_snapshot: Dict,
        *,
        report_type: str = 'dashboard_summary',
        timeframe: str = 'last_30_days',
        tone: str = 'executive',
        custom_instruction: str = '',
    ) -> str:
        """
        Generate narrative text for admin PDF reports.
        Uses the configured provider backend, but with an admin-report prompt
        (separate from patient chatbot instructions).
        """
        safe_snapshot = dashboard_snapshot if isinstance(dashboard_snapshot, dict) else {}
        compact = json.dumps(safe_snapshot, ensure_ascii=True, separators=(',', ':'))
        if len(compact) > 15000:
            compact = compact[:15000] + '... [truncated]'

        system_prompt = (
            "You are a healthcare operations analyst for a pharmacy platform. "
            "Write clear, factual report narratives from dashboard JSON. "
            "Do not invent metrics not present in input. "
            "Highlight trends, risks, and practical recommendations."
        )
        user_prompt = (
            f"Report type: {report_type}\n"
            f"Timeframe: {timeframe}\n"
            f"Tone: {tone}\n"
            f"Custom instruction: {custom_instruction or 'none'}\n\n"
            "Output format requirements:\n"
            "- Return STRICT markdown only (no JSON, no code fences unless showing sample snippets).\n"
            "- Use headings (##) and bullet lists for readability.\n"
            "- Keep style concise and professional for PDF rendering.\n\n"
            "Produce markdown with sections:\n"
            "1) Executive summary (3-5 bullets)\n"
            "2) KPI highlights\n"
            "3) Risks / gaps\n"
            "4) Recommended actions (prioritized)\n\n"
            f"Dashboard snapshot JSON:\n{compact}"
        )

        if self.backend == 'gemini':
            return self._call_gemini(system_prompt, [], user_prompt)
        return self._call_openrouter_text(system_prompt, user_prompt)
    
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
            base_prompt = self._effective_system_prompt(language_instruction)
            
            # Add system prompt as first message
            messages.append({
                "role": "user",
                "content": base_prompt
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
                
                # Add system prompt with language instruction + DB safety policy
                openrouter_messages.append({
                    "role": "system",
                    "content": base_prompt
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
                        system_content=base_prompt,
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
            # For symptom intent, supplement suggested_medicines from our map if AI didn't list any (for frontend only; AI response text is never overridden)
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
            
            # Rule-based fallback when API fails: keep the flow going.
            # 1) User said "yes"/"ok" after we suggested medicines → ask for location
            confirmation_words = ['yes', 'yeah', 'yep', 'ok', 'okay', 'sure', 'all of them', 'proceed', 'go ahead']
            suggested_from_context = (context or {}).get('suggested_medicines') or []
            if user_message.lower().strip() in confirmation_words and suggested_from_context:
                print("[INFO] Rule-based fallback: user confirmed medicines; asking for location.")
                return {
                    'response': "To find pharmacies near you, please share your location (e.g. area name or use your current location).",
                    'intent': 'medicine_selection',
                    'entities': {},
                    'requires_location': True,
                    'suggested_medicines': suggested_from_context,
                    'selected_medicines': suggested_from_context,
                    'confidence': 0.7,
                    'fallback': True,
                }
            # 2) User described symptoms → suggest medicines from built-in map
            symptom_words = [
                'headache', 'pain', 'pains', 'fever', 'stomach', 'runny stomach', 'sore leg', 'leg',
                'cough', 'cold', 'flu', 'nausea', 'sore throat', 'runny nose', 'body ache', 'muscle'
            ]
            if any(w in user_message.lower() for w in symptom_words):
                entities_fallback = self._extract_entities(user_message)
                suggested = self._suggest_medicines_from_symptoms(user_message, entities_fallback)
                if suggested:
                    med_lines = '\n'.join(f"• {m.title()}" for m in suggested[:5])
                    fallback_response = (
                        f"Based on your symptoms, you might need:\n{med_lines}\n\n"
                        "Would you like to search for these medicines? You can say yes or tell me which ones you want."
                    )
                    print("[INFO] Using rule-based fallback (API unavailable); suggested medicines from symptom map.")
                    return {
                        'response': fallback_response,
                        'intent': 'symptom_description',
                        'entities': entities_fallback,
                        'requires_location': False,
                        'suggested_medicines': suggested,
                        'confidence': 0.7,
                        'fallback': True,
                    }
            
            # No fallback match: return user-friendly error
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
        # Symptom description: user describes how they feel (must suggest medicines first, then ask for location)
        symptom_words = [
            'headache', 'pain', 'pains', 'fever', 'symptom', 'feeling', 'body pain', 'body ache', 'muscle',
            'stomach', 'runny stomach', 'upset stomach', 'diarrhea', 'diarrhoea', 'vomiting', 'vomit',
            'cough', 'cold', 'flu', 'nausea', 'dizziness', 'sore throat', 'runny nose', 'stuffy nose',
            'sore leg', 'sore legs', 'leg pain', 'legs', 'leg '
        ]
        if any(word in message_lower for word in symptom_words):
            return 'symptom_description'
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
            'sore throat', 'stomach ache', 'stomach', 'runny stomach', 'diarrhea', 'diarrhoea', 'runny nose',
            'stuffy nose', 'body ache', 'body pain', 'body pains', 'muscle ache', 'myalgia', 'upset stomach',
            'sore leg', 'sore legs', 'leg pain', 'legs', 'leg'
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
            if medicine in seen:
                continue
            # Avoid matching literal "ors" inside unrelated tokens; require word boundary for "ors" only.
            if medicine == 'ors':
                if not re.search(r'\bors\b', response_lower):
                    continue
            elif medicine not in response_lower:
                continue
            # Use canonical form (ORS -> oral rehydration salts) only for standalone ORS mentions
            canonical = 'oral rehydration salts' if medicine == 'ors' else medicine
            medicines.append(canonical)
            seen.add(medicine)
            seen.add(canonical)

        # Prescription-style numbered list: "Medicines found: 1. domperidone 2. ors sachet …"
        _rx_head = ('medicines found', 'processed your prescription')
        if any(h in response_lower for h in _rx_head):
            tail = ai_response
            low = response_lower
            for mk in (
                '**medicines found:**',
                '*medicines found:*',
                'medicines found:',
                'processed your prescription',
            ):
                pos = low.find(mk.lower())
                if pos != -1:
                    tail = ai_response[pos + len(mk) :]
                    break
            block_rx = {
                'dosage', 'dosages', 'confidence', 'medicine', 'medicines', 'found', 'processed',
                'your', 'prescription', 'the', 'and', 'or',
            }
            for chunk in re.split(r'\d+\s*[\.)]\s*', tail):
                piece = chunk.split('**')[0]
                piece = re.split(r'(?:dosage|confidence)\s*:', piece, flags=re.I)[0]
                name = piece.strip().strip('*,;:-• ').strip()
                name_lower = name.lower()
                if len(name_lower) < 3 or name_lower in block_rx:
                    continue
                if name_lower in seen:
                    continue
                medicines.append(name_lower)
                seen.add(name_lower)
        
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
            'sore leg': ['paracetamol', 'ibuprofen', 'diclofenac'],
            'sore legs': ['paracetamol', 'ibuprofen', 'diclofenac'],
            'leg pain': ['paracetamol', 'ibuprofen', 'diclofenac'],
            'legs': ['paracetamol', 'ibuprofen', 'diclofenac'],
            'leg': ['paracetamol', 'ibuprofen', 'diclofenac'],
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
            address = re.sub(r'\bmt\.?\s*pleasant\b', 'Mount Pleasant', address, flags=re.I)
            
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
    def _format_reverse_address(addr: dict, detail: str) -> str | None:
        """Build one line from Nominatim `address` object. Returns None if nothing usable."""
        detail = (detail or "full").lower()
        if detail == "short":
            suburb = addr.get("suburb") or addr.get("neighbourhood") or addr.get("village") or ""
            city = addr.get("city") or addr.get("town") or addr.get("municipality") or addr.get("state") or ""
            parts = [p.strip() for p in [suburb, city] if p and str(p).strip()]
            return ", ".join(parts) if parts else None

        # full: road → suburb/area → city → country (deduped)
        chunks: list[str] = []
        road = (
            addr.get("road")
            or addr.get("pedestrian")
            or addr.get("residential")
            or addr.get("path")
            or ""
        )
        if road and str(road).strip():
            chunks.append(str(road).strip())
        area = (
            addr.get("suburb")
            or addr.get("neighbourhood")
            or addr.get("village")
            or addr.get("quarter")
            or addr.get("hamlet")
            or ""
        )
        if area and str(area).strip():
            a = str(area).strip()
            if a.lower() not in {c.lower() for c in chunks}:
                chunks.append(a)
        city = (
            addr.get("city")
            or addr.get("town")
            or addr.get("municipality")
            or addr.get("city_district")
            or ""
        )
        if city and str(city).strip():
            c = str(city).strip()
            if c.lower() not in {x.lower() for x in chunks}:
                chunks.append(c)
        country = addr.get("country") or ""
        if country and str(country).strip():
            ctry = str(country).strip()
            if ctry.lower() not in {x.lower() for x in chunks}:
                chunks.append(ctry)
        if not chunks:
            return None
        return ", ".join(chunks)

    @staticmethod
    def reverse_geocode(
        lat: float,
        lon: float,
        fallback: str = "",
        *,
        detail: str = "full",
    ) -> str:
        """
        Convert coordinates to a human-readable line using OpenStreetMap Nominatim.

        - detail='full' (default): road/area, suburb, city, country — e.g.
          "Borrowdale Road, Borrowdale, Harare, Zimbabwe" when OSM has those fields.
        - detail='short': suburb and city only, e.g. "Borrowdale, Harare".

        On failure, returns `fallback` or empty string.
        """
        if lat is None or lon is None:
            return fallback or ""
        try:
            url = "https://nominatim.openstreetmap.org/reverse"
            headers = {
                "User-Agent": "PharmacyBackend/1.0"  # Required by Nominatim
            }
            params = {
                "lat": lat,
                "lon": lon,
                "format": "json",
                "addressdetails": 1,
            }
            resp = requests.get(url, params=params, headers=headers, timeout=10)
            if resp.status_code != 200:
                return fallback or ""
            data = resp.json() or {}
            addr = data.get("address") or {}
            formatted = LocationService._format_reverse_address(addr, detail)
            if formatted:
                return formatted

            display = (data.get("display_name") or "").strip()
            if display:
                if (detail or "full").lower() == "short":
                    return display.split(",")[0].strip()
                # full: first segments of display_name (avoids dumping entire global string)
                segs = [s.strip() for s in display.split(",") if s.strip()][:6]
                if segs:
                    return ", ".join(segs)
            return fallback or ""
        except Exception as e:
            print(f"[WARN] Reverse geocoding error for {lat},{lon}: {e}")
            return fallback or ""
    
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


def normalize_mcda_weights(custom: dict):
    """Return price/distance/rating/reliability as positive floats summing to 1. Accepts 0–1 or 0–100 scale."""
    keys = ('price', 'distance', 'rating', 'reliability')
    try:
        raw = {k: float(custom[k]) for k in keys}
    except (KeyError, TypeError, ValueError):
        return None
    s = sum(raw.values())
    if s <= 0:
        return None
    if s > 1.5:
        raw = {k: v / 100.0 for k, v in raw.items()}
        s = sum(raw.values())
    if s <= 0:
        return None
    return {k: raw[k] / s for k in keys}


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
            for p in Pharmacy.objects.filter(is_active=True, verification_status='verified'):
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

        Order: valid JSON on PlatformAdminSettings for this context → preset named
        ``active_ranking_profile`` (when stored JSON is empty) → built-in defaults.
        """
        try:
            from .models import PlatformAdminSettings

            row = PlatformAdminSettings.objects.filter(singleton_id='main').first()
            if row:
                return resolve_platform_mcda_weights(pharmacy_density, row)
        except Exception:
            pass
        return RankingEngine.default_weights(pharmacy_density >= 3)

    @staticmethod
    def default_weights(is_urban: bool) -> dict:
        if is_urban:
            return {
                'price': 0.35,
                'distance': 0.25,
                'rating': 0.25,
                'reliability': 0.15,
            }
        return {
            'price': 0.20,
            'distance': 0.45,
            'rating': 0.20,
            'reliability': 0.15,
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


def resolve_platform_mcda_weights(pharmacy_density: int, row) -> dict:
    """
    Effective MCDA weights (floats summing to 1) for a density context.

    Uses ``ranking_weights_urban`` / ``ranking_weights_rural`` when non-empty and valid;
    otherwise the ``urban`` / ``rural`` slice of ``RANKING_PROFILE_PRESETS`` matching
    ``active_ranking_profile`` (so "Save profile only" without persisting JSON still applies);
    otherwise ``RankingEngine.default_weights``.
    """
    is_urban_ctx = pharmacy_density >= 3
    field = 'ranking_weights_urban' if is_urban_ctx else 'ranking_weights_rural'
    custom = getattr(row, field, None) or {}
    merged = normalize_mcda_weights(custom)
    if merged:
        return merged
    try:
        from .admin_analytics import RANKING_PROFILE_PRESETS
    except Exception:
        return RankingEngine.default_weights(is_urban_ctx)
    prof = (getattr(row, 'active_ranking_profile', None) or '').strip()
    if prof:
        preset = next((p for p in RANKING_PROFILE_PRESETS if p['id'] == prof), None)
        if preset:
            ctx_key = 'urban' if is_urban_ctx else 'rural'
            raw = dict(preset.get(ctx_key) or {})
            merged = normalize_mcda_weights(raw)
            if merged:
                return merged
    return RankingEngine.default_weights(is_urban_ctx)


class DrugInteractionService:
    """Drug–drug interaction hints (embedded rules).

    Intended for clinician/patient awareness only — expand with DrugBank/OpenFDA/etc. when licensed.
    """

    DISCLAIMER = (
        'Interaction checks use a limited embedded ruleset — not exhaustive. '
        'This is not a substitute for professional medical advice or a licensed DDI database. '
        'Always confirm with a doctor or pharmacist.'
    )

    # Common interactions (medicine pair keys -> severity, description). Prefer generic names where possible.
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

    _SEVERITY_ORDER = {'severe': 3, 'moderate': 2, 'mild': 1}

    @classmethod
    def normalize_medicine(cls, name: str) -> str:
        return (name or '').lower().strip()

    @classmethod
    def check_interactions(cls, medicines: List[str]) -> List[Dict]:
        """Check all unordered pairs against KNOWN_INTERACTIONS using substring matching on normalized tokens."""
        if len(medicines) < 2:
            return []
        display = [str(m).strip() for m in medicines if m and str(m).strip()]
        norm = [cls.normalize_medicine(x) for x in display]
        if len(norm) < 2:
            return []
        results: List[Dict] = []
        for i in range(len(norm)):
            for j in range(i + 1, len(norm)):
                a, b = norm[i], norm[j]
                for (m1, m2), (sev, desc) in cls.KNOWN_INTERACTIONS.items():
                    hit = False
                    if (m1 in a or a in m1) and (m2 in b or b in m2):
                        hit = True
                    elif (m1 in b or b in m1) and (m2 in a or a in m2):
                        hit = True
                    if hit:
                        results.append({
                            'medicine_a': display[i],
                            'medicine_b': display[j],
                            'severity': sev,
                            'description': desc,
                        })
                        break

        dedup_keys = set()
        out = []
        for r in results:
            ka = str(r['medicine_a']).lower().strip()
            kb = str(r['medicine_b']).lower().strip()
            nk = tuple(sorted([ka, kb])) + (r['severity'], r['description'])
            if nk in dedup_keys:
                continue
            dedup_keys.add(nk)
            out.append(r)
        out.sort(key=lambda x: cls._SEVERITY_ORDER.get(str(x.get('severity') or ''), 0), reverse=True)
        return out

    @classmethod
    def build_payload(cls, normalized_medicines: List[str]) -> Dict[str, Any]:
        """
        Patient/API-safe block for chat and ranked payloads.
        Expects deduplicated medicine labels (caller may use views._normalize_medicine_names_list).
        """
        names = list(normalized_medicines or [])
        if len(names) < 2:
            return {
                'medicines_checked': names,
                'interactions': [],
                'has_interactions': False,
                'highest_severity': None,
                'disclaimer': cls.DISCLAIMER,
                'source': 'embedded_rules_v1',
            }
        ix = cls.check_interactions(names)
        hi = (ix[0].get('severity') if ix else None)
        return {
            'medicines_checked': names,
            'interactions': ix,
            'has_interactions': len(ix) > 0,
            'highest_severity': hi,
            'interaction_count': len(ix),
            'disclaimer': cls.DISCLAIMER,
            'source': 'embedded_rules_v1',
        }
