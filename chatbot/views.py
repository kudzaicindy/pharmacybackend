from rest_framework.decorators import api_view, permission_classes, authentication_classes
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone
from django.db.models import F, Count, Q, Max, OuterRef, Subquery
from django.db.models.functions import TruncDate
from django.http import HttpResponse, FileResponse
from collections import Counter
import mimetypes
from urllib.parse import quote
import difflib
from django.core.cache import cache
import csv
from django.shortcuts import get_object_or_404
from django.db import transaction
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.middleware.csrf import get_token
from datetime import timedelta, datetime
from decimal import Decimal
import os
import uuid

from .models import (
    ChatConversation, ChatMessage, MedicineRequest, MedicineRequestRankingSnapshot,
    PharmacyResponse, Pharmacy, Pharmacist,
    PharmacistDecline, PharmacyInventory, PharmacyRating, Reservation,
    PatientProfile, SavedMedicine, PatientNotification, AdminAuditLog,
    PlatformAdminSettings, ChatbotSafetyReview, PharmacySettings, PharmacySettingsHistory,
)
from .serializers import (
    ChatRequestSerializer, ChatResponseSerializer,
    ChatConversationSerializer, MedicineRequestSerializer,
    PharmacyResponseSerializer, PharmacistSerializer, PharmacistLoginSerializer,
    AdminLoginSerializer,
    EmailOtpVerifySerializer,
    PasswordResetRequestSerializer,
    PasswordResetConfirmSerializer,
    MfaLoginCompleteSerializer,
    AdminReportGenerateSerializer,
    PharmacySettingsHistorySerializer,
    PharmacyRegistrationSerializer, PharmacistRegistrationSerializer,
    PharmacySerializer
)
from .pharmacist_portal_settings import (
    serialize_pharmacist_settings_envelope,
    patch_pharmacist_settings_envelope,
)
from .pharmacist_portal_auth import pharmacist_jwt_tokens_or_empty, resolve_authenticated_pharmacist
from .services import (
    LocationService,
    OCRService,
    RankingEngine,
    DrugInteractionService,
    normalize_mcda_weights,
    resolve_platform_mcda_weights,
)
from .admin_analytics import (
    compute_nav_badges,
    compute_daily_active_sessions,
    compute_avg_first_response_ms,
    build_geo_heatmap,
    build_sla_by_region,
    build_verification_queue,
    build_watchlist,
    build_impact_equity,
    build_medi_bot_overview,
    build_admin_widgets_bundle,
    merge_chatbot_policy,
    get_platform_settings,
    DEFAULT_CHATBOT_POLICY,
    RANKING_PROFILE_PRESETS,
    weights_percent_display,
    search_volume_region_key_label,
    compute_effective_pharmacy_response_rates_for_ids,
)
from django.core.files.storage import default_storage
from django.conf import settings
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

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


def _truthy_multipart(val) -> bool:
    """Form bodies send booleans as strings; JSON may send native bool."""
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    return s in {'1', 'true', 'yes', 'on'}


def _archive_medicine_request_before_new_search(existing_request, *, recent: bool) -> None:
    """
    Patient started a new broadcast while an older open request still exists.
    Do not mark unreplied requests as 'completed' (that hides them from the patient's active list).
    """
    rc = existing_request.pharmacy_responses.count()
    if rc == 0:
        existing_request.status = 'superseded' if recent else 'expired'
    else:
        existing_request.status = 'completed'
    existing_request.save(update_fields=['status'])


_MEDICINE_NAME_GARBAGE = frozenset({
    'uploaded', 'prescription', 'image', 'jpeg', 'jpg', 'png', 'gif', 'webp',
    'file', 'whatsapp', 'photo', 'camera', 'screenshot', 'document', 'pdf', 'doc',
    'docx', 'png.', 'jpg.',
})


def _normalize_medicine_names_list(names) -> list:
    """
    Clean patient-request medicine name lists: drop OCR/UI tokens, dedupe,
    remove shorter tokens subsumed by a longer one (e.g. 'ors' when 'ors sachet' exists).
    """
    import re

    if not names:
        return []
    cleaned = []
    for n in names:
        if n is None:
            continue
        s = str(n).strip()
        if len(s) < 2:
            continue
        sl = s.lower()
        if sl in _MEDICINE_NAME_GARBAGE:
            continue
        if re.fullmatch(r'\d+(?:\.\d+)?', sl):
            continue
        cleaned.append(s)
    seen = set()
    deduped = []
    for s in cleaned:
        sl = s.lower()
        if sl in seen:
            continue
        seen.add(sl)
        deduped.append(s)
    lowers = [x.lower() for x in deduped]
    out = []
    for i, s in enumerate(deduped):
        sl = lowers[i]
        if any(
            j != i and len(lowers[j]) > len(sl) and lowers[j].startswith(sl + ' ')
            for j in range(len(lowers))
        ):
            continue
        out.append(s)
    return out


def _recover_prescription_medicines_from_assistant_messages(conversation, *, min_items: int = 2) -> list:
    """
    Recover a multi-item OCR list from a prior assistant message when metadata was
    overwritten by a follow-up turn (e.g. drug-interaction subset: ORS only).
    """
    best: list = []
    qs = ChatMessage.objects.filter(conversation=conversation, role='assistant').order_by('-created_at')[:18]
    for msg in qs:
        md = msg.metadata if isinstance(msg.metadata, dict) else {}
        sm = md.get('suggested_medicines') or []
        if not isinstance(sm, list) or len(sm) < min_items:
            continue
        it = (md.get('intent') or '').strip()
        content_l = (msg.content or '').lower()
        looks_like_rx_parse = (
            it == 'prescription_upload'
            or 'medicines found' in content_l
            or 'processed your prescription' in content_l
        )
        if not looks_like_rx_parse:
            continue
        cleaned = [str(x).strip() for x in sm if str(x).strip()]
        cleaned = _normalize_medicine_names_list(cleaned)
        if len(cleaned) > len(best):
            best = cleaned
    return best


def _repair_prescription_medicines_in_context(conversation, data: dict) -> None:
    """If prescription_medicines was shrunk by a later turn, restore from assistant history or client echo."""
    meta = conversation.context_metadata or {}
    cur = _normalize_medicine_names_list(list(meta.get('prescription_medicines') or []))
    recovered = _recover_prescription_medicines_from_assistant_messages(conversation)
    echo = data.get('suggested_medicines') or []
    echo_list = _normalize_medicine_names_list(list(echo)) if isinstance(echo, list) else []
    client_rx = _normalize_medicine_names_list(list(data.get('medicines') or []))
    candidates = [cur, recovered, echo_list, client_rx]
    best = max(candidates, key=lambda x: len(x) if x else 0)
    if best and len(best) > len(cur):
        meta = dict(meta)
        meta['prescription_medicines'] = list(best)
        conversation.context_metadata = meta
        conversation.save(update_fields=['context_metadata'])
        print(f'[INFO] Repaired prescription_medicines in context (len {len(cur)} → {len(best)}): {best}')


def _medicine_union_for_chat_drug_interactions(
    conversation,
    data: dict,
    medicines_from_flow: list,
    ai_result: dict,
    medicine_request_id,
) -> list:
    """
    Combine every medicine-bearing source so /chat exposes one DDI check per turn
    (OCR Rx, client echo, AI subset, broadcast request, selections).
    """
    raw = []
    raw.extend(_normalize_medicine_names_list(list(data.get('medicines') or [])))
    meta = conversation.context_metadata or {}
    raw.extend(_normalize_medicine_names_list(list(meta.get('prescription_medicines') or [])))
    raw.extend(_normalize_medicine_names_list(list(ai_result.get('suggested_medicines') or [])))
    raw.extend(_normalize_medicine_names_list(list(medicines_from_flow or [])))
    raw.extend(_recover_prescription_medicines_from_assistant_messages(conversation, min_items=2))
    sel = meta.get('selected_medicines')
    if isinstance(sel, list):
        raw.extend(_normalize_medicine_names_list(sel))
    if medicine_request_id:
        try:
            mr = MedicineRequest.objects.get(request_id=str(medicine_request_id))
            raw.extend(list(mr.medicine_names or []))
        except MedicineRequest.DoesNotExist:
            pass
    return _normalize_medicine_names_list(raw)


def _drug_interactions_block(names: list) -> dict:
    return DrugInteractionService.build_payload(_normalize_medicine_names_list(names or []))


def _prepend_drug_interaction_notice(response_text: str | None, ddi_block: dict) -> str | None:
    """Short preamble so patients see warnings even without reading JSON."""
    if not response_text or not isinstance(response_text, str):
        return response_text
    if not ddi_block.get('has_interactions'):
        return response_text
    ix = ddi_block.get('interactions') or []
    n = int(ddi_block.get('interaction_count') or len(ix))
    sev = ddi_block.get('highest_severity') or 'potential'
    line = (
        f"⚠️ **Drug interaction notice:** {n} potential pair(s) flagged ({sev} highest). "
        'See **drug_interactions** below — always confirm with your doctor or pharmacist.\n\n'
    )
    return line + response_text


def _attach_drug_interactions_to_chat_payload(
    conversation,
    data: dict,
    medicines_from_flow: list,
    ai_result: dict,
    medicine_request_id,
    response_data: dict,
) -> None:
    if not isinstance(response_data, dict):
        return
    union = _medicine_union_for_chat_drug_interactions(
        conversation, data, medicines_from_flow, ai_result, medicine_request_id,
    )
    ddi_block = _drug_interactions_block(union)
    response_data['drug_interactions'] = ddi_block
    if response_data.get('response'):
        response_data['response'] = _prepend_drug_interaction_notice(
            response_data.get('response'),
            ddi_block,
        )


def _ddi_for_medicine_request_model(medicine_request) -> dict:
    return _drug_interactions_block(list(medicine_request.medicine_names or []))


def _resolve_pharmacy_inventory_sku(pharmacy, medicine_name: str) -> str | None:
    """
    Map free-text medicine_name to this pharmacy's ``PharmacyInventory.medicine_name`` string.

    Order: case-insensitive exact match → single substring containment match → fuzzy match when
    unambiguous (avoids reserving the wrong drug when two SKU names tie).
    Aligns reservation / purchase lookups with fuzzy inventory behaviour used when ranking quotes.
    """
    raw = (medicine_name or '').strip()
    if not raw:
        return None
    names = list(
        PharmacyInventory.objects.filter(pharmacy=pharmacy).values_list('medicine_name', flat=True)
    )
    if not names:
        return None

    canon_by_lower: dict[str, str] = {}
    for nm in names:
        if not nm:
            continue
        k = nm.lower()
        canon_by_lower.setdefault(k, nm)

    rl = raw.lower()
    if rl in canon_by_lower:
        return canon_by_lower[rl]

    contain = [canon_by_lower[k] for k in canon_by_lower if rl in k or k in rl]
    if len(contain) == 1:
        return contain[0]

    uniq_lowers = sorted(canon_by_lower.keys())
    if len(raw) >= 4 and uniq_lowers:
        close = difflib.get_close_matches(rl, uniq_lowers, n=3, cutoff=0.86)
        if len(close) == 1:
            return canon_by_lower[close[0]]
        if len(close) >= 2:
            r0 = difflib.SequenceMatcher(a=rl, b=close[0]).ratio()
            r1 = difflib.SequenceMatcher(a=rl, b=close[1]).ratio()
            if r0 >= 0.92 and (r0 - r1) >= 0.04:
                return canon_by_lower[close[0]]

    return None


def _pharmacist_row_for_requested_med(req_lower: str, rows: list) -> dict | None:
    """Match a requested medicine to one pharmacist medicine_responses row (exact then fuzzy)."""
    if not req_lower or not rows:
        return None
    for mr in rows:
        if not isinstance(mr, dict):
            continue
        m = str(mr.get('medicine', '')).strip().lower()
        if m == req_lower:
            return mr
    best = None
    best_key = 0
    for mr in rows:
        if not isinstance(mr, dict):
            continue
        m = str(mr.get('medicine', '')).strip().lower()
        if not m:
            continue
        if req_lower in m or m in req_lower:
            key = min(len(m), len(req_lower))
            if key > best_key:
                best_key = key
                best = mr
    return best


def _medicine_request_preview_fields(medicine_request: MedicineRequest) -> dict:
    """
    Human-readable preview + flags for chat API and pharmacist lists.
    ``request_type`` on the model is ``symptom`` | ``direct`` | ``prescription``.
    """
    is_sym = medicine_request.request_type == 'symptom'
    meds = [str(m) for m in (medicine_request.medicine_names or []) if m]
    sy = (medicine_request.symptoms or '').strip()
    has_rx_img = bool(getattr(medicine_request, 'prescription_image', None))
    rx_type = getattr(medicine_request, 'request_type', '')
    parts: list[str] = []
    if sy:
        parts.append(f"Symptoms: {sy}")
    if meds:
        parts.append(f"Medicines: {', '.join(meds[:12])}")
    if not parts:
        if rx_type == 'prescription':
            preview = (
                'Prescription image — medicines not extracted; confirm from uploaded image'
                if has_rx_img
                else 'Prescription-related request · pharmacist confirms medicines'
            )
        elif is_sym:
            preview = 'Symptom-based request'
        else:
            preview = 'Medicine request'
    else:
        preview = ' · '.join(parts)
    return {
        'request_type': medicine_request.request_type,
        'is_symptom_request': is_sym,
        'symptoms': sy,
        'medicine_names': meds,
        'request_preview': preview,
        'needs_pharmacist_prescription_read': rx_type == 'prescription'
        and has_rx_img
        and len(meds) == 0,
    }


def _truncate_str(s: str, max_len: int) -> str:
    s = str(s or '')
    if len(s) <= max_len:
        return s
    return s[:max_len] + '\n...[truncated]'


def _snapshot_for_pharmacies_from_ocr(ocr_result: dict) -> dict:
    """Structured OCR payload for pharmacist verification (attached to MedicineRequest)."""
    return {
        'source': 'upload_prescription',
        'confidence': ocr_result.get('confidence'),
        'confidence_percent': int(ocr_result.get('confidence_percent') or 0),
        'reading_notes': str(ocr_result.get('reading_notes') or ''),
        'items': ocr_result.get('items') or [],
        'dosages': ocr_result.get('dosages') or {},
        'summary_markdown': str(ocr_result.get('summary_markdown') or ''),
        'raw_text_excerpt': _truncate_str(ocr_result.get('raw_text') or '', 8000),
    }


def _snapshot_for_pharmacists_prescription_fallback(ocr_result: dict | None, *, upload_filename: str = '') -> dict:
    """
    When OCR fails or returns no medicines, still attach context so pharmacists open the saved image first.
    """
    ocr_result = dict(ocr_result or {})
    snap: dict = {
        'source': 'prescription_image_pharmacist_review',
        'patient_message_for_pharmacist': (
            'The patient uploaded a prescription image but automatic reading did not produce a '
            'reliable medicine list. Open the prescription image and respond with stock, prices, '
            'and the medicines you can supply — use structured quote fields where your portal supports them.'
        ),
        'medicines_extracted_automatically': False,
        'confidence_percent': int(ocr_result.get('confidence_percent') or 0),
        'reading_notes': str(ocr_result.get('reading_notes') or '')[:4000],
    }
    err = ocr_result.get('error')
    if err:
        snap['ocr_error_summary'] = _truncate_str(str(err), 2000)
    if ocr_result.get('items'):
        snap['partial_items_preview'] = ocr_result['items'][:40]
    if upload_filename:
        snap['upload_filename'] = upload_filename[:255]
    return snap


def _snapshot_pharmacist_skip_ocr_upload(*, upload_filename: str = '') -> dict:
    snap = _snapshot_for_pharmacists_prescription_fallback(None, upload_filename=upload_filename)
    snap['source'] = 'upload_prescription_skip_ocr'
    snap['skipped_gemini_api'] = True
    return snap


def _prescription_review_from_conversation(conversation: ChatConversation) -> dict:
    """Build pharmacist snapshot from chat session metadata (no image unless upload stored it)."""
    meta = conversation.context_metadata or {}
    items = meta.get('prescription_items')
    meds = list(meta.get('prescription_medicines') or [])
    cp = int(meta.get('prescription_confidence_percent') or 0)
    notes = str(meta.get('prescription_reading_notes') or '')
    err_saved = str(meta.get('last_prescription_ocr_error') or '').strip()
    pharmacist_pending = bool(meta.get('prescription_pharmacist_read_pending'))
    skip_upload = bool(meta.get('prescription_skip_ocr_upload'))

    def _merged_base(out: dict) -> dict:
        if err_saved and not out.get('ocr_error_summary'):
            out['ocr_error_summary'] = err_saved[:2000]
        if pharmacist_pending or skip_upload:
            out['needs_pharmacist_review'] = True
        return out

    if isinstance(items, list) and items:
        return _merged_base({
            'source': 'conversation',
            'confidence_percent': cp,
            'reading_notes': notes,
            'items': items,
        })
    if meds:
        return _merged_base({
            'source': 'conversation',
            'confidence_percent': cp,
            'reading_notes': notes,
            'items': [
                {
                    'name': str(m),
                    'strength': '',
                    'dose': '',
                    'frequency': '',
                    'duration': '',
                    'instructions': '',
                }
                for m in meds
            ],
        })
    if pharmacist_pending or skip_upload or err_saved:
        return _merged_base({
            'source': 'conversation',
            'confidence_percent': cp,
            'reading_notes': notes,
            'items': [],
        })
    return {}


def _pharmacist_prescription_image_absolute_url(request, mr: MedicineRequest, pharmacist_id: str) -> str | None:
    if not mr.prescription_image or not str(pharmacist_id or '').strip():
        return None
    rid = mr.request_id
    qid = quote(str(pharmacist_id).strip(), safe='')
    return request.build_absolute_uri(
        f'/api/chatbot/pharmacist/requests/{rid}/prescription-image/?pharmacist_id={qid}'
    )


