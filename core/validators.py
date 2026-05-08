from core.schemas import VehicleVariant, VerificationStatus, BodyType


CRITICAL_FIELDS = ("body_type", "seats", "fuel_type", "engine", "transmission", "drivetrain")
ALL_FIELDS = ("body_type", "seats", "engine", "transmission", "fuel_type", "drivetrain")
IDENTITY_FIELDS = ("engine", "transmission", "fuel_type", "body_type", "generation")


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
    if variant.year_start > variant.year_end:
        return "unresolved"
    fields = {name: getattr(variant, name) for name in ALL_FIELDS}

    if any(fields[name].status == VerificationStatus.conflict for name in CRITICAL_FIELDS):
        return "conflict"

    if any(f.used_in_compare and f.sources_count == 0 for f in fields.values()):
        return "unresolved"

    sourced_critical = sum(1 for n in CRITICAL_FIELDS if fields[n].sources_count >= 1)
    multi_sourced_critical = sum(1 for n in CRITICAL_FIELDS if fields[n].sources_count >= 2)

    verified_ready = (
        fields["body_type"].status in {VerificationStatus.verified, VerificationStatus.partial}
        and fields["seats"].status in {VerificationStatus.verified, VerificationStatus.partial}
        and fields["fuel_type"].status in {VerificationStatus.verified, VerificationStatus.partial}
        and (
            fields["engine"].status == VerificationStatus.verified
            or fields["transmission"].status == VerificationStatus.verified
        )
        and sourced_critical >= 4
        and multi_sourced_critical >= 2
    )
    if verified_ready:
        return "verified"

    identity_values = {
        "engine": fields["engine"].value,
        "transmission": fields["transmission"].value,
        "fuel_type": fields["fuel_type"].value,
        "body_type": fields["body_type"].value,
        "generation": variant.generation,
    }
    has_usable_identity = any(v not in (None, "") for v in identity_values.values())
    non_null_critical = sum(1 for n in CRITICAL_FIELDS if fields[n].value not in (None, ""))

    if has_usable_identity and non_null_critical >= 2 and sourced_critical >= 1 and (variant.variant_id or "").strip():
        return "partial"

    if not has_usable_identity:
        return "unresolved"

    if all(fields[n].sources_count == 0 and fields[n].status in {VerificationStatus.unknown, VerificationStatus.unverified} for n in CRITICAL_FIELDS):
        return "unresolved"

    return "unverified"
