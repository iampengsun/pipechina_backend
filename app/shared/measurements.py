"""Finite before/after measurements and immutable criteria for independent retests."""

from datetime import UTC, datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.shared.errors import ConflictError


class RetestMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    metric: str = Field(min_length=2, max_length=120)
    unit: str = Field(min_length=1, max_length=40)
    before: float = Field(allow_inf_nan=False)
    after: float = Field(allow_inf_nan=False)
    minimum: float | None = Field(default=None, allow_inf_nan=False)
    maximum: float | None = Field(default=None, allow_inf_nan=False)
    criterion: str = Field(min_length=2, max_length=1000)
    measured_at: AwareDatetime

    @model_validator(mode="after")
    def valid_bounds(self):
        if self.minimum is None and self.maximum is None:
            raise ValueError("a retest needs an acceptance limit")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum exceeds maximum")
        return self


def measurements_by_key(measurements):
    values = {(m.metric, m.unit): m for m in measurements}
    if len(values) != len(measurements):
        raise ConflictError("duplicate retest metrics")
    if any(m.measured_at > datetime.now(UTC) for m in measurements):
        raise ConflictError("retest time must not be in the future")
    return values


def passes(measurement):
    return (measurement.minimum is None or measurement.after >= measurement.minimum) and (
        measurement.maximum is None or measurement.after <= measurement.maximum
    )


def verify_measurements(original_values, verification, *, approved):
    original = measurements_by_key([RetestMeasurement.model_validate(m) for m in original_values])
    verified = measurements_by_key(verification)
    if original.keys() != verified.keys():
        raise ConflictError("verification must cover the same retest metrics")
    for key, measurement in verified.items():
        source = original[key]
        if (
            measurement.before,
            measurement.minimum,
            measurement.maximum,
            measurement.criterion,
        ) != (
            source.before,
            source.minimum,
            source.maximum,
            source.criterion,
        ) or measurement.measured_at < source.measured_at:
            raise ConflictError(
                "verification must preserve acceptance criteria and follow the retest"
            )
        if approved and (not passes(source) or not passes(measurement)):
            raise ConflictError("failed measurements cannot be accepted")
