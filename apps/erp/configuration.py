import re
from decimal import Decimal, InvalidOperation
from rest_framework.exceptions import ValidationError


def validate_configuration(obj):
    data = obj.definition
    if not isinstance(data, dict): raise ValidationError({"definition": "Configuration must be an object."})
    if obj.kind == "workflow":
        stages = data.get("stages", [])
        if not 1 <= len(stages) <= 30 or len(set(stages)) != len(stages) or any(not isinstance(s, str) or not s.strip() for s in stages):
            raise ValidationError({"definition": "Provide 1-30 unique stage names."})
    elif obj.kind == "custom_field":
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,49}", data.get("key", "")): raise ValidationError("Field key must use lowercase letters, digits and underscores.")
        if data.get("type") not in ("text", "number", "date", "boolean", "select"): raise ValidationError("Choose a supported field type.")
        if data["type"] == "select" and not data.get("options"): raise ValidationError("Select fields require options.")
        from .models import Configuration
        duplicate=Configuration.objects.filter(tenant=obj.tenant,kind="custom_field",entity_type=obj.entity_type,status="published",archived=False,definition__key=data["key"]).exclude(pk=obj.pk).exists()
        if duplicate: raise ValidationError("A published field already uses this key for the selected record type. Archive or version that definition first.")
    elif obj.kind == "print_template":
        allowed = {"heading", "footer", "terms", "accent", "show_signature", "bank_details"}
        if set(data) - allowed: raise ValidationError("Template contains unsupported fields. HTML and scripts are not allowed.")
    elif obj.kind == "report_schedule":
        if data.get("frequency") not in ("daily", "weekly", "monthly"): raise ValidationError("Choose a daily, weekly or monthly schedule.")
        if not data.get("report"): raise ValidationError("Select a report.")
        import re
        if any(not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+",str(email)) for email in data.get("recipients",[])): raise ValidationError("Every scheduled report recipient must be a valid email address.")
    elif obj.kind == "integration":
        if data.get("provider") not in ("email", "whatsapp", "tally", "webhook"): raise ValidationError("Select a supported integration provider.")
        if any("secret" in str(k).lower() or "password" in str(k).lower() or "token" in str(k).lower() for k in data):
            raise ValidationError("Provider secrets must be configured in the server environment, not public configuration.")


def validate_custom_values(obj, values):
    from .models import Configuration
    from .security import features
    if not isinstance(values, dict): raise ValidationError({"custom_fields":"Custom fields must be an object."})
    entity = "customer" if obj.__class__.__name__ == "CustomerProfile" else obj.kind if obj.__class__.__name__ == "Document" else obj.__class__.__name__.lower()
    definitions = Configuration.objects.filter(tenant=obj.tenant,kind="custom_field",entity_type=entity,status="published",archived=False)
    if values and "custom_fields" not in features(obj.tenant): raise ValidationError({"custom_fields":"The custom fields module is not enabled."})
    definitions={x.definition.get("key"):x.definition for x in definitions}
    if set(values)-set(definitions): raise ValidationError({"custom_fields":"One or more fields are not published for this record type."})
    for key,value in values.items():
        definition=definitions[key];kind=definition.get("type")
        if kind=="number":
            try: Decimal(str(value))
            except (InvalidOperation,ValueError,TypeError): raise ValidationError({"custom_fields":f"{key} must be a number."})
        elif kind=="boolean" and not isinstance(value,bool): raise ValidationError({"custom_fields":f"{key} must be true or false."})
        elif kind=="date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}",str(value)): raise ValidationError({"custom_fields":f"{key} must be a date."})
        elif kind=="select" and value not in definition.get("options",[]): raise ValidationError({"custom_fields":f"Choose a valid value for {key}."})
        elif kind=="text" and (not isinstance(value,str) or len(value)>2000): raise ValidationError({"custom_fields":f"{key} must be text up to 2,000 characters."})
