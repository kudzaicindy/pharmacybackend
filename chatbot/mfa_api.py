"""Patient / pharmacist authenticator (TOTP) enrollment API (used by SPA settings)."""
import pyotp
from django.contrib.auth import authenticate
from django.core.cache import cache
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .pharmacist_portal_auth import resolve_authenticated_pharmacist

from .models import ChatConversation, PatientProfile


def _session_id_from_request(request):
    qp = request.query_params
    body = request.data if isinstance(request.data, dict) else {}
    sid = (qp.get('session_id') or body.get('session_id') or '').strip()
    conv_id = qp.get('conversation_id') or body.get('conversation_id')
    if conv_id and not sid:
        conv = ChatConversation.objects.filter(conversation_id=conv_id).first()
        if conv and conv.session_id:
            sid = str(conv.session_id).strip()
    return sid or None


def _patient_profile_or_error(request):
    sid = _session_id_from_request(request)
    if not sid:
        return None, Response(
            {'error': 'session_id or conversation_id is required'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    profile = PatientProfile.objects.filter(session_id=sid).first()
    if not profile:
        return None, Response({'error': 'Patient profile not found'}, status=status.HTTP_404_NOT_FOUND)
    return profile, None


def _mfasetup_patient_key(session_id: str) -> str:
    return f'mfasetup:patient:{session_id}'


@api_view(['GET', 'POST'])
@permission_classes([AllowAny])
def patient_mfa_status(request):
    profile, err = _patient_profile_or_error(request)
    if err:
        return err
    sid = (profile.session_id or '').strip()
    secret = (profile.mfa_totp_secret or '').strip()
    return Response(
        {
            'mfa_enabled': bool(profile.mfa_totp_enabled and secret),
            'totp_configured': bool(profile.mfa_totp_enabled and secret),
            'pending_setup': bool(sid and cache.get(_mfasetup_patient_key(sid))),
        },
        status=status.HTTP_200_OK,
    )


@api_view(['POST'])
@permission_classes([AllowAny])
def patient_mfa_setup_start(request):
    profile, err = _patient_profile_or_error(request)
    if err:
        return err
    sid = (profile.session_id or '').strip()
    if not sid:
        return Response({'error': 'profile has no session_id'}, status=status.HTTP_400_BAD_REQUEST)
    secret = pyotp.random_base32()
    cache.set(_mfasetup_patient_key(sid), secret, timeout=600)
    label = (profile.email or profile.display_name or sid)[:200]
    uri = pyotp.totp.TOTP(secret).provisioning_uri(name=label, issuer_name='MediConnect')
    return Response(
        {
            'secret': secret,
            'provisioning_uri': uri,
            'otpauth_uri': uri,
            'issuer': 'MediConnect',
            'expires_in_seconds': 600,
        },
        status=status.HTTP_200_OK,
    )


@api_view(['POST'])
@permission_classes([AllowAny])
def patient_mfa_setup_confirm(request):
    profile, err = _patient_profile_or_error(request)
    if err:
        return err
    code = str((request.data.get('code') if isinstance(request.data, dict) else '') or '').strip()
    if len(code) < 4:
        return Response({'error': 'code is required'}, status=status.HTTP_400_BAD_REQUEST)
    sid = (profile.session_id or '').strip()
    if not sid:
        return Response({'error': 'profile has no session_id'}, status=status.HTTP_400_BAD_REQUEST)
    secret = cache.get(_mfasetup_patient_key(sid))
    if not secret:
        return Response({'error': 'No pending setup; call setup/start first'}, status=status.HTTP_400_BAD_REQUEST)
    if not pyotp.TOTP(secret).verify(code, valid_window=1):
        return Response({'error': 'Invalid code'}, status=status.HTTP_400_BAD_REQUEST)
    profile.mfa_totp_secret = secret
    profile.mfa_totp_enabled = True
    profile.save(update_fields=['mfa_totp_secret', 'mfa_totp_enabled', 'updated_at'])
    cache.delete(_mfasetup_patient_key(sid))
    return Response({'message': 'Two-factor authentication enabled.', 'mfa_enabled': True}, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([AllowAny])
def patient_mfa_disable(request):
    profile, err = _patient_profile_or_error(request)
    if err:
        return err
    body = request.data if isinstance(request.data, dict) else {}
    code = str(body.get('code') or '').strip()
    password = str(body.get('password') or '').strip()
    secret = (profile.mfa_totp_secret or '').strip()
    if not profile.mfa_totp_enabled or not secret:
        return Response({'message': 'MFA is not enabled.', 'mfa_enabled': False}, status=status.HTTP_200_OK)
    verified = False
    if code and pyotp.TOTP(secret).verify(code, valid_window=1):
        verified = True
    elif password and profile.user:
        u = authenticate(username=profile.user.username, password=password)
        verified = bool(u and u.pk == profile.user_id)
    if not verified:
        return Response(
            {'error': 'Provide a valid authenticator code or account password.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    profile.mfa_totp_secret = ''
    profile.mfa_totp_enabled = False
    profile.save(update_fields=['mfa_totp_secret', 'mfa_totp_enabled', 'updated_at'])
    sid = (profile.session_id or '').strip()
    if sid:
        cache.delete(_mfasetup_patient_key(sid))
    return Response({'message': 'MFA disabled.', 'mfa_enabled': False}, status=status.HTTP_200_OK)


def _mfasetup_pharmacist_key(ph_id) -> str:
    return f'mfasetup:pharmacist:{ph_id}'


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def pharmacist_mfa_status(request):
    ph, err = resolve_authenticated_pharmacist(request)
    if err:
        return err
    secret = (ph.mfa_totp_secret or '').strip()
    return Response(
        {
            'mfa_enabled': bool(ph.mfa_totp_enabled and secret),
            'mfa_totp_enabled': bool(ph.mfa_totp_enabled and secret),
            'totp_configured': bool(ph.mfa_totp_enabled and secret),
            'pending_setup': bool(cache.get(_mfasetup_pharmacist_key(str(ph.pharmacist_id)))),
        },
        status=status.HTTP_200_OK,
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def pharmacist_mfa_setup_start(request):
    ph, err = resolve_authenticated_pharmacist(request)
    if err:
        return err
    secret = pyotp.random_base32()
    cache.set(_mfasetup_pharmacist_key(str(ph.pharmacist_id)), secret, timeout=600)
    label = (ph.email or ph.full_name)[:200]
    uri = pyotp.totp.TOTP(secret).provisioning_uri(name=label, issuer_name='MediConnect')
    return Response(
        {
            'secret': secret,
            'provisioning_uri': uri,
            'otpauth_uri': uri,
            'issuer': 'MediConnect',
            'expires_in_seconds': 600,
        },
        status=status.HTTP_200_OK,
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def pharmacist_mfa_setup_confirm(request):
    ph, err = resolve_authenticated_pharmacist(request)
    if err:
        return err
    code = str((request.data.get('code') if isinstance(request.data, dict) else '') or '').strip()
    if len(code) < 4:
        return Response({'error': 'code is required'}, status=status.HTTP_400_BAD_REQUEST)
    secret = cache.get(_mfasetup_pharmacist_key(str(ph.pharmacist_id)))
    if not secret:
        return Response({'error': 'No pending setup; call setup/start first'}, status=status.HTTP_400_BAD_REQUEST)
    if not pyotp.TOTP(secret).verify(code, valid_window=1):
        return Response({'error': 'Invalid code'}, status=status.HTTP_400_BAD_REQUEST)
    ph.mfa_totp_secret = secret
    ph.mfa_totp_enabled = True
    ph.save(update_fields=['mfa_totp_secret', 'mfa_totp_enabled', 'updated_at'])
    cache.delete(_mfasetup_pharmacist_key(str(ph.pharmacist_id)))
    return Response({'message': 'Two-factor authentication enabled.', 'mfa_enabled': True}, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def pharmacist_mfa_disable(request):
    ph, err = resolve_authenticated_pharmacist(request)
    if err:
        return err
    body = request.data if isinstance(request.data, dict) else {}
    code = str(body.get('code') or '').strip()
    password = str(body.get('password') or '').strip()
    secret = (ph.mfa_totp_secret or '').strip()
    if not ph.mfa_totp_enabled or not secret:
        return Response({'message': 'MFA is not enabled.', 'mfa_enabled': False}, status=status.HTTP_200_OK)
    verified = False
    if code and pyotp.TOTP(secret).verify(code, valid_window=1):
        verified = True
    elif password and ph.user:
        u = authenticate(username=ph.user.username, password=password)
        verified = bool(u and u.pk == ph.user_id)
    if not verified:
        return Response(
            {'error': 'Provide a valid authenticator code or account password.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    ph.mfa_totp_secret = ''
    ph.mfa_totp_enabled = False
    ph.save(update_fields=['mfa_totp_secret', 'mfa_totp_enabled', 'updated_at'])
    cache.delete(_mfasetup_pharmacist_key(str(ph.pharmacist_id)))
    return Response({'message': 'MFA disabled.', 'mfa_enabled': False}, status=status.HTTP_200_OK)