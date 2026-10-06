"""Policy thresholds for the invoice fraud detector.

All money values are in USD. Tune these to match your AP policy; the
checks in checks.py read everything from here so policy lives in one place.
"""

# Invoices at or above this amount require managerial approval.
APPROVAL_THRESHOLD = 10_000.0

# Split-invoice detection: how far apart (days) invoices from the same vendor
# may be while still counting as one cluster.
SPLIT_WINDOW_DAYS = 30

# Invoices at or above this amount must reference a purchase order.
NO_PO_LIMIT = 500.0

# Allowed deviation of an invoiced unit price / subtotal from the PO
# before it is flagged (fraction, e.g. 0.05 = 5%).
PO_PRICE_TOLERANCE = 0.05

# Deviation above PO_PRICE_TOLERANCE that escalates a price finding to high.
PO_PRICE_HIGH_WATERMARK = 0.20

# Rounding slack for arithmetic reconciliation (fraction of a cent issues).
MATH_TOLERANCE = 0.01

# Verdict mapping: the worst finding severity decides the verdict.
#   high   -> REJECT
#   medium -> FLAG_FOR_REVIEW
#   (none) -> APPROVE
