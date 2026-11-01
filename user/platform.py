"""Platform (Apex console) identity + authorization helpers.

This console is Apex-only: the founder's Django superusers are the operators.
Tenant-role logins belong to the other volosync repos and are rejected here.
"""

from functools import wraps

from rest_framework.response import Response


def is_platform_admin(user):
    """True when the user may operate the Apex console."""
    return bool(
        user is not None
        and getattr(user, "is_authenticated", False)
        and (user.is_superuser or user.is_staff)
    )


def require_platform_admin(func):
    """Decorate an APIView handler (get/post/patch/...) with a 403 guard.

    Unauthenticated callers never reach the handler — DRF's IsAuthenticated
    runs first and answers 401. Authenticated non-admins get:

        {"errors": {"message": "Platform admin only."}}   (403)
    """

    @wraps(func)
    def wrapper(self, request, *args, **kwargs):
        if not is_platform_admin(request.user):
            return Response(
                {"errors": {"message": "Platform admin only."}}, status=403
            )
        return func(self, request, *args, **kwargs)

    return wrapper
