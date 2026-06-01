"""
Consolidated pharmacist portal settings envelope (profile / operations / notifications / service).

Used by GET/PATCH ``/api/chatbot/pharmacist/settings/``. See docs/PHARMACY_SETTINGS_BACKEND_SPEC.md.
"""

from __future__ import annotations

from chatbot.admin_analytics import get_platform_settings
from chatbot.models import PharmacySettings

_DIGEST = {'instant', 'daily', 'weekly', 'muted'}
_OSB = {'hide', 'allow_backorder', 'notify_only'}
_UIDENS = {'compact', 'comfortable'}


def _strip_str(val, maxlen=None):
    if val is None:
        return ''
    s = str(val).strip()
    if maxlen:
        return s[:maxlen]
    return s


def _safe_profile_str(val, maxlen=None):
    """
    Coerce profile/contact inputs for JSON output and PATCH.
    Rejects dict/list (e.g. a mistaken nested API error body) so we never store or return
    ``{"detail": "Authentication credentials were not provided."}`` as ``whatsapp``.
    """
    if val is None:
        return ''
    if isinstance(val, (dict, list, tuple, set)):
        return ''
    s = str(val).strip()
    if maxlen is not None:
        s = s[:maxlen]
    return s


def _opening_hours_as_dict(raw) -> dict:
    """PharmacySettings.opening_hours must be an object — tolerate bad legacy shapes."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    return {}


def serialize_pharmacist_settings_envelope(pharmacist, settings_obj: PharmacySettings) -> dict:
    ph = pharmacist
    p = pharmacist.pharmacy
    s = settings_obj
    dn = (_strip_str(getattr(ph, 'display_name', '')) or ph.full_name)
    plat = get_platform_settings()
    oh = _opening_hours_as_dict(getattr(s, 'opening_hours', {}))
    pause_outside = bool(getattr(s, 'pause_outside_opening_hours', False))
    udf = getattr(s, 'ui_default_filters', None) or {}
    if not isinstance(udf, dict):
        udf = {}
    return {
        'profile': {
            'pharmacist_id': str(ph.pharmacist_id),
            'pharmacy_id': p.pharmacy_id,
            'verification_status': p.verification_status,
            'name': _safe_profile_str(p.name, 255),
            'display_name': dn,
            'license_number': _safe_profile_str(ph.license_number, 100),
            'tax_number': _safe_profile_str(getattr(p, 'tax_number', ''), 120),
            'address': _safe_profile_str(p.address, 500),
            'phone': _safe_profile_str(p.phone, 20),
            'whatsapp': _safe_profile_str(getattr(p, 'whatsapp', ''), 40),
            'email': _safe_profile_str((p.email or ph.email or '').strip(), 254),
            'website': _safe_profile_str(getattr(p, 'website', ''), 512),
            'description': _safe_profile_str(getattr(p, 'description', ''), 8000),
            'branch_name': s.branch_name or '',
            'city': s.city or '',
            'geo_region': s.geo_region or '',
        },
        'operations': {
            'accepting_requests': bool(getattr(s, 'accepting_requests', True)),
            'opening_hours': oh,
            'weekday_open': _strip_str(oh.get('weekday_open'), 12),
            'weekday_close': _strip_str(oh.get('weekday_close'), 12),
            'opening_hours_text': _strip_str(oh.get('opening_hours_text'), 8000),
            'holiday_notes': _strip_str(oh.get('holiday_notes'), 2000),
            'timezone': s.timezone,
            'holiday_mode': s.holiday_mode,
            'pause_outside_opening_hours': pause_outside,
            'pause_requests_outside_hours': pause_outside,
            'auto_accept_reservations': s.auto_accept_reservations,
            'max_reservation_window_minutes': s.max_reservation_window_minutes,
            'low_stock_threshold_default': s.low_stock_threshold_default,
            'out_of_stock_behavior': s.out_of_stock_behavior,
            'auto_substitute_enabled': s.auto_substitute_enabled,
        },
        'notifications': {
            'channels': {
                'email': bool(s.notify_channel_email),
                'sms': bool(s.notify_channel_sms),
                'in_app': bool(s.notify_channel_in_app),
            },
            'notify_new_request': s.notify_new_request,
            'notify_low_stock': s.notify_low_stock,
            'notify_reservation_expiry': s.notify_reservation_expiry,
            'email_on_low_stock': bool(s.notify_low_stock),
            'quiet_hours': getattr(s, 'notify_quiet_hours', {}) or {},
            'digest_frequency': getattr(s, 'notifications_digest_frequency', 'instant') or 'instant',
        },
        'service': {
            'radius_km': s.service_radius_km,
            'pickup_available': bool(getattr(s, 'service_pickup_available', True)),
            'delivery_available': bool(getattr(s, 'service_delivery_available', False)),
            'areas_covered': getattr(s, 'service_areas_covered', []) or [],
        },
        'preferences': {
            'disclaimer_visible': s.disclaimer_visible,
            'prescription_enforcement': s.prescription_enforcement,
            'audit_logging_enabled': s.audit_logging_enabled,
            'ui_dark_mode': s.ui_dark_mode,
            'ui_table_density': s.ui_table_density,
            'ui_default_page_size': s.ui_default_page_size,
            'ui_default_filters': udf,
            'preferred_profile': s.preferred_profile or '',
            'hide_zero_quantity_in_search': _coerce_bool(udf.get('hide_zero_quantity_in_search', False)),
            'auto_suggest_shift_on_open': _coerce_bool(
                udf.get('auto_suggest_shift_on_open', True)
            ),
        },
        'meta': {
            'feature_flags': {
                'settings_history': True,
                'settings_reset': True,
                'test_notification': True,
                'security_panel': True,
            },
            'current_ranking_profile': (plat.active_ranking_profile or 'urban_default'),
            'settings_version': s.version,
            'settings_updated_at': s.updated_at.isoformat() if s.updated_at else None,
            'mfa_totp_enabled': bool(getattr(ph, 'mfa_totp_enabled', False)),
        },
    }


def _coerce_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(v)


def patch_pharmacist_settings_envelope(pharmacist, settings_obj: PharmacySettings, raw: dict):
    """
    Apply nested PATCH sections. Mutates pharmacist, pharmacy row, PharmacySettings row.
    Returns dict changelog for PharmacySettingsHistory, or raises ValueError(message).
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError('Body must be a JSON object')
    changelog: dict = {}

    pf = raw.get('profile')
    if isinstance(pf, dict):
        pch = {}
        p = pharmacist.pharmacy
        str_map = {
            'name': ('name', 255),
            'address': ('address', 500),
            'phone': ('phone', 20),
            'whatsapp': ('whatsapp', 40),
            'email': ('email', 254),
            'tax_number': ('tax_number', 120),
            'website': ('website', 512),
            'description': ('description', 8000),
        }
        for k, (attr, mx) in str_map.items():
            if k in pf:
                val = _safe_profile_str(pf.get(k), mx)
                if getattr(p, attr) != val:
                    pch[k] = {'from': getattr(p, attr), 'to': val}
                    setattr(p, attr, val)
        if 'display_name' in pf:
            new_dn = _strip_str(pf.get('display_name'), 200)
            if pharmacist.display_name != new_dn:
                pch['display_name'] = {'from': pharmacist.display_name, 'to': new_dn}
                pharmacist.display_name = new_dn
        if 'license_number' in pf:
            ln = _strip_str(pf.get('license_number'), 100)
            if pharmacist.license_number != ln:
                pch['license_number'] = {'from': pharmacist.license_number, 'to': ln}
                pharmacist.license_number = ln
        for k in ('branch_name', 'city', 'geo_region'):
            if k in pf:
                val = _strip_str(pf.get(k), 255 if k == 'branch_name' else 120)
                prev = getattr(settings_obj, k, '')
                if prev != val:
                    pch[k] = {'from': prev, 'to': val}
                    setattr(settings_obj, k, val)
        if pch:
            changelog['profile'] = pch
            pharm_keys = [x for x in ('display_name', 'license_number') if x in pch]
            if pharm_keys:
                pharm_keys.append('updated_at')
                pharmacist.save(update_fields=pharm_keys)
            pkeys = [k for k in str_map if k in pch]
            if pkeys:
                pkeys.append('updated_at')
                p.save(update_fields=pkeys)

    op = raw.get('operations')
    if isinstance(op, dict):
        och = {}
        if 'accepting_requests' in op:
            nb = _coerce_bool(op['accepting_requests'])
            if settings_obj.accepting_requests != nb:
                och['accepting_requests'] = {'from': settings_obj.accepting_requests, 'to': nb}
                settings_obj.accepting_requests = nb
        pause_outside = None
        if 'pause_outside_opening_hours' in op:
            pause_outside = _coerce_bool(op['pause_outside_opening_hours'])
        elif 'pause_requests_outside_hours' in op:
            pause_outside = _coerce_bool(op['pause_requests_outside_hours'])
        if pause_outside is not None:
            prev = bool(getattr(settings_obj, 'pause_outside_opening_hours', False))
            if prev != pause_outside:
                och['pause_outside_opening_hours'] = {'from': prev, 'to': pause_outside}
                settings_obj.pause_outside_opening_hours = pause_outside
        oh_next = _opening_hours_as_dict(settings_obj.opening_hours)
        if 'opening_hours' in op and isinstance(op['opening_hours'], dict):
            oh_next = _opening_hours_as_dict(op['opening_hours'])
        oh_flat = {
            'weekday_open': 12,
            'weekday_close': 12,
            'opening_hours_text': 8000,
            'holiday_notes': 2000,
        }
        for fk, mx in oh_flat.items():
            if fk in op:
                oh_next[fk] = _strip_str(op.get(fk), mx)
        prev_oh = _opening_hours_as_dict(settings_obj.opening_hours)
        if oh_next != prev_oh:
            och['opening_hours'] = {'from': prev_oh, 'to': oh_next}
            settings_obj.opening_hours = oh_next
        if 'timezone' in op:
            tz = _strip_str(op.get('timezone'), 64)
            if settings_obj.timezone != tz:
                och['timezone'] = {'from': settings_obj.timezone, 'to': tz}
                settings_obj.timezone = tz
        if 'holiday_mode' in op:
            nb = _coerce_bool(op['holiday_mode'])
            if settings_obj.holiday_mode != nb:
                och['holiday_mode'] = {'from': settings_obj.holiday_mode, 'to': nb}
                settings_obj.holiday_mode = nb
        if 'auto_accept_reservations' in op:
            nb = _coerce_bool(op['auto_accept_reservations'])
            if settings_obj.auto_accept_reservations != nb:
                och['auto_accept_reservations'] = {'from': settings_obj.auto_accept_reservations, 'to': nb}
                settings_obj.auto_accept_reservations = nb
        if 'max_reservation_window_minutes' in op:
            try:
                mx = max(10, min(10080, int(op['max_reservation_window_minutes'])))
            except (TypeError, ValueError):
                raise ValueError('operations.max_reservation_window_minutes must be an integer 10–10080')
            if settings_obj.max_reservation_window_minutes != mx:
                och['max_reservation_window_minutes'] = {'from': settings_obj.max_reservation_window_minutes, 'to': mx}
                settings_obj.max_reservation_window_minutes = mx
        if 'low_stock_threshold_default' in op:
            try:
                lv = max(0, min(100000, int(op['low_stock_threshold_default'])))
            except (TypeError, ValueError):
                raise ValueError('operations.low_stock_threshold_default must be an integer')
            if settings_obj.low_stock_threshold_default != lv:
                och['low_stock_threshold_default'] = {'from': settings_obj.low_stock_threshold_default, 'to': lv}
                settings_obj.low_stock_threshold_default = lv
        if 'out_of_stock_behavior' in op:
            vb = op['out_of_stock_behavior']
            if vb not in _OSB:
                raise ValueError('operations.out_of_stock_behavior invalid')
            if settings_obj.out_of_stock_behavior != vb:
                och['out_of_stock_behavior'] = {'from': settings_obj.out_of_stock_behavior, 'to': vb}
                settings_obj.out_of_stock_behavior = vb
        if 'auto_substitute_enabled' in op:
            nb = _coerce_bool(op['auto_substitute_enabled'])
            if settings_obj.auto_substitute_enabled != nb:
                och['auto_substitute_enabled'] = {'from': settings_obj.auto_substitute_enabled, 'to': nb}
                settings_obj.auto_substitute_enabled = nb
        if och:
            changelog['operations'] = och

    nf = raw.get('notifications')
    if isinstance(nf, dict):
        nch = {}
        ch = nf.get('channels')
        if isinstance(ch, dict):
            for key, fld in [('email', 'notify_channel_email'), ('sms', 'notify_channel_sms'), ('in_app', 'notify_channel_in_app')]:
                if key in ch:
                    nb = _coerce_bool(ch[key])
                    if getattr(settings_obj, fld) != nb:
                        nch.setdefault('channels', {})[key] = {'from': getattr(settings_obj, fld), 'to': nb}
                        setattr(settings_obj, fld, nb)
        for fld in ('notify_new_request', 'notify_low_stock', 'notify_reservation_expiry'):
            if fld in nf:
                nb = _coerce_bool(nf[fld])
                if getattr(settings_obj, fld) != nb:
                    nch[fld] = {'from': getattr(settings_obj, fld), 'to': nb}
                    setattr(settings_obj, fld, nb)
        if 'email_on_low_stock' in nf:
            nb = _coerce_bool(nf['email_on_low_stock'])
            if settings_obj.notify_low_stock != nb:
                nch['notify_low_stock'] = {'from': settings_obj.notify_low_stock, 'to': nb}
                settings_obj.notify_low_stock = nb
        if 'quiet_hours' in nf:
            qh = nf['quiet_hours']
            if not isinstance(qh, dict):
                raise ValueError('notifications.quiet_hours must be an object')
            prev = getattr(settings_obj, 'notify_quiet_hours', {}) or {}
            if prev != qh:
                nch['quiet_hours'] = {'from': prev, 'to': qh}
                settings_obj.notify_quiet_hours = qh
        if 'digest_frequency' in nf:
            df = _strip_str(nf.get('digest_frequency'), 24)
            if df not in _DIGEST:
                raise ValueError('notifications.digest_frequency must be instant|daily|weekly|muted')
            if getattr(settings_obj, 'notifications_digest_frequency', '') != df:
                nch['digest_frequency'] = {'from': getattr(settings_obj, 'notifications_digest_frequency'), 'to': df}
                settings_obj.notifications_digest_frequency = df
        if nch:
            changelog['notifications'] = nch

    sv = raw.get('service')
    if isinstance(sv, dict):
        sch = {}
        if 'radius_km' in sv:
            r = sv['radius_km']
            if r is None:
                new_r = None
            else:
                try:
                    new_r = int(r)
                    if new_r < 1 or new_r > 600:
                        raise ValueError()
                except (ValueError, TypeError):
                    raise ValueError('service.radius_km must be null or integer 1–600')
            if settings_obj.service_radius_km != new_r:
                sch['radius_km'] = {'from': settings_obj.service_radius_km, 'to': new_r}
                settings_obj.service_radius_km = new_r
        if 'pickup_available' in sv:
            nb = _coerce_bool(sv['pickup_available'])
            if getattr(settings_obj, 'service_pickup_available', True) != nb:
                sch['pickup_available'] = {'from': getattr(settings_obj, 'service_pickup_available', True), 'to': nb}
                settings_obj.service_pickup_available = nb
        if 'delivery_available' in sv:
            nb = _coerce_bool(sv['delivery_available'])
            if getattr(settings_obj, 'service_delivery_available', False) != nb:
                sch['delivery_available'] = {'from': getattr(settings_obj, 'service_delivery_available', False), 'to': nb}
                settings_obj.service_delivery_available = nb
        if 'areas_covered' in sv:
            ar = sv['areas_covered']
            if ar is None:
                ar = []
            if not isinstance(ar, list):
                raise ValueError('service.areas_covered must be an array')
            prev = getattr(settings_obj, 'service_areas_covered', []) or []
            if prev != ar:
                sch['areas_covered'] = {'from': prev, 'to': ar}
                settings_obj.service_areas_covered = ar
        if sch:
            changelog['service'] = sch

    pr = raw.get('preferences')
    if isinstance(pr, dict):
        prh = {}
        if 'disclaimer_visible' in pr:
            nb = _coerce_bool(pr['disclaimer_visible'])
            if settings_obj.disclaimer_visible != nb:
                prh['disclaimer_visible'] = {'from': settings_obj.disclaimer_visible, 'to': nb}
                settings_obj.disclaimer_visible = nb
        if 'prescription_enforcement' in pr:
            nb = _coerce_bool(pr['prescription_enforcement'])
            if settings_obj.prescription_enforcement != nb:
                prh['prescription_enforcement'] = {'from': settings_obj.prescription_enforcement, 'to': nb}
                settings_obj.prescription_enforcement = nb
        if 'audit_logging_enabled' in pr:
            nb = _coerce_bool(pr['audit_logging_enabled'])
            if settings_obj.audit_logging_enabled != nb:
                prh['audit_logging_enabled'] = {'from': settings_obj.audit_logging_enabled, 'to': nb}
                settings_obj.audit_logging_enabled = nb
        if 'ui_dark_mode' in pr:
            nb = _coerce_bool(pr['ui_dark_mode'])
            if settings_obj.ui_dark_mode != nb:
                prh['ui_dark_mode'] = {'from': settings_obj.ui_dark_mode, 'to': nb}
                settings_obj.ui_dark_mode = nb
        if 'ui_table_density' in pr:
            d = _strip_str(pr.get('ui_table_density'), 16)
            if d not in _UIDENS:
                raise ValueError('preferences.ui_table_density must be compact|comfortable')
            if settings_obj.ui_table_density != d:
                prh['ui_table_density'] = {'from': settings_obj.ui_table_density, 'to': d}
                settings_obj.ui_table_density = d
        if 'ui_default_page_size' in pr:
            try:
                ps = max(5, min(200, int(pr['ui_default_page_size'])))
            except (TypeError, ValueError):
                raise ValueError('preferences.ui_default_page_size must be integer 5–200')
            if settings_obj.ui_default_page_size != ps:
                prh['ui_default_page_size'] = {'from': settings_obj.ui_default_page_size, 'to': ps}
                settings_obj.ui_default_page_size = ps
        if 'ui_default_filters' in pr and isinstance(pr['ui_default_filters'], dict):
            if settings_obj.ui_default_filters != pr['ui_default_filters']:
                prh['ui_default_filters'] = {'from': settings_obj.ui_default_filters, 'to': pr['ui_default_filters']}
                settings_obj.ui_default_filters = pr['ui_default_filters']
        cur_udf = settings_obj.ui_default_filters if isinstance(getattr(settings_obj, 'ui_default_filters', None), dict) else {}
        udf_merged = dict(cur_udf)
        udf_touched = False
        if 'hide_zero_quantity_in_search' in pr:
            udf_merged['hide_zero_quantity_in_search'] = _coerce_bool(pr['hide_zero_quantity_in_search'])
            udf_touched = True
        if 'auto_suggest_shift_on_open' in pr:
            udf_merged['auto_suggest_shift_on_open'] = _coerce_bool(pr['auto_suggest_shift_on_open'])
            udf_touched = True
        if udf_touched and udf_merged != cur_udf:
            prh['ui_default_filters'] = {'from': cur_udf, 'to': udf_merged}
            settings_obj.ui_default_filters = udf_merged
        if 'preferred_profile' in pr:
            pv = _strip_str(pr.get('preferred_profile'), 64)
            if settings_obj.preferred_profile != pv:
                prh['preferred_profile'] = {'from': settings_obj.preferred_profile, 'to': pv}
                settings_obj.preferred_profile = pv
        if prh:
            changelog['preferences'] = prh

    if changelog:
        settings_obj.pharmacist = pharmacist
        settings_obj.updated_by = pharmacist.email or pharmacist.full_name
        settings_obj.version = (settings_obj.version or 1) + 1
        settings_obj.save()
    return changelog
