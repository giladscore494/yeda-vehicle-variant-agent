from core.schemas import Confidence, VerificationStatus, VerifiedField

def calculate_field_confidence(status,sources_count,source_quality_scores=None)->Confidence:
    if status==VerificationStatus.verified: return Confidence.high if sources_count>=2 else Confidence.medium
    if status==VerificationStatus.partial: return Confidence.medium if sources_count>=1 else Confidence.low
    return Confidence.low

def calculate_overall_confidence(fields:list[VerifiedField])->Confidence:
    vals=[f.confidence for f in fields]
    if vals and all(v==Confidence.high for v in vals): return Confidence.high
    if any(v in (Confidence.high,Confidence.medium) for v in vals): return Confidence.medium
    return Confidence.low
