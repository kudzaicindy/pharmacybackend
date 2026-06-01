"""
Transactional email helpers (Django send_mail).

Configure in .env / settings: EMAIL_HOST, EMAIL_PORT, EMAIL_USE_TLS, EMAIL_HOST_USER,
EMAIL_HOST_PASSWORD, DEFAULT_FROM_EMAIL.

Optional: LOGIN_EMAIL_2FA=true, APP_PUBLIC_URL=https://...
"""
from __future__ import annotations

import os
import secrets
import string
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail


def email_configured() -> bool:
    host = getattr(settings, 'EMAIL_HOST', '') or ''
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', '') or ''
    return bool(host.strip() and from_email.strip())


def send_plain(subject: str, body: str, recipients: list[str], *, fail_silently: bool = True) -> bool:
    to = [e.strip() for e in (recipients or []) if e and str(e).strip()]
    if not to:
        return False
    if not email_configured():
        print('[WARN] Email not configured (EMAIL_HOST / DEFAULT_FROM_EMAIL); skip send.')
        return False
    try:
        send_mail(
            subject,
            body,
            settings.DEFAULT_FROM_EMAIL,
            to,
            fail_silently=fail_silently,
        )
        return True
    except Exception as exc:
        print(f'[WARN] send_mail failed: {exc}')
        return False


def generate_numeric_code(length: int = 6) -> str:
    return ''.join(secrets.choice(string.digits) for _ in range(length))


