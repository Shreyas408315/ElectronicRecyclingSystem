import re
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

def to_decimal(val: object, default: str = "0.00") -> Decimal:
    """Converts any numeric or string representation safely into a 2-decimal rounded Decimal."""
    if val is None or val == "":
        return Decimal(default).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    try:
        if isinstance(val, (int, float)):
            d = Decimal(str(val))
        elif isinstance(val, Decimal):
            d = val
        else:
            d = Decimal(str(val).strip())
        return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def validate_positive_decimal(val: object, field_label: str = "Amount", allow_zero: bool = True) -> tuple[bool, Decimal, str]:
    """Validates that an amount is a valid non-negative decimal."""
    try:
        dec = to_decimal(val)
        if not allow_zero and dec <= Decimal("0.00"):
            return False, dec, f"{field_label} must be greater than zero."
        if dec < Decimal("0.00"):
            return False, dec, f"{field_label} cannot be negative."
        return True, dec, ""
    except Exception:
        return False, Decimal("0.00"), f"Invalid numeric value for {field_label}."


GSTIN_REGEX = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$", re.IGNORECASE)
PAN_REGEX = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$", re.IGNORECASE)
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
PIN_CODE_REGEX = re.compile(r"^[1-9][0-9]{5}$")

def clean_string(val: str | None) -> str | None:
    if val is None:
        return None
    val = val.strip()
    return val if val else None

def validate_business_name(name: str | None) -> tuple[bool, str]:
    cleaned = clean_string(name)
    if not cleaned:
        return False, "Business/Supplier name is required."
    if len(cleaned) < 2:
        return False, "Business name must be at least 2 characters long."
    if len(cleaned) > 200:
        return False, "Business name cannot exceed 200 characters."
    return True, ""

def validate_phone(phone: str | None, field_label: str = "Phone number") -> tuple[bool, str]:
    cleaned = clean_string(phone)
    if not cleaned:
        return True, ""  # Optional
    
    if not re.fullmatch(r"[+\d\s().-]+", cleaned):
        return False, f"{field_label} contains unsupported characters."
    digits = re.sub(r"[^\d]", "", cleaned)
    if len(digits) < 10 or len(digits) > 13:
        return False, f"{field_label} must contain 10 to 13 digits (e.g. 9876543210 or +91 9876543210)."
    return True, ""

def validate_email(email: str | None) -> tuple[bool, str]:
    cleaned = clean_string(email)
    if not cleaned:
        return True, ""  # Optional
    if len(cleaned) > 120:
        return False, "Email address cannot exceed 120 characters."
    if not EMAIL_REGEX.match(cleaned):
        return False, "Please enter a valid email address (e.g., supplier@example.com)."
    return True, ""

def validate_gstin(gstin: str | None) -> tuple[bool, str]:
    cleaned = clean_string(gstin)
    if not cleaned:
        return True, ""  # Optional
    cleaned = cleaned.upper()
    if len(cleaned) != 15:
        return False, "GSTIN must be exactly 15 characters long (e.g. 27ABCDE1234F1Z5)."
    if not GSTIN_REGEX.match(cleaned):
        return False, "Invalid GSTIN format. Expected format: 2 numbers + 10 char PAN + 1 entity code + 'Z' + 1 check digit."
    return True, ""

def validate_pan(pan: str | None) -> tuple[bool, str]:
    cleaned = clean_string(pan)
    if not cleaned:
        return True, ""
    if not PAN_REGEX.fullmatch(cleaned):
        return False, "PAN must use the 10-character format (e.g. ABCDE1234F)."
    return True, ""

def validate_pin_code(pin_code: str | None) -> tuple[bool, str]:
    cleaned = clean_string(pin_code)
    if not cleaned:
        return True, ""  # Optional
    if not re.fullmatch(r"[\d\s-]+", cleaned):
        return False, "PIN Code must contain only digits, spaces, or hyphens."
    digits = re.sub(r"[^\d]", "", cleaned)
    if not PIN_CODE_REGEX.match(digits):
        return False, "PIN Code must be a valid 6-digit postal code (e.g. 400001)."
    return True, ""

def validate_opening_balance(balance_val: str | float | int | None) -> tuple[bool, float, str]:
    if balance_val is None or balance_val == "":
        return True, 0.0, ""
    try:
        val = float(balance_val)
        return True, round(val, 2), ""
    except (ValueError, TypeError):
        return False, 0.0, "Opening balance must be a valid numeric amount."

def format_currency(amount: float | None) -> str:
    if amount is None:
        amount = 0.0
    # Indian numbering format or standard two-decimal currency
    is_neg = amount < 0
    abs_amt = abs(amount)
    s = f"{abs_amt:.2f}"
    parts = s.split(".")
    integer_part = parts[0]
    decimal_part = parts[1]

    if len(integer_part) > 3:
        last3 = integer_part[-3:]
        remaining = integer_part[:-3]
        groups = []
        while len(remaining) > 2:
            groups.insert(0, remaining[-2:])
            remaining = remaining[:-2]
        if remaining:
            groups.insert(0, remaining)
        groups.append(last3)
        formatted_int = ",".join(groups)
    else:
        formatted_int = integer_part

    res = f"₹ {formatted_int}.{decimal_part}"
    return f"-{res}" if is_neg else res
