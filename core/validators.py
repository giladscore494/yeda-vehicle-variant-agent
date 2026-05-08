from core.schemas import VehicleVariant, VerificationStatus, BodyType


CRITICAL_FIELDS = ("body_type", "seats", "fuel_type", "engine", "transmission")
ALL_FIELDS = ("body_type", "seats", "engine", "transmission", "fuel_type", "drivetrain")


def _fields(variant: VehicleVariant):
    return [getattr(variant, name) for name in ALL_FIELDS]


def field_is_trusted(field):
    return (
        field.status == VerificationStatus.verified
        and field.sources_count >= 1
        and field.used_in_compare
    )


def validate_variant(variant: VehicleVariant):
    errs = []
    if variant.year_start > variant.year_end:
        errs.append("invalid_year_range")
    if not (variant.variant_id or "").strip():
        errs.append("variant_id_empty")

    for f in _fields(variant):
        if (
            f.status
            in {
                VerificationStatus.unknown,
                VerificationStatus.unverified,
                VerificationStatus.conflict,
            }
            and f.used_in_compare
        ):
            errs.append("invalid_used_in_compare")
        if f.status == VerificationStatus.verified and f.sources_count < 1:
            errs.append("verified_without_source")

    seats = variant.seats.value
    if not isinstance(seats, int):
        errs.append("invalid_seats")
    else:
        max_seats = (
            20
            if variant.body_type.value in {BodyType.van.value, BodyType.commercial.value}
            else 9
        )
        if not (1 <= seats <= max_seats):
            errs.append("invalid_seats")

    return (len(errs) == 0, sorted(set(errs)))


def classify_variant(variant: VehicleVariant) -> str:
    fields = {name: getattr(variant, name) for name in ALL_FIELDS}

    if any(fields[name].status == VerificationStatus.conflict for name in CRITICAL_FIELDS):
        return "conflict"

    if variant.confidence.value not in {"high", "medium"}:
        verified_ready = False
    else:
        verified_ready = (
            fields["body_type"].status == VerificationStatus.verified
            and fields["seats"].status == VerificationStatus.verified
            and fields["fuel_type"].status == VerificationStatus.verified
            and (
                fields["engine"].status == VerificationStatus.verified
                or fields["transmission"].status == VerificationStatus.verified
            )
        )

    if verified_ready:
        per_field_valid = True
        for f in fields.values():
            if f.status == VerificationStatus.verified and f.sources_count < 1:
                per_field_valid = False
                break
            if f.status in {
                VerificationStatus.unknown,
                VerificationStatus.unverified,
                VerificationStatus.conflict,
            } and f.used_in_compare:
                per_field_valid = False
                break
        if per_field_valid:
            return "verified"

    important_statuses = [
        fields["fuel_type"].status,
        fields["engine"].status,
        fields["transmission"].status,
        fields["drivetrain"].status,
    ]
    has_partial_or_unknown = any(
        s in {VerificationStatus.partial, VerificationStatus.inferred, VerificationStatus.unknown, VerificationStatus.unverified}
        for s in important_statuses
    )
    if (
        fields["body_type"].status == VerificationStatus.verified
        or fields["seats"].status == VerificationStatus.verified
    ) and has_partial_or_unknown:
        return "partial"

    return "unverified"
