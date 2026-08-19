from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.authentication import CSRFCheck
from rest_framework.exceptions import PermissionDenied


def enforce_csrf(request):
    check = CSRFCheck(lambda req: None)
    check.process_request(request)
    reason = check.process_view(request, None, (), {})
    if reason:
        raise PermissionDenied(f"CSRF failed: {reason}")


class CookieOrHeaderJWTAuthentication(JWTAuthentication):
    """Prefer Authorization: Bearer, with HttpOnly access-token cookie fallback."""

    def authenticate(self, request):
        header = self.get_header(request)
        if header is None:
            raw_token = request.COOKIES.get("access_token")
            used_cookie = raw_token is not None
        else:
            raw_token = self.get_raw_token(header)
            used_cookie = False
        if raw_token is None:
            return None
        validated_token = self.get_validated_token(raw_token)
        if used_cookie and request.method not in ("GET", "HEAD", "OPTIONS", "TRACE"):
            enforce_csrf(request)
        return self.get_user(validated_token), validated_token
