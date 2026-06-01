"""JWT + Pharmacist portal resolution for authenticated dashboard/settings routes."""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response

from chatbot.models import Pharmacist


def jwt_pair_for_user(user):
    """Return SimpleJWT access/refresh strings for a Django User."""
    from rest_framework_simplejwt.tokens import RefreshToken

    refresh = RefreshToken.for_user(user)
    return {'access': str(refresh.access_token), 'refresh': str(refresh)}


def pharmacist_jwt_tokens_or_empty(pharmacist: Pharmacist) -> dict[str, str]:
    if not pharmacist or not pharmacist.user_id:
        return {}
    try:
        return jwt_pair_for_user(pharmacist.user)
    except Exception as exc:
        print(f'[WARN] JWT tokens for pharmacist failed: {exc}')
        return {}


def resolve_authenticated_pharmacist(request):
    """
    Resolve the active Pharmacist from JWT Bearer or Django session auth.

    Optional query/body pharmacist_id must match (lets clients sanity-check stale ids).
    Returns (Pharmacist|None, Response|None error).
    """
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return None, Response(
            {'detail': 'Authentication credentials were not provided.'},
            status=status.HTTP_401_UNAUTHORIZED,
            headers={'WWW-Authenticate': 'Bearer'},
        )

    ph = (
        Pharmacist.objects.filter(user=user, is_active=True)
        .select_related('pharmacy', 'user')
        .first()
    )
    if not ph:
        return None, Response(
            {'detail': 'Authenticated user is not linked to an active pharmacist account.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    pid_explicit = None
    if request.query_params.get('pharmacist_id'):
        pid_explicit = str(request.query_params.get('pharmacist_id')).strip()
    elif isinstance(request.data, dict) and request.data.get('pharmacist_id'):
        pid_explicit = str(request.data.get('pharmacist_id')).strip()

    if pid_explicit and pid_explicit != str(ph.pharmacist_id):
        return None, Response(
            {'detail': 'pharmacist_id does not match the authenticated pharmacist.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    return ph, None