def _dedupe_emails(emails: list[str | None]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in emails:
        e = (raw or '').strip()
        if not e or '@' not in e:
            continue
        key = e.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def resolve_patient_emails_for_request(medicine_request) -> list[str]:
    from .models import PatientProfile

    emails: list[str] = []
    user = getattr(medicine_request, 'user', None)
    if user and getattr(user, 'email', None):
        emails.append(user.email)
    conv = medicine_request.conversation
    if conv and conv.session_id:
        prof = PatientProfile.objects.filter(session_id=conv.session_id).exclude(email='').first()
        if prof and prof.email:
            emails.append(prof.email)
    return _dedupe_emails(emails)


def notify_medicine_request_created(
    medicine_request,
    nearby: list[dict[str, Any]] | None,
    *,
    email_patient: bool = True,
    email_pharmacies: bool = True,
    extra_patient_emails: list[str] | None = None,
) -> None:
    """Email patient confirmation and/or nearby pharmacy branch contacts."""
    rid = str(medicine_request.request_id)
    meds = medicine_request.medicine_names or []
    if meds:
        med_txt = ', '.join(str(m) for m in meds[:20])
    elif getattr(medicine_request, 'request_type', None) == 'prescription':
        med_txt = '(prescription image — pharmacists confirm medicines from upload)'
    else:
        med_txt = '(symptom / general request)'
    sym = (medicine_request.symptoms or '').strip()
    area = medicine_request.location_suburb or medicine_request.location_address or 'your area'
    app_url = (getattr(settings, 'APP_PUBLIC_URL', None) or os.getenv('APP_PUBLIC_URL', '') or '').strip()

    patient_lines = [
        'Your medicine request was submitted successfully.',
        '',
        f'Request ID: {rid}',
        f'Medicines / topic: {med_txt}',
    ]
    if sym:
        patient_lines.append(f'Symptoms / notes: {sym[:500]}')
    patient_lines.extend([
        f'Location: {area}',
        '',
        'Nearby verified pharmacies have been notified by email (where configured).',
        'You will see responses in the app when pharmacists reply.',
    ])
    if app_url:
        patient_lines.append(f'Open: {app_url}')
    patient_body = '\n'.join(patient_lines)

    if email_patient:
        patient_tos = resolve_patient_emails_for_request(medicine_request)
        if extra_patient_emails:
            patient_tos = _dedupe_emails(patient_tos + list(extra_patient_emails))
        for to in patient_tos:
            send_plain(f'MediConnect: request submitted ({rid[:8]}…)', patient_body, [to])

    if not email_pharmacies:
        return

    pharm_lines = [
        'A patient has submitted a new medicine request near your branch.',
        '',
        f'Request ID: {rid}',
        f'Medicines: {med_txt}',
    ]
    if sym:
        pharm_lines.append(f'Symptoms / notes: {sym[:500]}')
    pharm_lines.extend([
        f'Patient area (approx.): {area}',
        'Please review your pharmacist dashboard and respond if you can fulfil the request.',
    ])
    if app_url:
        pharm_lines.append(f'Portal: {app_url}')
    pharm_body = '\n'.join(pharm_lines)

    seen_pharm: set[str] = set()
    for row in nearby or []:
        ph = row.get('pharmacy')
        if not ph:
            continue
        pid = getattr(ph, 'pharmacy_id', None) or str(ph.pk)
        if pid in seen_pharm:
            continue
        seen_pharm.add(str(pid))
        em = getattr(ph, 'email', None) or ''
        if em:
            dist = row.get('distance_km')
            subj = f'MediConnect: new patient request near you ({rid[:8]}…)'
            body = pharm_body + (f'\n\nApprox. distance from patient: {dist:.1f} km' if dist is not None else '')
            send_plain(subj, body, [em])


def notify_pharmacy_response_to_patient(medicine_request, response_obj, pharmacy) -> None:
    """Email patient when a pharmacy submits or updates a response."""
    rid = str(medicine_request.request_id)
    short = rid.replace('-', '')[:8].upper()
    pname = pharmacy.name if pharmacy else (response_obj.pharmacy_name or 'A pharmacy')
    med_names = medicine_request.medicine_names or []
    if med_names:
        first_med = str(med_names[0])
    elif getattr(medicine_request, 'request_type', None) == 'prescription':
        first_med = 'your prescription upload'
    else:
        first_med = 'your request'
    price_str = ''
    if response_obj.price:
        try:
            price_str = f"${float(response_obj.price):.2f}"
        except (TypeError, ValueError):
            price_str = str(response_obj.price)
    lines = [
        f'{pname} responded to your MediConnect request #{short}.',
        '',
        f'Request ID: {rid}',
        f'Medicine focus: {first_med}',
    ]
    if price_str:
        lines.append(f'Quoted total / headline price: {price_str}')
    if response_obj.notes:
        lines.append(f'Notes: {str(response_obj.notes)[:500]}')
    lines.append('')
    lines.append('Open the app to compare responses and reserve if you wish.')
    app_url = (getattr(settings, 'APP_PUBLIC_URL', None) or os.getenv('APP_PUBLIC_URL', '') or '').strip()
    if app_url:
        lines.append(app_url)
    body = '\n'.join(lines)
    for to in resolve_patient_emails_for_request(medicine_request):
        send_plain(f'MediConnect: {pname} responded (#{short})', body, [to])


# --- Email OTP (2FA login) ---

def create_email_login_challenge(*, user_id: int, kind: str, email: str, pharmacist_id: str | None = None) -> str:
    """Store OTP; return opaque challenge id for client round-trip."""
    import uuid

    challenge = str(uuid.uuid4())
    code = generate_numeric_code(6)
    cache.set(
        f'email2fa:{challenge}',
        {'user_id': user_id, 'code': code, 'kind': kind, 'pharmacist_id': pharmacist_id},
        timeout=600,
    )
    subj = f'MediConnect login code ({kind})'
    body = (
        f'Your verification code is: {code}\n\n'
        f'It expires in 10 minutes.\n'
        f'If you did not attempt to sign in, ignore this email.\n'
    )
    send_plain(subj, body, [email])
    return challenge


def get_email_login_challenge(challenge: str) -> dict | None:
    return cache.get(f'email2fa:{challenge}')


def delete_email_login_challenge(challenge: str) -> None:
    cache.delete(f'email2fa:{challenge}')


# --- TOTP second step after password (device MFA) ---

def create_totp_login_challenge(*, user_id, kind: str, pharmacist_id: str | None = None) -> str:
    import uuid

    token = str(uuid.uuid4())
    cache.set(
        f'totplogin:{token}',
        {'user_id': user_id, 'kind': kind, 'pharmacist_id': str(pharmacist_id) if pharmacist_id else None},
        timeout=600,
    )
    return token


def get_totp_login_challenge(token: str) -> dict | None:
    return cache.get(f'totplogin:{token}')


def delete_totp_login_challenge(token: str) -> None:
    cache.delete(f'totplogin:{token}')


# --- Password reset by email code (lookup by email + account_type + code) ---

def _pwdreset_cache_key(account_type: str, email: str) -> str:
    return f'pwdreset:{account_type}:{email.lower().strip()}'


def set_password_reset_challenge(*, user_id: int, email: str, account_type: str) -> None:
    code = generate_numeric_code(6)
    cache.set(
        _pwdreset_cache_key(account_type, email),
        {'user_id': user_id, 'code': code},
        timeout=900,
    )
    subj = 'MediConnect password reset code'
    body = (
        f'Your password reset code is: {code}\n\n'
        f'It expires in 15 minutes.\n'
        f'If you did not request a reset, ignore this email.\n'
    )
    send_plain(subj, body, [email])


def verify_password_reset_code(*, email: str, account_type: str, code: str) -> int | None:
    key = _pwdreset_cache_key(account_type, email)
    data = cache.get(key)
    if not data or str(data.get('code', '')) != str(code).strip():
        return None
    cache.delete(key)
    return data['user_id']
