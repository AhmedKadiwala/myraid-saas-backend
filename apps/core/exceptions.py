from rest_framework.views import exception_handler


def legacy_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        return response
    if isinstance(response.data, dict) and "message" not in response.data:
        detail = response.data.get("detail")
        response.data = {
            "message": str(detail or "Input validation error"),
            "error": response.data,
        }
    return response
