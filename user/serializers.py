from django.contrib.auth.models import User
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .platform import is_platform_admin


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Apex console login — superuser/staff only, generic failures.

    Success:  {access, refresh, user: {id, username, name, role: "APEX",
               tenant: null, tenant_id: null, must_change_password}}
    Failure:  401 {"errors": {"message": "Invalid credentials."}}  — the same
              answer for a wrong password, an unknown user, an inactive user
              or a non-superuser (this console is Apex-only).
    """

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        if is_platform_admin(user):
            # Platform admins carry no tenant — they see all of them.
            token["role"] = "APEX"
            token["tenant_id"] = None
            token["tenant_name"] = ""
        else:
            profile = getattr(user, "profile", None)
            token["role"] = profile.role if profile else ""
            token["tenant_id"] = profile.tenant_id if profile else None
            token["tenant_name"] = profile.tenant.name if profile and profile.tenant else ""
        token["name"] = user.get_full_name() or user.username
        return token

    def validate(self, attrs):
        try:
            data = super().validate(attrs)
        except AuthenticationFailed:
            raise AuthenticationFailed("Invalid credentials.")
        if not is_platform_admin(self.user):
            # Tenant-role accounts must sign in through their own repo.
            raise AuthenticationFailed("Invalid credentials.")
        # Superusers created by createsuperuser have no Profile row — never
        # require one; role/tenant come from the platform identity instead.
        data["user"] = {
            "id": self.user.id,
            "username": self.user.username,
            "name": self.user.get_full_name() or self.user.username,
            "role": "APEX",
            "tenant": None,
            "tenant_id": None,
            "must_change_password": False,
        }
        return data


class UserSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()
    tenant = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "username", "first_name", "last_name", "email", "role", "tenant"]

    def get_role(self, obj):
        if obj.is_superuser or obj.is_staff:
            return "APEX"
        profile = getattr(obj, "profile", None)
        return profile.role if profile else ""

    def get_tenant(self, obj):
        profile = getattr(obj, "profile", None)
        if profile and profile.tenant:
            return profile.tenant.name
        return None if (obj.is_superuser or obj.is_staff) else ""
