from core.schemas import VehicleVariant, VerificationStatus, BodyType

def field_is_trusted(field):
    return field.status==VerificationStatus.verified and field.sources_count>=1 and field.used_in_compare

def validate_variant(variant:VehicleVariant):
    errs=[]
    if variant.year_start>variant.year_end: errs.append('invalid_year_range')
    if not variant.variant_id: errs.append('variant_id_empty')
    for f in [variant.body_type,variant.seats,variant.engine,variant.transmission,variant.fuel_type,variant.drivetrain]:
        if f.status in {VerificationStatus.unknown,VerificationStatus.unverified,VerificationStatus.conflict} and f.used_in_compare:
            errs.append('invalid_used_in_compare')
        if f.status==VerificationStatus.verified and f.sources_count<1: errs.append('verified_without_source')
    seats=variant.seats.value
    if isinstance(seats,int):
        max_seats=20 if variant.body_type.value in {BodyType.van.value,BodyType.commercial.value} else 9
        if not (1<=seats<=max_seats): errs.append('invalid_seats')
    return (len(errs)==0,errs)

def classify_variant(variant:VehicleVariant)->str:
    critical=[variant.body_type,variant.seats,variant.engine,variant.transmission,variant.fuel_type,variant.drivetrain]
    if any(f.status==VerificationStatus.conflict for f in critical): return 'conflict'
    if variant.body_type.status==VerificationStatus.verified and variant.seats.status==VerificationStatus.verified and variant.confidence.value in {'high','medium'} and any(f.status in {VerificationStatus.verified,VerificationStatus.partial} for f in [variant.fuel_type,variant.engine,variant.transmission]):
        return 'verified'
    if variant.body_type.status==VerificationStatus.verified or variant.seats.status==VerificationStatus.verified:
        return 'partial'
    return 'unverified'
