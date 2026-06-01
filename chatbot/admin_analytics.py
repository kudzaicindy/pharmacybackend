"""
Aggregations for MediBot admin dashboard: badges, SLA, heatmap, watchlist, equity heuristics.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
import math
import re

from django.db.models import Count, Max, Min, Avg, Q
from django.db.models.functions import TruncDate, TruncHour, TruncWeek
from django.utils import timezone

from .models import (
    ChatConversation,
    ChatMessage,
    ChatbotSafetyReview,
    MedicineRequest,
    Pharmacy,
    PharmacyResponse,
    PlatformAdminSettings,
    Reservation,
)

# Zimbabwe-centric region buckets for heatmap / SLA (keyword match on suburb/address).
ZW_GEO_BUCKETS = (
    ('harare', ('harare', 'chitungwiza', 'epworth', 'norton', 'ruwa')),
    ('bulawayo', ('bulawayo',)),
    ('mutare', ('mutare', 'rusape')),
    ('gweru', ('gweru', 'kwekwe', 'kadoma', 'chegutu')),
    ('masvingo', ('masvingo', 'chiredzi', 'zaka')),
    ('bindura', ('bindura', 'mt darwin', 'mt. darwin')),
    ('chipinge', ('chipinge',)),
    ('hwange', ('hwange', 'victoria falls', 'livingstone')),
    ('kariba', ('kariba', 'karoi')),
)

DEFAULT_CHATBOT_POLICY = {
    'medical_disclaimer_all_responses': True,
    'restrict_dosage_advice': True,
    'paediatric_warning_under_5': True,
    'emergency_symptom_detection': True,
    'prescription_only_flag': False,
}

RANKING_PROFILE_PRESETS = [
    {
        'id': 'urban_default',
        'label': 'Urban default',
        'description': 'Higher weight on price; moderate distance (dense pharmacy coverage).',
        'urban': {'price': 35, 'distance': 25, 'rating': 25, 'reliability': 15},
        'rural': {'price': 20, 'distance': 45, 'rating': 20, 'reliability': 15},
    },
    {
        'id': 'rural_equity',
        'label': 'Rural equity',
        'description': 'Deprioritises distance slightly vs default rural; lifts rating/reliability.',
        'urban': {'price': 32, 'distance': 28, 'rating': 25, 'reliability': 15},
        'rural': {'price': 22, 'distance': 38, 'rating': 25, 'reliability': 15},
    },
    {
        'id': 'shortage_mode',
        'label': 'Shortage mode',
        'description': 'Emphasises reliability / stock signal proxy during stock-outs.',
        'urban': {'price': 25, 'distance': 20, 'rating': 25, 'reliability': 30},
        'rural': {'price': 15, 'distance': 35, 'rating': 25, 'reliability': 25},
    },
    {
        'id': 'affordability',
        'label': 'Affordability',
        'description': 'Prioritises price competitiveness for cost-sensitive patients.',
        'urban': {'price': 40, 'distance': 30, 'rating': 15, 'reliability': 15},
        'rural': {'price': 35, 'distance': 35, 'rating': 15, 'reliability': 15},
    },
]

# Dashboard city row order + emoji + SLA zone label (for UI copy).
CITY_CARD_META = {
    'harare': {'emoji': '🏙️', 'label': 'Harare', 'zone': 'urban'},
    'bulawayo': {'emoji': '🏙️', 'label': 'Bulawayo', 'zone': 'urban'},
    'mutare': {'emoji': '🏘️', 'label': 'Mutare', 'zone': 'urban'},
    'hwange': {'emoji': '🌾', 'label': 'Hwange', 'zone': 'rural'},
    'gweru': {'emoji': '🌾', 'label': 'Gweru', 'zone': 'rural'},
    'other': {'emoji': '🌿', 'label': 'Other', 'zone': 'rural'},
}

SLA_TARGET_MS_URBAN = 2000
SLA_TARGET_MS_RURAL = 5000

# Response rate = (patient requests our pharmacy responded to) / (patient requests in scope).
# Scope = medicine requests with patient coordinates in the time window, within this radius (km)
# of the pharmacy — i.e. local patient demand this pharmacy could reasonably serve.
COMPUTED_MATCH_RATE_WINDOW_DAYS = 90
COMPUTED_MATCH_RATE_RADIUS_KM = 50.0


def load_match_rate_window_data(window_days: int) -> tuple[list, dict[str, set]]:
    """
    Shared data for response-rate computation:
    - All patient medicine requests (with lat/lon) created in the window.
    - For each pharmacy id, the set of request_ids it submitted at least one PharmacyResponse for
      in that window (numerator: how many patient requests we responded to).
    """
    now = timezone.now()
    start = now - timedelta(days=window_days)
    requests_data = list(
        MedicineRequest.objects.filter(
            created_at__gte=start,
            location_latitude__isnull=False,
            location_longitude__isnull=False,
        ).values_list('request_id', 'location_latitude', 'location_longitude')
    )
    resp_by_pharmacy: dict[str, set] = defaultdict(set)
    for pid, rid in PharmacyResponse.objects.filter(
        submitted_at__gte=start,
        pharmacy__isnull=False,
    ).values_list('pharmacy_id', 'request_id'):
        resp_by_pharmacy[str(pid)].add(rid)
    return requests_data, resp_by_pharmacy


def compute_response_rate_tuple(
    p: Pharmacy,
    requests_data: list,
    resp_by_pharmacy: dict[str, set],
    *,
    radius_km: float,
) -> tuple[float | None, str, int, int]:
    """
    Response rate = patient requests we responded to / patient requests in scope.

    Denominator (patient_requests_in_range): patient medicine requests in the loaded window whose
    location is within radius_km of this pharmacy (local demand they could serve).

    Numerator (requests_responded_to): how many of those patient requests have at least one
    PharmacyResponse from this pharmacy in the window.

    Returns (rate_or_none_for_display, source, patient_requests_in_range, requests_responded_to).
    Metric keys elsewhere still use opportunities / matched_requests for API compatibility.
    """
    from .services import LocationService

    responded = resp_by_pharmacy.get(str(p.pharmacy_id), set())
    plat, plon = p.latitude, p.longitude
    if plat is None or plon is None:
        stored = float(p.response_rate) if p.response_rate is not None else None
        return stored, 'db_field_no_coordinates', 0, 0
    try:
        ph_lat, ph_lon = float(plat), float(plon)
    except (TypeError, ValueError):
        stored = float(p.response_rate) if p.response_rate is not None else None
        return stored, 'db_field_invalid_coordinates', 0, 0

    patient_requests_in_range = 0
    requests_responded_to = 0
    for rid, rlat, rlon in requests_data:
        try:
            d = LocationService.calculate_distance(
                float(rlat), float(rlon), ph_lat, ph_lon,
            )
        except (TypeError, ValueError):
            continue
        if d <= radius_km:
            patient_requests_in_range += 1
            if rid in responded:
                requests_responded_to += 1

    if patient_requests_in_range == 0:
        rate = float(p.response_rate) if p.response_rate is not None else None
        return rate, 'db_field_no_nearby_requests', 0, 0
    rate = round(100.0 * requests_responded_to / patient_requests_in_range, 2)
    return rate, 'computed', patient_requests_in_range, requests_responded_to


def compute_effective_pharmacy_response_rates_for_ids(
    pharmacy_ids: list[str],
    *,
    window_days: int = COMPUTED_MATCH_RATE_WINDOW_DAYS,
    radius_km: float = COMPUTED_MATCH_RATE_RADIUS_KM,
) -> dict[str, float]:
    """
    Patient-request response rate for ranking/API: (requests this pharmacy responded to)
    ÷ (patient medicine requests with location within service radius), in the rolling window.

    When not computable (no coords, no patient requests in range), falls back to stored
    Pharmacy.response_rate or 100.0.

    Returns pharmacy_id -> float in [0, 100] suitable for ranking (never None).
    """
    if not pharmacy_ids:
        return {}
    unique_ids = list(dict.fromkeys(str(x) for x in pharmacy_ids if x))
    if not unique_ids:
        return {}
    requests_data, resp_by_pharmacy = load_match_rate_window_data(window_days)
    pharmacies = {p.pharmacy_id: p for p in Pharmacy.objects.filter(pharmacy_id__in=unique_ids)}
    out: dict[str, float] = {}
    for pid in unique_ids:
        p = pharmacies.get(pid)
        if not p:
            out[pid] = 100.0
            continue
        rate, _src, _opp, _m = compute_response_rate_tuple(
            p, requests_data, resp_by_pharmacy, radius_km=radius_km,
        )
        out[pid] = float(rate) if rate is not None else 100.0
    return out


def weights_percent_display(w: dict) -> dict:
    """Integer percents for API (sum 100). `stock` mirrors reliability for UI copy."""
    keys = ('price', 'distance', 'rating', 'reliability')
    pct = {k: int(round(float(w.get(k, 0)) * 100)) for k in keys}
    diff = 100 - sum(pct.values())
    if diff and 'price' in pct:
        pct['price'] = max(0, pct['price'] + diff)
    return {
        'price': pct['price'],
        'distance': pct['distance'],
        'rating': pct['rating'],
        'stock': pct['reliability'],
        'reliability': pct['reliability'],
    }


def get_platform_settings() -> PlatformAdminSettings:
    s, _ = PlatformAdminSettings.objects.get_or_create(
        singleton_id='main',
        defaults={
            'chatbot_policy': dict(DEFAULT_CHATBOT_POLICY),
        },
    )
    return s


def merge_chatbot_policy(ps: PlatformAdminSettings | None = None) -> dict:
    """Merge DB `chatbot_policy` with defaults. Pass `ps` to avoid an extra DB hit when already loaded."""
    if ps is None:
        ps = get_platform_settings()
    return {**DEFAULT_CHATBOT_POLICY, **(ps.chatbot_policy or {})}


def build_safety_policies_list(policy: dict) -> list:
    """Rows for admin UI / widgets (keys match DEFAULT_CHATBOT_POLICY)."""
    return [
        {'key': 'medical_disclaimer_all_responses', 'label': 'Medical disclaimer on all responses', 'enabled': policy.get('medical_disclaimer_all_responses', True)},
        {'key': 'restrict_dosage_advice', 'label': 'Dosage advice restriction', 'enabled': policy.get('restrict_dosage_advice', True)},
        {'key': 'paediatric_warning_under_5', 'label': 'Paediatric warning (under-5s)', 'enabled': policy.get('paediatric_warning_under_5', True)},
        {'key': 'emergency_symptom_detection', 'label': 'Emergency symptom detection', 'enabled': policy.get('emergency_symptom_detection', True)},
        {'key': 'prescription_only_flag', 'label': 'Prescription-only drug flag', 'enabled': policy.get('prescription_only_flag', False)},
    ]


def build_admin_widgets_bundle(no_response_minutes: int = 10) -> dict:
    """Lightweight payload: system alerts + safety policy list + merged policy dict."""
    policy = merge_chatbot_policy()
    return {
        'generated_at': timezone.now().isoformat(),
        'system_alerts': build_system_alerts(no_response_minutes),
        'safety_policies': build_safety_policies_list(policy),
        'chatbot_policy': policy,
    }


def geo_region_key(suburb: str, address: str) -> str:
    blob = f'{suburb or ""} {address or ""}'.lower()
    for key, needles in ZW_GEO_BUCKETS:
        for n in needles:
            if n in blob:
                return key
    return 'other'


# Approximate bounding boxes (lat_min, lat_max, lon_min, lon_max) → ZW_GEO_BUCKETS key.
# Order: more specific / non-overlapping regions first where possible.
_ZW_COORD_REGION_BOXES: tuple[tuple[float, float, float, float, str], ...] = (
    (-20.75, -20.35, 32.35, 32.80, 'chipinge'),
    (-19.25, -18.70, 32.40, 32.90, 'mutare'),
    (-20.55, -19.75, 28.20, 29.05, 'bulawayo'),
    (-20.35, -19.80, 30.55, 31.15, 'masvingo'),
    (-19.70, -19.20, 29.50, 30.10, 'gweru'),
    (-18.35, -17.40, 30.72, 31.45, 'harare'),
    (-17.55, -17.05, 31.10, 31.55, 'bindura'),
    (-17.15, -16.45, 28.50, 29.10, 'kariba'),
    (-18.55, -17.35, 25.30, 27.35, 'hwange'),
)


def geo_region_key_from_coords(lat: float | None, lon: float | None) -> str:
    """Map lat/lon to the same region keys as keyword geo_region_key (Zimbabwe-focused)."""
    if lat is None or lon is None:
        return 'other'
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return 'other'
    if not (-26.0 <= la <= -14.0 and 22.0 <= lo <= 36.0):
        return 'other'
    for lat_min, lat_max, lon_min, lon_max, key in _ZW_COORD_REGION_BOXES:
        if lat_min <= la <= lat_max and lon_min <= lo <= lon_max:
            return key
    return 'other'


def _parse_lat_lon_from_text(text: str) -> tuple[float | None, float | None]:
    if not text or not isinstance(text, str):
        return None, None
    m = re.search(r'(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)', text)
    if not m:
        return None, None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None, None


def _is_likely_coordinate_only(s: str) -> bool:
    t = (s or '').strip()
    if not t:
        return False
    if re.fullmatch(r'-?\d+(?:\.\d+)?', t):
        return True
    return _parse_lat_lon_from_text(t)[0] is not None


def _last_segment_city(address: str) -> str:
    if not address:
        return ''
    parts = [p.strip() for p in str(address).split(',') if p.strip()]
    if len(parts) >= 2:
        return parts[-1]
    return parts[0] if parts else ''


def search_volume_region_key_label(
    location_suburb: str,
    location_address: str,
    location_latitude: float | None,
    location_longitude: float | None,
) -> tuple[str, str]:
    """
    Stable region key + human-readable city/label for admin search-volume analytics.
    Avoids showing raw coordinate fragments (e.g. a lone longitude in suburb) when lat/lon imply a city.
    """
    suburb = (location_suburb or '').strip()
    address = (location_address or '').strip()

    k = geo_region_key(suburb, address)
    if k != 'other':
        lbl = CITY_CARD_META.get(k, {}).get('label', k.replace('_', ' ').title())
        return k, lbl

    lat, lon = location_latitude, location_longitude
    if lat is None or lon is None:
        la, lo = _parse_lat_lon_from_text(f'{suburb} {address}')
        lat, lon = la, lo
    if lat is not None and lon is not None:
        k2 = geo_region_key_from_coords(lat, lon)
        if k2 != 'other':
            return k2, CITY_CARD_META.get(k2, {}).get('label', k2.title())
        return 'international', 'Other'

    if suburb and not _is_likely_coordinate_only(suburb):
        return 'named', suburb[:100]

    guess = _last_segment_city(address)
    if guess and not _is_likely_coordinate_only(guess):
        return 'named', guess[:100]

    return 'other', 'Other'


def search_volume_region_label(
    location_suburb: str,
    location_address: str,
    location_latitude: float | None,
    location_longitude: float | None,
) -> str:
    return search_volume_region_key_label(
        location_suburb, location_address, location_latitude, location_longitude,
    )[1]


def _percentile(sorted_vals: list, p: float) -> float | None:
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(sorted_vals[int(k)])
    return float(sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f))


def build_computed_pharmacy_match_rates(
    *,
    top_n: int = 10,
    window_days: int = COMPUTED_MATCH_RATE_WINDOW_DAYS,
    radius_km: float = COMPUTED_MATCH_RATE_RADIUS_KM,
    min_opportunities_for_avg: int = 1,
) -> tuple[list[dict], float | None]:
    """
    Patient-request response rate per pharmacy: (patient requests we responded to)
    ÷ (patient requests with location within radius_km), rolling window.

    Pharmacies without coordinates fall back to Pharmacy.response_rate for display only.

    Returns (rows for dashboard, mean computed rate over pharmacies with enough opportunities).
    """
    requests_data, resp_by_pharmacy = load_match_rate_window_data(window_days)

    rows_internal: list[dict] = []
    for p in Pharmacy.objects.filter(is_active=True):
        vs = getattr(p, 'verification_status', 'verified') or 'verified'
        responded = resp_by_pharmacy.get(str(p.pharmacy_id), set())
        rate, src, opportunities, matched = compute_response_rate_tuple(
            p, requests_data, resp_by_pharmacy, radius_km=radius_km,
        )
        rows_internal.append({
            'pharmacy_id': p.pharmacy_id,
            'name': p.name,
            'address': p.address,
            'response_rate': rate,
            'verification_status': vs,
            'metrics': {
                'window_days': window_days,
                'radius_km': radius_km,
                # Denominator / numerator (aliases: opportunities / matched_requests kept for clients)
                'patient_requests_in_range': opportunities,
                'responses_to_patient_requests': matched,
                'opportunities': opportunities,
                'matched_requests': matched,
                'responses_in_window': len(responded),
                'source': src,
            },
        })

    def _sort_key(r: dict) -> tuple:
        m = r['metrics']
        rate = r['response_rate']
        if m['source'] == 'computed' and m['opportunities'] > 0:
            return (0, -(rate if rate is not None else -1), -m['matched_requests'], -m['opportunities'])
        if m['responses_in_window'] > 0:
            return (1, -m['responses_in_window'], -(rate if rate is not None else -1))
        return (2, -(rate if rate is not None else -1), 0)

    rows_internal.sort(key=_sort_key)
    top_rows = rows_internal[:top_n]

    rates_for_avg = [
        float(r['response_rate'])
        for r in rows_internal
        if r['metrics']['source'] == 'computed'
        and r['metrics']['opportunities'] >= min_opportunities_for_avg
        and r['response_rate'] is not None
    ]
    avg_computed = round(sum(rates_for_avg) / len(rates_for_avg), 2) if rates_for_avg else None
    return top_rows, avg_computed


def compute_nav_badges() -> dict:
    verification_queue = Pharmacy.objects.filter(
        is_active=True, verification_status='pending_review',
    ).count()
    watchlist = len(watchlist_pharmacy_ids())
    chatbot_audit = ChatbotSafetyReview.objects.filter(resolved_at__isnull=True).count()
    return {
        'verification_queue': verification_queue,
        'watchlist': watchlist,
        'chatbot_audit': chatbot_audit,
    }


def watchlist_pharmacy_ids() -> set:
    """Heuristic governance watchlist (distinct pharmacy_ids)."""
    ids = set()
    stale_cutoff = timezone.now() - timedelta(days=90)
    low_rr = Pharmacy.objects.filter(
        response_rate__lt=45,
        rating_count__gte=2,
    ).values_list('pharmacy_id', flat=True)
    ids.update(low_rr)
    low_rating = Pharmacy.objects.filter(
        rating__lt=3.0,
        rating_count__gte=3,
    ).values_list('pharmacy_id', flat=True)
    ids.update(low_rating)
    stale_inv = (
        Pharmacy.objects.filter(inventory__isnull=False)
        .annotate(last_inv=Max('inventory__updated_at'))
        .filter(last_inv__lt=stale_cutoff)
        .values_list('pharmacy_id', flat=True)
    )
    ids.update(stale_inv)
    return ids


def compute_daily_active_sessions() -> int:
    start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        ChatConversation.objects.filter(updated_at__gte=start)
        .values('session_id')
        .distinct()
        .count()
    )


def compute_avg_first_response_ms(since_days: int = 14) -> float | None:
    start = timezone.now() - timedelta(days=since_days)
    deltas = []
    for row in (
        PharmacyResponse.objects.filter(submitted_at__gte=start, request__isnull=False)
        .select_related('request')
        .only('submitted_at', 'request__created_at')[:5000]
    ):
        req = row.request
        if not req or not req.created_at or not row.submitted_at:
            continue
        delta = (row.submitted_at - req.created_at).total_seconds() * 1000
        if 0 <= delta < 86400000:
            deltas.append(delta)
    if not deltas:
        return None
    return round(sum(deltas) / len(deltas), 2)


def build_geo_heatmap(days: int = 30) -> list:
    start = timezone.now() - timedelta(days=days)
    qs = MedicineRequest.objects.filter(created_at__gte=start).only(
        'location_suburb', 'location_address', 'location_latitude', 'location_longitude',
    )
    counter = Counter()
    lat_sum = defaultdict(float)
    lon_sum = defaultdict(float)
    n_coords = defaultdict(int)
    for r in qs.iterator(chunk_size=500):
        key = geo_region_key(r.location_suburb or '', r.location_address or '')
        counter[key] += 1
        if r.location_latitude is not None and r.location_longitude is not None:
            lat_sum[key] += float(r.location_latitude)
            lon_sum[key] += float(r.location_longitude)
            n_coords[key] += 1
    out = []
    for region, count in counter.most_common():
        entry = {'geo_region': region, 'count': count}
        nc = n_coords[region]
        if nc:
            entry['latitude'] = round(lat_sum[region] / nc, 6)
            entry['longitude'] = round(lon_sum[region] / nc, 6)
        out.append(entry)
    return out


def build_sla_by_region(since_days: int = 14, target_ms: int = 2000) -> list:
    start = timezone.now() - timedelta(days=since_days)
    # Per request: min first response latency (ms) and region key
    req_ids = MedicineRequest.objects.filter(created_at__gte=start).values_list('request_id', flat=True)
    first_resp = (
        PharmacyResponse.objects.filter(request_id__in=req_ids)
        .values('request_id')
        .annotate(first_at=Min('submitted_at'))
    )
    req_first = {x['request_id']: x['first_at'] for x in first_resp}
    reqs = MedicineRequest.objects.filter(request_id__in=req_first.keys()).only(
        'request_id', 'location_suburb', 'location_address', 'created_at',
    )
    by_region = defaultdict(list)
    for r in reqs:
        fa = req_first.get(r.request_id)
        if not fa or not r.created_at:
            continue
        ms = (fa - r.created_at).total_seconds() * 1000
        if ms < 0 or ms > 86400000:
            continue
        reg = geo_region_key(r.location_suburb or '', r.location_address or '')
        by_region[reg].append(ms)
    out = []
    for region, vals in sorted(by_region.items(), key=lambda x: -len(x[1])):
        vals.sort()
        p95 = _percentile(vals, 0.95)
        p95_i = int(round(p95)) if p95 is not None else None
        out.append({
            'region': region.replace('_', ' ').title() if region != 'other' else 'Other',
            'region_key': region,
            'p95_response_ms': p95_i,
            'sample_size': len(vals),
            'sla_met': p95_i is not None and p95_i <= target_ms,
            'target_ms': target_ms,
        })
    return out


def build_verification_queue(limit: int = 100) -> list:
    qs = (
        Pharmacy.objects.filter(is_active=True, verification_status='pending_review')
        .prefetch_related('pharmacists')
        .order_by('created_at')[:limit]
    )
    items = []
    for p in qs:
        ph0 = p.pharmacists.first()
        license_number = ph0.license_number if ph0 else ''
        owner_name = ph0.full_name if ph0 else ''
        items.append({
            'pharmacy_id': p.pharmacy_id,
            'name': p.name,
            'license_number': license_number or '',
            'submitted_at': p.created_at.isoformat() if p.created_at else None,
            'address': p.address,
            'owner_name': owner_name,
            'phone': p.phone,
            'email': p.email,
            'documents': [],
        })
    return items


def build_watchlist(limit: int = 100) -> list:
    ids = list(watchlist_pharmacy_ids())[:limit]
    if not ids:
        return []
    complaints = (
        Pharmacy.objects.filter(pharmacy_id__in=ids)
        .annotate(_rc=Count('ratings', distinct=True))
    )
    complaint_map = {p.pharmacy_id: p._rc for p in complaints}
    stale_cutoff = timezone.now() - timedelta(days=90)
    out = []
    for p in Pharmacy.objects.filter(pharmacy_id__in=ids).annotate(
        last_inv=Max('inventory__updated_at'),
    ):
        flags = 0
        reasons = []
        if p.response_rate is not None and p.response_rate < 45:
            flags += 1
            reasons.append('low_response_rate')
        if p.rating is not None and p.rating < 3 and (p.rating_count or 0) >= 3:
            flags += 1
            reasons.append('low_rating')
        if p.last_inv and p.last_inv < stale_cutoff:
            flags += 1
            reasons.append('stale_inventory')
        severity = 'critical' if flags >= 3 else ('warning' if flags else 'warning')
        out.append({
            'pharmacy_id': p.pharmacy_id,
            'name': p.name,
            'response_rate': float(p.response_rate) if p.response_rate is not None else None,
            'complaint_count': complaint_map.get(p.pharmacy_id, 0),
            'stock_issue_flags': 1 if (p.last_inv and p.last_inv < stale_cutoff) else 0,
            'severity': severity,
            'reasons': reasons,
            'address': p.address,
        })
    return out


def _request_is_urban_heuristic(lat, lon) -> bool | None:
    if lat is None or lon is None:
        return None
    try:
        from .services import RankingEngine
        d = RankingEngine.calculate_pharmacy_density(float(lat), float(lon))
        return d >= 3
    except Exception:
        return None


def build_impact_equity(days: int = 90) -> dict:
    start = timezone.now() - timedelta(days=days)
    qs = MedicineRequest.objects.filter(created_at__gte=start).only(
        'request_id', 'status', 'location_latitude', 'location_longitude',
    )
    urban_total = rural_total = urban_fulfilled = rural_fulfilled = 0
    unknown_total = unknown_fulfilled = 0
    for r in qs.iterator(chunk_size=300):
        u = _request_is_urban_heuristic(r.location_latitude, r.location_longitude)
        fulfilled = r.status == 'completed'
        if u is True:
            urban_total += 1
            if fulfilled:
                urban_fulfilled += 1
        elif u is False:
            rural_total += 1
            if fulfilled:
                rural_fulfilled += 1
        else:
            unknown_total += 1
            if fulfilled:
                unknown_fulfilled += 1
    return {
        'window_days': days,
        'fulfilment_rate_urban': round(urban_fulfilled / urban_total, 4) if urban_total else None,
        'fulfilment_rate_rural': round(rural_fulfilled / rural_total, 4) if rural_total else None,
        'requests_urban': urban_total,
        'requests_rural': rural_total,
        'requests_location_unknown': unknown_total,
        'avg_find_time_minutes_urban': None,
        'avg_find_time_minutes_rural': None,
        'estimated_savings_usd': None,
        'note': 'Fulfilment = status completed; urban/rural from pharmacy density at patient coordinates.',
    }


def _active_request_statuses():
    return ('broadcasting', 'awaiting_responses', 'responses_received', 'ranking', 'partial')


def compute_peak_active_sessions_today():
    """Best-effort peak concurrent distinct sessions per hour today (from chat messages)."""
    start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (
        ChatMessage.objects.filter(created_at__gte=start)
        .annotate(hour=TruncHour('created_at'))
        .values('hour')
        .annotate(sessions=Count('conversation_id', distinct=True))
        .order_by('-sessions')
    )
    top = rows.first()
    if not top or not top.get('hour'):
        return {'peak': 0, 'time_local': None}
    h = top['hour']
    return {'peak': top['sessions'], 'time_local': h.strftime('%H:%M') if hasattr(h, 'strftime') else str(h)[:5]}


def compute_dau_change_pct():
    """Rough % change vs yesterday same metric (distinct sessions active that day)."""
    now = timezone.now()
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    y0 = today0 - timedelta(days=1)
    y1 = today0
    d_today = (
        ChatConversation.objects.filter(updated_at__gte=today0)
        .values('session_id')
        .distinct()
        .count()
    )
    d_yest = (
        ChatConversation.objects.filter(updated_at__gte=y0, updated_at__lt=y1)
        .values('session_id')
        .distinct()
        .count()
    )
    if d_yest <= 0:
        return None
    return round(100.0 * (d_today - d_yest) / d_yest, 1)


def build_city_volume_cards(days: int = 30, sla_by_key: dict | None = None) -> list:
    heat = {row['geo_region']: row for row in build_geo_heatmap(days)}
    if sla_by_key is None:
        sla_by_key = {r['region_key']: r.get('p95_response_ms') for r in build_sla_by_region(days, SLA_TARGET_MS_URBAN)}
    order = ['harare', 'bulawayo', 'mutare', 'hwange', 'gweru', 'other']
    out = []
    for key in order:
        meta = CITY_CARD_META.get(key, {'emoji': '📍', 'label': key.title(), 'zone': 'rural'})
        row = heat.get(key, {})
        cnt = row.get('count', 0)
        rural_pct = None
        if cnt and key != 'other':
            rural_pct = _rural_share_for_region_bucket(key, days)
        out.append({
            'geo_region': key,
            'label': meta['label'],
            'emoji': meta['emoji'],
            'volume': cnt,
            'rural_share_pct': round(rural_pct * 100, 1) if rural_pct is not None else None,
            'p95_response_ms': sla_by_key.get(key),
            'zone': meta['zone'],
        })
    return out


def _rural_share_for_region_bucket(region_key: str, days: int) -> float | None:
    start = timezone.now() - timedelta(days=days)
    qs = MedicineRequest.objects.filter(created_at__gte=start).only(
        'location_suburb', 'location_address', 'location_latitude', 'location_longitude',
    )
    tot = ur = 0
    for r in qs.iterator(chunk_size=400):
        if geo_region_key(r.location_suburb or '', r.location_address or '') != region_key:
            continue
        tot += 1
        u = _request_is_urban_heuristic(r.location_latitude, r.location_longitude)
        if u is False:
            ur += 1
    if not tot:
        return None
    return ur / tot


def _sla_row_plain_language(row: dict, *, window_days: int = 14) -> dict:
    """
    Lay-friendly text for SLA rows. Technical fields (p95_ms, target_ms) stay for power users.
    """
    zone = row.get('zone') or 'rural'
    target_ms = row.get('target_ms')
    target_s = (target_ms / 1000.0) if target_ms else None
    p95_ms = row.get('p95_response_ms')
    p95_s = row.get('p95_response_seconds')
    if p95_s is None and p95_ms is not None:
        p95_s = round(p95_ms / 1000.0, 2)
    met = row.get('sla_met')
    n = int(row.get('sample_size') or 0)
    area = row.get('display_name') or row.get('region') or 'This area'
    zone_label = 'Urban' if zone == 'urban' else 'Rural'

    goal_sentence = (
        f'{zone_label} target: first pharmacy reply within {target_s:.0f} seconds from when the patient request was created.'
        if target_s is not None
        else 'No response-time target configured for this row.'
    )

    if p95_s is None or n <= 0:
        return {
            'what_we_measure': (
                'How long patients wait for the first pharmacy reply after their medicine request is sent '
                f'(requests grouped by suburb/city text, last {window_days} days).'
            ),
            'p95_in_plain_words': (
                'Upper-end wait time: about 95 out of 100 first replies arrived faster than this number. '
                'It is not the average wait.'
            ),
            'lay_summary': f'{area}: not enough requests ({n}) in this window to read speed reliably.',
            'lay_status': 'Not enough data',
            'lay_goal_sentence': goal_sentence,
        }

    if p95_s >= 120:
        time_human = f'{p95_s / 60:.1f} minutes'
    elif p95_s >= 60:
        time_human = f'{p95_s / 60:.1f} minutes ({p95_s:.0f} s)'
    else:
        time_human = f'{p95_s:.0f} seconds'

    status = 'meeting target' if met else 'slower than target'
    detail = (
        f'{area}: using {n} request(s) in the last {window_days} days, 95% of first replies '
        f'arrived faster than {time_human}. {zone_label} goal is {target_s:.0f} s. '
        f'Most patients hear back sooner than {time_human}; a few slow replies raise this upper figure.'
    )
    short = (
        f'{area}: {time_human} at the 95th percentile; goal {target_s:.0f} s — {status} ({n} requests).'
    )

    other_note = ''
    rk = row.get('region_key') or ''
    if rk == 'other':
        other_note = (
            ' “Other” means the address did not match a named city bucket (Harare, Bulawayo, etc.); '
            'with few requests the number can look extreme.'
        )

    return {
        'what_we_measure': (
            'How long patients wait for the first pharmacy reply after their medicine request is sent '
            f'(grouped by area label from patient address text, last {window_days} days).'
        ),
        'p95_in_plain_words': (
            'This row’s seconds/minutes are a 95th percentile: about 95 in 100 first replies were quicker than shown. '
            'This is not the average.'
        ),
        'lay_summary': short + other_note,
        'lay_detail': detail + (f' {other_note.strip()}' if other_note else ''),
        'lay_status': 'Meeting target' if met else 'Slower than target',
        'lay_goal_sentence': goal_sentence,
    }


def build_sla_by_region_display(days: int = 14, precomputed: list | None = None) -> list:
    """SLA rows with urban/rural target selection and avg p95 in seconds for UI."""
    rows = precomputed if precomputed is not None else build_sla_by_region(days, SLA_TARGET_MS_URBAN)
    out = []
    for row in rows:
        rk = row.get('region_key') or 'other'
        zone = CITY_CARD_META.get(rk, {}).get('zone', 'rural')
        target = SLA_TARGET_MS_URBAN if zone == 'urban' else SLA_TARGET_MS_RURAL
        p95 = row.get('p95_response_ms')
        met = p95 is not None and p95 <= target
        label = row['region']
        if zone == 'urban' and rk in ('harare', 'bulawayo'):
            label = f"{label} (Urban)"
        elif zone == 'rural' and rk in ('hwange', 'gweru', 'other'):
            label = f"{label} (Rural)" if rk != 'other' else label
        item = {
            **row,
            'display_name': label,
            'zone': zone,
            'target_ms': target,
            'sla_met': met,
            'p95_response_seconds': round(p95 / 1000, 2) if p95 is not None else None,
        }
        item.update(_sla_row_plain_language(item, window_days=days))
        out.append(item)
    return out


def _median(vals: list) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    m = len(s) // 2
    if len(s) % 2:
        return float(s[m])
    return (float(s[m - 1]) + float(s[m])) / 2.0


def compute_find_time_minutes_by_zone(days: int = 90) -> tuple:
    """Median minutes from request created to first pharmacy response, split urban/rural."""
    start = timezone.now() - timedelta(days=days)
    req_ids = MedicineRequest.objects.filter(created_at__gte=start).values_list('request_id', flat=True)
    first_map = {
        x['request_id']: x['first_at']
        for x in PharmacyResponse.objects.filter(request_id__in=req_ids)
        .values('request_id')
        .annotate(first_at=Min('submitted_at'))
    }
    urban_mins = []
    rural_mins = []
    for r in MedicineRequest.objects.filter(request_id__in=first_map.keys()).only(
        'request_id', 'created_at', 'location_latitude', 'location_longitude',
    ).iterator(chunk_size=500):
        fa = first_map.get(r.request_id)
        if not fa or not r.created_at:
            continue
        mins = (fa - r.created_at).total_seconds() / 60.0
        if mins < 0 or mins > 10080:
            continue
        z = _request_is_urban_heuristic(r.location_latitude, r.location_longitude)
        if z is True:
            urban_mins.append(mins)
        elif z is False:
            rural_mins.append(mins)
    return _median(urban_mins), _median(rural_mins)


def build_weekly_fulfilment_series(weeks: int = 8) -> list:
    now = timezone.now()
    start = now - timedelta(weeks=weeks)
    qs = MedicineRequest.objects.filter(created_at__gte=start)
    buckets = defaultdict(lambda: {'urban': [0, 0], 'rural': [0, 0]})
    for r in qs.annotate(week=TruncWeek('created_at')).values(
        'week', 'status', 'location_latitude', 'location_longitude',
    ):
        wk = r['week']
        if not wk:
            continue
        z = _request_is_urban_heuristic(r.get('location_latitude'), r.get('location_longitude'))
        if z is True:
            key = 'urban'
        elif z is False:
            key = 'rural'
        else:
            continue
        buckets[wk][key][0] += 1
        if r.get('status') == 'completed':
            buckets[wk][key][1] += 1
    out = []
    for wk in sorted(buckets.keys()):
        u_tot, u_ok = buckets[wk]['urban']
        r_tot, r_ok = buckets[wk]['rural']
        out.append({
            'week_start': wk.isoformat() if hasattr(wk, 'isoformat') else str(wk),
            'urban_fulfilment_pct': round(100 * u_ok / u_tot, 2) if u_tot else None,
            'rural_fulfilment_pct': round(100 * r_ok / r_tot, 2) if r_tot else None,
            'urban_requests': u_tot,
            'rural_requests': r_tot,
        })
    return out


def build_governance_watchlist_enriched(limit: int = 50) -> list:
    base = build_watchlist(limit)
    if not base:
        return []
    ids = [x['pharmacy_id'] for x in base]
    res_counts = dict(
        Reservation.objects.filter(pharmacy_id__in=ids, status__in=('pending', 'confirmed'))
        .values('pharmacy_id')
        .annotate(c=Count('reservation_id'))
        .values_list('pharmacy_id', 'c')
    )
    ph_map = {p.pharmacy_id: p for p in Pharmacy.objects.filter(pharmacy_id__in=ids)}
    enriched = []
    for row in base:
        pid = row['pharmacy_id']
        p = ph_map.get(pid)
        enriched.append({
            **row,
            'pending_incoming_requests': 0,
            'active_reservations': res_counts.get(pid, 0),
            'rating': float(p.rating) if p and p.rating is not None else None,
            'rating_count': p.rating_count if p else 0,
        })
    return enriched


def build_search_volume_bundle(days: int = 7) -> dict:
    start = timezone.now() - timedelta(days=days)
    qs = MedicineRequest.objects.filter(created_at__gte=start)
    by_day = list(
        qs.annotate(day=TruncDate('created_at'))
        .values('day')
        .annotate(count=Count('request_id'))
        .order_by('day')
    )
    medicine_counter = Counter()
    for r in qs.only('medicine_names'):
        for m in (r.medicine_names or []):
            if m:
                medicine_counter[str(m).strip().lower()] += 1
    return {
        'days': days,
        'requests_by_day': [
            {'date': row['day'].isoformat() if row['day'] else None, 'count': row['count']}
            for row in by_day
        ],
        'top_medicines': [{'medicine': k, 'count': v} for k, v in medicine_counter.most_common(25)],
    }


def build_system_alerts(no_response_minutes: int = 10) -> list:
    now = timezone.now()
    active = _active_request_statuses()
    cutoff = now - timedelta(minutes=no_response_minutes)
    stuck = (
        MedicineRequest.objects.filter(status__in=active, created_at__lte=cutoff)
        .annotate(rc=Count('pharmacy_responses', distinct=True))
        .filter(rc=0)
        .select_related('conversation')
        .order_by('-created_at')[:12]
    )
    alerts = []
    for r in stuck:
        area = (r.location_suburb or r.location_address or 'Unknown area')[:120]
        meds = ', '.join((r.medicine_names or [])[:3]) or 'Request'
        alerts.append({
            'type': 'patient_request_awaiting_response',
            'severity': 'warning',
            'title': meds,
            'detail': area,
            'request_id': str(r.request_id),
            'session_id': r.conversation.session_id if r.conversation else None,
        })
    for w in build_watchlist(5):
        alerts.append({
            'type': 'watchlist_pharmacy',
            'severity': 'warning',
            'title': w['name'],
            'detail': 'Queued for operational review',
            'pharmacy_id': w['pharmacy_id'],
        })
    return alerts


def build_medi_bot_overview() -> dict:
    from .services import resolve_platform_mcda_weights

    now = timezone.now()
    ps = get_platform_settings()
    nav = compute_nav_badges()
    start_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    requests_today = MedicineRequest.objects.filter(created_at__gte=start_day).count()
    requests_last_hour = MedicineRequest.objects.filter(created_at__gte=now - timedelta(hours=1)).count()
    dau = compute_daily_active_sessions()
    peak = compute_peak_active_sessions_today()
    dau_delta = compute_dau_change_pct()
    avg_ms = compute_avg_first_response_ms(14)
    avg_s = round(avg_ms / 1000, 2) if avg_ms is not None else None

    reg_total = Pharmacy.objects.count()
    reg_active = Pharmacy.objects.filter(is_active=True).count()
    reg_susp = Pharmacy.objects.filter(
        Q(is_active=False) | Q(verification_status='suspended'),
    ).count()
    reg_pending = Pharmacy.objects.filter(is_active=True, verification_status='pending_review').count()

    total_req_30 = MedicineRequest.objects.filter(
        created_at__gte=now - timedelta(days=30),
    ).count()
    rural_req = 0
    for r in MedicineRequest.objects.filter(
        created_at__gte=now - timedelta(days=30),
    ).only('location_latitude', 'location_longitude').iterator(chunk_size=400):
        u = _request_is_urban_heuristic(r.location_latitude, r.location_longitude)
        if u is False:
            rural_req += 1
    rural_share_30 = round(100 * rural_req / total_req_30, 1) if total_req_30 else 0.0

    sla_base_14 = build_sla_by_region(14, SLA_TARGET_MS_URBAN)
    sla_key_ms = {r['region_key']: r.get('p95_response_ms') for r in build_sla_by_region(30, SLA_TARGET_MS_URBAN)}
    city_cards_30 = build_city_volume_cards(30, sla_by_key=sla_key_ms)
    sla_rows = build_sla_by_region_display(14, precomputed=sla_base_14)
    worst = None
    for s in sla_rows:
        if s.get('p95_response_ms') is None:
            continue
        if worst is None or s['p95_response_ms'] > worst['p95_response_ms']:
            worst = s
    sla_narrative = None
    sla_narrative_followup = None
    if worst and worst.get('p95_response_seconds') is not None:
        wname = worst.get('display_name') or worst.get('region', 'One region')
        sec = float(worst.get('p95_response_seconds'))
        lim = (worst.get('target_ms') or SLA_TARGET_MS_RURAL) / 1000.0
        n = int(worst.get('sample_size') or 0)
        zl = 'Urban' if worst.get('zone') == 'urban' else 'Rural'
        met_txt = (
            'this bucket is meeting the speed target.'
            if worst.get('sla_met')
            else 'this bucket is not meeting the speed target yet (replies often slower than the goal).'
        )
        sla_narrative = (
            f'Slowest area in the last 14 days: {wname}. For {n} request(s) there, 95% of first pharmacy '
            f'replies arrived faster than {sec:.0f} seconds. {zl} goal: first reply within {lim:.0f} seconds; '
            f'{met_txt}'
        )
        if rural_share_30 >= 40:
            rural_cities = [
                x['label'] for x in city_cards_30
                if x['geo_region'] in ('hwange', 'gweru') and x.get('volume')
            ]
            outreach = ', '.join(rural_cities) if rural_cities else 'underserved rural areas'
            sla_narrative_followup = (
                f'Many requests look rural by location ({rural_share_30:.0f}% in the last 30 days). '
                f'Consider recruiting or activating more pharmacies near {outreach}.'
            )

    u_eff = resolve_platform_mcda_weights(3, ps)
    wdisp = weights_percent_display(u_eff)
    active_prof = (ps.active_ranking_profile or 'urban_default').strip()
    profiles_ui = []
    for p in RANKING_PROFILE_PRESETS:
        u = p['urban']
        profiles_ui.append({
            'id': p['id'],
            'label': p['label'],
            'active': p['id'] == active_prof,
            'weights_label': f"{u['price']} · {u['distance']} · {u['rating']} · {u['reliability']}",
            'urban': u,
            'rural': p['rural'],
        })

    equity = build_impact_equity(90)
    u_rate = (equity.get('fulfilment_rate_urban') or 0) * 100
    r_rate = (equity.get('fulfilment_rate_rural') or 0) * 100
    gap = abs(u_rate - r_rate) if equity.get('fulfilment_rate_urban') is not None else None
    urban_med, rural_med = compute_find_time_minutes_by_zone(90)
    fulfilled_u = int((equity.get('fulfilment_rate_urban') or 0) * (equity.get('requests_urban') or 0))
    fulfilled_r = int((equity.get('fulfilment_rate_rural') or 0) * (equity.get('requests_rural') or 0))
    fulfilled = fulfilled_u + fulfilled_r
    total_eq = (equity.get('requests_urban') or 0) + (equity.get('requests_rural') or 0)
    fulfil_pct = round(100 * fulfilled / total_eq, 2) if total_eq else None

    # Transport savings heuristic: avg distance on responses * picked_up reservations * $/km
    avg_dist = PharmacyResponse.objects.exclude(distance_km__isnull=True).aggregate(
        a=Avg('distance_km'),
    ).get('a')
    avg_dist_f = float(avg_dist) if avg_dist is not None else 0.0
    picked = Reservation.objects.filter(status='picked_up').count()
    usd_per_km = 0.12
    transport_saved = round(avg_dist_f * picked * usd_per_km, 2) if picked else None

    reviews = list(
        ChatbotSafetyReview.objects.filter(resolved_at__isnull=True)
        .select_related('conversation')
        .order_by('-created_at')[:8]
    )
    audit_cards = []
    for rev in reviews:
        audit_cards.append({
            'id': str(rev.review_id),
            'status': rev.status,
            'patient_query': (rev.patient_query or '')[:500],
            'bot_response': (rev.bot_response or '')[:500],
            'created_at': rev.created_at.isoformat() if rev.created_at else None,
            'conversation_id': str(rev.conversation_id),
        })

    policy = merge_chatbot_policy(ps)
    safety_policies = build_safety_policies_list(policy)

    # Stock accuracy heuristic
    stale_cutoff = now - timedelta(days=90)
    with_inv = Pharmacy.objects.filter(inventory__isnull=False).distinct().count()
    stale_ph = (
        Pharmacy.objects.filter(inventory__isnull=False)
        .annotate(last_inv=Max('inventory__updated_at'))
        .filter(last_inv__lt=stale_cutoff)
        .distinct()
        .count()
    )
    avg_stock_accuracy = round(100 * (1 - stale_ph / with_inv), 1) if with_inv else None

    avg_rr_stored = Pharmacy.objects.filter(is_active=True).aggregate(a=Avg('response_rate')).get('a')
    match_rates, avg_rr_computed = build_computed_pharmacy_match_rates(top_n=10)
    avg_rr_effective = (
        avg_rr_computed
        if avg_rr_computed is not None
        else (round(float(avg_rr_stored), 2) if avg_rr_stored is not None else None)
    )
    avg_rating = Pharmacy.objects.filter(rating_count__gt=0).aggregate(a=Avg('rating')).get('a')
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    suspended_month = Pharmacy.objects.filter(
        Q(verification_status='suspended') | Q(is_active=False),
        updated_at__gte=month_start,
    ).count()

    recent_pharm = []
    for p in Pharmacy.objects.order_by('-created_at')[:15]:
        vs = getattr(p, 'verification_status', 'verified') or 'verified'
        pill = 'Pending' if vs == 'pending_review' else ('Suspended' if not p.is_active or vs == 'suspended' else 'Active')
        recent_pharm.append({
            'name': p.name,
            'type': 'Pharmacy',
            'status': pill,
            'pharmacy_id': p.pharmacy_id,
            'verification_status': vs,
            'created_at': p.created_at.isoformat() if p.created_at else None,
        })

    stuck_qs = (
        MedicineRequest.objects.filter(
            status__in=_active_request_statuses(),
            created_at__lte=now - timedelta(minutes=10),
        )
        .annotate(rc=Count('pharmacy_responses', distinct=True))
        .filter(rc=0)
        .select_related('conversation')
        .order_by('-created_at')
    )
    stuck_count = stuck_qs.count()
    awaiting_preview = []
    for r in stuck_qs[:12]:
        meds = ', '.join((r.medicine_names or [])[:4]) or 'Medicine request'
        area = (r.location_suburb or r.location_address or '')[:100]
        awaiting_preview.append({
            'request_id': str(r.request_id),
            'summary': meds,
            'area': area or 'Location unknown',
            'session_id': r.conversation.session_id if r.conversation else None,
        })

    base = {
        'generated_at': now.isoformat(),
        'nav_badges': nav,
        'open_alerts_count': int(sum(nav.values())),
        'layer1_system_health': {
            'active_users': {
                'current': dau,
                'change_pct': dau_delta,
                'peak_today': peak['peak'],
                'peak_time': peak['time_local'],
            },
            'requests_today': requests_today,
            'requests_last_hour': requests_last_hour,
            'avg_response': {
                'seconds': avg_s,
                'milliseconds': avg_ms,
                'urban_target_seconds': SLA_TARGET_MS_URBAN / 1000,
                'rural_target_seconds': SLA_TARGET_MS_RURAL / 1000,
                'sla_ok': avg_ms is None or avg_ms <= SLA_TARGET_MS_RURAL,
            },
            'pharmacies': {
                'active': reg_active,
                'registered': reg_total,
                'suspended': reg_susp,
                'pending_verification': reg_pending,
            },
            'uptime': {
                'percent_this_month': float(ps.reported_uptime_percent) if ps.reported_uptime_percent is not None else None,
                'incidents_note': None,
            },
            'request_volume_by_city': city_cards_30,
            'rural_share_requests_pct_30d': rural_share_30,
            'sla_narrative': sla_narrative,
            'sla_narrative_followup': sla_narrative_followup,
            'sla_explainer': {
                'window_days': 14,
                'title': 'How to read SLA by region',
                'intro': (
                    'Each row groups patient medicine requests by rough area (from suburb or city text in the address). '
                    'We measure the time until the first pharmacy sends a reply—not total time to collect all quotes.'
                ),
                'what_p95_means': (
                    'The seconds/minutes shown are a 95th percentile: about 95 in 100 “first replies” were faster than '
                    'this. It is not an average. With very few requests (small sample size), treat the number as indicative only.'
                ),
                'what_other_means': (
                    '“Other” means the address did not match a named bucket such as Harare or Bulawayo. '
                    'Those rows often have small samples and can swing a lot.'
                ),
                'urban_rural_target': (
                    f'Urban centres use a {SLA_TARGET_MS_URBAN / 1000:.0f} second goal; other buckets use a '
                    f'{SLA_TARGET_MS_RURAL / 1000:.0f} second goal for that first reply. These are operational targets for the dashboard.'
                ),
            },
            'sla_by_region': sla_rows,
            'sla_targets': {'urban_ms': SLA_TARGET_MS_URBAN, 'rural_ms': SLA_TARGET_MS_RURAL},
        },
        'layer2_pharmacy_governance': {
            'verification_queue': build_verification_queue(50),
            'watchlist': build_governance_watchlist_enriched(50),
            'aggregates': {
                'avg_response_rate': avg_rr_effective,
                'avg_response_rate_computed': avg_rr_computed,
                'avg_response_rate_db_field': round(float(avg_rr_stored), 2) if avg_rr_stored is not None else None,
                'response_rate_definition': (
                    f'Patient-request response rate: (how many patient medicine requests this pharmacy '
                    f'submitted a response to) ÷ (how many patient requests had a location within '
                    f'{COMPUTED_MATCH_RATE_RADIUS_KM} km of the pharmacy in the last '
                    f'{COMPUTED_MATCH_RATE_WINDOW_DAYS} days). Numerator counts a request if the pharmacy '
                    f'has at least one PharmacyResponse to that request in the window. '
                    f'Pharmacies without coordinates fall back to the stored response_rate field.'
                ),
                'avg_stock_accuracy_pct': avg_stock_accuracy,
                'avg_patient_rating': round(float(avg_rating), 2) if avg_rating is not None else None,
                'suspended_this_month': suspended_month,
            },
            'patient_requests_awaiting_response_count': stuck_count,
            'awaiting_response_preview': awaiting_preview,
        },
        'layer3_algorithm': {
            'standard_weights': {
                'price_competitiveness_pct': wdisp['price'],
                'distance_travel_pct': wdisp['distance'],
                'patient_rating_pct': wdisp['rating'],
                'stock_reliability_pct': wdisp['stock'],
                'total_pct': wdisp['price'] + wdisp['distance'] + wdisp['rating'] + wdisp['stock'],
            },
            'active_ranking_profile': active_prof,
            'context_profiles': profiles_ui,
            'raw_urban_weights': weights_percent_display(u_eff),
        },
        'layer4_impact': {
            'avg_find_time_minutes': round((_median([x for x in [urban_med, rural_med] if x is not None]) or 0), 1) if (urban_med or rural_med) else None,
            'avg_find_time_minutes_urban': round(urban_med, 1) if urban_med is not None else None,
            'avg_find_time_minutes_rural': round(rural_med, 1) if rural_med is not None else None,
            'baseline_hours_before': '2–4',
            'fulfilment': {
                'rate_pct': fulfil_pct,
                'fulfilled_count': fulfilled,
                'total_requests': total_eq,
            },
            'transport_saved': {
                'estimated_usd': transport_saved,
                'method': 'avg_response_distance_km * picked_up_reservations * 0.12',
            },
            'equity': {
                'gap_pct': round(gap, 1) if gap is not None else None,
                'urban_fulfilment_pct': round(u_rate, 1) if equity.get('fulfilment_rate_urban') is not None else None,
                'rural_fulfilment_pct': round(r_rate, 1) if equity.get('fulfilment_rate_rural') is not None else None,
                'coverage_urban_pct': None,
                'coverage_rural_pct': None,
            },
            'weekly_fulfilment': build_weekly_fulfilment_series(8),
            'equity_snapshot_note': 'Rural gap persists — consider Rural equity profile and rural onboarding.',
        },
        'layer5_ai_safety': {
            'flagged_preview': audit_cards,
            'chatbot_policy': policy,
            'safety_policies': safety_policies,
        },
        'widgets': {
            'search_volume': build_search_volume_bundle(7),
            'system_alerts': build_system_alerts(10),
            'pharmacy_match_rates': match_rates,
            'recent_registrations': recent_pharm,
        },
    }
    return apply_medi_bot_frontend_aliases(base)


def apply_medi_bot_frontend_aliases(data: dict) -> dict:
    """
    Add keys/aliases expected by MediBotOverviewSections / pharmacyfrontend spec
    without removing existing snake_case fields.
    """
    l1 = data.get('layer1_system_health') or {}
    au = l1.get('active_users')
    dau = au.get('current') if isinstance(au, dict) else None
    if dau is not None:
        l1['daily_active_users'] = dau
        l1['active_users_count'] = dau
    ar = l1.get('avg_response') or {}
    l1['avg_response_time_ms'] = ar.get('milliseconds')
    l1['avg_response_seconds'] = ar.get('seconds')
    ut = l1.get('uptime') or {}
    pct = ut.get('percent_this_month')
    l1['uptime_percent'] = pct
    l1['uptime_pct_this_month'] = pct
    ph = l1.get('pharmacies') or {}
    l1['pharmacy_counts'] = {
        'registered': ph.get('registered'),
        'total': ph.get('registered'),
        'active': ph.get('active'),
        'suspended': ph.get('suspended'),
        'pending_verification': ph.get('pending_verification'),
    }
    peak = au.get('peak_today') if isinstance(au, dict) else None
    pt = au.get('peak_time') if isinstance(au, dict) else None
    if peak and pt:
        l1['active_users_peak_label'] = f'Peak today: {peak:,} · {pt}'
    elif peak:
        l1['active_users_peak_label'] = f'Peak today: {peak:,}'
    l1['response_targets_label'] = (
        f"Urban <{SLA_TARGET_MS_URBAN / 1000:.0f}s · Rural <{SLA_TARGET_MS_RURAL / 1000:.0f}s"
    )

    for city in l1.get('request_volume_by_city') or []:
        city['key'] = city.get('geo_region')
        city['count'] = city.get('volume')
        city['kind'] = 'rural' if city.get('zone') == 'rural' else 'urban'
        city['city'] = city.get('label')
        if city.get('rural_share_pct') is not None:
            city['rural_share'] = city['rural_share_pct'] / 100.0

    for row in l1.get('sla_by_region') or []:
        row['label'] = row.get('display_name') or row.get('region')
        sec = row.get('p95_response_seconds')
        if sec is not None:
            row['seconds'] = sec
            # Legacy alias; value is 95th percentile, not mean — prefer p95_response_seconds / lay_detail.
            row['avg_seconds'] = sec
            row['latency_seconds'] = sec
        row['p95_label'] = '95th percentile (first reply)'
        row['plain_english_summary'] = row.get('lay_summary') or row.get('lay_detail')
        row['is_rural'] = row.get('zone') == 'rural'
        row['tier'] = 'rural' if row['is_rural'] else 'urban'
        row['suffix'] = 's'

    l2 = data.get('layer2_pharmacy_governance') or {}
    vq = l2.get('verification_queue') or []
    l2['verification_queue_items'] = vq
    l2['verification_queue_results'] = {'items': vq, 'results': vq, 'data': vq}
    for item in vq:
        item.setdefault('id', item.get('pharmacy_id'))
        item.setdefault('pharmacy_name', item.get('name'))
        item.setdefault('licence_number', item.get('license_number'))
        item.setdefault('lic', item.get('license_number'))
        item.setdefault('created_at', item.get('submitted_at'))
        item.setdefault('meta', {'location': item.get('address', '')})

    for w in l2.get('watchlist') or []:
        w.setdefault('title', w.get('name'))
        w.setdefault('name', w.get('name'))
        parts = [w.get('name', '')]
        if w.get('reasons'):
            parts.append(', '.join(w['reasons']))
        w.setdefault('body', ' · '.join(parts))
        w.setdefault('summary', w.get('name'))
        w.setdefault('detail', ', '.join(w.get('reasons') or []))
        w.setdefault('tone', w.get('severity', 'warning'))

    l3 = data.get('layer3_algorithm') or {}
    sw = l3.get('standard_weights') or {}
    flat = {
        'price': sw.get('price_competitiveness_pct'),
        'distance': sw.get('distance_travel_pct'),
        'rating': sw.get('patient_rating_pct'),
        'stock': sw.get('stock_reliability_pct'),
    }
    for k, v in flat.items():
        if v is not None:
            sw[k] = v
    sw.setdefault('price_pct', flat.get('price'))
    sw.setdefault('distance_pct', flat.get('distance'))
    sw.setdefault('rating_pct', flat.get('rating'))
    sw.setdefault('stock_pct', flat.get('stock'))
    sw.setdefault('travel', flat.get('distance'))
    sw.setdefault('patient_rating', flat.get('rating'))
    sw.setdefault('stock_reliability', flat.get('stock'))

    profiles_out = []
    for p in l3.get('context_profiles') or []:
        u = p.get('urban') or {}
        wdict = {
            'price': u.get('price'),
            'distance': u.get('distance'),
            'rating': u.get('rating'),
            'stock': u.get('reliability'),
        }
        wpct = [u.get('price'), u.get('distance'), u.get('rating'), u.get('reliability')]
        profiles_out.append({
            **p,
            'key': p.get('id'),
            'weights': wdict,
            'weights_pct': wpct,
        })
    l3['context_profiles'] = profiles_out
    l3['active_profile'] = l3.get('active_ranking_profile')

    l4 = data.get('layer4_impact') or {}
    weekly = l4.get('weekly_fulfilment') or []
    l4['weekly_urban_rural_fulfilment'] = []
    for wk in weekly:
        label = (wk.get('week_start') or '')[:10]
        l4['weekly_urban_rural_fulfilment'].append({
            'label': label,
            'week': wk.get('week_start'),
            'urban': wk.get('urban_fulfilment_pct'),
            'urban_pct': wk.get('urban_fulfilment_pct'),
            'rural': wk.get('rural_fulfilment_pct'),
            'rural_pct': wk.get('rural_fulfilment_pct'),
        })
    l4['weekly_fulfilment_series'] = l4['weekly_urban_rural_fulfilment']

    med = l4.get('avg_find_time_minutes')
    l4['median_find_time_minutes'] = med
    l4['median_find_time_display'] = f'{med:g} min' if med is not None else None
    ful = l4.get('fulfilment') or {}
    l4['fulfilment_pct'] = ful.get('rate_pct')
    l4['fulfilment_done'] = ful.get('fulfilled_count')
    l4['fulfilment_total'] = ful.get('total_requests')
    if ful.get('fulfilled_count') is not None and ful.get('total_requests') is not None:
        l4['fulfilment_counts_label'] = f"{ful['fulfilled_count']:,} / {ful['total_requests']:,} req."
    ts = l4.get('transport_saved') or {}
    l4['transport_savings_estimate'] = ts.get('estimated_usd')
    eq = l4.get('equity') or {}
    l4['equity_gap_pct'] = eq.get('gap_pct')
    l4['urban_fulfilment_pct'] = eq.get('urban_fulfilment_pct')
    l4['rural_fulfilment_pct'] = eq.get('rural_fulfilment_pct')
    l4['equity_snapshot'] = eq
    l4['equity_narrative'] = l4.get('equity_snapshot_note')

    l5 = data.get('layer5_ai_safety') or {}
    actions_ui = ['Review', 'Escalate', 'Approve', 'flag_unsafe']
    for card in l5.get('flagged_preview') or []:
        card.setdefault('question', card.get('patient_query'))
        card.setdefault('summary', (card.get('patient_query') or '')[:120])
        card.setdefault('title', (card.get('status') or 'review').replace('_', ' ').title())
        card.setdefault('when', card.get('created_at'))
        card.setdefault('response', card.get('bot_response'))
        card.setdefault('bot_reply', card.get('bot_response'))
        card.setdefault('tone', card.get('status'))
        card.setdefault('actions', actions_ui)

    pol = l5.get('chatbot_policy') or {}
    l5['safety_policies_compact'] = {
        'disclaimer': pol.get('medical_disclaimer_all_responses', True),
        'dosage': pol.get('restrict_dosage_advice', True),
        'paediatric': pol.get('paediatric_warning_under_5', True),
        'emergency': pol.get('emergency_symptom_detection', True),
        'rx_flag': pol.get('prescription_only_flag', False),
    }

    nb = data.get('nav_badges') or {}
    data['navBadges'] = {
        'verificationQueue': nb.get('verification_queue', 0),
        'watchlist': nb.get('watchlist', 0),
        'chatbotAudit': nb.get('chatbot_audit', 0),
    }

    return data