def _enrich_chat_response_with_request_preview(response_data: dict) -> None:
    """Mutates chat JSON when a medicine request id is present."""
    rid = response_data.get('medicine_request_id')
    if not rid:
        return
    try:
        mr = MedicineRequest.objects.get(request_id=rid)
    except (MedicineRequest.DoesNotExist, ValueError, TypeError):
        return
    pv = _medicine_request_preview_fields(mr)
    response_data.update(pv)
    if pv.get('is_symptom_request'):
        response_data['intent'] = 'symptom_description'


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
    prescription_image_only_chat = bool(data.get('prescription_image_only'))
    ocr_failed_chat = bool(data.get('ocr_failed'))
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
    # Client often sends the full OCR/interaction list as `medicines` while the model returns a subset (e.g. ORS only).
    client_rx = _normalize_medicine_names_list(list(data.get('medicines') or []))
    if len(client_rx) >= 2:
        ai_sugg = ai_result.get('suggested_medicines') or []
        if len(client_rx) >= len(ai_sugg):
            ai_result['suggested_medicines'] = list(client_rx)
            print(f'[INFO] Applied client medicines list to ai_result suggested_medicines ({len(client_rx)} items)')

    # Manual / typed location: body often carries only coordinates + address as `message`; the LLM may return
    # general_inquiry with no medicines, which would wipe the prior medicine-search turn unless we preserve
    # client echoes and context_metadata. "Use my location" flows usually re-send suggested_medicines.
    lat_in = data.get('location_latitude')
    lon_in = data.get('location_longitude')
    has_location_coords = lat_in is not None and lon_in is not None
    has_location_address_body = bool((data.get('location_address') or '').strip())
    if has_location_coords or has_location_address_body:
        prev_meta_here = conversation.context_metadata or {}
        prev_suggested = prev_meta_here.get('suggested_medicines') or []
        prev_selected = prev_meta_here.get('selected_medicines') or []
        client_echo_med: list[str] = []
        for key in ('suggested_medicines', 'medicines', 'selected_medicines'):
            lm = data.get(key)
            if lm:
                client_echo_med.extend(list(lm))
        client_echo_med = _normalize_medicine_names_list(client_echo_med)
        ai_sugg_now = list(ai_result.get('suggested_medicines') or [])
        if client_echo_med:
            ai_result['suggested_medicines'] = list(dict.fromkeys(client_echo_med))
            print(
                "[INFO] Location submit: merged client echoed medicines into ai_result:",
                ai_result['suggested_medicines'],
            )
        elif not ai_sugg_now:
            fallback = prev_suggested if prev_suggested else prev_selected
            if fallback:
                ai_result['suggested_medicines'] = list(fallback)
                print(f"[INFO] Location submit: preserved suggested_medicines from prior turn: {fallback}")
        cur_intent = (ai_result.get('intent') or '').strip()
        if (ai_result.get('suggested_medicines')) and cur_intent not in (
            'medicine_search',
            'symptom_description',
            'medicine_selection',
            'location_provided',
        ):
            lip = (prev_meta_here.get('last_intent') or '').strip()
            if lip in ('medicine_search', 'medicine_selection', 'symptom_description'):
                ai_result['intent'] = lip
                print(f"[INFO] Location submit: restored intent from prior turn ({lip}) so broadcast gates run.")
            elif ai_result.get('suggested_medicines'):
                ai_result['intent'] = 'medicine_search'

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

    # Lock medicines from in-chat prescription OCR (not only /upload-prescription/), so a later
    # "yes" + location does not fall back to old symptom threads in the same conversation.
    suggested_rx = ai_result.get('suggested_medicines') or []
    resp_low = (ai_result.get('response') or '').lower()
    existing_rx = list(conversation.context_metadata.get('prescription_medicines') or [])
    strong_rx_turn = (
        ai_result.get('intent') == 'prescription_upload'
        or 'processed your prescription' in resp_low
        or 'medicines found' in resp_low
    )
    weak_rx_language = 'prescription' in resp_low and 'medicine' in resp_low
    if suggested_rx and (strong_rx_turn or weak_rx_language):
        # Never shrink a multi-item OCR list on a weak follow-up ("your prescription" + interaction subset).
        write_rx = False
        if strong_rx_turn:
            write_rx = True
        elif not existing_rx:
            write_rx = True
        elif len(suggested_rx) >= len(existing_rx):
            write_rx = True
        if write_rx:
            conversation.context_metadata['prescription_medicines'] = list(suggested_rx)
            conversation.save(update_fields=['context_metadata'])
            print(f'[INFO] Stored prescription_medicines from chat/OCR assistant turn: {suggested_rx}')

    # Restore full OCR list if metadata was previously overwritten (e.g. ORS-only interaction turn)
    _repair_prescription_medicines_in_context(conversation, data)
    
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
                        'vomiting', 'vomit', 'throwing up',
                        # Oral / dental (e.g. "bleeding gums") — must match for_has_symptom and pharmacist-facing symptoms field
                        'bleeding', 'gums', 'gum', 'toothache', 'tooth', 'teeth', 'dental', 'mouth ulcer', 'ulcer',
                        # GI / general (e.g. "burning sensation in tummy")
                        'tummy', 'burning', 'sensation', 'cramp', 'cramps', 'indigestion', 'heartburn', 'reflux',
                        'nauseous', 'bloated', 'bloating']
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

    if conversation.context_metadata.get('prescription_medicines') and is_confirmation:
        has_previous_symptom = False
    
    # User is checking for responses (no new search) - e.g. "any updates?", "got any?"
    # IMPORTANT: Only treat explicit follow-up phrases as "check for existing responses".
    # Bare confirmations like "yes", "ok" should NOT trigger fetching old responses.
    follow_up_check_phrases = ['any updates', 'any news', 'got any', 'any response', 'any responses', 'check', 'waiting']
    is_follow_up_check = any(p in message_lower for p in follow_up_check_phrases)
    
    # Also check AI response text for symptom mentions (e.g., "runny nose")
    ai_response_lower = ai_response_text.lower()
    ai_mentions_symptom = any(keyword in ai_response_lower for keyword in symptom_keywords)
    if conversation.context_metadata.get('prescription_medicines') and is_confirmation:
        ai_mentions_symptom = False
    
    # Extract location coordinates from request data OR from message text OR from AI response
    location_lat = data.get('location_latitude')
    location_lon = data.get('location_longitude')
    
    import re

    def _message_is_location_coordinates(text):
        if not text or not isinstance(text, str):
            return False
        tl = text.lower()
        if 'location' in tl and re.search(r'-?\d+\.?\d*\s*[,:]\s*-?\d+\.?\d*', text):
            return True
        return bool(re.fullmatch(r'\s*-?\d+\.?\d*\s*,\s*-?\d+\.?\d*\s*', text.strip()))
    
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
        loc_suburb = (data.get('location_suburb') or '').strip() if isinstance(data.get('location_suburb'), str) else ''
        address_to_geocode = location_address.strip() if isinstance(location_address, str) else ''
        if not address_to_geocode and loc_suburb:
            address_to_geocode = loc_suburb
        # Used again if user confirms ("yes") and we look up a prior message
        address_patterns = [
            r'\d+\s+[A-Za-z0-9\s,\']+(?:street|st|road|rd|avenue|ave|crescent|cres|drive|dr|way|lane|ln)\b',
            r'\d+\s+st\.?\s+[a-z0-9]',
            r'(?:Glen View|Avondale|Belvedere|Mbare|Highfield|Epworth|Hatfield|Waterfalls|Borrowdale|'
            r'Mount Pleasant|Mt\.?\s*Pleasant|Greendale|St\s+Kilda|Kopje|Marlborough|Alexandra Park)\b',
            r'Harare|Bulawayo|Gweru|Mutare|Kwekwe|Chitungwiza',
        ]

        # If no address in request body, check if message looks like an address
        # (contains numbers + street names, suburbs, etc.)
        if not address_to_geocode:
            # Check if message contains address-like patterns (numbers + street names)
            is_address_like = any(re.search(pattern, message, re.IGNORECASE) for pattern in address_patterns)

            # After the bot asked for location, treat the next substantive user line as an address
            # (regex misses many real addresses like "4 st kilda mt pleasant").
            if not is_address_like and previous_requires_location and not is_confirmation:
                msg_strip = (message or '').strip()
                low = msg_strip.lower()
                looks_clinical = any(kw in low for kw in symptom_keywords)
                too_short = len(msg_strip) < 4
                looks_coords = bool(re.search(r'(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)', msg_strip))
                if (
                    not too_short
                    and not looks_clinical
                    and not looks_coords
                    and not _message_is_location_coordinates(msg_strip)
                ):
                    is_address_like = True
                    print(f"[INFO] Treating message as address (previous_requires_location): {msg_strip[:120]!r}")

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
    prescription_broadcast = False  # True when broadcasting locked prescription list (not AI subset)
    
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
        (any(kw in message_lower for kw in ['have', 'feeling', 'hurts', 'pain', 'ache', 'sensation', 'burn']) and 
         not any(kw in message_lower for kw in ['medicine', 'prescription', 'looking for']))
    )
    if prescription_medicines and (
        is_confirmation
        or previous_requires_location
        or (location_lat and location_lon)
    ):
        is_symptom_based_query = False
    if prescription_medicines and not is_symptom_based_query:
        medicine_messages_for_matching.extend(prescription_medicines)
        print(f"[INFO] Found prescription medicines in conversation metadata: {prescription_medicines}")
    elif prescription_medicines and is_symptom_based_query:
        print(f"[INFO] Ignoring prescription medicines from metadata - this is a symptom-based query, not prescription-based")
    
    for msg in all_user_messages:
        msg_lower = msg.content.lower()
        # Check for symptoms (skip location-only lines — they are not clinical symptoms)
        if any(kw in msg_lower for kw in symptom_keywords) and not _message_is_location_coordinates(msg.content):
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
    if symptom_messages_for_matching:
        current_query_symptoms = symptom_messages_for_matching[0]
    elif _message_is_location_coordinates(message):
        current_query_symptoms = ''
    else:
        current_query_symptoms = message
    
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
    
    # Locked prescription list must win over this turn's AI suggested_medicines (e.g. interaction API → ORS only)
    filtered_prescription_medicines = _filter_valid_medicines(list(prescription_medicines or []))
    _prescription_send = bool(filtered_prescription_medicines) and bool(location_lat and location_lon) and (
        is_confirmation
        or previous_requires_location
        or intent == 'location_provided'
        or intent == 'prescription_upload'
        or _message_is_location_coordinates(message)
    )
    if _prescription_send:
        medicines = list(filtered_prescription_medicines)
        current_query_medicines = list(filtered_prescription_medicines)
        prescription_broadcast = True
        print(f'[INFO] Using locked prescription_medicines for broadcast ({len(medicines)} items): {medicines}')
    
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
        elif getattr(existing_request, 'request_type', None) == 'prescription':
            # Prescription never matched symptom/direct logic, so matching stayed False → the branch
            # below archived every active Rx and created duplicate MedicineRequests per follow-up chat.
            _mr_meta = conversation.context_metadata or {}
            ex_names = {(str(x) or '').lower() for x in (existing_request.medicine_names or [])}
            cur_rx = {(str(x) or '').lower() for x in (filtered_prescription_medicines or [])}
            if (
                prescription_broadcast
                or prescription_image_only_chat
                or intent == 'prescription_upload'
                or _mr_meta.get('prescription_pharmacist_read_pending')
                or _mr_meta.get('prescription_skip_ocr_upload')
                or _mr_meta.get('prescription_review_only_upload')
            ):
                existing_request_matches = True
            elif cur_rx and (ex_names == cur_rx or not ex_names):
                # Same OCR list again, or follow-up naming the same Rx after an image-first broadcast with no names
                existing_request_matches = True
    
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
                # Existing request doesn't match OR is too old — archive so patient can still see it in "active" if superseded
                if not is_recent_request:
                    print(f"[INFO] Existing request {existing_request.request_id} is too old (created {time_since_creation}), creating new request")
                _archive_medicine_request_before_new_search(existing_request, recent=is_recent_request)
                print(
                    f"[INFO] Marked existing request {existing_request.request_id} as "
                    f"{existing_request.status} (different query or too old)"
                )
                existing_request = None  # Treat as if no existing request

        # Frontend: POST /chat/ with prescription_image_only + ocr_failed (or pending pharmacist read)
        # after OCR failure — broadcast prescription-type request without a medicine_name list (image first).
        _meta_quick = conversation.context_metadata or {}
        if (
            prescription_image_only_chat
            and location_lat
            and location_lon
            and (
                ocr_failed_chat
                or _meta_quick.get('prescription_pharmacist_read_pending')
                or _meta_quick.get('prescription_skip_ocr_upload')
            )
        ):
            should_create_request = True
            prescription_broadcast = True
            medicines = []
            current_query_medicines = []
            print('[INFO] Chat prescription_image_only + location: forcing prescription broadcast without OCR medicines')

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
                # is_symptom_based_query includes "I have …" style complaints without medicine names; use it so we
                # don't default to medicine_search (which clears symptoms) when AI intent is generic.
                _medicines_resolved = medicines or selected_medicines
                _treat_as_symptom_intent = (
                    has_symptom
                    or has_previous_symptom
                    or ai_mentions_symptom
                    or selected_medicines
                    or (
                        is_symptom_based_query
                        and not _medicines_resolved
                    )
                )
                request_intent = (
                    intent
                    if intent in ['medicine_search', 'symptom_description']
                    else ('symptom_description' if _treat_as_symptom_intent else 'medicine_search')
                )
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

            if prescription_broadcast:
                request_intent = 'prescription_upload'
                symptoms_text = ''
            
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
            
            # Always create and broadcast the medicine request so pharmacists see it in their dashboard
            print(f"[INFO] Creating medicine request: intent={request_intent}, medicines={medicines_to_use}, symptoms={symptoms_text[:50] if symptoms_text else 'None'}")
            notify_patient_mail = bool(data.get('notify_patient_request_email', True))
            extra_mail = (data.get('patient_request_email') or '').strip()
            extra_patient_emails = [extra_mail] if extra_mail and '@' in extra_mail else None
            rx_review_snapshot = {}
            if prescription_broadcast:
                rx_review_snapshot = _prescription_review_from_conversation(conversation)
            medicine_request = create_medicine_request(
                conversation=conversation,
                user=request.user if request.user.is_authenticated else None,
                intent=request_intent,
                medicines=medicines_to_use,
                symptoms=symptoms_text,
                latitude=location_lat,
                longitude=location_lon,
                address=data.get('location_address', ''),
                suburb=data.get('location_suburb', ''),
                email_patient=notify_patient_mail,
                extra_patient_emails=extra_patient_emails,
                prescription_review_snapshot=rx_review_snapshot,
            )
            medicine_request_id = medicine_request.request_id
            is_new_request = True
            print(f"[INFO] Medicine request created: {medicine_request_id} (intent: {request_intent}, status: {medicine_request.status})")

            # Also query live inventory: if pharmacies have stock in DB, show those immediately (patient can reserve)
            live_results = []
            if medicines_to_use and location_lat and location_lon:
                live_results = get_live_inventory_ranked(location_lat, location_lon, medicines_to_use, limit=10)
                if not live_results:
                    print(f"[INFO] Live inventory: 0 results for {medicines_to_use} at ({location_lat},{location_lon})")
            if live_results:
                pharmacy_responses = list(live_results)
                # For a brand new request, show LIVE stock first on its own.
                # Pharmacist responses (if any) will be surfaced on follow-up/poll
                # as \"new\" responses instead of being merged into this first view.
                if not is_new_request and existing_request and existing_request.pharmacy_responses.exists():
                    pharmacist_responses = get_ranked_pharmacy_responses(existing_request, limit=10)
                    live_pharmacy_ids = {r.get('pharmacy_id') for r in pharmacy_responses if r.get('pharmacy_id')}
                    for pr in pharmacist_responses:
                        pid = pr.get('pharmacy_id')
                        if pid and pid not in live_pharmacy_ids:
                            pr['from_pharmacist_response'] = True
                            pr['from_live_inventory'] = False
                            pharmacy_responses.append(pr)
                            live_pharmacy_ids.add(pid)
                print(f"[INFO] Live inventory: {len(live_results)} pharmacies with stock; request {medicine_request_id} also sent to pharmacists")
            else:
                pharmacy_responses = None
                print(f"[INFO] Request {medicine_request_id} sent to pharmacists - waiting for responses")
            _ws_broadcast_medicine_request_chat_snapshot(
                medicine_request_id,
                list(pharmacy_responses or []),
                conversation_id=str(conversation.conversation_id),
                pharmacy_responses_phase=(
                    'live_inventory' if pharmacy_responses else 'awaiting_quotes'
                ),
            )
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
    new_arrivals_only = False  # True when showing only responses that arrived after the last message
    if existing_request and not is_new_request and pharmacy_responses:
        # Check recent messages to see if we already sent pharmacy responses
        recent_ai_messages = ChatMessage.objects.filter(
            conversation=conversation,
            role='assistant'
        ).order_by('-created_at')[:5]
        
        for msg in recent_ai_messages:
            if msg.metadata and isinstance(msg.metadata, dict) and msg.metadata.get('pharmacy_responses_shown'):
                last_response_time = msg.created_at
                new_responses_count = existing_request.pharmacy_responses.filter(
                    submitted_at__gt=last_response_time
                ).count()
                
                if new_responses_count == 0:
                    already_shown_responses = True
                    pharmacy_responses = None
                    print(f"[INFO] Responses already shown for request {existing_request.request_id}, no new responses")
                else:
                    # Filter to only responses submitted after we last showed results
                    def _submitted_after(r, cutoff):
                        t = r.get('submitted_at')
                        if not t:
                            return False
                        if hasattr(t, 'timestamp'):
                            if timezone.is_naive(t) and timezone.is_aware(cutoff):
                                t = timezone.make_aware(t, timezone.utc)
                            return t > cutoff
                        try:
                            parsed = datetime.fromisoformat(str(t).replace('Z', '+00:00'))
                            if timezone.is_naive(parsed) and timezone.is_aware(cutoff):
                                parsed = timezone.make_aware(parsed, timezone.utc)
                            return parsed > cutoff
                        except Exception:
                            return False
                    pharmacy_responses = [r for r in pharmacy_responses if _submitted_after(r, last_response_time)]
                    if pharmacy_responses:
                        new_arrivals_only = True
                        print(f"[INFO] Showing {len(pharmacy_responses)} new pharmacy response(s) (arrived after last message)")
                break
    
    # If we have pharmacy responses and haven't shown them yet, return them instead of AI response
    if pharmacy_responses and not already_shown_responses:
        best_pharmacy = pharmacy_responses[0] if pharmacy_responses else None
        recommendation = None
        mr_for_chat = None
        preview_prefix = ''
        if medicine_request_id:
            try:
                mr_for_chat = MedicineRequest.objects.get(request_id=medicine_request_id)
                pv0 = _medicine_request_preview_fields(mr_for_chat)
                if pv0.get('is_symptom_request') and pv0.get('symptoms'):
                    preview_prefix = f"📋 **Symptoms:** {pv0['symptoms'][:280]}\n\n"
            except (MedicineRequest.DoesNotExist, ValueError, TypeError):
                pass

        if best_pharmacy and not new_arrivals_only:
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
        
        from_live = bool(pharmacy_responses and pharmacy_responses[0].get('from_live_inventory') and not new_arrivals_only)
        # Always include medicine_request_id so frontend can consistently
        # track and update the specific request, even when results are from
        # live inventory.
        req_id_for_short = medicine_request_id
        short_req_id = str(req_id_for_short).replace('-', '')[:8].upper() if req_id_for_short else None
        
        # Message text: different when showing new arrivals so frontend can display "Pharmacy X responded they have..."
        if new_arrivals_only:
            parts = []
            for r in pharmacy_responses:
                name = r.get('pharmacy_name') or r.get('pharmacy_id') or 'A pharmacy'
                items = []
                for mr in (r.get('medicine_responses') or []):
                    if isinstance(mr, dict) and mr.get('available'):
                        m = mr.get('medicine', '')
                        p = mr.get('price')
                        items.append(f"{m}" + (f" (${p})" if p else ""))
                if not items and r.get('notes'):
                    items = [r.get('notes')]
                if not items:
                    items = ["stock available"] if r.get('medicine_available') else ["see details below"]
                parts.append(f"**{name}** responded they have: {', '.join(items)}.")
            response_text = preview_prefix + "✅ New response(s): " + " ".join(parts)
        else:
            response_text = preview_prefix + (
                f"✅ I found {len(pharmacy_responses)} {'pharmacies' if len(pharmacy_responses) != 1 else 'pharmacy'} with live stock. Here are the top ranked options (distance, price, availability, rating):"
                if from_live
                else f"✅ Your request has been sent to nearby pharmacies! I found {len(pharmacy_responses)} top {'pharmacies' if len(pharmacy_responses) != 1 else 'pharmacy'} with available options. Here are the top ranked responses:"
            )
        
        response_data = {
            'response': response_text,
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
            'is_new_pharmacy_responses': new_arrivals_only,
            'last_user_message': message,
        }
        if req_id_for_short:
            try:
                if mr_for_chat is None:
                    mr_for_chat = MedicineRequest.objects.get(request_id=req_id_for_short)
                persist_medicine_request_ranking_snapshot(
                    mr_for_chat,
                    pharmacy_responses,
                    'chat_assistant',
                    limit_applied=len(pharmacy_responses),
                )
            except MedicineRequest.DoesNotExist:
                pass
        
        ai_message.metadata = {'pharmacy_responses_shown': True, 'total_responses': len(pharmacy_responses), 'new_arrivals_only': new_arrivals_only}
        ai_message.save(update_fields=['metadata'])
        # Persist suggested_medicines to conversation so Reserve can use them when frontend sends only conversation_id + pharmacy_id
        if medicines:
            conversation.context_metadata['suggested_medicines'] = list(medicines)
            conversation.save(update_fields=['context_metadata'])
    elif medicine_request_id:
        # Request created - check if we have responses
        try:
            medicine_request = MedicineRequest.objects.get(request_id=medicine_request_id)
            pv_m = _medicine_request_preview_fields(medicine_request)
            sym_prefix = ''
            if pv_m.get('is_symptom_request') and pv_m.get('symptoms'):
                sym_prefix = f"📋 **Symptoms:** {pv_m['symptoms'][:280]}\n\n"
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
                msg = sym_prefix + msg
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
                if ranked_responses:
                    persist_medicine_request_ranking_snapshot(
                        medicine_request,
                        ranked_responses,
                        'chat_assistant',
                        limit_applied=3,
                    )
            else:
                # No responses yet - include poll hint so frontend can check for responses without user sending another message
                conversation_id_str = str(conversation.conversation_id)
                poll_url = f"/api/chatbot/request/{medicine_request_id}/ranked/?conversation_id={conversation_id_str}&limit=3"
                short_req_id = str(medicine_request_id).replace('-', '')[:8].upper()
                wait_msg = "✅ Request has been sent. Waiting for pharmacies to respond. Responses will appear as soon as pharmacies reply."
                response_data = {
                    'response': sym_prefix + wait_msg,
                    'conversation_id': conversation.conversation_id,
                    'message_id': ai_message.message_id,
                    'intent': 'medicine_search',
                    'requires_location': False,
                    'suggested_medicines': medicines,
                    'medicine_request_id': medicine_request_id,
                    'short_request_id': short_req_id,
                    # Empty list is normal: no pharmacist quotes yet and no matching live inventory rows.
                    # Live stock only appears for verified pharmacies (accepting_requests, in range ~50km) with
                    # PharmacyInventory overlap; polls still receive quotes via poll_url after pharmacists reply.
                    'pharmacy_responses': [],
                    'pharmacy_responses_phase': 'awaiting_quotes',
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
                sym_line = (current_query_symptoms or '').strip()
                if not sym_line and (has_symptom or is_symptom_based_query):
                    sym_line = (message or '').strip()
                if sym_line:
                    response_data = {
                        'response': (
                            f"📋 I've noted: **{sym_line[:280]}**\n\n"
                            "To find pharmacies near you, please share your location (e.g. area name or use your current location)."
                        ),
                        'conversation_id': conversation.conversation_id,
                        'message_id': ai_message.message_id,
                        'intent': 'symptom_description',
                        'requires_location': True,
                        'suggested_medicines': [],
                        'symptoms': sym_line,
                        'request_preview': f'Symptoms: {sym_line}',
                        'is_symptom_request': True,
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
    
    _enrich_chat_response_with_request_preview(response_data)
    if location_lat is not None and location_lon is not None:
        response_data['location_latitude'] = float(location_lat)
        response_data['location_longitude'] = float(location_lon)
    if (
        not response_data.get('medicine_request_id')
        and not response_data.get('request_preview')
        and (intent == 'symptom_description' or is_symptom_based_query or has_symptom)
    ):
        sy = (current_query_symptoms or '').strip() or (message or '').strip()
        if sy and len(sy) > 1:
            response_data['symptoms'] = sy
            response_data['request_preview'] = f'Symptoms: {sy}'
            response_data['is_symptom_request'] = True
    _attach_drug_interactions_to_chat_payload(
        conversation, data, medicines, ai_result, medicine_request_id, response_data,
    )
    return Response(response_data, status=status.HTTP_200_OK)


def _mongo_safe_str(value, *, default=''):
    """MongoDB django backend rejects None for CharField/TextField — use ''."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def create_medicine_request(
    conversation,
    user,
    intent,
    medicines,
    symptoms,
    latitude,
    longitude,
    address,
    suburb='',
    *,
    email_patient: bool = True,
    extra_patient_emails: list | None = None,
    prescription_review_snapshot: dict | None = None,
    prescription_image_file=None,
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
    if intent == 'symptom_description':
        request_type = 'symptom'
    elif intent in ('prescription_upload', 'prescription'):
        request_type = 'prescription'
    else:
        request_type = 'direct'
    
    # Urban (≥3 pharmacies within 5km): 30min timeout; rural: 2hr
    density = RankingEngine.calculate_pharmacy_density(latitude, longitude) if (latitude and longitude) else 0
    timeout_minutes = 30 if density >= 3 else 120
    expires_at = timezone.now() + timedelta(minutes=timeout_minutes)

    normalized_names = _normalize_medicine_names_list(
        medicines if isinstance(medicines, list) else list(medicines or [])
    )

    review = prescription_review_snapshot if isinstance(prescription_review_snapshot, dict) else {}

    medicine_request = MedicineRequest.objects.create(
        conversation=conversation,
        user=user,
        request_type=request_type,
        medicine_names=normalized_names,
        symptoms=_mongo_safe_str(symptoms),
        location_latitude=latitude,
        location_longitude=longitude,
        location_address=_mongo_safe_str(address),
        location_suburb=_mongo_safe_str(suburb),
        status='broadcasting',  # Immediately available to pharmacies
        expires_at=expires_at,
        prescription_review_snapshot=review,
    )
    if prescription_image_file is not None:
        try:
            orig = getattr(prescription_image_file, 'name', '') or 'prescription.jpg'
            base = os.path.basename(str(orig).replace('\\', '/')) or 'prescription.jpg'
            safe_name = f'{medicine_request.request_id}_{base}'[:240]
            medicine_request.prescription_image.save(safe_name, prescription_image_file, save=True)
        except Exception as exc:
            print(f'[WARN] Could not persist prescription_image for request {medicine_request.request_id}: {exc}')
    
    # Broadcast to nearby pharmacies
    # In production, this would:
    # 1. Query nearby pharmacies from database (within X km radius)
    # 2. Send push notifications/emails/SMS to pharmacies
    # 3. Pharmacies see request in dashboard and respond via API
    nearby = broadcast_to_pharmacies(medicine_request)
    try:
        from .email_service import notify_medicine_request_created

        notify_medicine_request_created(
            medicine_request,
            nearby,
            email_patient=email_patient,
            extra_patient_emails=extra_patient_emails,
        )
    except Exception as exc:
        print(f'[WARN] notify_medicine_request_created failed: {exc}')

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
        all_pharmacies = Pharmacy.objects.filter(is_active=True, verification_status='verified')
        
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
                    if not _pharmacy_accepts_patient_requests(pharmacy):
                        continue
                    nearby_pharmacies.append({
                        'pharmacy': pharmacy,
                        'distance_km': distance
                    })
        
        print(f"[INFO] Found {len(nearby_pharmacies)} nearby pharmacies for request {medicine_request.request_id}")
        
        # Push/SMS can be added here; email is sent from create_medicine_request via notify_medicine_request_created.

    return nearby_pharmacies


RANKING_DELAY_MINUTES = 2  # Apply MCDA ranking only after this many minutes from request creation


def _ranking_snapshot_json_safe(obj):
    """Make ranked-response payloads JSON-safe for MedicineRequestRankingSnapshot.ranked_items."""
    from decimal import Decimal
    if obj is None:
        return None
    if isinstance(obj, dict):
        return {str(k): _ranking_snapshot_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_ranking_snapshot_json_safe(v) for v in obj]
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def _ws_broadcast_medicine_request_chat_snapshot(
    request_id,
    pharmacy_responses: list | None,
    *,
    conversation_id=None,
    pharmacy_responses_phase: str | None = None,
):
    """
    Push the **same** ``pharmacy_responses`` list the HTTP ``/chatbot/chat/`` response would carry
    to anyone subscribed at ``ws/chatbot/<request_id>/``. Historically WS only fired on pharmacist
    ``POST`` quotes; patient UIs subscribing before the REST body was parsed missed live-inventory rows.
    """
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        rid = str(request_id)
        rows = list(pharmacy_responses or [])
        safe_rows = _ranking_snapshot_json_safe(rows)
        if not isinstance(safe_rows, list):
            safe_rows = []
        payload = {
            'event': 'medicine_request_snapshot',
            'medicine_request_id': rid,
            'pharmacy_responses': safe_rows,
            'total_responses': len(rows),
        }
        try:
            mr_obj = MedicineRequest.objects.get(request_id=rid)
            payload['drug_interactions'] = _ranking_snapshot_json_safe(_ddi_for_medicine_request_model(mr_obj))
        except MedicineRequest.DoesNotExist:
            payload['drug_interactions'] = _ranking_snapshot_json_safe(DrugInteractionService.build_payload([]))
        if pharmacy_responses_phase:
            payload['pharmacy_responses_phase'] = pharmacy_responses_phase
        if conversation_id:
            payload['poll_url'] = (
                f'/api/chatbot/request/{rid}/ranked/?conversation_id={conversation_id}&limit=10'
            )
        async_to_sync(channel_layer.group_send)(
            f'chat_request_{rid}',
            {'type': 'chatbot_update', 'data': payload},
        )
    except Exception as exc:
        print(f'[WARNING] WebSocket medicine_request_snapshot broadcast failed: {exc}')


def _ws_broadcast_medicine_request_ranked_update(
    request_id,
    ranked_rows: list | None,
    *,
    conversation_id=None,
    trigger: str | None = None,
    ws_ranked_limit: int | None = None,
):
    """
    Full reranked merged list (same builder as GET ``.../ranked/``) after pharmacists POST quotes —
    complements the immediate ``medicine_request_snapshot`` from ``/chat``.
    Clients should merge/replace by ``medicine_request_id`` and respect ``ranking_pending`` on rows.
    """
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        rid = str(request_id)
        rows = list(ranked_rows or [])
        safe = _ranking_snapshot_json_safe(rows)
        if not isinstance(safe, list):
            safe = []
        ranking_pending = bool(rows and isinstance(rows[0], dict) and rows[0].get('ranking_pending'))
        lm = ws_ranked_limit if ws_ranked_limit is not None else len(rows)
        payload = {
            'event': 'medicine_request_ranked_update',
            'medicine_request_id': rid,
            'pharmacy_responses': safe,
            'total_responses': len(rows),
            'limit_applied': lm,
            'ranking_pending': ranking_pending,
            'merged_rank_source': 'same_as_GET_ranked',
        }
        try:
            mr_obj = MedicineRequest.objects.get(request_id=rid)
            payload['drug_interactions'] = _ranking_snapshot_json_safe(_ddi_for_medicine_request_model(mr_obj))
        except MedicineRequest.DoesNotExist:
            payload['drug_interactions'] = _ranking_snapshot_json_safe(DrugInteractionService.build_payload([]))
        if trigger:
            payload['trigger'] = trigger
        if conversation_id:
            payload['poll_url'] = (
                f'/api/chatbot/request/{rid}/ranked/?conversation_id={conversation_id}'
                f'&limit={max(lm or 10, 10)}'
            )
        async_to_sync(channel_layer.group_send)(
            f'chat_request_{rid}',
            {'type': 'chatbot_update', 'data': payload},
        )
    except Exception as exc:
        print(f'[WARNING] WebSocket medicine_request_ranked_update failed: {exc}')


def persist_medicine_request_ranking_snapshot(medicine_request, ranked_items, source, limit_applied):
    """
    Store the exact ranked list shown to the patient. Skips a new row if the payload
    matches the latest snapshot (same hash), so repeated identical polls do not spam the DB.
    """
    import json
    from hashlib import sha256

    safe_items = _ranking_snapshot_json_safe(ranked_items)
    if not isinstance(safe_items, list):
        safe_items = []
    blob = json.dumps(safe_items, sort_keys=True, separators=(',', ':')).encode('utf-8')
    fingerprint = sha256(blob).hexdigest()
    prev = (
        MedicineRequestRankingSnapshot.objects.filter(request=medicine_request)
        .order_by('-created_at')
        .first()
    )
    if prev:
        prev_fp = sha256(
            json.dumps(prev.ranked_items, sort_keys=True, separators=(',', ':')).encode('utf-8')
        ).hexdigest()
        if prev_fp == fingerprint:
            return
    MedicineRequestRankingSnapshot.objects.create(
        request=medicine_request,
        source=(source or '')[:32],
        limit_applied=limit_applied,
        ranked_items=safe_items,
    )


def get_ranked_pharmacy_responses(medicine_request, limit=3):
    """
    Pharmacy replies shown in the chatbot for one medicine request.

    - First ``RANKING_DELAY_MINUTES`` (2): chronological order so patients see answers immediately;
      ``ranking_pending`` is True.
    - After that: **``RankingEngine.rank_responses``** — 4-criteria MCDA (price, distance, rating,
      reliability) with **weights from PlatformAdminSettings** (urban vs rural from **patient**
      request lat/lon density). Each criterion is min–max **normalized within this request’s
      response list**, then weighted and summed (higher score = better rank). Available and
      unavailable branches are ranked separately (available first).
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
        
        # Get requested medicines from the medicine request (normalize for display + matching)
        requested_medicines = _normalize_medicine_names_list(medicine_request.medicine_names or [])
        requested_medicines_lower = [m.lower() for m in requested_medicines]
        
        # ALWAYS check inventory first (source of truth - decreased when patients buy or pharmacists edit)
        # Store both quantity and price so we show LIVE stock to the patient, not the old response snapshot
        inventory_by_medicine = {}  # medicine_name_lower -> {'quantity': int, 'price': str or None}
        if response.pharmacy:
            for inv in PharmacyInventory.objects.filter(pharmacy=response.pharmacy, quantity__gt=F('reserved_quantity')):
                qty = inv.quantity - inv.reserved_quantity
                price_str = str(inv.price) if inv.price is not None else None
                inventory_by_medicine[inv.medicine_name.lower()] = {'quantity': qty, 'price': price_str}
        
        # Helper: get quantity from inventory (supports fuzzy match)
        def _live_qty(med_lower):
            if med_lower in inventory_by_medicine:
                return inventory_by_medicine[med_lower]['quantity']
            for inv_name, data in inventory_by_medicine.items():
                if med_lower in inv_name or inv_name in med_lower:
                    return data['quantity']
            return None
        def _live_price(med_lower):
            if med_lower in inventory_by_medicine:
                return inventory_by_medicine[med_lower]['price']
            for inv_name, data in inventory_by_medicine.items():
                if med_lower in inv_name or inv_name in med_lower:
                    return data['price']
            return None

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
        
        # Full per-medicine rows from DB (serializer can differ from model in edge cases).
        def _mr_available(mr):
            if not isinstance(mr, dict):
                return False
            v = mr.get('available')
            if v is True:
                return True
            if isinstance(v, str) and v.lower() in ('true', '1', 'yes'):
                return True
            return False

        # Fresh read from DB + merge serializer rows (fixes missing extras like amoxicillin
        # when ORM cache vs API payload differ).
        _mr_db = PharmacyResponse.objects.filter(
            response_id=response.response_id
        ).values_list('medicine_responses', flat=True).first()
        if not isinstance(_mr_db, list):
            _mr_db = []
        raw_pharmacist_mr = []
        _seen_med = set()
        for mr in _mr_db:
            try:
                if isinstance(mr, dict):
                    row = dict(mr)
                elif hasattr(mr, 'keys'):
                    row = {k: mr[k] for k in mr.keys()}
                else:
                    continue
                k = str(row.get('medicine', '')).strip().lower()
                if not k:
                    continue
                raw_pharmacist_mr.append(row)
                _seen_med.add(k)
            except Exception:
                continue
        for mr in (response_data.get('medicine_responses') or []):
            try:
                if not isinstance(mr, dict):
                    continue
                k = str(mr.get('medicine', '')).strip().lower()
                if not k or k in _seen_med:
                    continue
                raw_pharmacist_mr.append(dict(mr))
                _seen_med.add(k)
            except Exception:
                continue

        pharmacist_available_meds = set()
        for row in raw_pharmacist_mr:
            if _mr_available(row):
                name = str(row.get('medicine', '')).strip().lower()
                if name:
                    pharmacist_available_meds.add(name)

        # Create per-medicine breakdown (use LIVE inventory for quantity/price so UI matches DB)
        # If requested_medicines exist, show breakdown per medicine
        # Otherwise, show general availability
        if requested_medicines:
            for req_med in requested_medicines:
                req_med_lower = req_med.lower()
                live_qty = _live_qty(req_med_lower)
                live_pr = _live_price(req_med_lower)
                ph_row = _pharmacist_row_for_requested_med(req_med_lower, raw_pharmacist_mr)
                ph_price = ph_row.get('price') if isinstance(ph_row, dict) else None
                ph_qty = ph_row.get('quantity') if isinstance(ph_row, dict) else None
                try:
                    ph_qty_int = int(ph_qty) if ph_qty is not None and str(ph_qty).strip() != '' else None
                except (TypeError, ValueError):
                    ph_qty_int = None
                medicine_status = {
                    'medicine': req_med,
                    'available': False,
                    'price': None,
                    'quantity': live_qty,  # live stock from PharmacyInventory
                    'alternative': None
                }
                
                # Use pharmacist availability + inventory as source of truth.
                # A medicine is "available" if either:
                #   - it appears as available in pharmacist medicine_responses, OR
                #   - it exists in live inventory with quantity > 0.
                effective_available = req_med_lower in pharmacist_available_meds
                if not effective_available and ph_row:
                    effective_available = _mr_available(ph_row)
                effective_price = response_data.get('price') or (str(response.price) if response.price else None)
                # Prefer live inventory price, then this line's pharmacist price (multi-med rows)
                if live_pr is not None:
                    medicine_status['price'] = live_pr
                elif ph_price not in (None, ''):
                    medicine_status['price'] = str(ph_price)
                elif len(requested_medicines) == 1:
                    medicine_status['price'] = effective_price
                if medicine_status['quantity'] is None and ph_qty_int is not None:
                    medicine_status['quantity'] = ph_qty_int
                # Check if this medicine has alternatives (suggesting it's unavailable)
                if req_med_lower in alternatives_by_medicine:
                    medicine_status['available'] = False
                    medicine_status['alternative'] = alternatives_by_medicine[req_med_lower][0]
                elif effective_available:
                    medicine_status['available'] = True
                    if medicine_status['price'] is None:
                        if ph_price not in (None, ''):
                            medicine_status['price'] = str(ph_price)
                        elif len(requested_medicines) == 1:
                            medicine_status['price'] = effective_price
                else:
                    # Check inventory - source of truth (decreased on purchase/edit)
                    in_inv = req_med_lower in inventory_by_medicine
                    if not in_inv:
                        for inv_name in inventory_by_medicine:
                            if req_med_lower in inv_name or inv_name in req_med_lower:
                                in_inv = True
                                break
                    medicine_status['available'] = in_inv
                    if in_inv and medicine_status['price'] is None and effective_price:
                        if len(requested_medicines) == 1:
                            medicine_status['price'] = effective_price
                
                medicines_breakdown.append(medicine_status)

            # Medicines the pharmacist offered that were NOT in the patient's search — still show them.
            req_lower_set = {m.lower() for m in requested_medicines}
            seen_extra = set()
            for mr in raw_pharmacist_mr:
                if not isinstance(mr, dict) or not _mr_available(mr):
                    continue
                med_name = str(mr.get('medicine', '')).strip()
                if not med_name:
                    continue
                key = med_name.lower()
                if key in req_lower_set or key in seen_extra:
                    continue
                seen_extra.add(key)
                live_qty = _live_qty(key)
                live_pr = _live_price(key)
                ph_price = mr.get('price')
                ph_qty = mr.get('quantity')
                try:
                    ph_qty_int = int(ph_qty) if ph_qty is not None and str(ph_qty).strip() != '' else None
                except (TypeError, ValueError):
                    ph_qty_int = None
                medicines_breakdown.append({
                    'medicine': med_name,
                    'available': True,
                    'price': live_pr if live_pr is not None else (str(ph_price) if ph_price not in (None, '') else None),
                    'quantity': live_qty if live_qty is not None else ph_qty_int,
                    'alternative': None,
                    'from_pharmacist_only': True,
                })
        else:
            # No medicine_names on request (symptom / legacy). Use pharmacist medicine_responses first
            # so we do not replace real rows with placeholder and then drop them from medicine_responses.
            seen_symptom = set()
            for mr in raw_pharmacist_mr:
                if not isinstance(mr, dict):
                    continue
                med_name = str(mr.get('medicine', '')).strip()
                if not med_name:
                    continue
                key = med_name.lower()
                if key == 'requested medicines':
                    continue
                if key in seen_symptom:
                    continue
                seen_symptom.add(key)
                live_qty = _live_qty(key)
                live_pr = _live_price(key)
                ph_price = mr.get('price')
                ph_qty = mr.get('quantity')
                exp = mr.get('expiry')
                if exp is None:
                    exp = mr.get('expiry_date')
                try:
                    ph_qty_int = int(ph_qty) if ph_qty is not None and str(ph_qty).strip() != '' else None
                except (TypeError, ValueError):
                    ph_qty_int = None
                alt = mr.get('alternative')
                medicines_breakdown.append({
                    'medicine': med_name,
                    'available': _mr_available(mr),
                    'price': live_pr if live_pr is not None else (str(ph_price) if ph_price not in (None, '') else None),
                    'quantity': live_qty if live_qty is not None else ph_qty_int,
                    'expiry': exp,
                    'alternative': alt,
                })
            if not medicines_breakdown:
                medicines_breakdown.append({
                    'medicine': 'Requested medicines',
                    'available': response.medicine_available,
                    'price': str(response.price) if response.price and response.medicine_available else None,
                    'alternative': None,
                })
        
        # Add per-medicine breakdown to response
        response_data['medicines'] = medicines_breakdown
        
        # Patient-facing list: requested meds (live + pharmacist) plus extra meds only pharmacist listed.
        if requested_medicines or medicines_breakdown:
            response_data['medicine_responses'] = []
            for m in medicines_breakdown:
                if m.get('medicine') == 'Requested medicines':
                    continue
                pr = m.get('price')
                row_m = {
                    'medicine': m['medicine'],
                    'available': m['available'],
                    'price': pr if pr is not None else 'N/A',
                    'quantity': m.get('quantity'),
                    'expiry': m.get('expiry'),
                    'alternative': m.get('alternative'),
                    'from_pharmacist_only': bool(m.get('from_pharmacist_only')),
                }
                response_data['medicine_responses'].append(row_m)
            # Patient UIs often read top-level price/quantity; fill from first in-stock line when missing
            if response_data.get('medicine_available'):
                tops = response_data.get('medicine_responses') or []
                for row in tops:
                    if not row.get('available'):
                        continue
                    p = row.get('price')
                    if p not in (None, '', 'N/A'):
                        response_data['price'] = str(p).strip()
                        q = row.get('quantity')
                        if q is not None and str(q).strip() != '':
                            response_data['quantity'] = q
                        break
            # Symptom-only path: no requested_medicines rows in list — parse notes if needed
            if not response_data['medicine_responses'] and not requested_medicines:
                if response_data.get('price') and response_data.get('notes'):
                    try:
                        import re as _re_local_sym
                        raw_note = str(response_data.get('notes') or '').strip()
                        m_note = _re_local_sym.match(r"\s*([A-Za-z0-9\s]+?)(?:\s*\$?\s*(\d+(?:\.\d{1,2})?))?\s*$", raw_note)
                    except Exception:
                        m_note = None
                    if m_note:
                        med_name = m_note.group(1).strip()
                        price_from_note = m_note.group(2) or response_data.get('price')
                        if med_name:
                            response_data['medicine_responses'] = [{
                                'medicine': med_name,
                                'available': True,
                                'price': str(price_from_note),
                                'quantity': None,
                                'from_pharmacist_only': False,
                            }]
        
        # Also add requested medicines to response for frontend reference
        response_data['requested_medicines'] = requested_medicines
        
        ranked_responses.append(response_data)

    # Reliability signal for MCDA: computed match rate (nearby opportunities in window), not DB field.
    _pids_for_rr = [str(r['pharmacy_id']) for r in ranked_responses if r.get('pharmacy_id')]
    _rate_by_pid = (
        compute_effective_pharmacy_response_rates_for_ids(_pids_for_rr) if _pids_for_rr else {}
    )
    for r in ranked_responses:
        pid = r.get('pharmacy_id')
        if pid and str(pid) in _rate_by_pid:
            r['pharmacy_response_rate'] = _rate_by_pid[str(pid)]

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
    Nearest pharmacies that actually have the requested medicine in stock (available = qty − reserved).

    Only verified active branches within max_distance_km with a matching inventory row are returned;
    then ranked by blended score (40% closer distance, 30% price, 20% availability depth, 10% rating).
    Same product idea as “nearest pharmacy with medicine available” for live search/chat.
    """
    from decimal import Decimal
    if not medicine_names or not latitude or not longitude:
        return []
    medicine_names_lower = [m.lower().strip() for m in medicine_names if m and str(m).strip()]
    if not medicine_names_lower:
        return []

    # All inventory rows with stock (available > 0), for verified active pharmacies only
    inv_qs = PharmacyInventory.objects.filter(
        pharmacy__is_active=True,
        pharmacy__verification_status='verified',
        quantity__gt=F('reserved_quantity')
    ).select_related('pharmacy')

    # Filter to rows where medicine name matches any requested (contains or equals)
    matching_inv = []
    for inv in inv_qs:
        if not _pharmacy_accepts_patient_requests(inv.pharmacy):
            continue
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
            'pharmacy_contact': {
                'address': ((ph.address or '').strip() or None),
                'phone': ((ph.phone or '').strip() or None),
                'email': ((ph.email or '').strip() or None),
                'whatsapp': ((ph.whatsapp or '').strip() or None),
                'website': ((str(ph.website).strip() if ph.website else '') or None),
            },
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


def build_merged_ranked_rows_for_request(medicine_request, *, limit: int) -> list[dict]:
    """
    Patient-facing merge identical to GET ``.../ranked/``: pharmacist rows from
    ``get_ranked_pharmacy_responses`` (chrono for ~2 min then MCDA within each availability cohort)
    plus live-inventory-only pharmacies (same dedupe by ``pharmacy_id``). Final order: global sort
    by numeric ``ranking_score`` descending; absent scores sort last — then ``rank`` 1«…»:limit``.
    """
    pool = max(int(limit or 3) * 2, int(limit or 3), 3)
    ranked_responses = list(get_ranked_pharmacy_responses(medicine_request, limit=pool))
    seen_pharmacy_ids = {r.get('pharmacy_id') for r in ranked_responses if r.get('pharmacy_id')}
    if (
        medicine_request.location_latitude is not None
        and medicine_request.location_longitude is not None
        and (medicine_request.medicine_names or [])
    ):
        live_results = get_live_inventory_ranked(
            medicine_request.location_latitude,
            medicine_request.location_longitude,
            medicine_request.medicine_names,
            limit=pool,
        )
        for r in live_results:
            pid = r.get('pharmacy_id')
            if pid and pid not in seen_pharmacy_ids:
                rr = dict(r)
                rr['from_live_inventory'] = True
                ranked_responses.append(rr)
                seen_pharmacy_ids.add(pid)

    def _score_key(x):
        s = x.get('ranking_score')
        if s is None:
            return -1.0
        try:
            return float(s)
        except (TypeError, ValueError):
            return -1.0

    ranked_responses.sort(key=_score_key, reverse=True)
    payload_plain = ranked_responses[: int(limit)]
    for i, r in enumerate(payload_plain, 1):
        if isinstance(r, dict):
            r['rank'] = i
    return payload_plain


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
        responses = medicine_request.pharmacy_responses.select_related('pharmacy', 'request').all()

        from .pharmacy_portal_ranking import ensure_pharmacy_response_distance_saved

        for response in responses:
            if medicine_request.location_latitude and medicine_request.location_longitude:
                ensure_pharmacy_response_distance_saved(response, allow_geocode=True)

            # Calculate total time (preparation + travel)
            total_time = response.preparation_time or 0
            if response.estimated_travel_time is not None:
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
        loc_payload = _medicine_request_location_payload(medicine_request)
        for i, response in enumerate(sorted_responses):
            response_data[i]['total_time_minutes'] = getattr(response, 'total_time_minutes', response.preparation_time)
            response_data[i]['patient_location'] = loc_payload
        
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
        blob = DrugInteractionService.build_payload(_normalize_medicine_names_list(medicines))
        return Response({
            'medicines': medicines,
            'medicines_checked': blob.get('medicines_checked'),
            'interactions': [],
            'message': 'At least 2 medicines needed to check interactions',
            'has_interactions': False,
            'disclaimer': blob.get('disclaimer'),
            'drug_interactions': blob,
            'severity_highest': None,
        }, status=status.HTTP_200_OK)

    names_norm = _normalize_medicine_names_list(medicines)
    blob = DrugInteractionService.build_payload(names_norm)
    interactions = list(blob.get('interactions') or [])

    return Response({
        'medicines': medicines,
        'medicines_checked': blob.get('medicines_checked'),
        'interactions': interactions,
        'has_interactions': bool(blob.get('has_interactions')),
        'disclaimer': blob.get('disclaimer'),
        'drug_interactions': blob,
        'severity_highest': blob.get('highest_severity'),
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
    Upload prescription image and extract medicines via OCR.

    When OCR fails but ``location_latitude`` / ``location_longitude`` are posted, still creates a
    **prescription-type** broadcast with ``prescription_image`` + pharmacist instructions so branches
    can quote from the photo.
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

    skip_ocr = _truthy_multipart(request.data.get('skip_ocr'))
    pharmacist_review_only = _truthy_multipart(request.data.get('pharmacist_review_only'))
    bypass_gemini = skip_ocr or pharmacist_review_only

    try:
        session_id = request.data.get('session_id') or str(uuid.uuid4())
        conversation, _created_conv = ChatConversation.objects.get_or_create(
            session_id=session_id,
            defaults={'status': 'active'},
        )
        safe_fname = os.path.basename(getattr(image_file, 'name', '') or '') or 'prescription.jpg'
        latitude = request.data.get('location_latitude')
        longitude = request.data.get('location_longitude')

        if bypass_gemini:
            ChatMessage.objects.create(
                conversation=conversation,
                role='user',
                content=(
                    'Uploaded prescription image (saved without OCR — pharmacist manual review '
                    'from photo).'
                ),
            )
            meta = conversation.context_metadata or {}
            meta['prescription_pharmacist_read_pending'] = True
            meta['prescription_skip_ocr_upload'] = True
            meta['prescription_review_only_upload'] = True
            meta['prescription_confidence_percent'] = 0
            meta['prescription_reading_notes'] = ''
            meta.pop('prescription_medicines', None)
            meta.pop('prescription_items', None)
            meta.pop('last_prescription_ocr_error', None)
            conversation.context_metadata = meta
            conversation.save(update_fields=['context_metadata'])

            medicine_request_id = None
            broadcast_fallback = False
            if latitude and longitude:
                from django.core.files.base import ContentFile

                image_file.seek(0)
                stored_file = ContentFile(image_file.read())
                stored_file.name = safe_fname
                rx_snap = _snapshot_pharmacist_skip_ocr_upload(upload_filename=safe_fname)
                medicine_request = create_medicine_request(
                    conversation=conversation,
                    user=request.user if request.user.is_authenticated else None,
                    intent='prescription_upload',
                    medicines=[],
                    symptoms='',
                    latitude=float(latitude),
                    longitude=float(longitude),
                    address=_mongo_safe_str(request.data.get('location_address')),
                    suburb=_mongo_safe_str(request.data.get('location_suburb')),
                    prescription_review_snapshot=rx_snap,
                    prescription_image_file=stored_file,
                )
                medicine_request_id = medicine_request.request_id
                broadcast_fallback = True

            stub = {
                'medicines': [],
                'items': [],
                'dosages': {},
                'raw_text': '',
                'confidence': 'low',
                'confidence_percent': 0,
                'reading_notes': '',
                'summary_markdown': '',
                'skipped_ocr_api': True,
            }
            return Response({
                **stub,
                'conversation_id': conversation.conversation_id,
                'medicine_request_id': str(medicine_request_id) if medicine_request_id else None,
                'broadcasted_without_extracted_medicines': broadcast_fallback,
                'requires_location_for_broadcast': not bool(latitude and longitude),
                'skip_ocr': True,
                'pharmacist_review_only': True,
                'prescription_image_only': True,
                'ocr_failed': True,
                'message': (
                    'Prescription saved without automated reading.'
                    + (
                        ' Nearby pharmacies received your prescription image for manual review.'
                        if medicine_request_id
                        else ' Add your location to send it to pharmacies.'
                    )
                ),
                'drug_interactions': _drug_interactions_block([]),
            }, status=status.HTTP_200_OK)

        # Standard path: Gemini / OCR extract
        ocr_service = OCRService()
        
        # Extract prescription text
        ocr_result = ocr_service.extract_prescription_text(image_file)

        medicines_list = list(ocr_result['medicines'] or [])
        safe_fname = os.path.basename(getattr(image_file, 'name', '') or '') or 'prescription.jpg'

        if medicines_list:
            user_content = f"Uploaded prescription with medicines: {', '.join(medicines_list)}"
        else:
            user_content = (
                'Uploaded prescription image — automatic OCR did not return medicines; '
                'image forwarded to pharmacies when location is supplied for manual review.'
            )

        ChatMessage.objects.create(conversation=conversation, role='user', content=user_content)

        # Store OCR metadata + medicine list on conversation for later /chat broadcasts
        meta = conversation.context_metadata or {}
        meta['prescription_confidence_percent'] = int(ocr_result.get('confidence_percent') or 0)
        meta['prescription_reading_notes'] = str(ocr_result.get('reading_notes') or '')
        if medicines_list:
            meta['prescription_medicines'] = medicines_list
            if ocr_result.get('items'):
                meta['prescription_items'] = ocr_result['items']
            meta.pop('prescription_pharmacist_read_pending', None)
        else:
            meta['prescription_pharmacist_read_pending'] = True
        err_part = str(ocr_result.get('error') or '').strip()
        if err_part:
            meta['last_prescription_ocr_error'] = err_part[:2000]
        else:
            meta.pop('last_prescription_ocr_error', None)
        conversation.context_metadata = meta
        conversation.save(update_fields=['context_metadata'])
        if medicines_list:
            print(f"[INFO] Stored prescription medicines in conversation metadata: {medicines_list}")

        # Broadcast when we have coordinates: OCR success shares structured list + image,
        # OCR failure still saves the image so pharmacists quote from the Rx photo.
        medicine_request_id = None
        broadcast_fallback = False
        latitude = request.data.get('location_latitude')
        longitude = request.data.get('location_longitude')

        if latitude and longitude:
            from django.core.files.base import ContentFile

            image_file.seek(0)
            raw_bytes = image_file.read()
            stored_file = ContentFile(raw_bytes)
            stored_file.name = safe_fname

            if medicines_list:
                rx_snap = _snapshot_for_pharmacies_from_ocr(ocr_result)
                medicine_request = create_medicine_request(
                    conversation=conversation,
                    user=request.user if request.user.is_authenticated else None,
                    intent='prescription_upload',
                    medicines=medicines_list,
                    symptoms='',
                    latitude=float(latitude),
                    longitude=float(longitude),
                    address=_mongo_safe_str(request.data.get('location_address')),
                    suburb=_mongo_safe_str(request.data.get('location_suburb')),
                    prescription_review_snapshot=rx_snap,
                    prescription_image_file=stored_file,
                )
            else:
                broadcast_fallback = True
                rx_snap = _snapshot_for_pharmacists_prescription_fallback(
                    ocr_result, upload_filename=safe_fname
                )
                medicine_request = create_medicine_request(
                    conversation=conversation,
                    user=request.user if request.user.is_authenticated else None,
                    intent='prescription_upload',
                    medicines=[],
                    symptoms='',
                    latitude=float(latitude),
                    longitude=float(longitude),
                    address=_mongo_safe_str(request.data.get('location_address')),
                    suburb=_mongo_safe_str(request.data.get('location_suburb')),
                    prescription_review_snapshot=rx_snap,
                    prescription_image_file=stored_file,
                )
            medicine_request_id = medicine_request.request_id
        
        quota_or_ocr_notes = bool(
            str(ocr_result.get('error') or '').strip()
            or broadcast_fallback
            or not medicines_list
        )
        payload = {
            'medicines': ocr_result['medicines'],
            'items': ocr_result.get('items') or [],
            'dosages': ocr_result['dosages'],
            'raw_text': ocr_result['raw_text'],
            'confidence': ocr_result['confidence'],
            'confidence_percent': ocr_result.get('confidence_percent', 0),
            'reading_notes': ocr_result.get('reading_notes') or '',
            'summary_markdown': ocr_result.get('summary_markdown') or '',
            'conversation_id': conversation.conversation_id,
            'medicine_request_id': str(medicine_request_id) if medicine_request_id else None,
            'broadcasted_without_extracted_medicines': broadcast_fallback,
            'requires_location_for_broadcast': not bool(latitude and longitude),
            'skip_ocr': False,
            'pharmacist_review_only': False,
            'prescription_image_only': (not medicines_list) or broadcast_fallback,
            'ocr_failed': quota_or_ocr_notes,
            'message': (
                (
                    ocr_result.get('summary_markdown')
                    or 'Prescription processed successfully'
                )
                if medicines_list
                else (
                    (
                        'We could not read medicine names automatically, but your prescription image '
                        'was sent to nearby pharmacies so staff can confirm from the photo and reply with quotes.'
                    )
                    if medicine_request_id
                    else (
                        'Could not extract medicines from this image. '
                        'Add your location when uploading (or chat with location next) '
                        'so pharmacies can receive your prescription image and respond.'
                    )
                )
            ),
        }
        if ocr_result.get('error'):
            payload['error'] = ocr_result['error']
        payload['drug_interactions'] = _drug_interactions_block(medicines_list)
        return Response(payload, status=status.HTTP_200_OK)
    
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
        pharmacist = Pharmacist.objects.select_related('pharmacy').get(pharmacist_id=pharmacist_id)
    except Pharmacist.DoesNotExist:
        return Response(
            {'error': 'Pharmacist not found'},
            status=status.HTTP_404_NOT_FOUND
        )

    ph = pharmacist.pharmacy
    if ph and not _pharmacy_may_serve_patients(ph):
        return Response(
            {
                'error': (
                    'This pharmacy is not yet approved for patient requests. '
                    'Wait for an administrator to verify your registration.'
                ),
                'verification_status': getattr(ph, 'verification_status', None) or 'pending_review',
            },
            status=status.HTTP_403_FORBIDDEN,
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
    
    # Previously we hid active requests beyond 50km. For pharmacist dashboards
    # it's more useful to show *all* relevant requests and simply annotate
    # distance, so pharmacists can decide. So we no longer filter out by
    # distance; we just calculate it when coordinates are available.
    from .services import LocationService
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
                # Always include the request, but record the distance so UI
                # can sort or display it. No radius filtering here.
                nearby_requests.append((req, distance))
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
    _loc_text_cache: dict[tuple[float, float], str] = {}

    def _location_text_for_request(r):
        # Same gating as _location_text_from_request_fields: geocode only when no real text or "Location:" coords line.
        raw_area = (r.location_suburb or r.location_address or '').strip()
        lat, lon = r.location_latitude, r.location_longitude
        if lat is not None and lon is not None and (
            not raw_area or raw_area.startswith('Location:')
        ):
            key = (round(float(lat), 4), round(float(lon), 4))
            if key in _loc_text_cache:
                return _loc_text_cache[key]
            txt = _location_text_from_request_fields(
                r.location_suburb, r.location_address, lat, lon,
                reverse_geocode_if_needed=False,
            )
            _loc_text_cache[key] = txt
            return txt
        return _location_text_from_request_fields(
            r.location_suburb, r.location_address, lat, lon,
            reverse_geocode_if_needed=False,
        )

    for req, distance in nearby_requests:
        has_responded = PharmacyResponse.objects.filter(
            request=req,
            pharmacist=pharmacist
        ).exists()
        has_declined = PharmacistDecline.objects.filter(
            request=req,
            pharmacist=pharmacist
        ).exists()

        # Dashboard always lists active broadcasts so staff can respond or unpause;
        # accepting_requests still gates patient discovery / emails / live search elsewhere.
        response_count = req.pharmacy_responses.count()
        
        short_id = str(req.request_id).replace('-', '')[:8].upper()
        pv = _medicine_request_preview_fields(req)
        request_data.append({
            'request_id': str(req.request_id),
            'short_request_id': short_id,
            'request_type': req.request_type,
            'medicine_names': req.medicine_names,
            'symptoms': req.symptoms,
            'request_preview': pv['request_preview'],
            'is_symptom_request': pv['is_symptom_request'],
            'needs_pharmacist_prescription_read': bool(pv.get('needs_pharmacist_prescription_read')),
            'location': _medicine_request_location_payload(req, reverse_geocode_if_needed=False),
            'location_address': req.location_address,
            'location_suburb': req.location_suburb or '',
            'location_text': _location_text_for_request(req),
            'location_latitude': req.location_latitude,
            'location_longitude': req.location_longitude,
            'created_at': req.created_at,
            'expires_at': req.expires_at,
            'status': req.status,
            'has_responded': has_responded,
            'has_declined': has_declined,
            'response_count': response_count,
            'distance_km': round(distance, 2) if distance else None,
            'prescription_review': req.prescription_review_snapshot or {},
            'has_prescription_image': bool(req.prescription_image),
            'prescription_image_url': _pharmacist_prescription_image_absolute_url(
                request, req, pharmacist_id
            ),
        })
    
    print(f"[INFO] Returning {len(request_data)} requests for pharmacist {pharmacist_id}")
    return Response(request_data, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([AllowAny])
def pharmacist_request_prescription_image(request, request_id):
    """
    Stream the patient's prescription image for pharmacist verification.

    Requires ``pharmacist_id`` query param (same staffing model as pharmacist/requests/).
    """
    pharmacist_id = request.query_params.get('pharmacist_id')

    if not pharmacist_id:
        return Response(
            {'error': 'pharmacist_id query parameter is required'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        pharmacist = Pharmacist.objects.select_related('pharmacy').get(pharmacist_id=pharmacist_id)
    except Pharmacist.DoesNotExist:
        return Response({'error': 'Pharmacist not found'}, status=status.HTTP_404_NOT_FOUND)

    ph = pharmacist.pharmacy
    if ph and not _pharmacy_may_serve_patients(ph):
        return Response(
            {
                'error': (
                    'This pharmacy is not yet approved for patient requests. '
                    'Wait for an administrator to verify your registration.'
                ),
                'verification_status': getattr(ph, 'verification_status', None) or 'pending_review',
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        medicine_request = MedicineRequest.objects.get(request_id=request_id)
    except MedicineRequest.DoesNotExist:
        return Response({'error': 'Medicine request not found'}, status=status.HTTP_404_NOT_FOUND)

    if not medicine_request.prescription_image:
        return Response(
            {'error': 'No prescription image attached to this request'},
            status=status.HTTP_404_NOT_FOUND,
        )

    try:
        fh = medicine_request.prescription_image.open('rb')
    except Exception as exc:
        print(f'[ERROR] pharmacist_request_prescription_image open failed: {exc}')
        return Response(
            {'error': 'Could not read prescription file'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    filename = os.path.basename(medicine_request.prescription_image.name) or 'prescription'
    ctype = mimetypes.guess_type(filename)[0] or 'application/octet-stream'

    resp = FileResponse(fh, content_type=str(ctype))
    resp['Content-Disposition'] = f'inline; filename="{filename}"'
    resp['Cache-Control'] = 'private, max-age=3600'
    return resp


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

    if pharmacy and not _pharmacy_may_serve_patients(pharmacy):
        return Response(
            {
                'error': (
                    'This pharmacy is not approved to respond to patient requests yet. '
                    'Wait for administrator verification.'
                ),
                'verification_status': getattr(pharmacy, 'verification_status', None) or 'pending_review',
            },
            status=status.HTTP_403_FORBIDDEN,
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
    medicine_responses = request.data.get('medicine_responses', []) or []
    
    # If pharmacist only typed a simple note like "cough syrup $1" and did not
    # add a structured row for it, convert that note into a medicine_responses
    # entry so the chatbot/frontend show it as a proper medicine line.
    if isinstance(medicine_responses, list) and notes_text.strip():
        raw = notes_text.strip()
        try:
            import re as _re_local
            m = _re_local.match(r"\s*([A-Za-z0-9\s]+?)(?:\s*\$?\s*(\d+(?:\.\d{1,2})?))?\s*$", raw)
        except Exception:
            m = None
        if m:
            med_name = m.group(1).strip()
            price_from_notes = m.group(2)
            if med_name:
                exists = any(
                    isinstance(item, dict)
                    and str(item.get('medicine', '')).strip().lower() == med_name.lower()
                    for item in medicine_responses
                )
                if not exists:
                    med_entry = {
                        'medicine': med_name,
                        'available': True,
                        'price': price_from_notes or None,
                        'quantity': None,
                        'expiry': None,
                        'alternative': None,
                    }
                    medicine_responses.append(med_entry)
                    request.data['medicine_responses'] = medicine_responses
    
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
        if not request.data.get('quantity'):
            for item in medicine_responses:
                if not isinstance(item, dict):
                    continue
                if item.get('available') and item.get('quantity') not in (None, ''):
                    request.data['quantity'] = item.get('quantity')
                    break

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

    if response_obj.distance_km is None:
        from .pharmacy_portal_ranking import ensure_pharmacy_response_distance_saved

        ensure_pharmacy_response_distance_saved(response_obj, allow_geocode=True)
    
    serializer = PharmacyResponseSerializer(response_obj)

    # Broadcast websocket: legacy ping plus full reranked merged list (live + pharmacist, MCDA when due).
    WS_RANK_LIMIT = min(50, max(10, int(getattr(settings, 'WEBSOCKET_MERGED_RANKED_ROWS', 15) or 15)))

    try:
        channel_layer = get_channel_layer()
        if channel_layer:
            group_name = f"chat_request_{medicine_request.request_id}"
            async_to_sync(channel_layer.group_send)(
                group_name,
                {
                    "type": "chatbot_update",
                    "data": {
                        "event": "pharmacy_response",
                        "medicine_request_id": str(medicine_request.request_id),
                    },
                },
            )
    except Exception as e:
        print(f"[WARNING] Failed to broadcast pharmacy_response websocket event: {e}")

    try:
        conv = getattr(medicine_request, 'conversation', None)
        cid = str(conv.conversation_id) if conv else None
        ranked_rows_w = build_merged_ranked_rows_for_request(medicine_request, limit=WS_RANK_LIMIT)
        _ws_broadcast_medicine_request_ranked_update(
            medicine_request.request_id,
            ranked_rows_w,
            conversation_id=cid,
            trigger='pharmacist_submitted',
            ws_ranked_limit=WS_RANK_LIMIT,
        )
        persist_medicine_request_ranking_snapshot(
            medicine_request,
            ranked_rows_w,
            'pharmacist_submitted_ws',
            limit_applied=WS_RANK_LIMIT,
        )
    except Exception as exc:
        print(f'[WARNING] WebSocket medicine_request_ranked_update after pharmacist submit: {exc}')

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

    _notify_mail = request.data.get('notify_patient_by_email', True)
    if isinstance(_notify_mail, str):
        _notify_mail = _coerce_bool(_notify_mail)
    else:
        _notify_mail = True if _notify_mail is None else bool(_notify_mail)
    if _notify_mail:
        try:
            from .email_service import notify_pharmacy_response_to_patient

            notify_pharmacy_response_to_patient(medicine_request, response_obj, pharmacy)
        except Exception as exc:
            print(f'[WARN] notify_pharmacy_response_to_patient failed: {exc}')

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
        pharmacist = Pharmacist.objects.select_related('pharmacy').get(pharmacist_id=pharmacist_id)
    except Pharmacist.DoesNotExist:
        return Response({'error': 'Pharmacist not found'}, status=status.HTTP_404_NOT_FOUND)

    if pharmacist.pharmacy and not _pharmacy_may_serve_patients(pharmacist.pharmacy):
        return Response(
            {
                'error': 'This pharmacy is not approved to interact with patient requests yet.',
                'verification_status': getattr(pharmacist.pharmacy, 'verification_status', None) or 'pending_review',
            },
            status=status.HTTP_403_FORBIDDEN,
        )

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


def _pharmacy_inventory_payload(pharmacy):
    """Full inventory snapshot (same shape for GET and POST)."""
    items = list(PharmacyInventory.objects.filter(pharmacy=pharmacy).order_by('medicine_name'))
    in_stock = sum(1 for i in items if i.quantity >= i.low_stock_threshold)
    low_stock = sum(1 for i in items if 0 < i.quantity < i.low_stock_threshold)
    out_of_stock = sum(1 for i in items if i.quantity <= 0)
    return {
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
    }


def _delete_pharmacy_inventory_row_response(pharmacy, medicine_name):
    """Delete one inventory row if safe. Same behaviour as DELETE on ``pharmacist_inventory``."""
    nm = (medicine_name or '').strip()
    if not nm:
        return Response({'error': 'medicine_name is required'}, status=status.HTTP_400_BAD_REQUEST)
    inv = PharmacyInventory.objects.filter(pharmacy=pharmacy, medicine_name__iexact=nm).first()
    if not inv:
        return Response(
            {'error': f'Inventory row not found for medicine "{nm}"'},
            status=status.HTTP_404_NOT_FOUND,
        )
    reserved = max(0, int(inv.reserved_quantity or 0))
    if reserved > 0:
        return Response(
            {
                'error': (
                    f'Cannot delete "{inv.medicine_name}" while {reserved} unit(s) are reserved. '
                    'Cancel or fulfil reservations first.'
                ),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    deleted_name = inv.medicine_name
    inv.delete()
    payload = {
        'message': 'Inventory row deleted',
        'deleted_medicine_name': deleted_name,
        **_pharmacy_inventory_payload(pharmacy),
    }
    return Response(payload, status=status.HTTP_200_OK)


@api_view(['GET', 'POST', 'PATCH', 'DELETE'])
@permission_classes([AllowAny])
def pharmacist_inventory(request):
    """
    GET: List inventory for pharmacist's pharmacy
        ?pharmacist_id=uuid
    POST: Update inventory (bulk upsert)
        { "pharmacist_id": "uuid", "items": [{ "medicine_name": "paracetamol", "quantity": 100, "low_stock_threshold": 10, "price": 1.5 }, ...] }
    POST (delete alias — use when DELETE is blocked by a proxy/host or tooling):
        { "pharmacist_id": "uuid", "action": "delete", "medicine_name": "paracetamol" }
    PATCH: Update a single inventory row (only send fields you want to change)
        { "pharmacist_id": "uuid", "medicine_name": "paracetamol",
          "quantity"?: int, "low_stock_threshold"?: int, "price"?: number,
          "new_medicine_name"?: "paracetamol 650mg" }
        Rename uses lowercase storage (matches bulk POST); reservations for this pharmacy listing the old
        label are updated so pickup/stock flows keep working.
        quantity cannot fall below ``reserved_quantity`` (active reservations locking stock).
    DELETE: Remove one inventory row
        { "pharmacist_id": "uuid", "medicine_name": "paracetamol" }
        Query params also accepted: ?pharmacist_id=...&medicine_name=...
        Blocked while ``reserved_quantity`` > 0 (release/cancel reservations first).

    POST response includes the same full snapshot as GET (pharmacy_id, summary, items) plus
    ``updated_count`` and ``updated_items`` for bulk upserts; POST with ``action: delete``
    matches the DELETE response shape (``deleted_medicine_name``, full snapshot).
    PATCH / DELETE return the full snapshot plus ``deleted_medicine_name`` on delete.
    """
    if request.method == 'GET':
        pharmacist_id = request.query_params.get('pharmacist_id')
    elif request.method == 'DELETE':
        pharmacist_id = request.query_params.get('pharmacist_id') or (
            request.data.get('pharmacist_id') if isinstance(request.data, dict) else None
        )
    else:
        pharmacist_id = request.data.get('pharmacist_id') if hasattr(request, 'data') else None

    if not pharmacist_id:
        return Response({'error': 'pharmacist_id is required'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        pharmacist = Pharmacist.objects.get(pharmacist_id=pharmacist_id)
        pharmacy = pharmacist.pharmacy
    except Pharmacist.DoesNotExist:
        return Response({'error': 'Pharmacist not found'}, status=status.HTTP_404_NOT_FOUND)
    if not pharmacy:
        return Response({'error': 'Pharmacist has no pharmacy'}, status=status.HTTP_400_BAD_REQUEST)

    def _inventory_row(pharmacy_obj, raw_name):
        nm = (raw_name or '').strip()
        if not nm:
            return None
        return PharmacyInventory.objects.filter(pharmacy=pharmacy_obj, medicine_name__iexact=nm).first()

    if request.method == 'GET':
        return Response(_pharmacy_inventory_payload(pharmacy), status=status.HTTP_200_OK)

    if request.method == 'PATCH':
        data = request.data if isinstance(request.data, dict) else {}
        medicine_name = (data.get('medicine_name') or '').strip()
        if not medicine_name:
            return Response({'error': 'medicine_name is required'}, status=status.HTTP_400_BAD_REQUEST)
        inv = _inventory_row(pharmacy, medicine_name)
        if not inv:
            return Response(
                {'error': f'Inventory row not found for medicine "{medicine_name}"'},
                status=status.HTTP_404_NOT_FOUND,
            )
        patched = []

        if 'quantity' in data:
            try:
                q = int(data['quantity'])
            except (TypeError, ValueError):
                return Response({'error': 'quantity must be an integer'}, status=status.HTTP_400_BAD_REQUEST)
            q = max(0, q)
            reserved = max(0, int(inv.reserved_quantity or 0))
            if q < reserved:
                return Response(
                    {
                        'error': (
                            f'quantity ({q}) cannot be less than reserved_quantity ({reserved}); '
                            'wait for pickups/expiry or adjust reservations.'
                        ),
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            inv.quantity = q
            patched.append('quantity')

        if 'low_stock_threshold' in data:
            try:
                th = max(1, int(data['low_stock_threshold']))
                inv.low_stock_threshold = th
                patched.append('low_stock_threshold')
            except (TypeError, ValueError):
                return Response(
                    {'error': 'low_stock_threshold must be a positive integer'}, status=status.HTTP_400_BAD_REQUEST
                )

        if 'price' in data and data['price'] is not None and str(data['price']).strip() != '':
            try:
                price_val = float(data['price'])
                if price_val < 0:
                    price_val = 0
                inv.price = price_val
                patched.append('price')
            except (TypeError, ValueError):
                return Response({'error': 'Invalid price'}, status=status.HTTP_400_BAD_REQUEST)

        if 'new_medicine_name' in data:
            raw_new = data.get('new_medicine_name')
            candidate = (raw_new or '').strip()
            if candidate:
                new_normalized = candidate.lower()
                # Canonical storage matches bulk POST (lowercase).
                if (inv.medicine_name or '') != new_normalized:
                    conflict = PharmacyInventory.objects.filter(
                        pharmacy=pharmacy, medicine_name__iexact=new_normalized
                    ).exclude(pk=inv.pk).first()
                    if conflict:
                        return Response(
                            {
                                'error': (
                                    f'Inventory already has a row for "{conflict.medicine_name}". '
                                    'Remove or rename that row first.'
                                ),
                            },
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    Reservation.objects.filter(
                        pharmacy=pharmacy,
                        medicine_name__iexact=inv.medicine_name,
                    ).update(medicine_name=new_normalized)
                    inv.medicine_name = new_normalized
                    patched.append('medicine_name')

        if not patched:
            return Response(
                {
                    'error': (
                        'Provide at least one of: quantity, low_stock_threshold, price, '
                        'new_medicine_name (non-empty string)'
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        inv.save()
        payload = {
            'message': 'Inventory row updated',
            'patched_fields': patched,
            **_pharmacy_inventory_payload(pharmacy),
        }
        return Response(payload, status=status.HTTP_200_OK)

    if request.method == 'DELETE':
        medicine_name = (
            (request.query_params.get('medicine_name') or '').strip()
            or ((request.data.get('medicine_name') or '').strip() if isinstance(request.data, dict) else '')
        )
        return _delete_pharmacy_inventory_row_response(pharmacy, medicine_name)

    # POST - optional delete alias, else bulk inventory upsert
    post_data = request.data if isinstance(request.data, dict) else {}
    if (post_data.get('action') or '').strip().lower() == 'delete':
        medicine_name = (post_data.get('medicine_name') or '').strip()
        return _delete_pharmacy_inventory_row_response(pharmacy, medicine_name)

    # POST - update inventory (bulk)
    items_data = request.data.get('items', [])
    if not items_data:
        return Response({'error': 'items array is required'}, status=status.HTTP_400_BAD_REQUEST)

    updated = []
    for item in items_data:
        medicine_name = (item.get('medicine_name') or '').strip()
        if not medicine_name:
            continue
        quantity = max(0, int(item.get('quantity', 0)))
        existing = PharmacyInventory.objects.filter(pharmacy=pharmacy, medicine_name__iexact=medicine_name).first()
        reserved = max(0, int(existing.reserved_quantity or 0)) if existing else 0
        if quantity < reserved:
            return Response(
                {
                    'error': (
                        f'quantity for "{medicine_name}" ({quantity}) cannot be below reserved_quantity ({reserved}).'
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
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
            'quantity': quantity,
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

    payload = {
        'message': 'Inventory updated',
        'updated_count': len(updated),
        'updated_items': updated,
        **_pharmacy_inventory_payload(pharmacy),
    }
    return Response(payload, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([AllowAny])
def pharmacist_reservations_list(request):
    """
    GET: List reservations for the pharmacist's pharmacy.

    Query:
    - pharmacist_id (required): pharmacist UUID
    - scope=active (default): only pending/confirmed that have not expired (for counter/pickup workflow)
    - scope=recent: last `limit` reservations for this pharmacy (any status), newest first — for history / debugging
    - limit: max rows when scope=recent (default 50, max 200)
    - include_meta=1: include counts so an empty active list is explainable (all expired? none ever?)
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
    scope = (request.query_params.get('scope') or 'active').strip().lower()
    try:
        lim = int(request.query_params.get('limit', 50))
    except (TypeError, ValueError):
        lim = 50
    lim = max(1, min(lim, 200))

    if scope == 'recent':
        pending = (
            Reservation.objects.filter(pharmacy=pharmacy)
            .select_related('conversation')
            .order_by('-reserved_at')[:lim]
        )
    elif scope == 'active':
        pending = (
            Reservation.objects.filter(
                pharmacy=pharmacy,
                status__in=['pending', 'confirmed'],
                expires_at__gt=now,
            )
            .select_related('conversation')
            .order_by('reserved_at')
        )
    else:
        return Response(
            {'error': 'scope must be "active" (default) or "recent"'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    out = []
    _res_cache = {}
    for r in pending:
        patient_name, patient_location = _reservation_patient_name_and_location(
            r, _res_cache, reverse_geocode_if_needed=False,
        )
        out.append({
            'reservation_id': str(r.reservation_id),
            'medicine_name': r.medicine_name,
            'quantity': r.quantity,
            'price_at_reservation': str(r.price_at_reservation) if r.price_at_reservation else None,
            'status': r.status,
            'reserved_at': r.reserved_at.isoformat(),
            'expires_at': r.expires_at.isoformat(),
            'patient_name': patient_name,
            'patient_phone': r.patient_phone or '',
            'patient_location': patient_location,
        })
    payload = {
        'pharmacy_id': pharmacy.pharmacy_id,
        'scope': scope,
        'reservations': out,
    }
    if request.query_params.get('include_meta') in ('1', 'true', 'yes'):
        from django.db.models import Count

        base = Reservation.objects.filter(pharmacy=pharmacy)
        total = base.count()
        by_status = dict(
            base.values('status').annotate(n=Count('reservation_id')).values_list('status', 'n')
        )
        non_expired_active = base.filter(
            status__in=['pending', 'confirmed'],
            expires_at__gt=now,
        ).count()
        payload['meta'] = {
            'total_reservations': total,
            'by_status': by_status,
            'active_non_expired_pending_or_confirmed': non_expired_active,
            'active_filter': 'pending or confirmed, expires_at > now',
        }
        if scope == 'active' and not out and total > 0:
            payload['meta']['hint'] = (
                'Some reservations exist but none match the active filter (e.g. expired, picked_up, '
                'cancelled). Try ?scope=recent to list history.'
            )
        elif scope == 'active' and total == 0:
            payload['meta']['hint'] = (
                'No reservations in the database for this pharmacy yet. Patients create them via '
                'POST /api/chatbot/reserve/ with this pharmacy_id after choosing stock.'
            )
    return Response(payload, status=status.HTTP_200_OK)


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
        reservation.save()
        return Response({'error': 'Reservation has expired'}, status=status.HTTP_400_BAD_REQUEST)
    reservation.status = 'confirmed'
    reservation.confirmed_at = timezone.now()
    reservation.save()
    return Response({
        'success': True,
        'reservation_id': str(reservation.reservation_id),
        'status': reservation.status,
        'confirmed_at': reservation.confirmed_at.isoformat() if reservation.confirmed_at else None,
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
        reservation.save()
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
        reservation.save()

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
    Body: ``pharmacy_id``, ``medicine_name`` (optional if ``conversation_id`` + suggested_medicines),
    ``quantity``, ``conversation_id`` / ``session_id``, optional ``patient_name`` / ``patient_phone``.
    Optionally ``request_id`` / ``medicine_request_id`` (MedicineRequest UUID) to tie reservations to the
    broadcast → patient GETs return ``reservations[]`` and pharmacist confirm updates propagate after refresh.

    Duplicate **active** reservation (broadcast + pharmacy + SKU when linked; else conversation/session slice):
    **409 Conflict** with existing identifiers.
    Concurrency-safe: ``select_for_update`` on inventory; duplicate probe uses locking row queryset.
    """
    from django.db import transaction
    pharmacy_id = request.data.get('pharmacy_id')
    medicine_name = (request.data.get('medicine_name') or '').strip()
    quantity = max(1, int(request.data.get('quantity', 1)))
    conversation_id = request.data.get('conversation_id')
    session_id = (request.data.get('session_id') or '').strip()
    patient_name = (request.data.get('patient_name') or '').strip()
    patient_phone = (request.data.get('patient_phone') or '').strip()
    raw_mr = request.data.get('medicine_request_id') or request.data.get('request_id')

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
                session_id = (conversation.session_id or '').strip()
            # Derive medicine_name from conversation if not provided (e.g. frontend only sent pharmacy_id + conversation_id)
            if not medicine_name and conversation.context_metadata:
                suggested = conversation.context_metadata.get('suggested_medicines') or []
                if isinstance(suggested, list) and len(suggested) > 0:
                    first = suggested[0]
                    medicine_name = (first if isinstance(first, str) else str(first)).strip()
        except ChatConversation.DoesNotExist:
            pass

    medicine_req = None
    if raw_mr:
        try:
            medicine_req = MedicineRequest.objects.select_related('conversation').get(request_id=raw_mr)
        except MedicineRequest.DoesNotExist:
            return Response({'error': 'Medicine request not found'}, status=status.HTTP_404_NOT_FOUND)
        mr_conv = medicine_req.conversation
        sess_match = bool(session_id and mr_conv.session_id == session_id)
        conv_match = bool(conversation and mr_conv.pk == conversation.pk)
        if not sess_match and not conv_match:
            return Response(
                {'error': 'Medicine request does not match conversation_id or session_id'},
                status=status.HTTP_403_FORBIDDEN,
            )
        conversation = mr_conv

    if not patient_name and session_id:
        profile = PatientProfile.objects.filter(session_id=session_id).first()
        if profile:
            patient_name = (profile.display_name or '').strip()
            if not patient_phone:
                patient_phone = (profile.phone or '').strip()

    if not medicine_name:
        return Response(
            {'error': 'medicine_name is required. Include it in the request body, or ensure the conversation has suggested_medicines (e.g. from the last search).'},
            status=status.HTTP_400_BAD_REQUEST
        )

    sku_canon = _resolve_pharmacy_inventory_sku(pharmacy, medicine_name)
    if not sku_canon:
        return Response(
            {
                'error': (
                    f'Medicine "{medicine_name}" not found at this pharmacy. '
                    'Check spelling matches your stock list or use the name shown in ranked results.'
                ),
            },
            status=status.HTTP_404_NOT_FOUND,
        )

    with transaction.atomic():
        inv_qs = PharmacyInventory.objects.filter(
            pharmacy=pharmacy,
            medicine_name=sku_canon,
        ).select_for_update()
        inv = inv_qs.first()
        if not inv:
            return Response(
                {'error': f'Medicine "{medicine_name}" is no longer listed at this pharmacy.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        sku_key = inv.medicine_name
        expires_at = timezone.now() + timedelta(hours=2)
        now_ts = timezone.now()

        dup_filters = dict(
            pharmacy=pharmacy,
            medicine_name__iexact=sku_key,
            status__in=('pending', 'confirmed'),
            expires_at__gt=now_ts,
        )
        dup_q = Reservation.objects.select_for_update().filter(**dup_filters)
        if medicine_req:
            dup_q = dup_q.filter(medicine_request=medicine_req)
        else:
            if conversation:
                dup_q = dup_q.filter(conversation=conversation)
            elif session_id:
                dup_q = dup_q.filter(session_id=session_id)
            else:
                dup_q = Reservation.objects.none()
        dup = dup_q.first()
        if dup:
            return Response(
                {
                    'error': 'An active reservation already exists for this pharmacy and medicine.',
                    'reservation_id': str(dup.reservation_id),
                    'medicine_request_id': str(dup.medicine_request_id) if dup.medicine_request_id else None,
                    'request_id': str(dup.medicine_request_id) if dup.medicine_request_id else None,
                    'pharmacy_id': dup.pharmacy.pharmacy_id if dup.pharmacy_id else pharmacy_id,
                    'pharmacy_name': dup.pharmacy.name if dup.pharmacy else pharmacy.name,
                    'medicine_name': dup.medicine_name,
                    'quantity': dup.quantity,
                    'status': dup.status,
                    'expires_at': dup.expires_at.isoformat(),
                    'confirmed_at': dup.confirmed_at.isoformat() if dup.confirmed_at else None,
                },
                status=status.HTTP_409_CONFLICT,
            )

        available = inv.quantity - inv.reserved_quantity
        if available < quantity:
            return Response(
                {'error': f'Only {available} available (you requested {quantity})'},
                status=status.HTTP_400_BAD_REQUEST
            )

        res_session_id = session_id or (medicine_req.conversation.session_id if medicine_req else '') or str(uuid.uuid4())
        reservation = Reservation.objects.create(
            pharmacy=pharmacy,
            medicine_request=medicine_req,
            conversation=conversation or (medicine_req.conversation if medicine_req else None),
            session_id=res_session_id,
            patient_name=patient_name,
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
        'medicine_request_id': str(medicine_req.request_id) if medicine_req else None,
        'request_id': str(medicine_req.request_id) if medicine_req else None,
        'status': reservation.status,
        'pharmacy_id': pharmacy.pharmacy_id,
        'pharmacy_name': pharmacy.name,
        'medicine_name': inv.medicine_name,
        'quantity': quantity,
        'expires_at': expires_at.isoformat(),
        'confirmed_at': reservation.confirmed_at.isoformat() if reservation.confirmed_at else None,
        'message': f'Reservation confirmed. Please pick up within 2 hours. {quantity} x {inv.medicine_name} at {pharmacy.name}.',
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


def _ranked_api_cache_fingerprint(medicine_request) -> str:
    """
    Cheap invalidation for GET .../ranked/ polling — when these change, recompute rankings.
    Includes latest inventory touch for requested medicine names so live-stock rows refresh.
    """
    from django.db.models import Count, Max, Q, F

    pr_agg = PharmacyResponse.objects.filter(request=medicine_request).aggregate(
        c=Count('response_id'),
        m=Max('submitted_at'),
    )
    dc = PharmacistDecline.objects.filter(request=medicine_request).count()
    m = pr_agg['m']

    names = [
        str(x).strip().lower()
        for x in (medicine_request.medicine_names or [])
        if x and str(x).strip()
    ][:14]
    inv_t = None
    if names:
        med_q = Q()
        for n in names:
            med_q |= Q(medicine_name__iexact=n)
        row = PharmacyInventory.objects.filter(
            med_q,
            quantity__gt=F('reserved_quantity'),
            pharmacy__is_active=True,
            pharmacy__verification_status='verified',
        ).aggregate(mx=Max('updated_at'))
        inv_t = row.get('mx')

    since = timezone.now() - medicine_request.created_at
    ranking_gate = '1' if since >= timedelta(minutes=RANKING_DELAY_MINUTES) else '0'

    res_mr = Reservation.objects.filter(medicine_request=medicine_request).aggregate(mx=Max('updated_at')).get('mx')
    res_leg_mx = None
    if medicine_request.conversation_id:
        res_leg_mx = (
            Reservation.objects.filter(
                conversation=medicine_request.conversation,
                medicine_request__isnull=True,
            )
            .aggregate(mx=Max('updated_at'))
            .get('mx')
        )
    res_stamps = [x for x in (res_mr, res_leg_mx) if x is not None]
    res_sync = max(res_stamps).isoformat() if res_stamps else ''

    return '|'.join(
        [
            'v6',
            str(pr_agg['c']),
            m.isoformat() if m else '',
            str(dc),
            inv_t.isoformat() if inv_t else '',
            ranking_gate,
            res_sync,
        ]
    )


@api_view(['GET'])
@permission_classes([AllowAny])
def get_ranked_responses(request, request_id):
    """
    Get ranked pharmacy responses for a medicine request
    Ranking based on: availability, total time (prep + travel), price, distance
    Returns top 3 by default, or specify limit query parameter

    SECURITY: Requires conversation_id or session_id to verify ownership
    Each request can only be accessed by the user who created it.

    Responses are cached briefly (default ~22s, ``RANKED_API_CACHE_SECONDS``) keyed by pharmacy
    response count/timestamps, declines, latest matching inventory timestamps, and reservation
    activity so aggressive client polling hits cache when nothing meaningful changed. Bypass with
    ``skip_ranked_cache=true``.

    Query ``envelope=true`` returns JSON with ``results``/``items``, ``reservations`` (active pickup
    snapshots for this broadcast), ``meta``, and ``meta['drug_interactions']`` (embedded rules-based
    DDI hints for multiple medicines).

    Flat array mode returns only pharmacy rows unless ``include_drug_interactions=true`` is passed,
    in which case the response is ``{ \"items\", \"drug_interactions\", \"count\" }`` instead.
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
        envelope = request.query_params.get('envelope', '').lower() in ('1', 'true', 'yes')
        skip_cache = request.query_params.get('skip_ranked_cache', '').lower() in ('1', 'true', 'yes')
        include_ddi_flat = request.query_params.get('include_drug_interactions', '').lower() in (
            '1', 'true', 'yes',
        ) and not envelope

        fingerprint = _ranked_api_cache_fingerprint(medicine_request)
        cache_key = (
            f'ranked_api:v7:{medicine_request.request_id}:{limit}:{fingerprint}:'
            f'{1 if envelope else 0}:{1 if include_ddi_flat else 0}'
        )

        if not skip_cache:
            cached = cache.get(cache_key)
            if cached is not None:
                shown_rows, pickups = _decorate_ranked_with_pickups(medicine_request, cached)
                ddi_blob = _ddi_for_medicine_request_model(medicine_request)
                if envelope:
                    return Response(
                        {
                            'results': shown_rows,
                            'items': shown_rows,
                            'count': len(shown_rows),
                            'reservations': pickups,
                            'meta': {
                                'scoring': 'mcda_live_inventory_mixed',
                                'limit': limit,
                                'ranking_note': (
                                    'Higher ranking_score = better. Pharmacist rows: MCDA vs peers on this '
                                    'request. Live rows: blended score vs other live branches. Mixed list '
                                    'is sorted by score; rank is recomputed 1..limit.'
                                ),
                                'drug_interactions': ddi_blob,
                            },
                        },
                        status=status.HTTP_200_OK,
                    )
                if include_ddi_flat:
                    return Response(
                        {
                            'items': shown_rows,
                            'count': len(shown_rows),
                            'drug_interactions': ddi_blob,
                        },
                        status=status.HTTP_200_OK,
                    )
                return Response(shown_rows, status=status.HTTP_200_OK)

        _rank_note = (
            'Higher ranking_score is better where present. Pharmacist replies: chronological for the '
            f'first ~{RANKING_DELAY_MINUTES} minutes after request (ranking_pending), then multi-criteria '
            'MCDA within availability groups vs other replies on this request. Live-stock-only rows '
            'use an internal blended score. The merged list is sorted globally by numeric ranking_score; '
            'rank is recomputed from 1 to limit.'
        )

        payload_plain = build_merged_ranked_rows_for_request(medicine_request, limit=limit)
        cache_payload = [{**dict(r)} for r in payload_plain]
        persist_medicine_request_ranking_snapshot(
            medicine_request, cache_payload, 'ranked_api', limit_applied=limit,
        )

        if not skip_cache:
            ttl = int(getattr(settings, 'RANKED_API_CACHE_SECONDS', 22) or 22)
            ttl = max(5, min(ttl, 90))
            cache.set(cache_key, cache_payload, timeout=ttl)

        shown_rows, pickups = _decorate_ranked_with_pickups(medicine_request, payload_plain)
        ddi_blob = _ddi_for_medicine_request_model(medicine_request)

        if envelope:
            return Response(
                {
                    'results': shown_rows,
                    'items': shown_rows,
                    'count': len(shown_rows),
                    'reservations': pickups,
                    'meta': {
                        'scoring': 'mcda_live_inventory_mixed',
                        'limit': limit,
                        'ranking_delay_minutes': RANKING_DELAY_MINUTES,
                        'ranking_note': _rank_note,
                        'drug_interactions': ddi_blob,
                    },
                },
                status=status.HTTP_200_OK,
            )

        if include_ddi_flat:
            return Response(
                {
                    'items': shown_rows,
                    'count': len(shown_rows),
                    'drug_interactions': ddi_blob,
                },
                status=status.HTTP_200_OK,
            )

        return Response(shown_rows, status=status.HTTP_200_OK)
    
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


@csrf_exempt
@api_view(['POST'])
@authentication_classes([])
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

        if (
            getattr(pharmacist, 'mfa_totp_enabled', False)
            and (getattr(pharmacist, 'mfa_totp_secret', '') or '').strip()
            and pharmacist.user
        ):
            from .email_service import create_totp_login_challenge

            ch = create_totp_login_challenge(
                user_id=pharmacist.user.pk,
                kind='pharmacist',
                pharmacist_id=str(pharmacist.pharmacist_id),
            )
            return Response(_second_factor_login_pending_payload(ch, via_email=False), status=status.HTTP_200_OK)

        if getattr(settings, 'LOGIN_EMAIL_2FA', False):
            dest = (pharmacist.email or (pharmacist.user.email if pharmacist.user else '') or '').strip()
            if dest and pharmacist.user:
                from .email_service import create_email_login_challenge

                ch = create_email_login_challenge(
                    user_id=pharmacist.user.pk,
                    kind='pharmacist',
                    email=dest,
                    pharmacist_id=str(pharmacist.pharmacist_id),
                )
                return Response(
                    _second_factor_login_pending_payload(ch, via_email=True),
                    status=status.HTTP_200_OK,
                )
            print('[INFO] LOGIN_EMAIL_2FA is on but pharmacist has no email; completing login without OTP.')

        # Return pharmacist information
        pharmacist_serializer = PharmacistSerializer(pharmacist)
        payload = {'pharmacist': pharmacist_serializer.data, 'message': 'Login successful'}
        payload.update(pharmacist_jwt_tokens_or_empty(pharmacist))
        return Response(payload, status=status.HTTP_200_OK)
    
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


def _get_or_create_pharmacy_settings(pharmacist: Pharmacist) -> PharmacySettings:
    defaults = {
        'branch_name': '',
        'city': '',
        'geo_region': '',
        'opening_hours': {},
        'timezone': 'Africa/Harare',
        'holiday_mode': False,
        'accepting_requests': True,
        'pause_outside_opening_hours': False,
        'auto_accept_reservations': False,
        'max_reservation_window_minutes': 120,
        'low_stock_threshold_default': 5,
        'out_of_stock_behavior': 'hide',
        'auto_substitute_enabled': False,
        'notify_new_request': True,
        'notify_low_stock': True,
        'notify_reservation_expiry': True,
        'notify_channel_sms': False,
        'notify_channel_email': True,
        'notify_channel_in_app': True,
        'notify_quiet_hours': {},
        'notifications_digest_frequency': 'instant',
        'preferred_profile': '',
        'service_radius_km': None,
        'service_pickup_available': True,
        'service_delivery_available': False,
        'service_areas_covered': [],
        'disclaimer_visible': True,
        'prescription_enforcement': False,
        'audit_logging_enabled': True,
        'ui_dark_mode': False,
        'ui_table_density': 'comfortable',
        'ui_default_page_size': 25,
        'ui_default_filters': {},
        'updated_by': pharmacist.email or pharmacist.full_name,
    }
    s, _ = PharmacySettings.objects.get_or_create(pharmacy=pharmacist.pharmacy, defaults={**defaults, 'pharmacist': pharmacist})
    return s


@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def pharmacist_settings(request):
    """
    GET/PATCH ``/api/chatbot/pharmacist/settings/``

    Requires ``Authorization: Bearer <access>`` from pharmacist login. Optional ``pharmacist_id``
    query/body must match the authenticated pharmacist.

    GET returns nested envelope: ``profile``, ``operations``, ``notifications``, ``service``,
    ``preferences``, ``meta``.

    PATCH: send partial sub-objects, e.g. ``{"operations": {"accepting_requests": false}}``.
    Optional ``expect_version`` aligns with ``meta.settings_version``.
    """
    pharmacist, err = resolve_authenticated_pharmacist(request)
    if err:
        return err
    settings_obj = _get_or_create_pharmacy_settings(pharmacist)

    if request.method == 'GET':
        return Response(serialize_pharmacist_settings_envelope(pharmacist, settings_obj), status=status.HTTP_200_OK)

    payload = request.data if isinstance(request.data, dict) else {}
    expect_version = payload.get('expect_version')
    patch_body = dict(payload)
    patch_body.pop('expect_version', None)
    patch_body.pop('pharmacist_id', None)

    if expect_version is not None:
        try:
            if int(expect_version) != int(settings_obj.version or 1):
                return Response(
                    {
                        'error': 'Version conflict',
                        'code': 'settings_version_conflict',
                        'current_version': settings_obj.version,
                        'updated_at': settings_obj.updated_at,
                    },
                    status=status.HTTP_409_CONFLICT,
                )
        except (TypeError, ValueError):
            return Response({'error': 'expect_version must be an integer'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        changelog = patch_pharmacist_settings_envelope(pharmacist, settings_obj, patch_body)
    except ValueError as exc:
        return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    if changelog:
        PharmacySettingsHistory.objects.create(
            settings=settings_obj,
            changed_by=settings_obj.updated_by or (pharmacist.email or ''),
            action='patch',
            payload={'changed_subsections': changelog, 'version': settings_obj.version},
        )

    return Response(serialize_pharmacist_settings_envelope(pharmacist, settings_obj), status=status.HTTP_200_OK)


@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def pharmacist_profile_settings(request):
    """``profile`` JSON object (same nested shape as pharmacist/settings GET). Bearer auth."""
    pharmacist, err = resolve_authenticated_pharmacist(request)
    if err:
        return err
    settings_obj = _get_or_create_pharmacy_settings(pharmacist)
    if request.method == 'GET':
        env = serialize_pharmacist_settings_envelope(pharmacist, settings_obj)
        return Response(env['profile'], status=status.HTTP_200_OK)

    wrapper = {'profile': request.data if isinstance(request.data, dict) else {}}
    try:
        changelog = patch_pharmacist_settings_envelope(pharmacist, settings_obj, wrapper)
    except ValueError as exc:
        return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    if changelog:
        PharmacySettingsHistory.objects.create(
            settings=settings_obj,
            changed_by=settings_obj.updated_by or (pharmacist.email or ''),
            action='patch',
            payload={'changed_subsections': changelog, 'version': settings_obj.version},
        )
    env = serialize_pharmacist_settings_envelope(pharmacist, settings_obj)
    return Response(env['profile'], status=status.HTTP_200_OK)


@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def pharmacist_security_settings(request):
    """Security prefs (audit). Password: ``POST pharmacist/password/change/``."""
    pharmacist, err = resolve_authenticated_pharmacist(request)
    if err:
        return err
    s = _get_or_create_pharmacy_settings(pharmacist)
    if request.method == 'GET':
        return Response(
            {
                'pharmacist_id': pharmacist.pharmacist_id,
                'mfa_totp_enabled': bool(getattr(pharmacist, 'mfa_totp_enabled', False)),
                'password_last_changed_at': pharmacist.updated_at,
                'audit_logging_enabled': s.audit_logging_enabled,
            },
            status=status.HTTP_200_OK,
        )

    data = request.data if isinstance(request.data, dict) else {}
    if 'audit_logging_enabled' in data:
        s.audit_logging_enabled = _coerce_bool(data.get('audit_logging_enabled'))
    s.pharmacist = pharmacist
    s.version = (s.version or 1) + 1
    s.updated_by = pharmacist.email or pharmacist.full_name
    s.save()
    return Response({'message': 'Security settings updated'}, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def pharmacist_password_change(request):
    """
    POST /api/chatbot/pharmacist/password/change/

    JSON: ``current_password`` (or legacy ``old_password``), ``new_password``.
    """
    pharmacist, err = resolve_authenticated_pharmacist(request)
    if err:
        return err
    if not pharmacist.user:
        return Response({'error': 'Pharmacist account is not linked to Django user'}, status=status.HTTP_400_BAD_REQUEST)
    body = request.data if isinstance(request.data, dict) else {}
    cur = body.get('current_password') if body.get('current_password') is not None else body.get('old_password')
    new_pw = body.get('new_password')
    if cur is None or new_pw is None:
        return Response({'error': 'current_password and new_password are required'}, status=status.HTTP_400_BAD_REQUEST)
    if len(str(new_pw)) < 8:
        return Response({'error': 'new_password must be at least 8 characters'}, status=status.HTTP_400_BAD_REQUEST)
    if not pharmacist.user.check_password(str(cur)):
        return Response({'error': 'Invalid current_password'}, status=status.HTTP_400_BAD_REQUEST)
    pharmacist.user.set_password(str(new_pw))
    pharmacist.user.save(update_fields=['password'])
    pharmacist.save(update_fields=['updated_at'])
    out = {'message': 'Password updated'}
    out.update(pharmacist_jwt_tokens_or_empty(pharmacist))
    return Response(out, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def pharmacist_settings_test_notification(request):
    pharmacist, err = resolve_authenticated_pharmacist(request)
    if err:
        return err
    s = _get_or_create_pharmacy_settings(pharmacist)
    channels = []
    if s.notify_channel_sms:
        channels.append('sms')
    if s.notify_channel_email:
        channels.append('email')
    if s.notify_channel_in_app:
        channels.append('in_app')
    return Response({
        'message': 'Test notification dispatched (simulated)',
        'channels': channels or ['in_app'],
        'dispatched_at': timezone.now().isoformat(),
    }, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def pharmacist_settings_history(request):
    pharmacist, err = resolve_authenticated_pharmacist(request)
    if err:
        return err
    s = _get_or_create_pharmacy_settings(pharmacist)
    limit = min(max(int(request.query_params.get('limit', 30)), 1), 200)
    rows = s.history.all()[:limit]
    return Response(PharmacySettingsHistorySerializer(rows, many=True).data, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def pharmacist_settings_reset(request):
    pharmacist, err = resolve_authenticated_pharmacist(request)
    if err:
        return err
    s = _get_or_create_pharmacy_settings(pharmacist)
    defaults = PharmacySettings()
    reset_fields = [
        'opening_hours', 'timezone', 'holiday_mode',
        'accepting_requests', 'pause_outside_opening_hours',
        'auto_accept_reservations', 'max_reservation_window_minutes',
        'low_stock_threshold_default', 'out_of_stock_behavior', 'auto_substitute_enabled',
        'notify_new_request', 'notify_low_stock', 'notify_reservation_expiry',
        'notify_channel_sms', 'notify_channel_email', 'notify_channel_in_app',
        'notify_quiet_hours', 'notifications_digest_frequency',
        'preferred_profile',
        'service_radius_km', 'service_pickup_available', 'service_delivery_available', 'service_areas_covered',
        'disclaimer_visible', 'prescription_enforcement', 'audit_logging_enabled',
        'ui_dark_mode', 'ui_table_density', 'ui_default_page_size', 'ui_default_filters',
    ]
    for f in reset_fields:
        setattr(s, f, getattr(defaults, f))
    s.pharmacist = pharmacist
    s.version = (s.version or 1) + 1
    s.updated_by = pharmacist.email or pharmacist.full_name
    s.save()
    PharmacySettingsHistory.objects.create(
        settings=s,
        changed_by=s.updated_by,
        action='reset',
        payload={'version': s.version},
    )
    return Response(
        serialize_pharmacist_settings_envelope(pharmacist, s),
        status=status.HTTP_200_OK,
    )


@api_view(['GET'])
@permission_classes([AllowAny])
def pharmacist_ranking_summary(request, pharmacist_id):
    """
    GET /api/chatbot/pharmacist/<pharmacist_id>/ranking-summary/
    Composite 0–100 score + component %s + full leaderboard (all active pharmacies) + your rank.
    Query: history_limit (default 60, max 500) — number of past score snapshots to return (oldest→newest).

    Each GET persists a snapshot when composite or rank changes so score_history stays accurate.

    Clients should show the current algorithm from top-level ``formula`` and ``composite_weights`` (admin MCDA).
    Do not use ``score_history[].formula`` for the live formula text — those rows may still store older
    snapshot strings from before a scoring upgrade.
    """
    from .pharmacy_portal_ranking import pharmacist_ranking_summary_payload

    try:
        pharmacist = Pharmacist.objects.select_related('pharmacy').get(
            pharmacist_id=pharmacist_id, is_active=True,
        )
    except Pharmacist.DoesNotExist:
        return Response({'error': 'Pharmacist not found'}, status=status.HTTP_404_NOT_FOUND)
    if not pharmacist.pharmacy:
        return Response({'error': 'Pharmacist has no pharmacy'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        hl = int(request.query_params.get('history_limit', 60))
    except (TypeError, ValueError):
        hl = 60
    hl = max(1, min(hl, 500))
    cache_key = f"pharmacist_ranking_summary:v2:{pharmacist.pharmacy_id}:{hl}"
    cached = cache.get(cache_key)
    if cached is not None:
        return Response(cached, status=status.HTTP_200_OK)
    payload = pharmacist_ranking_summary_payload(pharmacist, history_limit=hl)
    cache.set(cache_key, payload, timeout=30)
    return Response(payload, status=status.HTTP_200_OK)


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
    
    # Create pharmacy (self-service: pending admin verification before patient network)
    pharmacy = Pharmacy.objects.create(
        pharmacy_id=data['pharmacy_id'],
        name=data['name'],
        address=data['address'],
        latitude=latitude,
        longitude=longitude,
        phone=data.get('phone', ''),
        email=data.get('email', ''),
        is_active=True,
        verification_status='pending_review',
    )
    
    serializer_response = PharmacySerializer(pharmacy)
    return Response({
        'message': (
            'Pharmacy registered successfully. An administrator must verify your listing '
            'before you can view patient requests and appear in search results.'
        ),
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


@csrf_exempt
@api_view(['POST'])
@authentication_classes([])  # DRF SessionAuthentication enforces CSRF on POST; bypass for SPA login
@permission_classes([AllowAny])
def patient_login(request):
    """
    POST /api/chatbot/patient/login/
    Body: { "email": "...", "password": "..." }
    Authenticates a Django user and returns the PatientProfile session_id used by ?session_id=... APIs.
    The user must already exist (e.g. created via your signup flow). Anonymous-only patients should keep using
    POST /api/chatbot/register/patient/ without a password.
    """
    email = (request.data.get('email') or '').strip().lower()
    password = request.data.get('password') or ''
    if not email or not password:
        return Response(
            {'error': 'email and password are required'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    from django.contrib.auth import authenticate, get_user_model

    User = get_user_model()
    candidates = list(User.objects.filter(email__iexact=email).order_by('date_joined', 'pk'))
    if not candidates:
        return Response({'error': 'Invalid credentials'}, status=status.HTTP_401_UNAUTHORIZED)

    user = None
    if len(candidates) == 1:
        user = authenticate(request, username=candidates[0].username, password=password)
    else:
        for u in candidates:
            authenticated = authenticate(request, username=u.username, password=password)
            if authenticated is not None:
                user = authenticated
                break
    if user is None:
        return Response({'error': 'Invalid credentials'}, status=status.HTTP_401_UNAUTHORIZED)
    if not user.is_active:
        return Response({'error': 'Account disabled'}, status=status.HTTP_403_FORBIDDEN)

    prof_mfa = PatientProfile.objects.filter(user=user).first()
    if (
        prof_mfa
        and getattr(prof_mfa, 'mfa_totp_enabled', False)
        and (getattr(prof_mfa, 'mfa_totp_secret', '') or '').strip()
    ):
        from .email_service import create_totp_login_challenge

        ch = create_totp_login_challenge(user_id=user.pk, kind='patient', pharmacist_id=None)
        return Response(_second_factor_login_pending_payload(ch, via_email=False), status=status.HTTP_200_OK)

    if getattr(settings, 'LOGIN_EMAIL_2FA', False):
        dest = (getattr(user, 'email', None) or '').strip()
        if dest:
            from .email_service import create_email_login_challenge

            ch = create_email_login_challenge(
                user_id=user.pk,
                kind='patient',
                email=dest,
                pharmacist_id=None,
            )
            return Response(
                _second_factor_login_pending_payload(ch, via_email=True),
                status=status.HTTP_200_OK,
            )
        print('[INFO] LOGIN_EMAIL_2FA is on but patient user has no email; completing login without OTP.')

    return Response(_patient_login_response_payload(user, email), status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([AllowAny])
def list_pharmacies(request):
    """
    List active pharmacies approved for the patient network.
    """
    pharmacies = Pharmacy.objects.filter(
        is_active=True, verification_status='verified',
    ).order_by('name')
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


def _admin_user_payload(user):
    return {
        'id': str(user.pk) if user.pk is not None else None,
        'username': user.username,
        'email': getattr(user, 'email', '') or '',
        'is_staff': user.is_staff,
        'is_superuser': user.is_superuser,
    }


def _patient_login_response_payload(user, email_fallback=''):
    """JSON body for successful patient login (session_id + profile); does not call Django login()."""
    profile = PatientProfile.objects.filter(user=user).first()
    email_fallback = (email_fallback or '').strip()[:254]
    if not profile:
        session_id = 'session_' + uuid.uuid4().hex
        profile = PatientProfile.objects.create(
            session_id=session_id,
            user=user,
            email=(user.email or email_fallback)[:254],
        )
    else:
        session_id = profile.session_id
        if not session_id:
            session_id = 'session_' + uuid.uuid4().hex
            profile.session_id = session_id
            profile.save(update_fields=['session_id'])
    return {
        'message': 'Login successful',
        'session_id': session_id,
        'profile': {
            'display_name': profile.display_name,
            'email': profile.email,
            'phone': profile.phone,
            'date_of_birth': str(profile.date_of_birth) if profile.date_of_birth else None,
            'home_area': profile.home_area,
            'preferred_language': profile.preferred_language,
        },
    }


def _resolve_user_for_password_reset(email: str, account_type: str):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    e = email.strip().lower()
    if account_type == 'admin':
        return User.objects.filter(email__iexact=e, is_staff=True).order_by('date_joined', 'pk').first()
    if account_type == 'pharmacist':
        ph = (
            Pharmacist.objects.filter(email__iexact=e, is_active=True)
            .select_related('user')
            .order_by('date_joined', 'pk')
            .first()
        )
        return ph.user if ph and ph.user else None
    for u in User.objects.filter(email__iexact=e).order_by('date_joined', 'pk'):
        if PatientProfile.objects.filter(user=u).exists():
            return u
    return User.objects.filter(email__iexact=e, is_staff=False).order_by('date_joined', 'pk').first()


def _location_text_from_request_fields(
    location_suburb,
    location_address,
    latitude,
    longitude,
    *,
    reverse_geocode_if_needed=True,
):
    """Build human-friendly area text from request location fields.

    When reverse_geocode_if_needed is False, never call external geocoders (dashboard / list APIs).
    """
    raw_area = location_suburb or location_address or ''
    needs_resolve = (
        latitude is not None
        and longitude is not None
        and (not raw_area or raw_area.startswith('Location:'))
    )
    if needs_resolve and reverse_geocode_if_needed:
        from .services import LocationService

        return LocationService.reverse_geocode(latitude, longitude, fallback=raw_area)
    if needs_resolve and not reverse_geocode_if_needed:
        latf = float(latitude)
        lonf = float(longitude)
        return raw_area if raw_area and not raw_area.startswith('Location:') else f'Location: {latf:.5f}, {lonf:.5f}'
    return raw_area


def _medicine_request_location_payload(req, *, reverse_geocode_if_needed=True):
    """Structured patient location for medicine-request API responses."""
    return {
        'address': req.location_address or '',
        'suburb': req.location_suburb or '',
        'latitude': req.location_latitude,
        'longitude': req.location_longitude,
        'text': _location_text_from_request_fields(
            req.location_suburb,
            req.location_address,
            req.location_latitude,
            req.location_longitude,
            reverse_geocode_if_needed=reverse_geocode_if_needed,
        ),
    }


def _reservation_patient_name_and_location(reservation, cache=None, *, reverse_geocode_if_needed=True):
    """
    Resolve patient_name and patient_location for a reservation.
    Sources:
    - reservation.patient_name
    - PatientProfile.display_name/home_area by session_id
    - latest MedicineRequest location by conversation/session

    Uses conversation_id / values_list instead of reservation.conversation to avoid
    an extra lazy Mongo fetch per row (and degrades gracefully on DatabaseError).
    """
    from django.db import DatabaseError

    cache = cache or {}
    conv_cache = cache.setdefault('conv_request', {})
    sess_cache = cache.setdefault('sess_request', {})
    profile_cache = cache.setdefault('profile', {})
    name_fallback = (getattr(reservation, 'patient_name', None) or '').strip()

    try:
        conv_fk = getattr(reservation, 'conversation_id', None)
        session_id = (reservation.session_id or '').strip()
        if not session_id and conv_fk:
            sid_row = (
                ChatConversation.objects.filter(pk=conv_fk)
                .values_list('session_id', flat=True)
                .first()
            )
            session_id = (sid_row or '').strip()

        conversation_id_key = str(conv_fk) if conv_fk else None

        profile = None
        if session_id:
            if session_id in profile_cache:
                profile = profile_cache[session_id]
            else:
                profile = PatientProfile.objects.filter(session_id=session_id).first()
                profile_cache[session_id] = profile

        patient_name = (reservation.patient_name or '').strip()
        if not patient_name and profile:
            patient_name = (profile.display_name or '').strip()

        req = None
        if conv_fk and conversation_id_key:
            if conversation_id_key in conv_cache:
                req = conv_cache[conversation_id_key]
            else:
                req = (
                    MedicineRequest.objects.filter(conversation_id=conv_fk)
                    .order_by('-created_at')
                    .first()
                )
                conv_cache[conversation_id_key] = req
        elif session_id:
            if session_id in sess_cache:
                req = sess_cache[session_id]
            else:
                req = (
                    MedicineRequest.objects.filter(conversation__session_id=session_id)
                    .order_by('-created_at')
                    .first()
                )
                sess_cache[session_id] = req

        patient_location = ''
        if req:
            patient_location = _location_text_from_request_fields(
                req.location_suburb,
                req.location_address,
                req.location_latitude,
                req.location_longitude,
                reverse_geocode_if_needed=reverse_geocode_if_needed,
            )
        if not patient_location and profile:
            patient_location = profile.home_area or ''

        return patient_name, patient_location
    except DatabaseError:
        return name_fallback, ''


def _city_from_address(address):
    if not address:
        return ''
    parts = [p.strip() for p in str(address).split(',') if p.strip()]
    if len(parts) >= 2:
        return parts[-1]
    return parts[0] if parts else ''


def _pharmacy_registry_pill_status(pharmacy):
    """Registry status pill: verified | pending_review | suspended."""
    if not pharmacy.is_active:
        return 'suspended'
    vs = getattr(pharmacy, 'verification_status', None) or 'verified'
    if vs == 'suspended':
        return 'suspended'
    if vs == 'pending_review':
        return 'pending_review'
    return 'verified'


def _pharmacy_may_serve_patients(pharmacy) -> bool:
    """Only active, registry-verified pharmacies appear to patients and may respond to requests."""
    if not pharmacy or not pharmacy.is_active:
        return False
    vs = (getattr(pharmacy, 'verification_status', None) or 'verified') or 'verified'
    return vs == 'verified'


def _pharmacy_accepts_patient_requests(pharmacy):
    """When False this branch is excluded from broadcasts / live-search / active pharmacist inbox."""
    if not pharmacy:
        return False
    s = PharmacySettings.objects.filter(pharmacy=pharmacy).only('accepting_requests').first()
    if s is None:
        return True
    return bool(getattr(s, 'accepting_requests', True))


def _coerce_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(value)


def _client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR') or ''


def _log_admin_action(request, action, target_type='', target_id='', detail=None, success=True):
    try:
        AdminAuditLog.objects.create(
            user=request.user if getattr(request, 'user', None) and request.user.is_authenticated else None,
            username=(
                request.user.get_username()
                if getattr(request, 'user', None) and request.user.is_authenticated
                else ''
            ),
            action=action,
            target_type=target_type,
            target_id=str(target_id)[:500] if target_id is not None else '',
            success=success,
            detail=detail or {},
            ip_address=_client_ip(request) or None,
        )
    except Exception as e:
        print(f'[WARN] AdminAuditLog failed: {e}')


def _second_factor_login_pending_payload(token: str, *, via_email: bool) -> dict:
    msg = (
        'Enter the verification code sent to your email.'
        if via_email
        else 'Enter the 6-digit code from your authenticator app.'
    )
    out = {
        'requires_mfa': True,
        'requires_otp': True,
        'mfa_required': True,
        'mfa_token': token,
        'mfa_challenge_token': token,
        'message': msg,
    }
    if via_email:
        out['needs_email_otp'] = True
        out['otp_challenge'] = token
    return out


def _auth_second_factor_complete(request, *, mfa_token: str, otp_code: str, user_type: str | None):
    """
    Finish login after password: device TOTP (totplogin:*) or email OTP (email2fa:*).
    user_type must match cache when provided (required for /auth/mfa/login/complete/).
    """
    import pyotp
    from django.contrib.auth import login, get_user_model
    from .email_service import (
        delete_email_login_challenge,
        delete_totp_login_challenge,
        get_email_login_challenge,
        get_totp_login_challenge,
    )

    token = str(mfa_token).strip()
    code = str(otp_code).strip()
    User = get_user_model()

    totp_data = get_totp_login_challenge(token)
    if totp_data:
        kind = totp_data.get('kind') or ''
        if user_type and kind != user_type:
            return Response({'error': 'Invalid or expired code'}, status=status.HTTP_401_UNAUTHORIZED)
        try:
            user = User.objects.get(pk=totp_data['user_id'])
        except (User.DoesNotExist, TypeError, ValueError):
            return Response({'error': 'Invalid or expired code'}, status=status.HTTP_401_UNAUTHORIZED)
        if not user.is_active:
            return Response({'error': 'Account disabled'}, status=status.HTTP_403_FORBIDDEN)

        if kind == 'patient':
            prof = PatientProfile.objects.filter(user=user).first()
            secret = (prof.mfa_totp_secret if prof else '') or ''
            if not prof or not prof.mfa_totp_enabled or not secret.strip():
                return Response({'error': 'Invalid or expired code'}, status=status.HTTP_401_UNAUTHORIZED)
            if not pyotp.TOTP(secret).verify(code, valid_window=1):
                return Response({'error': 'Invalid or expired code'}, status=status.HTTP_401_UNAUTHORIZED)
            delete_totp_login_challenge(token)
            return Response(_patient_login_response_payload(user, user.email or ''), status=status.HTTP_200_OK)

        if kind == 'pharmacist':
            pid = totp_data.get('pharmacist_id')
            ph = (
                Pharmacist.objects.filter(pharmacist_id=pid, is_active=True)
                .select_related('user', 'pharmacy')
                .first()
            )
            if not ph or not ph.user_id or ph.user_id != user.id:
                return Response({'error': 'Invalid or expired code'}, status=status.HTTP_401_UNAUTHORIZED)
            secret = (ph.mfa_totp_secret or '').strip()
            if not ph.mfa_totp_enabled or not secret:
                return Response({'error': 'Invalid or expired code'}, status=status.HTTP_401_UNAUTHORIZED)
            if not pyotp.TOTP(secret).verify(code, valid_window=1):
                return Response({'error': 'Invalid or expired code'}, status=status.HTTP_401_UNAUTHORIZED)
            delete_totp_login_challenge(token)
            pay = {'pharmacist': PharmacistSerializer(ph).data, 'message': 'Login successful'}
            pay.update(pharmacist_jwt_tokens_or_empty(ph))
            return Response(pay, status=status.HTTP_200_OK)

        return Response({'error': 'Invalid challenge'}, status=status.HTTP_400_BAD_REQUEST)

    email_data = get_email_login_challenge(token)
    if not email_data:
        return Response({'error': 'Invalid or expired code'}, status=status.HTTP_401_UNAUTHORIZED)
    kind = email_data.get('kind') or ''
    if user_type and kind != user_type:
        return Response({'error': 'Invalid or expired code'}, status=status.HTTP_401_UNAUTHORIZED)
    if str(email_data.get('code', '')) != code:
        return Response({'error': 'Invalid or expired code'}, status=status.HTTP_401_UNAUTHORIZED)
    try:
        user = User.objects.get(pk=email_data['user_id'])
    except (User.DoesNotExist, TypeError, ValueError):
        return Response({'error': 'Invalid or expired code'}, status=status.HTTP_401_UNAUTHORIZED)
    if not user.is_active:
        return Response({'error': 'Account disabled'}, status=status.HTTP_403_FORBIDDEN)

    if kind == 'admin':
        if not user.is_staff:
            return Response({'error': 'Admin access required'}, status=status.HTTP_403_FORBIDDEN)
        delete_email_login_challenge(token)
        login(request, user)
        return Response(
            {
                'message': 'Login successful',
                'user': _admin_user_payload(user),
                'csrfToken': get_token(request),
            },
            status=status.HTTP_200_OK,
        )
    if kind == 'patient':
        delete_email_login_challenge(token)
        return Response(_patient_login_response_payload(user, user.email or ''), status=status.HTTP_200_OK)
    if kind == 'pharmacist':
        pid = email_data.get('pharmacist_id')
        ph = (
            Pharmacist.objects.filter(pharmacist_id=pid, is_active=True)
            .select_related('user', 'pharmacy')
            .first()
        )
        if not ph or not ph.user_id or ph.user_id != user.id:
            return Response({'error': 'Invalid or expired code'}, status=status.HTTP_401_UNAUTHORIZED)
        delete_email_login_challenge(token)
        pay = {'pharmacist': PharmacistSerializer(ph).data, 'message': 'Login successful'}
        pay.update(pharmacist_jwt_tokens_or_empty(ph))
        return Response(pay, status=status.HTTP_200_OK)
    return Response({'error': 'Invalid challenge'}, status=status.HTTP_400_BAD_REQUEST)


@csrf_exempt
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def auth_verify_login_email_otp(request):
    """
    POST /api/chatbot/auth/login/verify-email-otp/
    Body: { "otp_challenge": "<uuid from login>", "code": "123456" }
    Completes LOGIN_EMAIL_2FA after admin / patient / pharmacist password step.
    """
    ser = EmailOtpVerifySerializer(data=request.data)
    if not ser.is_valid():
        return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
    challenge = str(ser.validated_data['otp_challenge']).strip()
    code = str(ser.validated_data['code']).strip()
    return _auth_second_factor_complete(request, mfa_token=challenge, otp_code=code, user_type=None)


@csrf_exempt
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def auth_mfa_login_complete(request):
    """
    POST /api/chatbot/auth/mfa/login/complete/
    Body: { "user_type": "patient"|"pharmacist"|"admin", "mfa_token": "...", "otp_code": "123456" }
    """
    ser = MfaLoginCompleteSerializer(data=request.data)
    if not ser.is_valid():
        return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
    return _auth_second_factor_complete(
        request,
        mfa_token=str(ser.validated_data['mfa_token']).strip(),
        otp_code=str(ser.validated_data['otp_code']).strip(),
        user_type=ser.validated_data['user_type'],
    )


@csrf_exempt
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def auth_password_reset_request(request):
    """
    POST /api/chatbot/auth/password-reset/request/
    Body: { "email": "...", "user_type": "admin"|"patient"|"pharmacist" } (account_type also accepted)
    """
    from .email_service import email_configured, set_password_reset_challenge

    ser = PasswordResetRequestSerializer(data=request.data)
    if not ser.is_valid():
        return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
    email = ser.validated_data['email'].strip()
    account_type = ser.validated_data['account_type']
    msg = 'If an account exists for this email, a reset code has been sent.'
    user = _resolve_user_for_password_reset(email, account_type)
    if user and getattr(user, 'email', None) and email_configured():
        try:
            set_password_reset_challenge(user_id=user.pk, email=user.email.strip(), account_type=account_type)
        except Exception as exc:
            print(f'[WARN] password reset email failed: {exc}')
    elif user and getattr(user, 'email', None) and not email_configured():
        print('[WARN] Password reset requested but email is not configured (EMAIL_HOST / DEFAULT_FROM_EMAIL).')
    return Response({'message': msg}, status=status.HTTP_200_OK)


@csrf_exempt
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def auth_password_reset_confirm(request):
    """
    POST /api/chatbot/auth/password-reset/confirm/
    Body: { "email", "user_type", "code", "new_password" } (account_type also accepted)
    """
    from django.contrib.auth import get_user_model
    from .email_service import verify_password_reset_code

    ser = PasswordResetConfirmSerializer(data=request.data)
    if not ser.is_valid():
        return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
    email = ser.validated_data['email'].strip()
    account_type = ser.validated_data['account_type']
    code = str(ser.validated_data['code']).strip()
    new_password = ser.validated_data['new_password']
    uid = verify_password_reset_code(email=email, account_type=account_type, code=code)
    if not uid:
        return Response({'error': 'Invalid or expired code'}, status=status.HTTP_400_BAD_REQUEST)
    User = get_user_model()
    user = User.objects.filter(pk=uid).first()
    if not user:
        return Response({'error': 'Invalid or expired code'}, status=status.HTTP_400_BAD_REQUEST)
    expected = _resolve_user_for_password_reset(email, account_type)
    if not expected or expected.pk != user.pk:
        return Response({'error': 'Invalid or expired code'}, status=status.HTTP_400_BAD_REQUEST)
    user.set_password(new_password)
    user.save(update_fields=['password'])
    return Response({'message': 'Password updated. You can sign in with your new password.'}, status=status.HTTP_200_OK)


@csrf_exempt
@api_view(['POST'])
@authentication_classes([])  # DRF SessionAuthentication enforces CSRF on POST; bypass for SPA login
@permission_classes([AllowAny])
def admin_login(request):
    """
    POST /api/chatbot/admin/login/
    Body: { "username": "...", "password": "..." } or { "email": "...", "password": "..." }
    Establishes Django session; use credentials/cookies on subsequent admin API calls.
    CSRF-exempt (bootstrap). Response includes csrfToken — send it as header X-CSRFToken on PATCH/POST/DELETE.
    """
    serializer = AdminLoginSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    from django.contrib.auth import authenticate, login, get_user_model

    User = get_user_model()
    username = (serializer.validated_data.get('username') or '').strip()
    email = (serializer.validated_data.get('email') or '').strip()
    password = serializer.validated_data['password']

    user = None
    if email and not username:
        # Same email can exist on more than one row (imports / legacy data); never use get().
        candidates = list(
            User.objects.filter(email__iexact=email).order_by('date_joined', 'pk')
        )
        if not candidates:
            return Response({'error': 'Invalid credentials'}, status=status.HTTP_401_UNAUTHORIZED)
        if len(candidates) == 1:
            username = candidates[0].username
        else:
            matched = []
            for u in candidates:
                authenticated = authenticate(request, username=u.username, password=password)
                if authenticated is not None:
                    matched.append(authenticated)
            if not matched:
                return Response({'error': 'Invalid credentials'}, status=status.HTTP_401_UNAUTHORIZED)
            staff_matches = [u for u in matched if u.is_staff]
            user = staff_matches[0] if staff_matches else matched[0]

    if not username and user is None:
        return Response({'error': 'username or email is required'}, status=status.HTTP_400_BAD_REQUEST)

    if user is None:
        user = authenticate(request, username=username, password=password)
    if user is None:
        return Response({'error': 'Invalid credentials'}, status=status.HTTP_401_UNAUTHORIZED)
    if not user.is_active:
        return Response({'error': 'Account disabled'}, status=status.HTTP_403_FORBIDDEN)
    if not user.is_staff:
        return Response({'error': 'Admin access required'}, status=status.HTTP_403_FORBIDDEN)

    if getattr(settings, 'LOGIN_EMAIL_2FA', False):
        dest = (getattr(user, 'email', None) or '').strip()
        if dest:
            from .email_service import create_email_login_challenge

            ch = create_email_login_challenge(
                user_id=user.pk,
                kind='admin',
                email=dest,
                pharmacist_id=None,
            )
            return Response(
                _second_factor_login_pending_payload(ch, via_email=True),
                status=status.HTTP_200_OK,
            )
        print('[INFO] LOGIN_EMAIL_2FA is on but staff user has no email; completing login without OTP.')

    login(request, user)
    return Response({
        'message': 'Login successful',
        'user': _admin_user_payload(user),
        'csrfToken': get_token(request),
    }, status=status.HTTP_200_OK)


@csrf_exempt
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def admin_logout(request):
    """POST /api/chatbot/admin/logout/ — clear Django session (CSRF-exempt for SPA convenience)."""
    from django.contrib.auth import logout

    logout(request)
    return Response({'message': 'Logged out'}, status=status.HTTP_200_OK)


@ensure_csrf_cookie
@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_me(request):
    """
    GET /api/chatbot/admin/me/ — current staff user (session).
    Ensures csrftoken cookie is set; use csrfToken in JSON for X-CSRFToken on mutating requests.
    """
    return Response({
        **_admin_user_payload(request.user),
        'csrfToken': get_token(request),
    }, status=status.HTTP_200_OK)


@ensure_csrf_cookie
@api_view(['GET'])
@permission_classes([AllowAny])
def admin_csrf_cookie(request):
    """
    GET /api/chatbot/admin/csrf/ — sets csrftoken cookie and returns csrfToken (before or after login).
    Call with credentials: 'include' if you already have a session cookie.
    """
    return Response({'detail': 'ok', 'csrfToken': get_token(request)}, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_dashboard_data(request):
    """
    GET /api/chatbot/admin/dashboard/data/?limit=50&verification_queue_limit=100

    Consolidated data source for admin dashboard:
    overview metrics + latest pharmacies/pharmacists/requests/reservations + verification_queue.

    Reverse geocoding (Nominatim) is disabled by default — it is sequential and can stall the
    request for minutes when ``limit`` is large. Pass ``include_geocode=true`` only when you need
    resolved place names on list rows (``patient_area``, ``lists.patient_requests[].location.text``,
    and reservation ``patient_location``).

    Frontend should keep ``limit`` modest unless geocoding is off.
    """
    limit = min(max(int(request.query_params.get('limit', 50)), 1), 200)
    vq_limit = min(max(int(request.query_params.get('verification_queue_limit', 100)), 1), 500)
    # Geocoding can be slow because it calls an external service per unique coordinate.
    # Keep dashboard fast by default; frontend can opt-in with include_geocode=true.
    include_geocode = request.query_params.get('include_geocode', 'false').lower() == 'true'
    dashboard_cache_key = f"admin_dashboard_data:v3:{limit}:{vq_limit}:{1 if include_geocode else 0}"
    cached = cache.get(dashboard_cache_key)
    if cached is not None:
        return Response(cached, status=status.HTTP_200_OK)

    verification_queue = build_verification_queue(vq_limit)

    pharmacies_qs = Pharmacy.objects.annotate(
        pharmacists_count=Count('pharmacists', distinct=True),
        reservations_count=Count('reservations', distinct=True),
        medicine_count=Count('inventory', distinct=True),
        inventory_last_updated=Max('inventory__updated_at'),
    ).order_by('-created_at')

    pharmacists_qs = Pharmacist.objects.select_related('pharmacy').annotate(
        responses_count=Count('responses', distinct=True),
        declines_count=Count('declined_requests', distinct=True),
    ).order_by('-created_at')

    requests_qs = MedicineRequest.objects.select_related('conversation').annotate(
        responses_count=Count('pharmacy_responses', distinct=True),
        declines_count=Count('pharmacist_declines', distinct=True),
    ).order_by('-created_at')

    reservations_qs = Reservation.objects.select_related('pharmacy').order_by('-reserved_at')

    pharmacies = []
    for p in pharmacies_qs[:limit]:
        last_sync = getattr(p, 'last_inventory_sync_at', None) or p.inventory_last_updated
        pill = _pharmacy_registry_pill_status(p)
        vs = getattr(p, 'verification_status', 'verified') or 'verified'
        pharmacies.append({
            'id': p.pharmacy_id,
            'pharmacy_id': p.pharmacy_id,
            'name': p.name,
            'city': _city_from_address(p.address),
            'address': p.address,
            'pharmacy_type': getattr(p, 'pharmacy_type', '') or '',
            'verification_status': vs,
            'status': pill,
            'is_verified': vs == 'verified' and p.is_active,
            'account_status': (
                'suspended' if (not p.is_active or vs == 'suspended') else (
                    'pending' if vs == 'pending_review' else 'active'
                )
            ),
            'medicine_count': p.medicine_count,
            'medicines_listed_count': p.medicine_count,
            'inventory_count': p.medicine_count,
            'last_sync_at': last_sync.isoformat() if last_sync else None,
            'match_rate': float(p.response_rate) if p.response_rate is not None else None,
            'phone': p.phone,
            'email': p.email,
            'is_active': p.is_active,
            'rating': p.rating,
            'rating_count': p.rating_count,
            'response_rate': p.response_rate,
            'pharmacists_count': p.pharmacists_count,
            'reservations_count': p.reservations_count,
            'created_at': p.created_at.isoformat() if p.created_at else None,
        })

    reg_total = Pharmacy.objects.count()
    reg_verified = Pharmacy.objects.filter(
        is_active=True, verification_status='verified',
    ).count()
    reg_pending = Pharmacy.objects.filter(
        is_active=True, verification_status='pending_review',
    ).count()
    reg_suspended = Pharmacy.objects.filter(
        Q(is_active=False) | Q(verification_status='suspended'),
    ).count()
    registry_summary = {
        'total_registered': reg_total,
        'verified': reg_verified,
        'pending_review': reg_pending,
        'suspended': reg_suspended,
    }

    pharmacists = [{
        'pharmacist_id': str(ph.pharmacist_id),
        'full_name': ph.full_name,
        'first_name': ph.first_name,
        'last_name': ph.last_name,
        'email': ph.email,
        'phone': ph.phone,
        'license_number': ph.license_number,
        'is_active': ph.is_active,
        'pharmacy_id': ph.pharmacy.pharmacy_id if ph.pharmacy else None,
        'pharmacy_name': ph.pharmacy.name if ph.pharmacy else None,
        'responses_count': ph.responses_count,
        'declines_count': ph.declines_count,
        'created_at': ph.created_at.isoformat() if ph.created_at else None,
    } for ph in pharmacists_qs[:limit]]

    # Precompute short patient area label for admin list.
    # Reverse geocoding is optional to avoid slow dashboard loads.
    from .services import LocationService
    _geo_cache = {}
    requests_data = []
    for req in requests_qs[:limit]:
        raw_area = req.location_suburb or req.location_address or ''
        # Many legacy rows store "Location: <lat>, <lon>" as address; prefer a real place name.
        patient_area = raw_area
        if include_geocode and (not patient_area or patient_area.startswith('Location:')) and req.location_latitude and req.location_longitude:
            geo_bucket_key = (
                f"{round(float(req.location_latitude), 4)},{round(float(req.location_longitude), 4)}"
            )
            if geo_bucket_key in _geo_cache:
                patient_area = _geo_cache[geo_bucket_key]
            else:
                patient_area = LocationService.reverse_geocode(
                    req.location_latitude, req.location_longitude, fallback=patient_area
                )
                _geo_cache[geo_bucket_key] = patient_area
        created_iso = req.created_at.isoformat() if req.created_at else None
        requests_data.append({
            'request_id': str(req.request_id),
            'session_id': req.conversation.session_id if req.conversation else None,
            'request_type': req.request_type,
            'medicine_names': req.medicine_names or [],
            'symptoms': req.symptoms or '',
            'patient_area': patient_area,
            'location': _medicine_request_location_payload(
                req, reverse_geocode_if_needed=include_geocode,
            ),
            'status': req.status,
            'responses_count': req.responses_count,
            'declines_count': req.declines_count,
            'created_at': created_iso,
            'submitted_at': created_iso,
            'expires_at': req.expires_at.isoformat() if req.expires_at else None,
        })

    reservations = []
    _res_cache = {}
    for r in reservations_qs[:limit]:
        patient_name, patient_location = _reservation_patient_name_and_location(
            r, _res_cache, reverse_geocode_if_needed=include_geocode,
        )
        reservations.append({
            'reservation_id': str(r.reservation_id),
            'pharmacy_id': r.pharmacy.pharmacy_id if r.pharmacy else None,
            'pharmacy_name': r.pharmacy.name if r.pharmacy else None,
            'session_id': r.session_id,
            'patient_name': patient_name,
            'patient_phone': r.patient_phone or '',
            'patient_location': patient_location,
            'medicine_name': r.medicine_name,
            'quantity': r.quantity,
            'status': r.status,
            'price_at_reservation': str(r.price_at_reservation) if r.price_at_reservation is not None else None,
            'reserved_at': r.reserved_at.isoformat() if r.reserved_at else None,
            'expires_at': r.expires_at.isoformat() if r.expires_at else None,
        })

    from django.contrib.auth import get_user_model
    _User = get_user_model()
    overview = {
        'registered_pharmacies': Pharmacy.objects.count(),
        'active_pharmacies': Pharmacy.objects.filter(is_active=True).count(),
        'registered_pharmacists': Pharmacist.objects.count(),
        'active_pharmacists': Pharmacist.objects.filter(is_active=True).count(),
        'total_patient_requests': MedicineRequest.objects.count(),
        'awaiting_responses_requests': MedicineRequest.objects.filter(status='awaiting_responses').count(),
        'completed_requests': MedicineRequest.objects.filter(status='completed').count(),
        'expired_or_timeout_requests': MedicineRequest.objects.filter(status__in=['expired', 'timeout']).count(),
        'total_reservations': Reservation.objects.count(),
        'active_reservations': Reservation.objects.filter(status__in=['pending', 'confirmed']).count(),
        'picked_up_reservations': Reservation.objects.filter(status='picked_up').count(),
        'expired_or_cancelled_reservations': Reservation.objects.filter(status__in=['expired', 'cancelled']).count(),
        'total_pharmacy_responses': PharmacyResponse.objects.count(),
        'total_declines': PharmacistDecline.objects.count(),
        'total_patients': ChatConversation.objects.values('session_id').distinct().count(),
        'total_users': _User.objects.count(),
    }

    nav_badges = compute_nav_badges()
    start_day = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    requests_today = MedicineRequest.objects.filter(created_at__gte=start_day).count()
    _ps = get_platform_settings()
    _avg_ms = compute_avg_first_response_ms(14)
    overview['requests_today'] = requests_today
    overview['daily_active_users'] = compute_daily_active_sessions()
    overview['active_users'] = overview['daily_active_users']
    overview['avg_response_time_ms'] = _avg_ms
    overview['avg_response_time_seconds'] = round(_avg_ms / 1000, 3) if _avg_ms is not None else None
    overview['uptime_percent'] = (
        float(_ps.reported_uptime_percent) if _ps.reported_uptime_percent is not None else None
    )
    overview['uptime_pct_this_month'] = overview['uptime_percent']
    overview['pharmacy_registry_counts'] = {
        'total_registered': reg_total,
        'verified': reg_verified,
        'pending_review': reg_pending,
        'suspended': reg_suspended,
    }

    reservations_by_status = dict(
        Reservation.objects.values('status').annotate(total=Count('reservation_id')).values_list('status', 'total')
    )
    requests_by_status = dict(
        MedicineRequest.objects.values('status').annotate(total=Count('request_id')).values_list('status', 'total')
    )
    top_pharmacies_by_reservations = list(
        Pharmacy.objects.annotate(total_reservations=Count('reservations', distinct=True))
        .filter(total_reservations__gt=0)
        .order_by('-total_reservations', 'name')
        .values('pharmacy_id', 'name', 'total_reservations')[:10]
    )
    top_pharmacists_by_responses = list(
        Pharmacist.objects.annotate(total_responses=Count('responses', distinct=True))
        .filter(total_responses__gt=0)
        .order_by('-total_responses', 'last_name', 'first_name')
        .values('pharmacist_id', 'first_name', 'last_name', 'email', 'total_responses')[:10]
    )

    payload = {
        'overview': overview,
        'registry': {
            'summary': registry_summary,
            'verification_queue': verification_queue,
            'pending_count': reg_pending,
        },
        'verification_queue': verification_queue,
        'verification_queue_results': {'items': verification_queue, 'results': verification_queue},
        'breakdown': {
            'requests_by_status': requests_by_status,
            'reservations_by_status': reservations_by_status,
            'top_pharmacies_by_reservations': top_pharmacies_by_reservations,
            'top_pharmacists_by_responses': top_pharmacists_by_responses,
            'pharmacy_registry': registry_summary,
            'verification_queue': verification_queue,
        },
        'lists': {
            'pharmacies': pharmacies,
            'pharmacists': pharmacists,
            'patient_requests': requests_data,
            'reservations': reservations,
            'verification_queue': verification_queue,
        },
        'nav_badges': nav_badges,
        'open_alerts_count': int(sum(nav_badges.values())),
        'meta': {
            'limit': limit,
            'verification_queue_limit': vq_limit,
        },
    }
    cache.set(dashboard_cache_key, payload, timeout=45)
    return Response(payload, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_medi_bot_overview(request):
    """
    GET /api/chatbot/admin/overview/medi-bot/
    Single payload for MediBot admin UI: layers 1–5 + search volume, alerts, match rates, registrations.
    """
    cache_key = "admin_medi_bot_overview:v1"
    cached = cache.get(cache_key)
    if cached is not None:
        return Response(cached, status=status.HTTP_200_OK)
    payload = build_medi_bot_overview()
    cache.set(cache_key, payload, timeout=60)
    return Response(payload, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([AllowAny])
def admin_health(request):
    """
    GET /api/chatbot/admin/health/
    Lightweight status: server time + alert counts (optional for header widgets; no auth).
    """
    badges = compute_nav_badges()
    return Response({
        'server_time_iso': timezone.now().isoformat(),
        'open_alerts_count': int(sum(badges.values())),
        'nav_badges': badges,
    }, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_dashboard_widgets(request):
    """
    GET /api/chatbot/admin/dashboard/widgets/
    System alerts (stuck requests + watchlist) and safety policies without the full medi-bot overview.
    Query: no_response_minutes (1–120, default 10) — threshold for "awaiting pharmacy response" alerts.
    """
    raw = request.query_params.get('no_response_minutes', '10')
    try:
        n = int(raw)
    except ValueError:
        n = 10
    n = min(max(n, 1), 120)
    cache_key = f"admin_dashboard_widgets:v1:{n}"
    cached = cache.get(cache_key)
    if cached is not None:
        return Response(cached, status=status.HTTP_200_OK)
    payload = build_admin_widgets_bundle(no_response_minutes=n)
    cache.set(cache_key, payload, timeout=20)
    return Response(payload, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_generate_report(request):
    """
    POST /api/chatbot/admin/reports/generate/
    Generate AI narrative text for admin PDF/report export flows.
    """
    serializer = AdminReportGenerateSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(
            {'error': 'Validation failed', 'details': serializer.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )

    data = serializer.validated_data
    chatbot_service = get_chatbot_service()
    if not chatbot_service:
        return Response({
            'error': (
                'AI service unavailable. Configure OPENROUTER_API_KEY or GEMINI_API_KEY.'
            )
        }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    try:
        narrative = chatbot_service.generate_admin_report_narrative(
            dashboard_snapshot=data.get('dashboard_snapshot') or {},
            report_type=data.get('report_type') or 'dashboard_summary',
            timeframe=data.get('timeframe') or 'last_30_days',
            tone=data.get('tone') or 'executive',
            custom_instruction=data.get('custom_instruction') or '',
        )
    except Exception as e:
        return Response(
            {'error': f'Failed to generate report narrative: {str(e)}'},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    _log_admin_action(request, 'admin_report_generate', 'platform', 'main', {
        'report_type': data.get('report_type') or 'dashboard_summary',
        'timeframe': data.get('timeframe') or 'last_30_days',
        'tone': data.get('tone') or 'executive',
    })
    return Response({
        'report_type': data.get('report_type') or 'dashboard_summary',
        'timeframe': data.get('timeframe') or 'last_30_days',
        'tone': data.get('tone') or 'executive',
        'format': 'markdown',
        'narrative': narrative,
        'narrative_markdown': narrative,
        'generated_at': timezone.now().isoformat(),
    }, status=status.HTTP_200_OK)


def _ranking_config_get_response():
    s = get_platform_settings()
    u_eff = resolve_platform_mcda_weights(3, s)
    r_eff = resolve_platform_mcda_weights(2, s)
    return {
        'active_ranking_profile': (s.active_ranking_profile or 'urban_default')[:64],
        'urban': weights_percent_display(u_eff),
        'rural': weights_percent_display(r_eff),
        'stored_raw': {
            'urban': s.ranking_weights_urban or {},
            'rural': s.ranking_weights_rural or {},
        },
        'profiles': RANKING_PROFILE_PRESETS,
    }


def _coerce_ranking_payload_pct(data):
    if not isinstance(data, dict):
        return None
    d = dict(data)
    if 'stock' in d and 'reliability' not in d:
        d['reliability'] = d['stock']
    keys = ('price', 'distance', 'rating', 'reliability')
    try:
        out = {k: int(round(float(d[k]))) for k in keys}
    except (KeyError, TypeError, ValueError):
        return None
    if abs(sum(out.values()) - 100) > 2:
        return None
    return out


def _coerce_from_standard_weights(sw) -> dict | None:
    """Map dashboard ``standard_weights`` (GET aliases) into urban/rural PATCH shape."""
    if not isinstance(sw, dict):
        return None
    d = {
        'price': sw.get('price_competitiveness_pct', sw.get('price')),
        'distance': sw.get('distance_travel_pct', sw.get('distance')),
        'rating': sw.get('patient_rating_pct', sw.get('rating')),
        'reliability': sw.get('stock_reliability_pct', sw.get('stock', sw.get('reliability'))),
    }
    return _coerce_ranking_payload_pct(d)


@api_view(['GET', 'PATCH'])
@permission_classes([IsAdminUser])
def admin_ranking_config(request):
    """
    GET/PATCH /api/chatbot/admin/ranking/config/
    Weights as integer percents (sum 100).

    PATCH body is partial. Valid keys (any combination):

    - ``active_ranking_profile`` (string): persist which ranking preset is live for stewardship / UI.
      May be sent **alone** (e.g. "Save profile only") without ``urban`` / ``rural`` / ``apply_preset``.
    - ``apply_preset`` (string id): copy built-in preset urban+rural weights and set profile to that id.
    - ``urban`` / ``rural``: objects with price, distance, rating, reliability (or stock) percents summing ~100.
    - ``standard_weights``: same four-fields shape as ``layer3_algorithm.standard_weights`` in the dashboard GET
      (e.g. ``price_competitiveness_pct`` …). Applied as **urban** weights when ``urban`` is omitted.

    If ``apply_preset`` is present but does not match a known preset, it is ignored and
    ``active_ranking_profile`` is still applied when provided.
    """
    if request.method == 'GET':
        return Response(_ranking_config_get_response(), status=status.HTTP_200_OK)

    s = get_platform_settings()
    body = request.data if isinstance(request.data, dict) else {}
    preset_applied = False
    preset_id = body.get('apply_preset')
    if preset_id:
        preset = next((p for p in RANKING_PROFILE_PRESETS if p['id'] == preset_id), None)
        if preset:
            s.ranking_weights_urban = dict(preset['urban'])
            s.ranking_weights_rural = dict(preset['rural'])
            s.active_ranking_profile = preset['id'][:64]
            preset_applied = True
    arp = body.get('active_ranking_profile')
    if arp is not None and not preset_applied:
        s.active_ranking_profile = str(arp).strip()[:64]
    u_patch = _coerce_ranking_payload_pct(body.get('urban'))
    if u_patch is None:
        u_patch = _coerce_from_standard_weights(body.get('standard_weights'))
    r_patch = _coerce_ranking_payload_pct(body.get('rural'))
    if u_patch:
        s.ranking_weights_urban = u_patch
    if r_patch:
        s.ranking_weights_rural = r_patch
    s.save()
    _log_admin_action(request, 'ranking_config_update', 'platform', 'main', {
        'profile': s.active_ranking_profile,
    })
    return Response(_ranking_config_get_response(), status=status.HTTP_200_OK)


@api_view(['GET', 'PATCH'])
@permission_classes([IsAdminUser])
def admin_chatbot_policy(request):
    """GET/PATCH /api/chatbot/admin/chatbot/policy/ — content safety toggles."""
    s = get_platform_settings()
    merged = merge_chatbot_policy(s)
    if request.method == 'GET':
        return Response(merged, status=status.HTTP_200_OK)
    incoming = request.data if isinstance(request.data, dict) else {}
    for k, v in DEFAULT_CHATBOT_POLICY.items():
        if k in incoming:
            merged[k] = bool(incoming[k])
    s.chatbot_policy = {k: merged[k] for k in DEFAULT_CHATBOT_POLICY}
    s.save()
    _log_admin_action(request, 'chatbot_policy_update', 'platform', 'main', merged)
    return Response(merged, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_analytics_geo_heatmap(request):
    """GET ?days=30"""
    days = min(max(int(request.query_params.get('days', 30)), 1), 366)
    return Response({'days': days, 'heatmap': build_geo_heatmap(days)}, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_metrics_sla(request):
    """GET ?days=14&target_ms=2000"""
    days = min(max(int(request.query_params.get('days', 14)), 1), 90)
    target_ms = min(max(int(request.query_params.get('target_ms', 2000)), 500), 60000)
    return Response({
        'days': days,
        'target_ms': target_ms,
        'sla_by_region': build_sla_by_region(days, target_ms),
    }, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_pharmacies_verification_queue(request):
    limit = min(max(int(request.query_params.get('limit', 100)), 1), 500)
    return Response({'items': build_verification_queue(limit)}, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_pharmacies_watchlist(request):
    limit = min(max(int(request.query_params.get('limit', 100)), 1), 500)
    return Response({'items': build_watchlist(limit)}, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_analytics_impact_equity(request):
    days = min(max(int(request.query_params.get('days', 90)), 7), 366)
    return Response(build_impact_equity(days), status=status.HTTP_200_OK)


@api_view(['GET', 'POST'])
@permission_classes([IsAdminUser])
def admin_chatbot_reviews(request):
    """
    GET paginated AI safety review queue. POST create manual flag.
    PATCH resolve: use admin_chatbot_review_resolve
    """
    if request.method == 'POST':
        cid = request.data.get('conversation_id')
        if not cid:
            return Response({'error': 'conversation_id required'}, status=status.HTTP_400_BAD_REQUEST)
        conv = get_object_or_404(ChatConversation, conversation_id=cid)
        mid = request.data.get('message_id')
        msg = None
        if mid:
            msg = get_object_or_404(ChatMessage, message_id=mid, conversation=conv)
        status_val = (request.data.get('status') or 'warning').lower()
        if status_val not in ('critical', 'warning', 'safe'):
            status_val = 'warning'
        rev = ChatbotSafetyReview.objects.create(
            conversation=conv,
            message=msg,
            status=status_val,
            patient_query=(request.data.get('patient_query') or '')[:8000],
            bot_response=(request.data.get('bot_response') or '')[:80000],
            notes=(request.data.get('notes') or '')[:2000],
        )
        _log_admin_action(request, 'chatbot_review_create', 'chatbot_safety', str(rev.review_id), {})
        return Response({'review_id': str(rev.review_id)}, status=status.HTTP_201_CREATED)

    page = max(int(request.query_params.get('page', 1)), 1)
    page_size = min(max(int(request.query_params.get('page_size', 25)), 1), 100)
    status_filter = (request.query_params.get('status') or '').strip().lower()
    open_only = request.query_params.get('open_only', 'true').lower() != 'false'
    qs = ChatbotSafetyReview.objects.select_related('conversation').order_by('-created_at')
    if open_only:
        qs = qs.filter(resolved_at__isnull=True)
    if status_filter in ('critical', 'warning', 'safe'):
        qs = qs.filter(status=status_filter)
    total = qs.count()
    offset = (page - 1) * page_size
    rows = qs[offset:offset + page_size]
    actions = ['escalate', 'approve', 'flag_unsafe']
    return Response({
        'total': total,
        'page': page,
        'page_size': page_size,
        'actions': actions,
        'results': [{
            'id': str(r.review_id),
            'status': r.status,
            'patient_query': r.patient_query,
            'bot_response': r.bot_response,
            'conversation_id': str(r.conversation_id),
            'message_id': str(r.message_id) if r.message_id else None,
            'notes': r.notes,
            'resolved_at': r.resolved_at.isoformat() if r.resolved_at else None,
            'created_at': r.created_at.isoformat() if r.created_at else None,
            'actions': actions,
        } for r in rows],
    }, status=status.HTTP_200_OK)


@api_view(['PATCH'])
@permission_classes([IsAdminUser])
def admin_chatbot_review_resolve(request, review_id):
    from uuid import UUID
    try:
        rid = UUID(str(review_id))
    except ValueError:
        return Response({'error': 'invalid review_id'}, status=status.HTTP_400_BAD_REQUEST)
    rev = get_object_or_404(ChatbotSafetyReview, review_id=rid)
    rev.resolved_at = timezone.now()
    if request.data.get('status') in ('critical', 'warning', 'safe'):
        rev.status = request.data['status']
    rev.save(update_fields=['resolved_at', 'status'])
    _log_admin_action(request, 'chatbot_review_resolve', 'chatbot_safety', str(rev.review_id), {})
    return Response({'review_id': str(rev.review_id), 'resolved_at': rev.resolved_at.isoformat()}, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_uptime_report(request):
    """POST body { uptime_percent: 99.9 } — manual dashboard uptime when not instrumented."""
    pct = request.data.get('uptime_percent')
    if pct is None:
        return Response({'error': 'uptime_percent required'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        v = Decimal(str(pct))
    except Exception:
        return Response({'error': 'invalid number'}, status=status.HTTP_400_BAD_REQUEST)
    if v < 0 or v > 100:
        return Response({'error': 'must be 0–100'}, status=status.HTTP_400_BAD_REQUEST)
    s = get_platform_settings()
    s.reported_uptime_percent = v
    s.save(update_fields=['reported_uptime_percent'])
    _log_admin_action(request, 'uptime_report', 'platform', 'main', {'uptime_percent': float(v)})
    return Response({'uptime_percent': float(v)}, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_create_pharmacy(request):
    """Create a pharmacy from admin dashboard."""
    serializer = PharmacyRegistrationSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({'error': 'Validation failed', 'details': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)

    data = serializer.validated_data
    if Pharmacy.objects.filter(pharmacy_id=data['pharmacy_id']).exists():
        return Response({'error': 'pharmacy_id already exists'}, status=status.HTTP_400_BAD_REQUEST)

    pharmacy = Pharmacy.objects.create(
        pharmacy_id=data['pharmacy_id'],
        name=data['name'],
        address=data['address'],
        latitude=data.get('latitude'),
        longitude=data.get('longitude'),
        phone=data.get('phone', ''),
        email=data.get('email', ''),
        verification_status='pending_review',
    )
    _log_admin_action(request, 'pharmacy.create', 'pharmacy', pharmacy.pharmacy_id, {'name': pharmacy.name})
    return Response(PharmacySerializer(pharmacy).data, status=status.HTTP_201_CREATED)


@api_view(['PATCH', 'PUT'])
@permission_classes([IsAdminUser])
def admin_update_pharmacy(request, pharmacy_id):
    """
    Edit pharmacy from admin dashboard.
    Includes registry status: verification_status (verified | pending_review | suspended) and is_active.
    For status-only updates you can also use PATCH .../pharmacies/<id>/status/
    """
    pharmacy = get_object_or_404(Pharmacy, pharmacy_id=pharmacy_id)
    allowed = {
        'name', 'address', 'latitude', 'longitude', 'phone', 'email', 'is_active',
        'pharmacy_type', 'verification_status', 'last_inventory_sync_at',
    }
    updates = {k: request.data[k] for k in allowed if k in request.data}
    if not updates:
        return Response({'error': 'No editable fields supplied'}, status=status.HTTP_400_BAD_REQUEST)

    for key, value in list(updates.items()):
        if key == 'is_active':
            updates[key] = _coerce_bool(value)
        if key == 'last_inventory_sync_at' and value not in (None, ''):
            if isinstance(value, str):
                try:
                    from django.utils.dateparse import parse_datetime
                    dt = parse_datetime(value)
                    if dt is None:
                        return Response({'error': 'Invalid last_inventory_sync_at'}, status=status.HTTP_400_BAD_REQUEST)
                    updates[key] = dt
                except Exception:
                    return Response({'error': 'Invalid last_inventory_sync_at'}, status=status.HTTP_400_BAD_REQUEST)
        if key == 'verification_status' and value not in ('verified', 'pending_review', 'suspended', None, ''):
            return Response({'error': 'Invalid verification_status'}, status=status.HTTP_400_BAD_REQUEST)

    for key, value in updates.items():
        setattr(pharmacy, key, value)
    pharmacy.save(update_fields=list(updates.keys()) + ['updated_at'])
    _log_admin_action(request, 'pharmacy.update', 'pharmacy', pharmacy_id, {'fields': list(updates.keys())})
    return Response(PharmacySerializer(pharmacy).data, status=status.HTTP_200_OK)


@api_view(['PATCH'])
@permission_classes([IsAdminUser])
def admin_patch_pharmacy_status(request, pharmacy_id):
    """
    PATCH /api/chatbot/admin/pharmacies/<pharmacy_id>/status/
    Body: { "verification_status": "verified" | "pending_review" | "suspended", "is_active": true/false }
    Either or both fields may be sent.
    """
    pharmacy = get_object_or_404(Pharmacy, pharmacy_id=pharmacy_id)
    data = request.data
    fields = []
    if 'verification_status' in data:
        v = data['verification_status']
        if v not in ('verified', 'pending_review', 'suspended'):
            return Response(
                {'error': 'Invalid verification_status. Use verified, pending_review, or suspended.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        pharmacy.verification_status = v
        fields.append('verification_status')
    if 'is_active' in data:
        pharmacy.is_active = _coerce_bool(data['is_active'])
        fields.append('is_active')
    if not fields:
        return Response(
            {'error': 'Send verification_status and/or is_active.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    pharmacy.save(update_fields=fields + ['updated_at'])
    _log_admin_action(
        request,
        'pharmacy.status',
        'pharmacy',
        pharmacy_id,
        {k: getattr(pharmacy, k) for k in fields},
    )
    return Response(PharmacySerializer(pharmacy).data, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_create_pharmacist(request):
    """Create pharmacist from admin dashboard."""
    pharmacy_id = request.data.get('pharmacy_id')
    if not pharmacy_id:
        return Response({'error': 'pharmacy_id is required'}, status=status.HTTP_400_BAD_REQUEST)

    pharmacy = get_object_or_404(Pharmacy, pharmacy_id=pharmacy_id)
    required = ['first_name', 'last_name', 'email']
    missing = [f for f in required if not request.data.get(f)]
    if missing:
        return Response({'error': f"Missing required fields: {', '.join(missing)}"}, status=status.HTTP_400_BAD_REQUEST)

    email = request.data.get('email', '').strip().lower()
    if Pharmacist.objects.filter(email=email).exists():
        return Response({'error': 'Pharmacist email already exists'}, status=status.HTTP_400_BAD_REQUEST)

    pharmacist = Pharmacist.objects.create(
        pharmacy=pharmacy,
        first_name=request.data.get('first_name', '').strip(),
        last_name=request.data.get('last_name', '').strip(),
        email=email,
        phone=request.data.get('phone', '').strip(),
        license_number=request.data.get('license_number', '').strip(),
        is_active=request.data.get('is_active', True),
    )
    return Response(PharmacistSerializer(pharmacist).data, status=status.HTTP_201_CREATED)


@api_view(['PATCH'])
@permission_classes([IsAdminUser])
def admin_update_pharmacist(request, pharmacist_id):
    """Edit pharmacist details from admin dashboard."""
    pharmacist = get_object_or_404(Pharmacist, pharmacist_id=pharmacist_id)
    allowed = {'first_name', 'last_name', 'email', 'phone', 'license_number', 'is_active', 'pharmacy_id'}
    updates = {k: request.data[k] for k in allowed if k in request.data}
    if not updates:
        return Response({'error': 'No editable fields supplied'}, status=status.HTTP_400_BAD_REQUEST)

    if 'pharmacy_id' in updates:
        pharmacist.pharmacy = get_object_or_404(Pharmacy, pharmacy_id=updates.pop('pharmacy_id'))

    if 'email' in updates:
        email = str(updates['email']).strip().lower()
        if Pharmacist.objects.exclude(pk=pharmacist.pk).filter(email=email).exists():
            return Response({'error': 'Pharmacist email already exists'}, status=status.HTTP_400_BAD_REQUEST)
        updates['email'] = email

    for key, value in updates.items():
        setattr(pharmacist, key, value)
    pharmacist.save()
    return Response(PharmacistSerializer(pharmacist).data, status=status.HTTP_200_OK)


@api_view(['PATCH'])
@permission_classes([IsAdminUser])
def admin_update_request_status(request, request_id):
    """Update patient request status from admin dashboard."""
    medicine_request = get_object_or_404(MedicineRequest, request_id=request_id)
    new_status = request.data.get('status')
    if not new_status:
        return Response({'error': 'status is required'}, status=status.HTTP_400_BAD_REQUEST)
    valid_statuses = {choice[0] for choice in MedicineRequest._meta.get_field('status').choices}
    if new_status not in valid_statuses:
        return Response({'error': f'Invalid status. Allowed: {sorted(valid_statuses)}'}, status=status.HTTP_400_BAD_REQUEST)

    medicine_request.status = new_status
    medicine_request.save(update_fields=['status'])
    _log_admin_action(request, 'request.status', 'medicine_request', str(request_id), {'status': new_status})
    return Response({
        'request_id': str(medicine_request.request_id),
        'status': medicine_request.status,
    }, status=status.HTTP_200_OK)


@api_view(['PATCH'])
@permission_classes([IsAdminUser])
def admin_update_reservation_status(request, reservation_id):
    """Update reservation status from admin dashboard."""
    reservation = get_object_or_404(Reservation, reservation_id=reservation_id)
    new_status = request.data.get('status')
    if not new_status:
        return Response({'error': 'status is required'}, status=status.HTTP_400_BAD_REQUEST)
    valid_statuses = {choice[0] for choice in Reservation._meta.get_field('status').choices}
    if new_status not in valid_statuses:
        return Response({'error': f'Invalid status. Allowed: {sorted(valid_statuses)}'}, status=status.HTTP_400_BAD_REQUEST)

    reservation.status = new_status
    reservation.save()
    return Response({
        'reservation_id': str(reservation.reservation_id),
        'status': reservation.status,
    }, status=status.HTTP_200_OK)


@api_view(['DELETE'])
@permission_classes([IsAdminUser])
def admin_delete_pharmacist(request, pharmacist_id):
    """Delete pharmacist from admin dashboard."""
    pharmacist = get_object_or_404(Pharmacist, pharmacist_id=pharmacist_id)

    if pharmacist.responses.exists() or pharmacist.declined_requests.exists():
        return Response({
            'error': 'Cannot delete pharmacist with request history. Set is_active=false instead.'
        }, status=status.HTTP_400_BAD_REQUEST)

    pharmacist.delete()
    return Response({'deleted': True, 'pharmacist_id': str(pharmacist_id)}, status=status.HTTP_200_OK)


@api_view(['DELETE'])
@permission_classes([IsAdminUser])
def admin_delete_pharmacy(request, pharmacy_id):
    """
    DELETE /api/chatbot/admin/pharmacies/<pharmacy_id>/delete/
    Removes the pharmacy. If it has related rows, pass ?force=true to cascade-delete
    (pharmacists, inventory, reservations, responses, ratings — Django FK CASCADE).
    """
    pharmacy = get_object_or_404(Pharmacy, pharmacy_id=pharmacy_id)
    force = str(request.query_params.get('force', '')).lower() in ('1', 'true', 'yes')

    linked = {
        'pharmacists': pharmacy.pharmacists.count(),
        'reservations': pharmacy.reservations.count(),
        'responses': pharmacy.responses.count(),
        'inventory': pharmacy.inventory.count(),
        'ratings': pharmacy.ratings.count(),
    }
    total_linked = sum(linked.values())

    if total_linked and not force:
        return Response(
            {
                'error': (
                    'Pharmacy has linked records. Repeat DELETE with query parameter force=true '
                    'to permanently remove the pharmacy and related data.'
                ),
                'linked_counts': linked,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        pharmacy.delete()
    _log_admin_action(
        request,
        'pharmacy.delete',
        'pharmacy',
        pharmacy_id,
        {'force': force, 'had_linked': total_linked > 0, 'linked_counts': linked},
    )
    return Response({'deleted': True, 'pharmacy_id': pharmacy_id}, status=status.HTTP_200_OK)


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


def _maps_search_query(*parts) -> str:
    """Single string for Google Maps search / deep links."""
    return ' '.join(str(p).strip() for p in parts if p and str(p).strip())


def _recommended_pickup_from_response(resp):
    """
    Build a pickup card payload from a PharmacyResponse (cheap listing / best-price heuristic).
    """
    if resp is None:
        return None
    ph = resp.pharmacy
    name = (ph.name if ph else None) or (getattr(resp, 'pharmacy_name', None) or '').strip()
    if not name:
        return None
    address = (ph.address if ph else '') or ''
    phone = (ph.phone if ph else '') or ''
    lat = float(ph.latitude) if ph and ph.latitude is not None else None
    lon = float(ph.longitude) if ph and ph.longitude is not None else None
    dist = float(resp.distance_km) if resp.distance_km is not None else None
    return {
        'pharmacy_id': str(ph.pharmacy_id) if ph else getattr(resp, 'pharmacy_id', None),
        'pharmacy_name': name,
        'address': address,
        'phone': phone,
        'latitude': lat,
        'longitude': lon,
        'distance_km': dist,
        'estimated_travel_time': getattr(resp, 'estimated_travel_time', None),
        'price': str(resp.price) if resp.price is not None else None,
        'medicine_available': getattr(resp, 'medicine_available', None),
        'maps_query': _maps_search_query(name, address),
        'rank': None,
        'ranking_pending': None,
    }


def _recommended_from_ranked_row(row):
    """Pickup payload from a row returned by get_ranked_pharmacy_responses (includes rank / pending)."""
    if not row or not isinstance(row, dict):
        return None
    pid = row.get('pharmacy_id')
    ph = None
    if pid:
        try:
            ph = Pharmacy.objects.get(pharmacy_id=pid)
        except Pharmacy.DoesNotExist:
            ph = None
    name = (ph.name if ph else None) or (row.get('pharmacy_name') or '').strip()
    if not name:
        return None
    address = (ph.address if ph else '') or ''
    phone = (ph.phone if ph else '') or ''
    lat = float(ph.latitude) if ph and ph.latitude is not None else None
    lon = float(ph.longitude) if ph and ph.longitude is not None else None
    dist = row.get('distance_km')
    if dist is not None:
        try:
            dist = float(dist)
        except (TypeError, ValueError):
            dist = None
    et = row.get('estimated_travel_time')
    price = row.get('price')
    if price is not None and price != '':
        price = str(price)
    return {
        'pharmacy_id': str(ph.pharmacy_id) if ph else (str(pid) if pid is not None else None),
        'pharmacy_name': name,
        'address': address,
        'phone': phone,
        'latitude': lat,
        'longitude': lon,
        'distance_km': dist,
        'estimated_travel_time': et,
        'price': price,
        'medicine_available': row.get('medicine_available'),
        'maps_query': _maps_search_query(name, address),
        'rank': row.get('rank'),
        'ranking_pending': row.get('ranking_pending'),
        'ranking_score': row.get('ranking_score'),
    }


def _reservation_dict_for_patient_pickup(reservation):
    """Fields expected by SPA pickup / banners (aligned with PATIENT_PICKUP_BACKEND_SPEC)."""
    if not reservation:
        return None
    ph = reservation.pharmacy
    mr_id = getattr(reservation, 'medicine_request_id', None)
    return {
        'reservation_id': str(reservation.reservation_id),
        'medicine_request_id': str(mr_id) if mr_id else None,
        'request_id': str(mr_id) if mr_id else None,
        'pharmacy_id': ph.pharmacy_id if ph else None,
        'pharmacy_name': ph.name if ph else '',
        'medicine_name': reservation.medicine_name,
        'quantity': reservation.quantity,
        'status': reservation.status,
        'price_at_reservation': str(reservation.price_at_reservation) if reservation.price_at_reservation is not None else None,
        'reserved_at': reservation.reserved_at.isoformat() if reservation.reserved_at else None,
        'expires_at': reservation.expires_at.isoformat() if reservation.expires_at else None,
        'confirmed_at': reservation.confirmed_at.isoformat() if reservation.confirmed_at else None,
    }


def _next_same_conversation_request_created_at_after(medicine_request):
    """Timestamp of next search after this broadcast, for legacy reservation attribution."""
    if not medicine_request or not getattr(medicine_request, 'conversation_id', None):
        return None
    return (
        MedicineRequest.objects.filter(conversation=medicine_request.conversation, created_at__gt=medicine_request.created_at)
        .order_by('created_at')
        .values_list('created_at', flat=True)
        .first()
    )


def _reservations_unscoped_for_request(medicine_request):
    """
    All reservations for this broadcast: FK-linked, else inferred from conversation timestamps (legacy rows).
    """
    if not medicine_request:
        return Reservation.objects.none()
    fk_qs = (
        Reservation.objects.filter(medicine_request=medicine_request)
        .select_related('pharmacy', 'medicine_request')
        .order_by('-reserved_at')
    )
    if fk_qs.exists():
        return fk_qs
    conv = medicine_request.conversation
    if not conv:
        return Reservation.objects.none()
    cutoff = _next_same_conversation_request_created_at_after(medicine_request)
    leg_qs = Reservation.objects.filter(
        conversation=conv,
        medicine_request__isnull=True,
        reserved_at__gte=medicine_request.created_at,
    ).select_related('pharmacy', 'medicine_request')
    if cutoff:
        leg_qs = leg_qs.filter(reserved_at__lt=cutoff)
    return leg_qs.order_by('-reserved_at')


def _active_reservations_for_request(medicine_request, *, now=None):
    """pending|confirmed non-expired for this broadcast (hints + lightweight ranked payloads)."""
    now = now or timezone.now()
    return _reservations_unscoped_for_request(medicine_request).filter(status__in=('pending', 'confirmed'), expires_at__gt=now)


def _reservations_payload_for_request(medicine_request):
    return [_reservation_dict_for_patient_pickup(r) for r in _reservations_unscoped_for_request(medicine_request)]


def _active_reservation_hints_by_pharmacy_id(medicine_request):
    """Map pharmacy_id str -> pickup hint dict for ranked pharmacy rows."""
    hints = {}
    for r in _active_reservations_for_request(medicine_request):
        if not r.pharmacy_id:
            continue
        pid = str(r.pharmacy.pharmacy_id)
        hints[pid] = {
            'has_reservation': True,
            'reservation_status': r.status,
            'reservation_id': str(r.reservation_id),
        }
    return hints


def _apply_reservation_hints_to_ranked(payload, hints_by_pid):
    for row in payload or []:
        if not isinstance(row, dict):
            continue
        pid = row.get('pharmacy_id')
        if pid is None:
            continue
        hid = hints_by_pid.get(str(pid))
        if not hid:
            continue
        row['has_reservation'] = hid.get('has_reservation', True)
        row['reservation_status'] = hid.get('reservation_status')
        row['reservation_id'] = hid.get('reservation_id')


def _decorate_ranked_with_pickups(medicine_request, ranked_rows_plain):
    """Copy ranked rows (dicts only), attach active reservation hints, return pickup snapshot list."""
    rows = []
    for r in ranked_rows_plain or []:
        rows.append(dict(r) if isinstance(r, dict) else r)
    _apply_reservation_hints_to_ranked(rows, _active_reservation_hints_by_pharmacy_id(medicine_request))
    pickups = [_reservation_dict_for_patient_pickup(r) for r in _active_reservations_for_request(medicine_request)]
    return rows, pickups


def _serialize_active_reservation(res):
    if res is None:
        return None
    ph = res.pharmacy
    name = ph.name if ph else ''
    addr = ph.address if ph else ''
    base = _reservation_dict_for_patient_pickup(res)
    if base is None:
        return None
    base['maps_query'] = _maps_search_query(name, addr)
    return base


def _latest_active_reservation_for_conversation(conversation):
    if not conversation:
        return None
    now = timezone.now()
    return (
        Reservation.objects.filter(
            conversation=conversation,
            status__in=('pending', 'confirmed'),
            expires_at__gt=now,
        )
        .select_related('pharmacy')
        .order_by('-reserved_at')
        .first()
    )


def _latest_active_reservation_for_medicine_request(medicine_request):
    """Most recent pending/confirmed non-expired row tied to this broadcast (FK or legacy window)."""
    return (
        _active_reservations_for_request(medicine_request)
        .order_by('-reserved_at')
        .select_related('pharmacy')
        .first()
    )


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
    active_statuses = (
        'broadcasting',
        'awaiting_responses',
        'responses_received',
        'ranking',
        'partial',
        'superseded',
    )
    active = requests_qs.filter(status__in=active_statuses).count()
    fulfilled = requests_qs.filter(status='completed').count()
    expired = requests_qs.filter(status__in=('expired', 'timeout')).count()
    superseded = requests_qs.filter(status='superseded').count()
    from django.db.models import Avg, Min
    best_prices = PharmacyResponse.objects.filter(request__in=requests_qs.filter(status='completed'), price__isnull=False).values('request').annotate(min_price=Min('price'))
    avg_savings = None  # placeholder: could compare min_price to a baseline
    return Response({
        'active_requests': active,
        'fulfilled_count': fulfilled,
        'expired_count': expired,
        'superseded_count': superseded,
        'avg_savings': avg_savings,
        'time_saved_hrs': None,
    }, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([AllowAny])
def patient_my_requests(request):
    """
    GET /api/chatbot/patient/requests/?session_id=... or ?conversation_id=...&status=all|active|fulfilled|expired
    List medicine requests for this patient with response count, best price, status,
    ``conversation_id``, ``recommended_pharmacy`` (address + ``maps_query``), and ``active_reservation`` when applicable.

    ``active`` includes ``superseded`` (patient opened a newer request before anyone replied to the older one)
    so earlier attempts stay visible instead of looking like they vanished.
    """
    session_id, err = _patient_session_from_request(request)
    if err:
        return err
    status_filter = request.query_params.get('status', 'all')
    conversations = ChatConversation.objects.filter(session_id=session_id).values_list('pk', flat=True)
    requests_qs = MedicineRequest.objects.filter(conversation_id__in=conversations).select_related('conversation').prefetch_related('pharmacy_responses').order_by('-created_at')
    if status_filter == 'active':
        requests_qs = requests_qs.filter(
            status__in=(
                'broadcasting',
                'awaiting_responses',
                'responses_received',
                'ranking',
                'partial',
                'superseded',
            )
        )
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
        best_resp = None
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
                best_resp = with_price
            else:
                best_resp = responses.order_by('created_at').first()
        pharmacy_names = list(responses.values_list('pharmacy__name', flat=True))[:3]
        pharmacy_names = [n for n in pharmacy_names if n]
        short_id = str(req.request_id).replace('-', '')[:8].upper()
        pv = _medicine_request_preview_fields(req)
        recommended_pharmacy = _recommended_pickup_from_response(best_resp)
        active_reservation = _serialize_active_reservation(
            _latest_active_reservation_for_medicine_request(req)
        )
        results.append({
            'request_id': str(req.request_id),
            'conversation_id': str(req.conversation.conversation_id),
            'short_request_id': short_id,
            'medicine_names': req.medicine_names or [],
            'symptoms': req.symptoms or '',
            'request_preview': pv['request_preview'],
            'is_symptom_request': pv['is_symptom_request'],
            'location': _medicine_request_location_payload(req),
            'location_address': req.location_address or req.location_suburb or '',
            'location_text': _location_text_from_request_fields(
                req.location_suburb,
                req.location_address,
                req.location_latitude,
                req.location_longitude,
            ),
            'location_latitude': req.location_latitude,
            'location_longitude': req.location_longitude,
            'location_suburb': req.location_suburb or '',
            'submitted_at': req.created_at.isoformat() if req.created_at else None,
            'response_count': response_count,
            'pharmacy_names': pharmacy_names,
            'best_price': best_price,
            'best_pharmacy_id': best_pharmacy_id,
            'best_pharmacy_name': best_pharmacy_name,
            'best_medicine_name': best_medicine_name,
            'recommended_pharmacy': recommended_pharmacy,
            'active_reservation': active_reservation,
            'reservations': _reservations_payload_for_request(req),
            'status': req.status,
        })
    return Response(results, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([AllowAny])
def patient_active_request(request):
    """
    GET /api/chatbot/patient/active-request/?session_id=... or ?conversation_id=...

    Returns the most recent *actionable* medicine request for this browser session (same active
    statuses as ``patient/requests/?status=active``), plus ``recommended_pharmacy`` and any
    non-expired reservation so lightweight / landing UIs can resume after refresh without a dashboard.
    """
    session_id, err = _patient_session_from_request(request)
    if err:
        return err
    conversations = ChatConversation.objects.filter(session_id=session_id).values_list('pk', flat=True)
    active_statuses = (
        'broadcasting',
        'awaiting_responses',
        'responses_received',
        'ranking',
        'partial',
        'superseded',
    )
    req = (
        MedicineRequest.objects.filter(conversation_id__in=conversations, status__in=active_statuses)
        .select_related('conversation')
        .order_by('-created_at')
        .first()
    )
    if not req:
        return Response(
            {
                'active': False,
                'session_id': session_id,
                'request_id': None,
                'conversation_id': None,
                'message': 'No active medicine request for this session.',
            },
            status=status.HTTP_200_OK,
        )

    short_id = str(req.request_id).replace('-', '')[:8].upper()
    pv = _medicine_request_preview_fields(req)
    ranked = get_ranked_pharmacy_responses(req, limit=5)
    persist_medicine_request_ranking_snapshot(
        req, ranked, 'patient_portal_active', limit_applied=5,
    )
    recommended = _recommended_from_ranked_row(ranked[0]) if ranked else None
    if recommended is None:
        responses = req.pharmacy_responses.all()
        br = None
        if responses.exists():
            wp = responses.filter(price__isnull=False).order_by('price').first()
            br = wp or responses.order_by('created_at').first()
        recommended = _recommended_pickup_from_response(br)
    active_reservation = _serialize_active_reservation(
        _latest_active_reservation_for_medicine_request(req)
    )
    pickup_reservations = _reservations_payload_for_request(req)
    ranking_pending = bool(ranked and ranked[0].get('ranking_pending'))

    return Response(
        {
            'active': True,
            'session_id': session_id,
            'request_id': str(req.request_id),
            'short_request_id': short_id,
            'conversation_id': str(req.conversation.conversation_id),
            'status': req.status,
            'medicine_names': req.medicine_names or [],
            'symptoms': req.symptoms or '',
            'request_preview': pv['request_preview'],
            'is_symptom_request': pv['is_symptom_request'],
            'location': _medicine_request_location_payload(req),
            'location_text': _location_text_from_request_fields(
                req.location_suburb,
                req.location_address,
                req.location_latitude,
                req.location_longitude,
            ),
            'response_count': req.pharmacy_responses.count(),
            'recommended_pharmacy': recommended,
            'active_reservation': active_reservation,
            'reservations': pickup_reservations,
            'ranking_pending': ranking_pending,
        },
        status=status.HTTP_200_OK,
    )


@api_view(['GET'])
@permission_classes([AllowAny])
def patient_request_detail(request, request_id):
    """
    GET /api/chatbot/patient/requests/<request_id>/?session_id=... or conversation_id=...
    Single request with ranked pharmacy responses, ``conversation_id``, ``recommended_pharmacy`` (rank #1),
    ``active_reservation`` for this broadcast’s latest active pickup row, ``reservations`` (history for
    this search, including pharmacist confirmations), and ``pharmacy_responses``.
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
    persist_medicine_request_ranking_snapshot(
        medicine_request, ranked, 'patient_portal', limit_applied=10,
    )
    recommended = _recommended_from_ranked_row(ranked[0]) if ranked else None
    if recommended is None:
        responses = medicine_request.pharmacy_responses.all()
        br = None
        if responses.exists():
            wp = responses.filter(price__isnull=False).order_by('price').first()
            br = wp or responses.order_by('created_at').first()
        recommended = _recommended_pickup_from_response(br)
    active_reservation = _serialize_active_reservation(
        _latest_active_reservation_for_medicine_request(medicine_request)
    )
    short_id = str(medicine_request.request_id).replace('-', '')[:8].upper()
    return Response({
        'request_id': str(medicine_request.request_id),
        'short_request_id': short_id,
        'conversation_id': str(medicine_request.conversation.conversation_id),
        'medicine_names': medicine_request.medicine_names or [],
        'symptoms': medicine_request.symptoms or '',
        'location': _medicine_request_location_payload(medicine_request),
        'location_address': medicine_request.location_address or medicine_request.location_suburb or '',
        'location_text': _location_text_from_request_fields(
            medicine_request.location_suburb,
            medicine_request.location_address,
            medicine_request.location_latitude,
            medicine_request.location_longitude,
        ),
        'location_latitude': medicine_request.location_latitude,
        'location_longitude': medicine_request.location_longitude,
        'location_suburb': medicine_request.location_suburb or '',
        'status': medicine_request.status,
        'submitted_at': medicine_request.created_at.isoformat() if medicine_request.created_at else None,
        'prescription_review': medicine_request.prescription_review_snapshot or {},
        'has_prescription_image': bool(medicine_request.prescription_image),
        'recommended_pharmacy': recommended,
        'active_reservation': active_reservation,
        'reservations': _reservations_payload_for_request(medicine_request),
        'pharmacy_responses': ranked,
    }, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_request_detail(request, request_id):
    """
    GET /api/chatbot/admin/requests/<request_id>/
    Full admin view of a patient medicine request:
    - What the patient requested (medicines, symptoms, location, timestamps).
    - What was broadcast and which pharmacies responded (ranked responses).
    - What the patient did after broadcast (reservations, ratings, notifications).
    """
    medicine_request = get_object_or_404(MedicineRequest.objects.select_related('conversation'), request_id=request_id)
    conversation = medicine_request.conversation
    session_id = conversation.session_id if conversation else None

    # Ranked pharmacy responses for this exact request.
    ranked = get_ranked_pharmacy_responses(medicine_request, limit=20)

    # If no direct responses exist yet, provide related ranked responses from
    # other requests in the same conversation/session so admin can still see
    # what happened after broadcast in this patient journey.
    related_ranked = []
    if not ranked:
        related_qs = MedicineRequest.objects.exclude(request_id=medicine_request.request_id)
        if conversation:
            related_qs = related_qs.filter(conversation=conversation)
        elif session_id:
            related_qs = related_qs.filter(conversation__session_id=session_id)
        else:
            related_qs = MedicineRequest.objects.none()
        related_qs = related_qs.order_by('-created_at')[:10]
        for rel_req in related_qs:
            rel_ranked = get_ranked_pharmacy_responses(rel_req, limit=10)
            if rel_ranked:
                related_ranked.append({
                    'request_id': str(rel_req.request_id),
                    'created_at': rel_req.created_at.isoformat() if rel_req.created_at else None,
                    'status': rel_req.status,
                    'medicine_names': rel_req.medicine_names or [],
                    'symptoms': rel_req.symptoms or '',
                    'responses': rel_ranked,
                })

    # Reservations linked to this conversation/session (patient follow-up)
    reservations_qs = Reservation.objects.all()
    if conversation:
        reservations_qs = reservations_qs.filter(conversation=conversation)
    elif session_id:
        reservations_qs = reservations_qs.filter(session_id=session_id)
    else:
        reservations_qs = Reservation.objects.none()
    reservations_qs = reservations_qs.select_related('pharmacy').order_by('-reserved_at')
    reservations = []
    _res_cache = {}
    for r in reservations_qs:
        patient_name, patient_location = _reservation_patient_name_and_location(r, _res_cache)
        reservations.append({
            'reservation_id': str(r.reservation_id),
            'pharmacy_id': r.pharmacy.pharmacy_id if r.pharmacy else None,
            'pharmacy_name': r.pharmacy.name if r.pharmacy else None,
            'medicine_name': r.medicine_name,
            'quantity': r.quantity,
            'status': r.status,
            'price_at_reservation': str(r.price_at_reservation) if r.price_at_reservation is not None else None,
            'patient_name': patient_name,
            'patient_phone': r.patient_phone or '',
            'patient_location': patient_location,
            'reserved_at': r.reserved_at.isoformat() if r.reserved_at else None,
            'expires_at': r.expires_at.isoformat() if r.expires_at else None,
            'confirmed_at': r.confirmed_at.isoformat() if r.confirmed_at else None,
            'picked_up_at': r.picked_up_at.isoformat() if r.picked_up_at else None,
            'cancelled_at': r.cancelled_at.isoformat() if r.cancelled_at else None,
        })

    # Ratings linked to this request (or related requests in same journey if direct is empty)
    rating_requests = [medicine_request]
    if related_ranked:
        related_ids = [item['request_id'] for item in related_ranked]
        rating_requests = list(MedicineRequest.objects.filter(request_id__in=related_ids)) + [medicine_request]
    ratings_qs = PharmacyRating.objects.filter(response__request__in=rating_requests).select_related('pharmacy', 'response')
    ratings = [{
        'pharmacy_id': pr.pharmacy.pharmacy_id if pr.pharmacy else None,
        'pharmacy_name': pr.pharmacy.name if pr.pharmacy else None,
        'response_id': str(pr.response.response_id) if pr.response else None,
        'rating': pr.rating,
        'notes': pr.notes,
        'created_at': pr.created_at.isoformat() if pr.created_at else None,
    } for pr in ratings_qs]

    # Notifications related to this request/patient journey (what the patient saw)
    notifications_qs = PatientNotification.objects.none()
    if session_id:
        notifications_qs = PatientNotification.objects.filter(session_id=session_id)
    else:
        notifications_qs = PatientNotification.objects.filter(related_request_id=medicine_request.request_id)
    notifications_qs = notifications_qs.order_by('-created_at')[:100]
    notifications = [{
        'id': n.id,
        'notification_type': n.notification_type,
        'title': n.title,
        'body': n.body,
        'related_response_id': str(n.related_response_id) if n.related_response_id else None,
        'read': n.read_at is not None,
        'read_at': n.read_at.isoformat() if n.read_at else None,
        'created_at': n.created_at.isoformat() if n.created_at else None,
    } for n in notifications_qs]

    # Human-readable patient area for admin detail
    from .services import LocationService
    raw_area = medicine_request.location_suburb or medicine_request.location_address or ''
    patient_area = raw_area
    if (not patient_area or patient_area.startswith('Location:')) and medicine_request.location_latitude and medicine_request.location_longitude:
        patient_area = LocationService.reverse_geocode(
            medicine_request.location_latitude,
            medicine_request.location_longitude,
            fallback=patient_area,
        )

    short_id = str(medicine_request.request_id).replace('-', '')[:8].upper()
    request_payload = {
        'request_id': str(medicine_request.request_id),
        'short_request_id': short_id,
        'session_id': session_id,
        'conversation_id': str(conversation.conversation_id) if conversation else None,
        'request_type': medicine_request.request_type,
        'medicine_names': medicine_request.medicine_names or [],
        'symptoms': medicine_request.symptoms or '',
        'location': _medicine_request_location_payload(medicine_request),
        'location_latitude': medicine_request.location_latitude,
        'location_longitude': medicine_request.location_longitude,
        'location_address': medicine_request.location_address or '',
        'location_suburb': medicine_request.location_suburb or '',
        'patient_area': patient_area,
        'status': medicine_request.status,
        'created_at': medicine_request.created_at.isoformat() if medicine_request.created_at else None,
        'expires_at': medicine_request.expires_at.isoformat() if medicine_request.expires_at else None,
        'prescription_review': medicine_request.prescription_review_snapshot or {},
        'has_prescription_image': bool(medicine_request.prescription_image),
    }

    snap_qs = medicine_request.ranking_snapshots.all().order_by('-created_at')[:40]
    ranking_snapshot_history = [{
        'snapshot_id': str(s.snapshot_id),
        'created_at': s.created_at.isoformat() if s.created_at else None,
        'source': s.source,
        'limit_applied': s.limit_applied,
        'ranked_responses': s.ranked_items,
    } for s in snap_qs]
    ranking_snapshots_total = medicine_request.ranking_snapshots.count()

    # Simple summary of follow-up actions for quick admin glance
    summary = {
        'responses_count': len(ranked),
        'related_requests_with_responses': len(related_ranked),
        'reservations_count': len(reservations),
        'ratings_count': len(ratings),
        'notifications_count': len(notifications),
        'ranking_snapshots_stored': ranking_snapshots_total,
    }

    return Response({
        'request': request_payload,
        'pharmacy_responses': ranked,
        'ranking_snapshot_history': ranking_snapshot_history,
        'related_ranked_responses': related_ranked,
        'reservations': reservations,
        'ratings': ratings,
        'notifications': notifications,
        'summary': summary,
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
@permission_classes([IsAdminUser])
def admin_patient_overview(request, session_id):
    """
    GET /api/chatbot/admin/patients/<session_id>/overview/
    Admin view of a patient's dashboard data (read-only):
    - Profile
    - Dashboard stats
    - Recent requests
    - Saved medicines
    - Notifications
    """
    # Profile (may not exist yet)
    profile = PatientProfile.objects.filter(session_id=session_id).first()
    profile_data = None
    if profile:
        profile_data = {
            'session_id': profile.session_id,
            'display_name': profile.display_name,
            'email': profile.email,
            'phone': profile.phone,
            'home_area': profile.home_area,
            'preferred_language': profile.preferred_language,
            'created_at': profile.created_at.isoformat() if profile.created_at else None,
            'updated_at': profile.updated_at.isoformat() if profile.updated_at else None,
        }

    # Conversations and requests for this session
    conversations = ChatConversation.objects.filter(session_id=session_id).values_list('pk', flat=True)
    requests_qs = MedicineRequest.objects.filter(conversation_id__in=conversations).order_by('-created_at')

    # Stats (same as patient_dashboard_stats, but admin-only)
    active_statuses = ('broadcasting', 'awaiting_responses', 'responses_received', 'ranking', 'partial')
    stats = {
        'active_requests': requests_qs.filter(status__in=active_statuses).count(),
        'fulfilled_count': requests_qs.filter(status='completed').count(),
        'expired_count': requests_qs.filter(status__in=('expired', 'timeout')).count(),
    }

    # Recent requests list
    req_results = []
    from django.db.models import Min
    for req in requests_qs.select_related('conversation').prefetch_related('pharmacy_responses')[:50]:
        responses = req.pharmacy_responses.all()
        resp_count = responses.count()
        best_price = None
        best_pharmacy_name = None
        if resp_count:
            with_price = responses.filter(price__isnull=False).order_by('price').first()
            if with_price:
                best_price = str(with_price.price)
                best_pharmacy_name = with_price.pharmacy.name if with_price.pharmacy else with_price.pharmacy_name
        short_id = str(req.request_id).replace('-', '')[:8].upper()
        req_results.append({
            'request_id': str(req.request_id),
            'short_request_id': short_id,
            'request_type': req.request_type,
            'medicine_names': req.medicine_names or [],
            'symptoms': req.symptoms or '',
            'location': _medicine_request_location_payload(req),
            'location_address': req.location_address or req.location_suburb or '',
            'submitted_at': req.created_at.isoformat() if req.created_at else None,
            'status': req.status,
            'response_count': resp_count,
            'best_price': best_price,
            'best_pharmacy_name': best_pharmacy_name,
        })

    # Saved medicines
    saved_qs = SavedMedicine.objects.filter(session_id=session_id).order_by('-created_at')
    saved = [{
        'id': s.id,
        'medicine_name': s.medicine_name,
        'display_name': s.display_name or s.medicine_name,
        'last_searched_at': s.last_searched_at.isoformat() if s.last_searched_at else None,
        'created_at': s.created_at.isoformat() if s.created_at else None,
    } for s in saved_qs]

    # Notifications
    notifs_qs = PatientNotification.objects.filter(session_id=session_id).order_by('-created_at')[:100]
    notifications = [{
        'id': n.id,
        'notification_type': n.notification_type,
        'title': n.title,
        'body': n.body,
        'related_request_id': str(n.related_request_id) if n.related_request_id else None,
        'related_response_id': str(n.related_response_id) if n.related_response_id else None,
        'read': n.read_at is not None,
        'read_at': n.read_at.isoformat() if n.read_at else None,
        'created_at': n.created_at.isoformat() if n.created_at else None,
    } for n in notifs_qs]

    return Response({
        'session_id': session_id,
        'profile': profile_data,
        'stats': stats,
        'requests': req_results,
        'saved_medicines': saved,
        'notifications': notifications,
    }, status=status.HTTP_200_OK)


@api_view(['PATCH'])
@permission_classes([IsAdminUser])
def admin_update_patient_profile(request, session_id):
    """
    PATCH /api/chatbot/admin/patients/<session_id>/profile/
    Create or update a PatientProfile for this session.
    """
    profile, _created = PatientProfile.objects.get_or_create(session_id=session_id)
    allowed = {
        'display_name',
        'email',
        'phone',
        'home_area',
        'preferred_language',
    }
    updates = {k: request.data[k] for k in allowed if k in request.data}
    if not updates:
        return Response({'error': 'No editable fields supplied'}, status=status.HTTP_400_BAD_REQUEST)
    for key, value in updates.items():
        setattr(profile, key, value)
    profile.save()
    return Response({
        'session_id': profile.session_id,
        'display_name': profile.display_name,
        'email': profile.email,
        'phone': profile.phone,
        'home_area': profile.home_area,
        'preferred_language': profile.preferred_language,
    }, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_patient_saved_medicines(request, session_id):
    """
    GET /api/chatbot/admin/patients/<session_id>/saved-medicines/
    Admin view of patient's saved medicines.
    """
    saved_qs = SavedMedicine.objects.filter(session_id=session_id).order_by('-created_at')
    results = [{
        'id': s.id,
        'medicine_name': s.medicine_name,
        'display_name': s.display_name or s.medicine_name,
        'last_searched_at': s.last_searched_at.isoformat() if s.last_searched_at else None,
        'created_at': s.created_at.isoformat() if s.created_at else None,
    } for s in saved_qs]
    return Response(results, status=status.HTTP_200_OK)


@api_view(['DELETE'])
@permission_classes([IsAdminUser])
def admin_clear_patient_saved_medicines(request, session_id):
    """
    DELETE /api/chatbot/admin/patients/<session_id>/saved-medicines/clear/
    Remove all saved medicines for this session.
    """
    deleted, _ = SavedMedicine.objects.filter(session_id=session_id).delete()
    return Response({'cleared': deleted}, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_patient_notifications(request, session_id):
    """
    GET /api/chatbot/admin/patients/<session_id>/notifications/
    Admin view of patient's notifications.
    """
    qs = PatientNotification.objects.filter(session_id=session_id).order_by('-created_at')[:200]
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


@api_view(['DELETE'])
@permission_classes([IsAdminUser])
def admin_clear_patient_notifications(request, session_id):
    """
    DELETE /api/chatbot/admin/patients/<session_id>/notifications/clear/
    Remove all notifications for this session.
    """
    deleted, _ = PatientNotification.objects.filter(session_id=session_id).delete()
    return Response({'cleared': deleted}, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_control_center(request):
    """
    GET /api/chatbot/admin/control/center/
    Operational control view for admins:
    - stuck/no-response requests
    - expiring reservations
    - low-rated pharmacies
    - summary counts
    Query params:
      - no_response_minutes (default 10)
      - expiring_minutes (default 30)
      - limit (default 50, max 200)
    """
    now = timezone.now()
    no_response_minutes = min(max(int(request.query_params.get('no_response_minutes', 10)), 1), 240)
    expiring_minutes = min(max(int(request.query_params.get('expiring_minutes', 30)), 1), 240)
    limit = min(max(int(request.query_params.get('limit', 50)), 1), 200)

    active_statuses = ('broadcasting', 'awaiting_responses', 'responses_received', 'ranking', 'partial')
    no_response_cutoff = now - timedelta(minutes=no_response_minutes)
    expiring_cutoff = now + timedelta(minutes=expiring_minutes)

    requests_qs = MedicineRequest.objects.select_related('conversation').annotate(
        responses_count=Count('pharmacy_responses', distinct=True),
        declines_count=Count('pharmacist_declines', distinct=True),
    )

    # Requests that are active but still have no pharmacy response.
    no_response_requests_qs = requests_qs.filter(
        status__in=active_statuses,
        created_at__lte=no_response_cutoff,
        responses_count=0,
    ).order_by('-created_at')[:limit]

    no_response_requests = [{
        'request_id': str(r.request_id),
        'session_id': r.conversation.session_id if r.conversation else None,
        'request_type': r.request_type,
        'medicine_names': r.medicine_names or [],
        'symptoms': r.symptoms or '',
        'status': r.status,
        'responses_count': r.responses_count,
        'declines_count': r.declines_count,
        'created_at': r.created_at.isoformat() if r.created_at else None,
        'submitted_at': r.created_at.isoformat() if r.created_at else None,
        'expires_at': r.expires_at.isoformat() if r.expires_at else None,
    } for r in no_response_requests_qs]

    # Reservations that are still active and expiring soon.
    expiring_reservations_qs = Reservation.objects.select_related('pharmacy', 'conversation').filter(
        status__in=['pending', 'confirmed'],
        expires_at__gt=now,
        expires_at__lte=expiring_cutoff,
    ).order_by('expires_at')[:limit]

    _res_cache = {}
    expiring_reservations = []
    for r in expiring_reservations_qs:
        patient_name, patient_location = _reservation_patient_name_and_location(
            r, _res_cache, reverse_geocode_if_needed=False,
        )
        expiring_reservations.append({
            'reservation_id': str(r.reservation_id),
            'pharmacy_id': r.pharmacy.pharmacy_id if r.pharmacy else None,
            'pharmacy_name': r.pharmacy.name if r.pharmacy else None,
            'medicine_name': r.medicine_name,
            'quantity': r.quantity,
            'status': r.status,
            'patient_name': patient_name,
            'patient_phone': r.patient_phone or '',
            'patient_location': patient_location,
            'reserved_at': r.reserved_at.isoformat() if r.reserved_at else None,
            'expires_at': r.expires_at.isoformat() if r.expires_at else None,
        })

    # Pharmacies likely needing intervention.
    low_rated_pharmacies_qs = Pharmacy.objects.annotate(
        pharmacists_count=Count('pharmacists', distinct=True),
        reservations_count=Count('reservations', distinct=True),
    ).filter(
        rating_count__gte=3,
        rating__lt=3.0,
    ).order_by('rating', '-rating_count', 'name')[:limit]

    low_rated_pharmacies = [{
        'pharmacy_id': p.pharmacy_id,
        'name': p.name,
        'address': p.address,
        'phone': p.phone,
        'email': p.email,
        'is_active': p.is_active,
        'rating': p.rating,
        'rating_count': p.rating_count,
        'response_rate': p.response_rate,
        'pharmacists_count': p.pharmacists_count,
        'reservations_count': p.reservations_count,
    } for p in low_rated_pharmacies_qs]

    summary = {
        'total_active_requests': requests_qs.filter(status__in=active_statuses).count(),
        'no_response_requests': len(no_response_requests),
        'expiring_reservations': len(expiring_reservations),
        'low_rated_pharmacies': len(low_rated_pharmacies),
    }

    return Response({
        'summary': summary,
        'queues': {
            'no_response_requests': no_response_requests,
            'expiring_reservations': expiring_reservations,
            'low_rated_pharmacies': low_rated_pharmacies,
        },
        'meta': {
            'no_response_minutes': no_response_minutes,
            'expiring_minutes': expiring_minutes,
            'generated_at': now.isoformat(),
            'limit': limit,
        }
    }, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_export_pharmacies_csv(request):
    """
    GET /api/chatbot/admin/pharmacies/export/
    CSV export for pharmacy registry table (same columns UI expects).
    """
    qs = Pharmacy.objects.annotate(
        medicine_count=Count('inventory', distinct=True),
        inventory_last_updated=Max('inventory__updated_at'),
    ).order_by('name')
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="pharmacies_registry.csv"'
    writer = csv.writer(response)
    writer.writerow([
        'id', 'name', 'city', 'pharmacy_type', 'verification_status', 'status_pill',
        'medicine_count', 'last_sync_at', 'match_rate', 'is_active',
        'address', 'phone', 'email',
    ])
    for p in qs:
        last_sync = getattr(p, 'last_inventory_sync_at', None) or p.inventory_last_updated
        writer.writerow([
            p.pharmacy_id,
            p.name,
            _city_from_address(p.address),
            getattr(p, 'pharmacy_type', '') or '',
            getattr(p, 'verification_status', 'verified') or 'verified',
            _pharmacy_registry_pill_status(p),
            p.medicine_count,
            last_sync.isoformat() if last_sync else '',
            p.response_rate if p.response_rate is not None else '',
            p.is_active,
            p.address,
            p.phone,
            p.email,
        ])
    return response


@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_search_analytics(request):
    """
    GET /api/chatbot/admin/analytics/search-volume/?days=30
    Search/request volume trends and rough aggregates (server-side).
    """
    days = min(max(int(request.query_params.get('days', 30)), 1), 366)
    start = timezone.now() - timedelta(days=days)
    qs = MedicineRequest.objects.filter(created_at__gte=start)
    by_day = list(
        qs.annotate(day=TruncDate('created_at'))
        .values('day')
        .annotate(count=Count('request_id'))
        .order_by('day')
    )
    by_day_out = [{
        'date': row['day'].isoformat() if row['day'] else None,
        'count': row['count'],
    } for row in by_day]

    medicine_counter = Counter()
    region_counter = Counter()
    for r in qs.only(
        'medicine_names',
        'location_suburb',
        'location_address',
        'location_latitude',
        'location_longitude',
    ):
        for m in (r.medicine_names or []):
            if m:
                medicine_counter[str(m).strip().lower()] += 1
        _rk, city_label = search_volume_region_key_label(
            r.location_suburb,
            r.location_address,
            r.location_latitude,
            r.location_longitude,
        )
        region_counter[city_label] += 1

    annotated = qs.annotate(rc=Count('pharmacy_responses', distinct=True))
    zero_result = annotated.filter(rc=0).count()
    total = qs.count()
    zero_rate = round(zero_result / total, 6) if total else 0.0

    return Response({
        'days': days,
        'requests_by_day': by_day_out,
        'top_medicines': [{'medicine': k, 'count': v} for k, v in medicine_counter.most_common(40)],
        'top_regions': [{'region': k, 'count': v} for k, v in region_counter.most_common(40)],
        'zero_result_requests': zero_result,
        'zero_result_rate': zero_rate,
        'total_requests_in_window': total,
    }, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_users_list(request):
    """
    GET /api/chatbot/admin/users/?page=1&page_size=50&search=
    Paginated Django users (staff can audit platform accounts).
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()
    page = max(int(request.query_params.get('page', 1)), 1)
    page_size = min(max(int(request.query_params.get('page_size', 50)), 1), 200)
    search = (request.query_params.get('search') or '').strip()
    qs = User.objects.all().order_by('-date_joined')
    if search:
        qs = qs.filter(
            Q(username__icontains=search)
            | Q(email__icontains=search)
            | Q(first_name__icontains=search)
            | Q(last_name__icontains=search)
        )
    total = qs.count()
    offset = (page - 1) * page_size
    users = [{
        'id': str(u.pk),
        'username': u.username,
        'email': u.email or '',
        'first_name': u.first_name or '',
        'last_name': u.last_name or '',
        'is_staff': u.is_staff,
        'is_superuser': u.is_superuser,
        'is_active': u.is_active,
        'date_joined': u.date_joined.isoformat() if u.date_joined else None,
        'last_login': u.last_login.isoformat() if u.last_login else None,
    } for u in qs[offset:offset + page_size]]
    return Response({
        'total': total,
        'page': page,
        'page_size': page_size,
        'users': users,
    }, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_patients_list(request):
    """
    GET /api/chatbot/admin/patients-list/?page=1&page_size=50&search=
    Anonymous + profiled patients keyed by session_id (from chat conversations).
    Use admin/patients/<session_id>/overview/ for full detail.
    Note: path cannot be /admin/patients/ without conflicting with …/overview/;
    this lives at /admin/patients-list/ (see urls).
    """
    page = max(int(request.query_params.get('page', 1)), 1)
    page_size = min(max(int(request.query_params.get('page_size', 50)), 1), 200)
    search = (request.query_params.get('search') or '').strip()

    session_qs = (
        ChatConversation.objects.values('session_id')
        .annotate(
            last_active=Max('updated_at'),
            conversation_count=Count('conversation_id'),
        )
        .order_by('-last_active')
    )
    if search:
        session_qs = session_qs.filter(session_id__icontains=search)

    total = session_qs.count()
    offset = (page - 1) * page_size
    page_rows = list(session_qs[offset:offset + page_size])

    profile_map = {}
    if page_rows:
        sids = [r['session_id'] for r in page_rows]
        for prof in PatientProfile.objects.filter(session_id__in=sids):
            profile_map[prof.session_id] = prof

    patients = []
    for row in page_rows:
        sid = row['session_id']
        prof = profile_map.get(sid)
        patients.append({
            'session_id': sid,
            'last_active': row['last_active'].isoformat() if row['last_active'] else None,
            'conversation_count': row['conversation_count'],
            'profile': None if not prof else {
                'display_name': prof.display_name or '',
                'email': prof.email or '',
                'phone': prof.phone or '',
                'home_area': prof.home_area or '',
                'updated_at': prof.updated_at.isoformat() if prof.updated_at else None,
            },
        })

    return Response({
        'total': total,
        'page': page,
        'page_size': page_size,
        'patients': patients,
    }, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_chatbot_logs(request):
    """
    GET /api/chatbot/admin/chatbot/logs/?page=1&page_size=30&search=&session_id=
    Recent AI chat conversations (metadata only; use …/logs/<conversation_id>/ for messages).
    """
    page = max(int(request.query_params.get('page', 1)), 1)
    page_size = min(max(int(request.query_params.get('page_size', 30)), 1), 100)
    search = (request.query_params.get('search') or '').strip()
    session_filter = (request.query_params.get('session_id') or '').strip()
    cache_key = f"admin_chatbot_logs:v2:{page}:{page_size}:{search}:{session_filter}"
    cached = cache.get(cache_key)
    if cached is not None:
        return Response(cached, status=status.HTTP_200_OK)

    last_content = ChatMessage.objects.filter(conversation=OuterRef('pk')).order_by('-created_at').values('content')[:1]
    last_role = ChatMessage.objects.filter(conversation=OuterRef('pk')).order_by('-created_at').values('role')[:1]
    qs = ChatConversation.objects.annotate(
        message_count=Count('messages', distinct=True),
        last_message_content=Subquery(last_content),
        last_message_role=Subquery(last_role),
    ).order_by('-updated_at')
    if session_filter:
        qs = qs.filter(session_id=session_filter)
    if search:
        qs = qs.filter(Q(session_id__icontains=search) | Q(conversation_id__icontains=search))

    total = qs.count()
    offset = (page - 1) * page_size
    conversations = []
    for conv in qs[offset:offset + page_size]:
        preview = (conv.last_message_content or '')[:200]
        conversations.append({
            'conversation_id': str(conv.conversation_id),
            'session_id': conv.session_id,
            'status': conv.status,
            'message_count': conv.message_count,
            'created_at': conv.created_at.isoformat() if conv.created_at else None,
            'updated_at': conv.updated_at.isoformat() if conv.updated_at else None,
            'last_message_preview': preview,
            'last_message_role': conv.last_message_role,
        })
    payload = {
        'total': total,
        'page': page,
        'page_size': page_size,
        'conversations': conversations,
    }
    cache.set(cache_key, payload, timeout=30)
    return Response(payload, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_chatbot_conversation_logs(request, conversation_id):
    """
    GET /api/chatbot/admin/chatbot/logs/<conversation_id>/
    Full AI chat transcript for one conversation (user/assistant/system + metadata).
    """
    conv = get_object_or_404(ChatConversation, conversation_id=conversation_id)
    msgs = ChatMessage.objects.filter(conversation=conv).order_by('created_at')
    return Response({
        'conversation_id': str(conv.conversation_id),
        'session_id': conv.session_id,
        'status': conv.status,
        'context_metadata': conv.context_metadata or {},
        'created_at': conv.created_at.isoformat() if conv.created_at else None,
        'updated_at': conv.updated_at.isoformat() if conv.updated_at else None,
        'messages': [{
            'message_id': str(m.message_id),
            'role': m.role,
            'content': m.content,
            'metadata': m.metadata or {},
            'created_at': m.created_at.isoformat() if m.created_at else None,
        } for m in msgs],
    }, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_audit_logs(request):
    """
    GET /api/chatbot/admin/audit/logs/?page=1&page_size=50
    Paged admin action audit trail.
    """
    page = max(int(request.query_params.get('page', 1)), 1)
    page_size = min(max(int(request.query_params.get('page_size', 50)), 1), 200)
    offset = (page - 1) * page_size
    qs = AdminAuditLog.objects.all().order_by('-created_at')
    total = qs.count()
    rows = qs[offset:offset + page_size]
    return Response({
        'total': total,
        'page': page,
        'page_size': page_size,
        'results': [{
            'id': log.pk,
            'username': log.username,
            'action': log.action,
            'target_type': log.target_type,
            'target_id': log.target_id,
            'success': log.success,
            'detail': log.detail,
            'ip_address': str(log.ip_address) if log.ip_address else None,
            'created_at': log.created_at.isoformat() if log.created_at else None,
        } for log in rows],
    }, status=status.HTTP_200_OK)


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
            # Aliases expected by SPA patient/settings
            'email_notifications': profile.notify_pharmacy_responses,
            'drug_interaction_alerts': profile.notify_drug_interactions,
            'notification_method': profile.notification_method,
            'share_location_with_pharmacies': profile.share_location_with_pharmacies,
            'save_search_history': profile.save_search_history,
        }, status=status.HTTP_200_OK)
    allowed = {
        'display_name', 'email', 'phone', 'date_of_birth', 'home_area', 'preferred_language',
        'allergies', 'conditions', 'max_search_radius_km', 'sort_results_by',
        'notify_pharmacy_responses', 'notify_request_expiry', 'notify_drug_interactions',
        'notify_medibot_followup', 'notification_method', 'share_location_with_pharmacies', 'save_search_history',
        'email_notifications', 'drug_interaction_alerts',
    }
    rd = request.data if isinstance(request.data, dict) else {}
    updates = {}
    if 'email_notifications' in rd:
        updates['notify_pharmacy_responses'] = _coerce_bool(rd.get('email_notifications'))
    if 'drug_interaction_alerts' in rd:
        updates['notify_drug_interactions'] = _coerce_bool(rd.get('drug_interaction_alerts'))
    for k in allowed:
        if k in rd and k not in ('email_notifications', 'drug_interaction_alerts'):
            updates[k] = rd[k]
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
        'email_notifications': profile.notify_pharmacy_responses,
        'drug_interaction_alerts': profile.notify_drug_interactions,
    }, status=status.HTTP_200_OK)
