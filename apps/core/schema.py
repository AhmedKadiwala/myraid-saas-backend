from drf_spectacular.extensions import OpenApiAuthenticationExtension


class CookieOrHeaderJWTScheme(OpenApiAuthenticationExtension):
    target_class = "apps.core.authentication.CookieOrHeaderJWTAuthentication"
    name = ["jwtAuth", "cookieAuth"]

    def get_security_definition(self, auto_schema):
        return [
            {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
            },
            {
                "type": "apiKey",
                "in": "cookie",
                "name": "access_token",
            },
        ]
