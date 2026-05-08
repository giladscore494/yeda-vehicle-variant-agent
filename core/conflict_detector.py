from collections import defaultdict
from datetime import datetime, timezone
from core.schemas import ConflictRecord

def detect_conflicts(variants):
    groups=defaultdict(list)
    for v in variants: groups[(v.make,v.model,v.year_start,v.year_end)].append(v)
    out=[]
    now=datetime.now(timezone.utc).isoformat()
    for (make,model,ys,ye),items in sorted(groups.items()):
        seats={i.seats.value for i in items if i.seats.value is not None}
        body={i.body_type.value for i in items if i.body_type.value is not None}
        engines={i.engine.value for i in items if i.engine.value is not None}
        if len(seats)>1: out.append(ConflictRecord(conflict_id=f'{make}_{model}_{ys}_{ye}_seats',make=make,model=model,year_start=ys,year_end=ye,field_name='seats',values_found=sorted(seats),source_ids=[],reason='Conflicting seats',recommended_action='review',created_at=now))
        if len(body)>1: out.append(ConflictRecord(conflict_id=f'{make}_{model}_{ys}_{ye}_body_type',make=make,model=model,year_start=ys,year_end=ye,field_name='body_type',values_found=sorted(body),source_ids=[],reason='Conflicting body type',recommended_action='review',created_at=now))
        if len(engines)>1: out.append(ConflictRecord(conflict_id=f'{make}_{model}_{ys}_{ye}_engine',make=make,model=model,year_start=ys,year_end=ye,field_name='engine',values_found=sorted(engines),source_ids=[],reason='Multiple engines without split',recommended_action='split_variants',created_at=now))
    return out
