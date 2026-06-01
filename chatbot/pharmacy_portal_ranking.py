"""
Pharmacy portal leaderboard score (0–100), aligned with patient MCDA weights from PlatformAdminSettings:

  score = round(w_price·P + w_distance·D + w_rating·T + w_reliability·Rel)  clamped 0–100

P = price competitiveness % (0–100), D = proximity %, T = patient rating %,
Rel = average(response_rate %, stock_reliability %) so four inputs match four MCDA dimensions.

Weights = RankingEngine.get_context_weights(density at this pharmacy’s coordinates): urban if ≥3
verified pharmacies within 5 km of the listing, else rural — same rule as patient ranking.

Price competitiveness uses PharmacyInventory the way pharmacist inventory GET does.
D uses median distance_km on PharmacyResponses where medicine_available=True (90d).
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from datetime import timedelta
from typing import Any

from django.db.models import F
from django.utils import timezone

from .admin_analytics import CITY_CARD_META, compute_effective_pharmacy_response_rates_for_ids, geo_region_key
from .models import Pharmacy, PharmacyCompositeScoreSnapshot, PharmacyInventory, PharmacyResponse

COMPUTED_DISTANCE_WINDOW_DAYS = 90


def ensure_pharmacy_response_distance_saved(
    resp: PharmacyResponse,
    *,
    allow_geocode: bool = False,
) -> float | None:
    """
    Return usable distance_km (patient request → responding pharmacy). If the row has no value
    but both sides have coordinates, compute Haversine distance and persist ``distance_km`` +
    ``estimated_travel_time`` so ranking and history charts include the response.

    When ``allow_geocode`` is True and the pharmacy has an address but no lat/lon, geocode once
    and update the pharmacy row (same pattern as patient-facing ranking flow).
    """
    if resp.distance_km is not None:
        try:
            v = float(resp.distance_km)
            if v >= 0:
                return v
        except (TypeError, ValueError):
            pass

    req = getattr(resp, 'request', None)
    ph = getattr(resp, 'pharmacy', None)
    if not req or not ph:
        return None
    if req.location_latitude is None or req.location_longitude is None:
        return None

    plat, plon = ph.latitude, ph.longitude
    if (plat is None or plon is None) and allow_geocode and (getattr(ph, 'address', None) or '').strip():
        try:
            from .services import LocationService

            lat, lon = LocationService.geocode_address(ph.address)
            if lat is not None and lon is not None:
                Pharmacy.objects.filter(pk=ph.pk).update(latitude=lat, longitude=lon)
                plat, plon = float(lat), float(lon)
                ph.latitude, ph.longitude = plat, plon
        except Exception:
            pass

    if plat is None or plon is None:
        return None

    try:
        from .services import LocationService

        d = float(
            LocationService.calculate_distance(
                float(req.location_latitude),
                float(req.location_longitude),
                float(plat),
                float(plon),
            )
        )
        if d < 0:
            return None
        tt = int(LocationService.estimate_travel_time(d, 'urban'))
        PharmacyResponse.objects.filter(pk=resp.pk).update(distance_km=d, estimated_travel_time=tt)
        resp.distance_km = d
        resp.estimated_travel_time = tt
        return d
    except Exception:
        return None


def reliability_composite_pct(response_rate_pct: float, stock_reliability_pct: float) -> float:
    """Single 0–100 reliability input: average of response rate and stock reliability (one MCDA dimension)."""
    return round((float(response_rate_pct) + float(stock_reliability_pct)) / 2.0, 1)


def get_portal_mcda_weights(pharmacy: Pharmacy) -> tuple[dict[str, float], str]:
    """MCDA weights for patient ranking, from admin settings + density at this pharmacy’s coordinates."""
    from .services import RankingEngine

    density = 0
    if pharmacy.latitude is not None and pharmacy.longitude is not None:
        try:
            density = RankingEngine.calculate_pharmacy_density(float(pharmacy.latitude), float(pharmacy.longitude))
        except Exception:
            density = 0
    w = RankingEngine.get_context_weights(density)
    ctx = 'urban' if density >= 3 else 'rural'
    return w, ctx


def portal_mcda_score(P: float, D: float, T: float, Rel: float, w: dict[str, float]) -> int:
    raw = (
        w['price'] * P
        + w['distance'] * D
        + w['rating'] * T
        + w['reliability'] * Rel
    )
    return max(0, min(100, int(round(raw))))


def _dynamic_formula_string(w: dict[str, float]) -> str:
    return (
        f"{w['price']:.4f}×P+{w['distance']:.4f}×D+{w['rating']:.4f}×T+{w['reliability']:.4f}×Rel"
    )


def portal_composite_weights_payload(pharmacy: Pharmacy) -> dict[str, Any]:
    from .admin_analytics import get_platform_settings

    ps = get_platform_settings()
    w, ctx = get_portal_mcda_weights(pharmacy)
    comps = [
        {'criterion': 'price', 'field': 'price_competitiveness_pct', 'mcda_key': 'price', 'weight': w['price'], 'weight_percent': round(100.0 * w['price'], 1)},
        {'criterion': 'distance', 'field': 'distance_pct', 'mcda_key': 'distance', 'weight': w['distance'], 'weight_percent': round(100.0 * w['distance'], 1)},
        {'criterion': 'rating', 'field': 'patient_rating_pct', 'mcda_key': 'rating', 'weight': w['rating'], 'weight_percent': round(100.0 * w['rating'], 1)},
        {'criterion': 'reliability', 'field': 'reliability_composite_pct', 'mcda_key': 'reliability', 'weight': w['reliability'], 'weight_percent': round(100.0 * w['reliability'], 1)},
    ]
    return {
        'scoring_method': 'pharmacy_portal_mcda_admin_aligned',
        'active_ranking_profile': (ps.active_ranking_profile or 'urban_default').strip(),
        'context': ctx,
        'context_rule': 'urban if >=3 verified pharmacies within 5 km of this pharmacy coordinates, else rural',
        'weights': {k: round(float(v), 4) for k, v in w.items()},
        'weights_percent': {k: round(100 * float(v), 1) for k, v in w.items()},
        'components': comps,
        'reliability_input': (
            'reliability_composite_pct = average of response_rate_pct and stock_reliability_pct (each 0-100).'
        ),
        'score_formula': 'round(sum of weight * input) clamped 0-100; weights sum to 1',
        'weighted_sum_linear': _dynamic_formula_string(w),
    }


def portal_composite_breakdown_for_row(pharmacy: Pharmacy, row: dict) -> dict[str, Any]:
    w, ctx = get_portal_mcda_weights(pharmacy)
    P = float(row['price_competitiveness_pct'])
    D = float(row['distance_pct'])
    T = float(row['patient_rating_pct'])
    Rel = float(row['reliability_composite_pct'])
    pieces = [
        ('price', 'price_competitiveness_pct', w['price'], P),
        ('distance', 'distance_pct', w['distance'], D),
        ('rating', 'patient_rating_pct', w['rating'], T),
        ('reliability', 'reliability_composite_pct', w['reliability'], Rel),
    ]
    contributions: list[dict[str, Any]] = []
    raw = 0.0
    for crit, field, wt, v in pieces:
        c = wt * v
        raw += c
        entry: dict[str, Any] = {
            'criterion': crit,
            'field': field,
            'weight': wt,
            'weight_percent': round(100.0 * wt, 1),
            'input_0_100': round(v, 1),
            'weight_times_input': round(c, 4),
        }
        if crit == 'reliability':
            entry['from_response_rate_pct'] = round(float(row['response_rate_pct']), 1)
            entry['from_stock_reliability_pct'] = round(float(row['stock_reliability_pct']), 1)
        contributions.append(entry)
    score = max(0, min(100, int(round(raw))))
    return {
        'context': ctx,
        'weighted_sum': round(raw, 4),
        'score_before_round': round(raw, 4),
        'ranking_score_0_100': score,
        'contributions': contributions,
    }


def _score_row_for_pharmacy(p: Pharmacy, row: dict) -> int:
    w, _ = get_portal_mcda_weights(p)
    return portal_mcda_score(
        float(row['price_competitiveness_pct']),
        float(row['distance_pct']),
        float(row['patient_rating_pct']),
        float(row['reliability_composite_pct']),
        w,
    )


def _norm_medicine_name(name: str) -> str:
    """Normalize for cross-pharmacy matching (same medicine, different spacing/case)."""
    return " ".join((name or "").strip().lower().split())


def _price_key(name: str) -> str:
    """
    Product key for peer pricing: normalized name with optional strength suffix folded
    (e.g. 'paracetamol' and 'paracetamol 500mg' compare together — same as dashboard lines).
    """
    base = _norm_medicine_name(name)
    if not base:
        return ""
    folded = re.sub(r"\s+\d+(\.\d+)?\s*mg\b", "", base, flags=re.IGNORECASE).strip()
    return folded or base


def _ratio_to_price_score(ratio: float) -> float:
    """ratio = this pharmacy price / network median for that medicine (lower ratio = cheaper vs peers)."""
    if ratio <= 0.85:
        return 95.0
    if ratio <= 1.0:
        return 82.0
    if ratio <= 1.12:
        return 68.0
    if ratio <= 1.25:
        return 55.0
    return 45.0


def _inventory_qs_priced_sellable():
    """Same stock lines as patient-facing inventory: active pharmacy, price set, available qty > 0."""
    return PharmacyInventory.objects.filter(
        pharmacy__is_active=True,
        quantity__gt=F("reserved_quantity"),
    ).exclude(price__isnull=True)


def build_medicine_price_index(max_rows: int = 80000) -> dict[str, list[tuple[str, float]]]:
    """
    Build peer price table from PharmacyInventory (pharmacist stock lines): for each product key
    (_price_key), one benchmark unit price per pharmacy (minimum across lines that map to that key).
    """
    raw: dict[str, list[tuple[str, float]]] = defaultdict(list)
    qs = _inventory_qs_priced_sellable().values_list("pharmacy_id", "medicine_name", "price")[:max_rows]
    for pharmacy_id, med_name, price in qs:
        key = _price_key(med_name)
        if not key:
            continue
        try:
            raw[key].append((str(pharmacy_id), float(price)))
        except (TypeError, ValueError):
            continue

    # One price per pharmacy per product key (cheapest listed line for that product at that branch)
    by_med: dict[str, list[tuple[str, float]]] = {}
    for key, pairs in raw.items():
        best: dict[str, float] = {}
        for pid, pr in pairs:
            if pid not in best or pr < best[pid]:
                best[pid] = pr
        by_med[key] = [(pid, p) for pid, p in best.items()]
    return by_med


def price_competitiveness_pct(
    pharmacy: Pharmacy,
    *,
    medicine_price_index: dict[str, list[tuple[str, float]]] | None = None,
) -> float:
    """
    Compare this branch's **PharmacyInventory** prices (same rows as pharmacist GET inventory) to
    other pharmacies for the same product key: network median of peers' best price per key.

    Uses only sellable, priced lines (quantity > reserved), matching dashboard stock lines.
    """
    index = medicine_price_index if medicine_price_index is not None else build_medicine_price_index()
    inv = list(
        _inventory_qs_priced_sellable()
        .filter(pharmacy=pharmacy)
        .order_by("medicine_name")[:400]
    )
    if not inv:
        return 72.0

    # Best unit price per product key at this branch (two lines "paracetamol" / "paracetamol 500mg" → one key)
    my_best: dict[str, float] = {}
    for row in inv:
        key = _price_key(row.medicine_name)
        if not key:
            continue
        try:
            pr = float(row.price)
        except (TypeError, ValueError):
            continue
        if key not in my_best or pr < my_best[key]:
            my_best[key] = pr

    contributions: list[float] = []
    for key, my_price in my_best.items():
        rows = index.get(key) or []
        all_prices = [p for _pid, p in rows]
        if len(all_prices) < 2:
            continue
        median_net = statistics.median(all_prices)
        if median_net <= 0:
            continue
        ratio = my_price / median_net
        contributions.append(_ratio_to_price_score(ratio))

    if not contributions:
        return 72.0
    return float(statistics.mean(contributions))


def stock_reliability_pct(pharmacy: Pharmacy) -> float:
    """
    Compare lines **at or above** `low_stock_threshold` vs lines in the **low-stock band** (same as
    pharmacist inventory GET: in_stock vs low_stock among SKUs that still have quantity > 0).

    - Above threshold: quantity >= low_stock_threshold
    - Low stock: 0 < quantity < low_stock_threshold
    - Out of stock (quantity <= 0) does not enter this ratio.

    S = 100 × above ÷ (above + low). If no line has quantity > 0, S = 0.
    """
    inv = list(pharmacy.inventory.all()[:500])
    if not inv:
        return 55.0
    above = sum(1 for i in inv if i.quantity >= i.low_stock_threshold)
    low = sum(1 for i in inv if 0 < i.quantity < i.low_stock_threshold)
    stocked = above + low
    if stocked <= 0:
        return 0.0
    return round(100.0 * above / stocked, 1)


def patient_rating_pct(pharmacy: Pharmacy) -> float:
    r = float(pharmacy.rating or 0)
    return round((r / 5.0) * 100, 1) if r > 0 else 0.0


def response_rate_pct(pharmacy: Pharmacy) -> float:
    m = compute_effective_pharmacy_response_rates_for_ids([pharmacy.pharmacy_id])
    return m.get(str(pharmacy.pharmacy_id), float(pharmacy.response_rate or 100))


def build_median_response_distance_km_by_pharmacy(pharmacy_ids: list[str]) -> dict[str, float | None]:
    """
    Median distance_km per pharmacy for responses where medicine was **available** (patient-facing
    “nearest pharmacy with medicine” signal). Rows with medicine_available=False are excluded.

    Responses with null ``distance_km`` are still considered: when the patient request and pharmacy
    have coordinates, distance is computed and **persisted** so future queries are cheap.
    Geocoding is not run here (only on submit / explicit patient GET) to keep leaderboard fast.
    """
    if not pharmacy_ids:
        return {}
    start = timezone.now() - timedelta(days=COMPUTED_DISTANCE_WINDOW_DAYS)
    bucket: dict[str, list[float]] = defaultdict(list)
    qs = (
        PharmacyResponse.objects.filter(
            submitted_at__gte=start,
            pharmacy__isnull=False,
            medicine_available=True,
        )
        .select_related('request', 'pharmacy')
        .order_by('response_id')
    )
    for resp in qs.iterator(chunk_size=500):
        dkm = ensure_pharmacy_response_distance_saved(resp, allow_geocode=False)
        if dkm is None:
            continue
        pid = getattr(resp.pharmacy, 'pharmacy_id', None)
        if pid is None:
            continue
        bucket[str(pid)].append(float(dkm))
    out: dict[str, float | None] = {}
    for pid in pharmacy_ids:
        key = str(pid)
        vals = bucket.get(key)
        if not vals:
            out[key] = None
        else:
            out[key] = float(statistics.median(vals))
    return out


def proximity_pct(
    pharmacy_id: str,
    median_by_pharmacy: dict[str, float | None],
) -> float:
    """
    0–100: lower median distance (when medicine was available) vs peers = higher score.
    No qualifying responses → neutral 72.
    """
    med = median_by_pharmacy.get(str(pharmacy_id))
    comps = [v for v in median_by_pharmacy.values() if v is not None and v >= 0]
    if med is None or not comps:
        return 72.0
    lo, hi = min(comps), max(comps)
    if hi <= lo:
        return 85.0
    x = (hi - float(med)) / (hi - lo)
    return round(100.0 * max(0.0, min(1.0, x)), 1)


def compute_pharmacy_ranking_bundle(
    pharmacy: Pharmacy,
    *,
    median_by_pharmacy: dict[str, float | None] | None = None,
) -> dict[str, Any]:
    if median_by_pharmacy is None:
        ids = list(Pharmacy.objects.filter(is_active=True).values_list('pharmacy_id', flat=True))
        median_by_pharmacy = build_median_response_distance_km_by_pharmacy(ids)
    P = price_competitiveness_pct(pharmacy)
    R = response_rate_pct(pharmacy)
    S = stock_reliability_pct(pharmacy)
    T = patient_rating_pct(pharmacy)
    D = proximity_pct(pharmacy.pharmacy_id, median_by_pharmacy)
    row = {
        'price_competitiveness_pct': round(P, 1),
        'response_rate_pct': round(R, 1),
        'stock_reliability_pct': round(S, 1),
        'patient_rating_pct': round(T, 1),
        'distance_pct': round(D, 1),
    }
    row['reliability_composite_pct'] = reliability_composite_pct(
        row['response_rate_pct'], row['stock_reliability_pct'],
    )
    row['ranking_score_0_100'] = _score_row_for_pharmacy(pharmacy, row)
    w, _ = get_portal_mcda_weights(pharmacy)
    return {
        **row,
        'formula': _dynamic_formula_string(w),
    }


def leaderboard_for_pharmacy(pharmacy: Pharmacy) -> dict[str, Any]:
    """
    Rank all active pharmacies by the same composite score, and return the full ordered list
    plus this pharmacy's position.
    """
    region = geo_region_key('', pharmacy.address or '')
    cohort = list(
        Pharmacy.objects.filter(is_active=True)
        .prefetch_related('inventory')
        .order_by('name', 'pharmacy_id')
    )
    med_price_index = build_medicine_price_index()
    rr_by_id = compute_effective_pharmacy_response_rates_for_ids([p.pharmacy_id for p in cohort])
    median_by = build_median_response_distance_km_by_pharmacy([p.pharmacy_id for p in cohort])

    rows: list[dict[str, Any]] = []
    for p in cohort:
        P = price_competitiveness_pct(p, medicine_price_index=med_price_index)
        R = rr_by_id.get(str(p.pharmacy_id), float(p.response_rate or 100))
        S = stock_reliability_pct(p)
        T = patient_rating_pct(p)
        D = proximity_pct(p.pharmacy_id, median_by)
        r = {
            'pharmacy_id': p.pharmacy_id,
            'pharmacy_name': p.name,
            'price_competitiveness_pct': round(P, 1),
            'response_rate_pct': round(R, 1),
            'stock_reliability_pct': round(S, 1),
            'patient_rating_pct': round(T, 1),
            'distance_pct': round(D, 1),
        }
        r['reliability_composite_pct'] = reliability_composite_pct(
            r['response_rate_pct'], r['stock_reliability_pct'],
        )
        r['ranking_score_0_100'] = _score_row_for_pharmacy(p, r)
        rows.append(r)
    rows.sort(key=lambda r: (-r['ranking_score_0_100'], r['pharmacy_id']))

    leaderboard: list[dict[str, Any]] = []
    my_rank: int | None = None
    for i, r in enumerate(rows, 1):
        entry = {'rank': i, **r}
        leaderboard.append(entry)
        if r['pharmacy_id'] == pharmacy.pharmacy_id:
            my_rank = i

    area_label = CITY_CARD_META.get(region, {}).get('label', region.title() if region else 'National')
    return {
        'leaderboard_rank': my_rank,
        'leaderboard_total': len(leaderboard),
        'leaderboard_area': area_label,
        'leaderboard_area_key': region or 'other',
        'leaderboard_scope': 'all_active_pharmacies',
        'leaderboard': leaderboard,
    }


def _composite_fingerprint(bundle: dict, lb: dict) -> tuple:
    """Stable tuple for deduplicating consecutive identical snapshots."""
    return (
        int(bundle['ranking_score_0_100']),
        round(float(bundle['price_competitiveness_pct']), 1),
        round(float(bundle['response_rate_pct']), 1),
        round(float(bundle['stock_reliability_pct']), 1),
        round(float(bundle['patient_rating_pct']), 1),
        round(float(bundle.get('distance_pct', 0)), 1),
        lb.get('leaderboard_rank'),
        lb.get('leaderboard_total'),
    )


def persist_pharmacy_composite_score_snapshot(pharmacy: Pharmacy, bundle: dict, lb: dict) -> None:
    """Store a new row only when composite score or rank changed vs last snapshot."""
    fp = _composite_fingerprint(bundle, lb)
    prev = (
        PharmacyCompositeScoreSnapshot.objects.filter(pharmacy=pharmacy)
        .order_by('-created_at')
        .first()
    )
    if prev:
        prev_fp = (
            int(prev.ranking_score_0_100),
            round(prev.price_competitiveness_pct, 1),
            round(prev.response_rate_pct, 1),
            round(prev.stock_reliability_pct, 1),
            round(prev.patient_rating_pct, 1),
            round(prev.distance_pct or 0.0, 1),
            prev.leaderboard_rank,
            prev.leaderboard_total,
        )
        if prev_fp == fp:
            return
    w_snap, _ = get_portal_mcda_weights(pharmacy)
    formula_stored = ((bundle.get('formula') or '').strip()[:96] or _dynamic_formula_string(w_snap))
    PharmacyCompositeScoreSnapshot.objects.create(
        pharmacy=pharmacy,
        ranking_score_0_100=int(bundle['ranking_score_0_100']),
        price_competitiveness_pct=float(bundle['price_competitiveness_pct']),
        response_rate_pct=float(bundle['response_rate_pct']),
        stock_reliability_pct=float(bundle['stock_reliability_pct']),
        patient_rating_pct=float(bundle['patient_rating_pct']),
        distance_pct=float(bundle.get('distance_pct') or 0.0),
        formula=formula_stored,
        leaderboard_rank=lb.get('leaderboard_rank'),
        leaderboard_total=lb.get('leaderboard_total'),
    )


def fetch_pharmacy_score_history(pharmacy: Pharmacy, limit: int) -> list[dict[str, Any]]:
    """Oldest → newest points for charts (last `limit` snapshots)."""
    lim = max(1, min(int(limit), 500))
    rows = list(
        PharmacyCompositeScoreSnapshot.objects.filter(pharmacy=pharmacy).order_by('-created_at')[:lim]
    )
    rows.reverse()
    out: list[dict[str, Any]] = []
    for s in rows:
        out.append({
            'recorded_at': s.created_at.isoformat() if s.created_at else None,
            'ranking_score_0_100': s.ranking_score_0_100,
            'price_competitiveness_pct': round(s.price_competitiveness_pct, 1),
            'response_rate_pct': round(s.response_rate_pct, 1),
            'stock_reliability_pct': round(s.stock_reliability_pct, 1),
            'patient_rating_pct': round(s.patient_rating_pct, 1),
            'distance_pct': round(s.distance_pct, 1) if getattr(s, 'distance_pct', None) is not None else None,
            'leaderboard_rank': s.leaderboard_rank,
            'leaderboard_total': s.leaderboard_total,
            'formula': s.formula,
        })
    return out


def pharmacist_ranking_summary_payload(pharmacist, *, history_limit: int = 60) -> dict[str, Any]:
    pharmacy = pharmacist.pharmacy
    lb = leaderboard_for_pharmacy(pharmacy)
    me = next((e for e in lb['leaderboard'] if e['pharmacy_id'] == pharmacy.pharmacy_id), None)
    w_applied, _ctx0 = get_portal_mcda_weights(pharmacy)
    if me:
        rel_c = me.get('reliability_composite_pct')
        if rel_c is None:
            rel_c = reliability_composite_pct(me['response_rate_pct'], me['stock_reliability_pct'])
        bundle = {
            'ranking_score_0_100': me['ranking_score_0_100'],
            'price_competitiveness_pct': me['price_competitiveness_pct'],
            'response_rate_pct': me['response_rate_pct'],
            'stock_reliability_pct': me['stock_reliability_pct'],
            'reliability_composite_pct': rel_c,
            'patient_rating_pct': me['patient_rating_pct'],
            'distance_pct': me['distance_pct'],
            'formula': _dynamic_formula_string(w_applied),
        }
    else:
        bundle = compute_pharmacy_ranking_bundle(pharmacy)
    persist_pharmacy_composite_score_snapshot(pharmacy, bundle, lb)
    score_history = fetch_pharmacy_score_history(pharmacy, history_limit)
    bd = portal_composite_breakdown_for_row(pharmacy, bundle)
    formula_current = _dynamic_formula_string(w_applied)
    return {
        'ranking_summary_payload_version': 2,
        'pharmacy_id': pharmacy.pharmacy_id,
        'pharmacy_name': pharmacy.name,
        'pharmacist_id': str(pharmacist.pharmacist_id),
        **bundle,
        **lb,
        'formula': formula_current,
        'algorithm_source': (
            'Weights and context (urban vs rural) match PlatformAdminSettings / RankingEngine — same MCDA '
            'as patient ranking. Changing the active profile in admin updates these coefficients.'
        ),
        'composite_weights': portal_composite_weights_payload(pharmacy),
        'composite_breakdown': bd,
        'score_history': score_history,
        'definitions': {
            'P': (
                'Price competitiveness (0–100). Per inventory line with price + sellable stock: your best '
                'unit price vs network median; variants like paracetamol / paracetamol 500mg are grouped.'
            ),
            'D': (
                'Distance / proximity (0–100). Median distance_km on PharmacyResponses with '
                'medicine_available=true (90d), vs peers. No such responses → neutral 72.'
            ),
            'T': 'Patient rating (0–100): average rating as % of 5 stars.',
            'Rel': (
                'Reliability (0–100), the fourth MCDA criterion (admin “reliability / stock” weight): '
                'average of response match rate (R) and stock reliability (S). Score = round(P×wP + D×wD + '
                'T×wT + Rel×wRel) clamped 0–100 with weights from the active admin profile.'
            ),
            'R': (
                'Response rate input (0–100): share of nearby patient requests (90d, 50km) with ≥1 response; '
                'else stored field. Fed into Rel (half).'
            ),
            'S': (
                'Stock reliability input (0–100): among stocked lines, % at/above low_stock_threshold vs '
                'low-stock band (0 < qty < threshold). Out-of-stock lines excluded. Fed into Rel (half).'
            ),
        },
    }
