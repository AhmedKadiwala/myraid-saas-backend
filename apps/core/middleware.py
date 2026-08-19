class TenantContextMiddleware:
    """Capture requested tenant/branch identifiers; authorization resolves them later."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.requested_tenant_id = request.headers.get("X-Tenant-ID")
        request.requested_branch_id = request.headers.get("X-Branch-ID")
        return self.get_response(request)
