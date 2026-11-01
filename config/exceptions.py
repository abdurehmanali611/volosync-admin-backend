from rest_framework.views import exception_handler


def envelope_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        return response
    detail = response.data
    if isinstance(detail, dict) and set(detail.keys()) == {"detail"}:
        detail = {"message": str(detail["detail"])}
    response.data = {"errors": detail}
    return response
