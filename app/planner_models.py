"""
Pydantic request/response models for the planner endpoints (app/planner.py).
Split out to keep planner.py focused on the planning logic.
"""
from typing import Optional

from pydantic import BaseModel


class CharConfigEntry(BaseModel):
    character_id: int
    planet_limit: Optional[int] = None
    extractor_limit: Optional[int] = None  # None=all extractor, 0=factory only, N=max extractors
    ccu: Optional[int] = None              # command-centre level override (None = use ESI value)


class SaveConfigRequest(BaseModel):
    configs: list[CharConfigEntry]


class PlanRequest(BaseModel):
    type_id: int
    overproduction_pct: int = 10
    use_existing: bool = True
    constellations: list[str] = []
    preferred_systems: int = 1
    chosen_systems: list[str] = []
    factory_system: str = ""
    factory_output_per_hour: Optional[float] = None  # override SDE rate; use 0.5 for P4
    factory_character_ids: list[int] = []  # if set, only these chars do factories
    max_jumps: int = 1  # prefer system combos clustered within this many jumps (0–5)
    split_mode: str = "off"  # off | on — split-extraction (consolidate freed planets → factories)
    distribution_mode: str = "stability"  # stability (count ∝ need/density) | need (∝ need)
    min_density_pct: int = 0  # ignore planets thinner than this %, in the plan AND the recs (0 = off)
    extractor_no_storage: bool = False  # extractor template buffers P0 in the launchpad (no storage hub)


class PlanShareSave(BaseModel):
    payload: dict
    anonymize: bool = True   # default to the safe option (no locatable data stored)


class ProfileSave(BaseModel):
    name: str
    type_id: int
    type_name: str = ""
    overproduction_pct: int = 10
    preferred_systems: int = 1
    constellations: list[str] = []
    use_existing: bool = True
    factory_system: str = ""
    factory_output_per_hour: Optional[float] = None
    factory_character_ids: list[int] = []
    max_jumps: int = 1
    factory_planet_types: list[str] = []
    split_mode: str = "off"
    distribution_mode: str = "stability"
    min_density_pct: int = 0


class PlanSnapshotSave(BaseModel):
    name: str
    snapshot: dict
