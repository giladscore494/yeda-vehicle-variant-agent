from __future__ import annotations
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field, model_validator

class VerificationStatus(str, Enum):
    verified = "verified"
    partial = "partial"
    conflict = "conflict"
    unverified = "unverified"
    unknown = "unknown"

class Confidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"

class BodyType(str, Enum):
    sedan="sedan"; hatchback="hatchback"; suv="suv"; crossover="crossover"; wagon="wagon"; mpv="mpv"; pickup="pickup"; van="van"; coupe="coupe"; convertible="convertible"; minivan="minivan"; commercial="commercial"; unknown="unknown"

class FuelType(str, Enum):
    petrol="petrol"; diesel="diesel"; hybrid="hybrid"; plug_in_hybrid="plug_in_hybrid"; electric="electric"; hydrogen="hydrogen"; lpg="lpg"; unknown="unknown"

class Transmission(str, Enum):
    manual="manual"; automatic="automatic"; cvt="cvt"; e_cvt="e_cvt"; dual_clutch="dual_clutch"; single_speed_ev="single_speed_ev"; unknown="unknown"

class Market(str, Enum):
    IL="IL"; EU="EU"; US="US"; GLOBAL="GLOBAL"; UNKNOWN="UNKNOWN"

class VehicleModelSeed(BaseModel):
    make: str
    model_raw: str
    model: str
    aliases: list[str] = Field(default_factory=list)
    year_start: int | None = None
    year_end: int | None = None
    source: str = "car_models_dict"

class EvidenceSource(BaseModel):
    source_id: str
    source_name: str | None = None
    url: str
    source_type: str
    market_scope: Market
    title: str | None = None
    retrieved_at: str
    evidence_snippet: str | None = None
    reliability_score: int
    fields_supported: list[str] = Field(default_factory=list)

class VerifiedField(BaseModel):
    value: Any = None
    status: VerificationStatus
    confidence: Confidence
    sources_count: int = 0
    source_ids: list[str] = Field(default_factory=list)
    used_in_compare: bool = False
    reason: str | None = None

class VehicleVariant(BaseModel):
    variant_id: str
    make: str
    model: str
    aliases: list[str] = Field(default_factory=list)
    year_start: int
    year_end: int
    market: Market
    generation: str | None = None
    body_type: VerifiedField
    seats: VerifiedField
    engine: VerifiedField
    transmission: VerifiedField
    fuel_type: VerifiedField
    drivetrain: VerifiedField
    trim: VerifiedField | None = None
    doors: VerifiedField | None = None
    verification_status: VerificationStatus
    confidence: Confidence
    sources_count: int
    created_at: str
    updated_at: str
    notes: list[str] = Field(default_factory=list)
    candidate_raw: dict = Field(default_factory=dict)
    identity_confidence: str = "unknown"

    @model_validator(mode="after")
    def check_years(self):
        if self.year_start > self.year_end:
            raise ValueError("year_start must be <= year_end")
        return self

class ConflictRecord(BaseModel):
    conflict_id: str
    make: str
    model: str
    year_start: int | None = None
    year_end: int | None = None
    field_name: str
    values_found: list[Any] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    reason: str
    recommended_action: str
    created_at: str

class RunTrace(BaseModel):
    run_id: str
    input: dict
    started_at: str
    finished_at: str | None = None
    status: str
    search_queries: list[str] = Field(default_factory=list)
    sources_found: int = 0
    facts_extracted: int = 0
    variants_created: int = 0
    verified_count: int = 0
    partial_count: int = 0
    conflict_count: int = 0
    unresolved_count: int = 0
    blocked_fields: list[str] = Field(default_factory=list)
    final_decision: dict = Field(default_factory=dict)
    error: str | None = None
