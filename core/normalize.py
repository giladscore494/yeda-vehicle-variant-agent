import re
from core.schemas import BodyType, FuelType, Transmission, Market

def clean_text(value:str)->str: return re.sub(r"\s+"," ",(value or "").strip())
def normalize_make(make:str)->str: return clean_text(make).title()
def normalize_model(model:str)->str: return clean_text(model)

def _contains(v,*k): v=v.lower(); return any(x in v for x in k)

def normalize_body_type(value:str)->BodyType:
    v=clean_text(value).lower()
    if _contains(v,"sport utility","jeep","suv"): return BodyType.suv
    if _contains(v,"crossover","cuv"): return BodyType.crossover
    if _contains(v,"estate","touring"): return BodyType.wagon
    if "saloon" in v: return BodyType.sedan
    if "people carrier" in v: return BodyType.mpv
    if "minivan" in v: return BodyType.minivan
    if "commercial" in v: return BodyType.commercial
    if "van" in v: return BodyType.van
    return BodyType(v) if v in BodyType._value2member_map_ else BodyType.unknown

def normalize_fuel_type(value:str)->FuelType:
    v=clean_text(value).lower()
    if _contains(v,"gasoline","benzine","petrol"): return FuelType.petrol
    if "diesel" in v: return FuelType.diesel
    if _contains(v,"phev","plug-in hybrid","plug in hybrid"): return FuelType.plug_in_hybrid
    if _contains(v,"hev","hybrid"): return FuelType.hybrid
    if _contains(v,"ev","bev","electric"): return FuelType.electric
    if _contains(v,"hydrogen","fuel cell"): return FuelType.hydrogen
    if _contains(v,"lpg","gas"): return FuelType.lpg
    return FuelType.unknown

def normalize_transmission(value:str)->Transmission:
    v=clean_text(value).lower()
    if _contains(v,"dct","dsg","dual clutch"): return Transmission.dual_clutch
    if _contains(v,"e-cvt","ecvt"): return Transmission.e_cvt
    if "cvt" in v: return Transmission.cvt
    if _contains(v,"single speed","single-speed","ev reduction"): return Transmission.single_speed_ev
    if _contains(v,"manual"," mt"): return Transmission.manual
    if _contains(v,"automatic","auto","a/t"): return Transmission.automatic
    return Transmission.unknown

def normalize_market(value:str)->Market:
    v=clean_text(value).lower()
    if v in {"israel","il"}: return Market.IL
    if v in {"europe","eu"}: return Market.EU
    if v in {"usa","us","america"}: return Market.US
    if v in {"global","worldwide"}: return Market.GLOBAL
    return Market.UNKNOWN

def normalize_drivetrain(value:str|None)->str|None:
    if not value: return None
    v=clean_text(value).upper().replace(" ","")
    for x in ["FWD","RWD","AWD","4WD"]:
        if x in v: return x
    return clean_text(value)


def normalize_verification_status(value:str)->str:
    v=clean_text(str(value or "")).lower()
    mapping={
        "verified":"verified","partial":"partial","conflict":"conflict","unverified":"unverified","unknown":"unknown",
        "inferred":"unknown","assumed":"unknown","likely":"unknown","typical":"unknown","common":"unknown","estimated":"unknown","guessed":"unknown",
    }
    return mapping.get(v,"unknown")
