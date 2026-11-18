"""Named research locations. Airport coordinates follow the documented FMZ specification."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Airport:
    icao: str
    name: str
    latitude: float
    longitude: float
    timezone: str

    def metadata(self) -> dict:
        return asdict(self)


PUDONG = Airport("ZSPD", "上海浦东国际机场", 31.1443, 121.8083, "Asia/Shanghai")


@dataclass(frozen=True)
class SamplingPoint:
    id: str
    name: str
    latitude: float
    longitude: float


# Reproducible samples around the reference, not five official observing stations.
# Latitude offsets are about 1.1 km; longitude offsets are about 0.95 km here.
PUDONG_SAMPLING_POINTS = (
    SamplingPoint("center", "机场中心参考点", PUDONG.latitude, PUDONG.longitude),
    SamplingPoint("north", "机场参考点以北采样点", 31.1543, 121.8083),
    SamplingPoint("south", "机场参考点以南采样点", 31.1343, 121.8083),
    SamplingPoint("east", "机场参考点以东采样点", 31.1443, 121.8183),
    SamplingPoint("west", "机场参考点以西采样点", 31.1443, 121.7983),
)
