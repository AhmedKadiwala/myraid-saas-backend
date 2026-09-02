"""Canonical commercial capabilities: pricing is independent of user grants."""
FEATURES = {
    "crm_advanced": ("Advanced CRM", 500, []), "purchase": ("Purchase", 500, []),
    "inventory": ("Inventory", 1000, []), "multi_warehouse": ("Multiple warehouses", 500, ["inventory"]),
    "work_orders": ("Jobs & work orders", 1000, []), "production_tracking": ("Production stages", 500, ["work_orders"]),
    "dispatch": ("Dispatch", 500, []), "expense_management": ("Expenses & recurring costs", 500, []),
    "profitability": ("Profitability", 500, []), "hrms": ("People", 500, []),
    "attendance_hr": ("Attendance, leave & shifts", 500, ["hrms"]), "payroll": ("Payroll", 1000, ["hrms"]),
    "approvals": ("Approvals", 500, []), "tasks": ("Tasks", 200, []), "multi_branch": ("Multiple branches", 500, []),
    "custom_workflows": ("Custom workflows", 500, []), "custom_fields": ("Custom fields", 200, []),
    "advanced_analytics": ("Advanced reports", 500, []), "communications": ("Email & WhatsApp", 500, []),
    "api_access": ("API & webhooks", 500, []), "custom_templates": ("Print templates", 200, []),
    "scheduled_reports": ("Scheduled reports", 200, []), "tally_integration": ("Tally exports", 500, []),
    "gst_integrations": ("GST integrations (post-V1)", 500, []),
}

DOCUMENT_FEATURES = {
    "quotation": "basic", "sales_order": "basic", "invoice": "basic", "credit_note": "basic",
    "requisition": "purchase", "purchase_order": "purchase", "goods_receipt": "purchase",
    "supplier_bill": "purchase", "supplier_return": "purchase", "dispatch": "dispatch",
}
DOCUMENT_PERMISSION = {
    "quotation": "quotation", "sales_order": "order", "invoice": "invoice", "credit_note": "invoice",
    "requisition": "purchase", "purchase_order": "purchase", "goods_receipt": "purchase",
    "supplier_bill": "payable", "supplier_return": "purchase", "dispatch": "dispatch",
}
PERMISSIONS = {
    "workspace": ["view"], "item": ["view", "create", "edit", "export"],
    "customer": ["view", "create", "edit", "export"], "supplier": ["view", "create", "edit", "export"],
    "warehouse": ["view", "create", "edit"], "stock": ["view", "view_cost", "inward", "issue", "adjust", "transfer", "reserve", "export", "reverse"],
    "quotation": ["view", "create", "edit", "approve", "post", "convert", "export"],
    "order": ["view", "create", "edit", "approve", "post", "convert", "export"],
    "purchase": ["view", "create", "edit", "approve", "post", "receive", "convert", "export"],
    "job": ["view", "create", "edit", "progress", "export"],
    "dispatch": ["view", "create", "edit", "approve", "post", "convert", "export"],
    "invoice": ["view", "create", "edit", "approve", "post", "convert", "void", "export"],
    "payment": ["view", "record", "void", "export"], "payable": ["view", "create", "edit", "approve", "post", "export"],
    "expense": ["view", "create", "edit", "approve", "post", "void", "export"],
    "profitability": ["view", "export"], "employee": ["view", "create", "edit", "view_private", "export"],
    "attendance": ["view", "create", "edit", "checkin", "approve", "export"],
    "leave": ["view", "create", "edit", "approve", "export"],
    "payroll": ["view", "create", "edit", "approve", "finalize", "record_payment", "export"],
    "task": ["view", "create", "edit"], "approval": ["view", "decide", "manage"],
    "settings": ["view", "manage"], "document": ["view", "upload", "print", "share"],
    "data": ["import", "export"], "report": ["view", "export"],
}


def price_quote(keys):
    from rest_framework.exceptions import ValidationError
    selected = set(keys)
    if selected - set(FEATURES):
        raise ValidationError({"features": "Unknown feature key."})
    if "gst_integrations" in selected:
        raise ValidationError({"features": "GST integrations are explicitly post-V1 and cannot be purchased here."})
    missing = {dep for key in selected for dep in FEATURES[key][2] if dep not in selected}
    if missing:
        raise ValidationError({"features": f"Required modules: {', '.join(sorted(missing))}."})
    subtotal = 2999 + sum(FEATURES[key][1] for key in selected)
    return {"base": 2999, "subtotal": subtotal, "discount": max(0, subtotal - 11999), "monthly": min(subtotal, 11999), "currency": "INR", "usage_excluded": True, "features": sorted(selected)}
