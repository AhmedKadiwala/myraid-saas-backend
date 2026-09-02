from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from rest_framework.exceptions import ValidationError

CENT = Decimal(".01")
ZERO = Decimal(0)


def decimal(value, field="amount", minimum=None):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or abs(number) >= Decimal("10000000000000"):
            raise InvalidOperation
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError({field: "Enter a valid decimal amount."})
    if minimum is not None and number < Decimal(str(minimum)):
        raise ValidationError({field: f"Must be at least {minimum}."})
    return number


def money(value):
    return decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def calculate_line(data, mode="inclusive", jurisdiction="intra"):
    with localcontext() as ctx:
        ctx.prec = 40
        qty = decimal(data.get("quantity", 1), "quantity", "0.000001")
        rate = decimal(data.get("rate", 0), "rate", 0)
        discount = decimal(data.get("discount", 0), "discount", 0)
        tax_rate = decimal(data.get("tax_rate", 0), "tax_rate", 0)
        if tax_rate > 100:
            raise ValidationError({"tax_rate": "Tax rate cannot exceed 100%."})
        price = money(qty * rate - discount)
        if price < 0:
            raise ValidationError({"discount": "Discount cannot exceed the line amount."})
        if mode == "inclusive":
            gross = price
            taxable = money(gross / (1 + tax_rate / 100))
            tax = gross - taxable
        elif mode == "exclusive":
            taxable = price
            tax = money(taxable * tax_rate / 100)
            gross = taxable + tax
        else:
            raise ValidationError({"tax_mode": "Select inclusive or exclusive pricing."})
        cgst = money(tax / 2) if jurisdiction == "intra" else ZERO
        sgst = tax - cgst if jurisdiction == "intra" else ZERO
        return {"quantity": qty, "rate": rate, "discount": discount, "tax_rate": tax_rate,
                "taxable": taxable, "tax": tax, "gross": gross, "cgst": cgst, "sgst": sgst,
                "igst": tax if jurisdiction == "inter" else ZERO}


def allocate_paise(total, balances):
    """Bounded largest-remainder split; every component remains nonnegative."""
    total = int(total)
    weight = sum(balances)
    if total < 0 or total > weight:
        raise ValidationError("The allocated amount exceeds the original commercial value.")
    if not total:
        return [0] * len(balances)
    with localcontext() as ctx:
        ctx.prec = 40
        exact = [Decimal(total) * b / weight for b in balances]
        result = [int(x) for x in exact]
        order = sorted(range(len(result)), key=lambda i: (-(exact[i] - result[i]), i))
        for i in order[:total - sum(result)]:
            result[i] += 1
        return result
