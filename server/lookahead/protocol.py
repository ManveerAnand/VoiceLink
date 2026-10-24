"""Strict models and limits for VoiceLink look-ahead protocol 1."""

from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator


PROTOCOL = 1
MAX_UNITS = 12
MAX_CHARACTERS = 12_000
MAX_UNIT_CHARACTERS = 4_000
MAX_BODY_BYTES = 65_536
MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991

SafeNonNegativeInteger = Annotated[
    int, Field(strict=True, ge=0, le=MAX_SAFE_JSON_INTEGER)
]


class Direction(str, Enum):
    LTR = "ltr"
    RTL = "rtl"
    AUTO = "auto"


class BlockKind(str, Enum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list-item"
    BLOCKQUOTE = "blockquote"
    CAPTION = "caption"
    TABLE_CELL = "table-cell"
    CODE = "code"
    MATH = "math"
    IMAGE_DESCRIPTION = "image-description"
    PAGE_BREAK = "page-break"
    OTHER = "other"


class BoundaryKind(str, Enum):
    NONE = "none"
    SENTENCE = "sentence"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list-item"
    HEADING_TO_BODY = "heading-to-body"
    SECTION = "section"
    DOCUMENT = "document"


class LookAheadSource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rate: float = Field(strict=True, allow_inf_nan=False)
    voice_name: str | None = Field(default=None, alias="voiceName")
    voice_uri: str | None = Field(default=None, alias="voiceURI")
    lang: str | None = None

    @field_validator("voice_name", "voice_uri", "lang", mode="before")
    @classmethod
    def optional_strings_must_be_omitted_not_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("optional values must be omitted, not null")
        return value


class LookAheadSynthesisHint(BaseModel):
    model_config = ConfigDict(extra="ignore")

    route: str
    voice: str
    speed: float = Field(strict=True, allow_inf_nan=False)
    format: str


class LookAheadBlock(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    kind: BlockKind
    html_tag: str | None = Field(default=None, alias="htmlTag")
    heading_level: SafeNonNegativeInteger | None = Field(default=None, alias="headingLevel")
    epub_types: list[str] | None = Field(default=None, alias="epubTypes")
    aria_role: str | None = Field(default=None, alias="ariaRole")

    @field_validator("html_tag", "heading_level", "epub_types", "aria_role", mode="before")
    @classmethod
    def optionals_must_be_omitted_not_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("optional values must be omitted, not null")
        return value


class LookAheadPosition(BaseModel):
    model_config = ConfigDict(extra="ignore")

    index_in_block: SafeNonNegativeInteger = Field(alias="indexInBlock")
    first_in_block: bool = Field(strict=True, alias="firstInBlock")
    last_in_block: bool = Field(strict=True, alias="lastInBlock")


class LookAheadUnit(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sequence: SafeNonNegativeInteger
    text: str = Field(min_length=1)
    lang: str | None = None
    direction: Direction | None = None
    block: LookAheadBlock
    position: LookAheadPosition
    boundary_before: BoundaryKind = Field(alias="boundaryBefore")
    boundary_after: BoundaryKind = Field(alias="boundaryAfter")

    @field_validator("lang", "direction", mode="before")
    @classmethod
    def optionals_must_be_omitted_not_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("optional values must be omitted, not null")
        return value


class LookAheadSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    protocol: Literal[PROTOCOL]
    generation: SafeNonNegativeInteger
    revision: SafeNonNegativeInteger
    current_sequence: SafeNonNegativeInteger = Field(alias="currentSequence")
    source: LookAheadSource
    synthesis_hint: LookAheadSynthesisHint | None = Field(default=None, alias="synthesisHint")
    units: list[LookAheadUnit] = Field(min_length=1)
    _body_digest: str = PrivateAttr(default="")

    @field_validator("synthesis_hint", mode="before")
    @classmethod
    def synthesis_hint_must_be_omitted_not_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("optional values must be omitted, not null")
        return value

    @model_validator(mode="after")
    def units_start_at_current_sequence_and_are_consecutive(self) -> "LookAheadSnapshot":
        for offset, unit in enumerate(self.units):
            if unit.sequence != self.current_sequence + offset:
                raise ValueError("unit sequences must start at currentSequence and be consecutive")
        return self

    def scalar_count(self) -> int:
        """Return Unicode scalar count; Python strings count Unicode code points."""
        return sum(len(unit.text) for unit in self.units)

    def limit_error(self) -> str | None:
        if len(self.units) > MAX_UNITS:
            return f"at most {MAX_UNITS} units are permitted"
        for unit in self.units:
            if len(unit.text) > MAX_UNIT_CHARACTERS:
                return f"each unit may contain at most {MAX_UNIT_CHARACTERS} characters"
        if self.scalar_count() > MAX_CHARACTERS:
            return f"at most {MAX_CHARACTERS} characters are permitted"
        return None


class LookAheadCapabilities(BaseModel):
    protocols: list[int]
    max_units: int = Field(alias="maxUnits")
    max_characters: int = Field(alias="maxCharacters")
    max_unit_characters: int = Field(alias="maxUnitCharacters")
    max_body_bytes: int = Field(alias="maxBodyBytes")


class ProtocolError(BaseModel):
    code: str
    message: str


class ProtocolErrorBody(BaseModel):
    error: ProtocolError
