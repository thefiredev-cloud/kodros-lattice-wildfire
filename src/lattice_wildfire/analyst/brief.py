"""Pure brief rendering: facts-only LLM prompt, deterministic verifier, Markdown and JSON.

The model sees only the facts JSON. Its six-line answer is then checked deterministically: every
number, compass direction, station or hospital name and FHSZ class it writes must already appear
in the facts, so an invented value cannot ship. A line that fails is replaced by that section's
deterministic facts-only line and the brief states how many lines were replaced. Known model
glossary slips are normalised before the check (``Fuel hazard severity zone`` ->
``Fire Hazard Severity Zone``). When the local model is down the brief ships the deterministic
facts with an explicit line saying no narrative was generated.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

BRIEF_SCHEMA = "lattice-wildfire/analyst-brief/v2"
BRIEF_PREFIX = "lattice-wildfire/briefs"

LABELS = ("LOCATION", "FUEL/FHSZ", "WEATHER/WIND", "EXPOSURES", "NEAREST RESOURCES", "UNKNOWNS")

NARRATIVE_OK = "ok"
NARRATIVE_UNAVAILABLE = "unavailable"
NARRATIVE_REJECTED = "rejected"

UNAVAILABLE_LINE = "Narrative unavailable: local model down"
REJECTED_LINE = "Narrative rejected: local model output broke the six-line facts-only format"

SYSTEM_PROMPT = (
    "You are Kodros Analyst, writing a wildfire tactical brief for Los Angeles County fire operations.\n"
    "Use ONLY the facts in the JSON the user sends. Do not add any place, number, unit, time, cause, "
    "fuel type, fire behavior, or recommendation that is not in the JSON. Do not convert units. "
    "Use numbers only as they appear in the JSON. If a value is null, empty, or missing, write 'unknown'. "
    "Copy station and hospital names exactly as the JSON spells them, write compass directions exactly "
    "as the JSON spells them (for example WSW), and name the FHSZ class exactly as the JSON does. "
    "Write plain operational English and never print JSON key names. Answer directly; do not "
    "re-check each value at length.\n"
    "LOCATION covers the target, place, zones and nearby fire activity. WEATHER/WIND covers every "
    "forecast period and every alert. EXPOSURES covers hospitals; NEAREST RESOURCES covers fire stations.\n"
    "Answer with exactly six lines, no preamble, no Markdown, no blank lines, in this order and "
    "starting with these labels:\n" + "\n".join(f"{label}: <one sentence>" for label in LABELS)
)

_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_LINE_PREFIX = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])?\s*(?:\*\*)?\s*")

_FACILITY_KEYS = ("name", "distance_km", "direction")

# Glossary of model slips that mean a real term. Applied before verification, not as a rewrite of
# the operator's vocabulary: each entry is a spelling the cluster model has produced for a term the
# facts use.
GLOSSARY: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bfuel\s+hazard\s+severity\s+zone(s?)\b", re.IGNORECASE), "Fire Hazard Severity Zone"),
)


def apply_glossary(text: str) -> str:
    """Fix known model slips so a correct term is not counted as an unsupported word."""
    for pattern, replacement in GLOSSARY:
        text = pattern.sub(lambda m: replacement + (m.group(1) or ""), text)
    return text


# --------------------------------------------------------------------------- verifier vocabulary

# Facts keys that carry machine identifiers rather than reported values. Digits inside them would make
# almost any invented small integer look supported.
_BOOKKEEPING_KEYS = frozenset({"entity_id", "task_id", "id", "schema", "url"})

_COMPASS_WORDS = {
    "N": "north",
    "NNE": "north-northeast",
    "NE": "northeast",
    "ENE": "east-northeast",
    "E": "east",
    "ESE": "east-southeast",
    "SE": "southeast",
    "SSE": "south-southeast",
    "S": "south",
    "SSW": "south-southwest",
    "SW": "southwest",
    "WSW": "west-southwest",
    "W": "west",
    "WNW": "west-northwest",
    "NW": "northwest",
    "NNW": "north-northwest",
}
_ABBR = re.compile(r"(?<![\w/])(" + "|".join(sorted(_COMPASS_WORDS, key=len, reverse=True)) + r")(?![\w/])")
_DIRECTION_WORD = re.compile(
    r"\b(" + "|".join(sorted((w for w in _COMPASS_WORDS.values()), key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

_FACILITY_WORDS = frozenset(
    {"station", "stations", "hospital", "hospitals", "clinic", "clinics", "center", "centers",
     "department", "institute", "medical", "services", "service"}
)  # fmt: skip
_CAPITALISED_RUN = re.compile(r"\b[A-Z][\w'\u2019.-]*(?:\s+[A-Z0-9][\w'\u2019.-]*){0,5}\b")
_LEADING_STOPWORDS = frozenset(
    {"the", "a", "an", "its", "this", "that", "see", "at", "in", "near", "from", "and"}
)
_UNIT_WORDS = frozenset({"km", "mi", "miles", "mile", "mph", "deg", "degrees", "ft", "m"})
_MEASURE = re.compile(r"\d+(?:\.\d+)?(?:[a-z%]+)?$", re.IGNORECASE)
_STATION_NUMBER = re.compile(
    r"\b(?:fire\s+)?stations?\s*(?:#|no\.?)?\s*(\d{1,3}[a-z]?)(?!\s*(?:k?m|mi|miles?|ft)\b)", re.IGNORECASE
)

_FHSZ_TOKEN = re.compile(r"\b(Very\s+High|Non[\s-]?Wildland|Non\s+Wildland|Moderate|High)\b")
_FHSZ_CLASSES = ("Very High", "High", "Moderate", "NonWildland")

_NUMBER_TOLERANCE = 0.05  # distances are reported to 0.1 km, so a 0.05 match is the same value

_DATE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?")


def _strip_dates(text: str) -> str:
    """``text`` with ISO timestamps and dates blanked, so only reported numbers remain."""
    return _TIMESTAMP.sub(" ", _DATE.sub(" ", text))


def _numbers(text: str) -> list[float]:
    """Numbers in ``text``, ignoring ISO dates and times (those are checked as dates)."""
    return [float(m) for m in _NUMBER.findall(_strip_dates(text))]


def facts_numbers(facts: dict[str, Any]) -> list[float]:
    """Every number the facts report, excluding timestamps, ids and URLs.

    Timestamps would otherwise make an invented small integer look supported (any hour or month);
    a narrative date is checked against the dates the facts carry instead (``unsupported_reasons``).
    """
    out: list[float] = []

    def walk(value: Any, key: str | None) -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                if child_key in _BOOKKEEPING_KEYS:
                    continue
                walk(child, child_key)
        elif isinstance(value, list):
            for item in value:
                walk(item, key)
        elif isinstance(value, bool) or value is None:
            return
        elif isinstance(value, int | float):
            out.append(float(value))
        elif isinstance(value, str):
            out.extend(_numbers(value))

    walk(facts, None)
    return out


def facts_dates(facts: dict[str, Any]) -> set[str]:
    """Every ``YYYY-MM-DD`` the facts carry (forecast periods, alert ends, generation time)."""
    return set(_DATE.findall(json.dumps(facts, default=str)))


def _norm(text: str) -> str:
    """Lowercase, and collapse everything that is not a letter or digit to single spaces."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def facts_directions(facts: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Compass tokens the facts report: (abbreviations the facts write, full words they stand for)."""
    abbr: set[str] = set()
    words: set[str] = set()

    def walk(value: Any, key: str | None) -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                walk(child, child_key)
        elif isinstance(value, list):
            for item in value:
                walk(item, key)
        elif isinstance(value, str) and key in ("direction", "wind_direction"):
            token = value.strip().upper()
            if token in _COMPASS_WORDS:
                abbr.add(token)
                words.add(_COMPASS_WORDS[token])
                words.add(_COMPASS_WORDS[token].replace("-", ""))

    walk(facts, None)
    return abbr, words


def facility_names(facts: dict[str, Any]) -> list[str]:
    """Normalised station and hospital names the facts report."""
    rows = list((facts.get("resources") or {}).get("fire_stations") or []) + list(
        (facts.get("exposures") or {}).get("hospitals") or []
    )
    return [_norm(str(row.get("name") or "")) for row in rows if row.get("name")]


def _name_tokens(run: str) -> list[str]:
    """Tokens of a capitalised run with trailing measurements, units and compass tokens removed.

    A model writes "Station 71 WSW 1.7"; without this, the compass token and distance would be
    glued into the name and a correct station would look invented.
    """
    tokens = run.split()
    while tokens:
        last = tokens[-1].strip(".,")
        if last.upper() in _COMPASS_WORDS or last.lower() in _UNIT_WORDS or _MEASURE.fullmatch(last):
            tokens.pop()
            continue
        break
    while len(tokens) > 1 and tokens[0].lower().strip(".,") in _LEADING_STOPWORDS:
        tokens = tokens[1:]
    return tokens


def _facility_mention(run: str, names: list[str]) -> bool | None:
    """True when a capitalised run is a known facility name, None when it is not a name at all."""
    tokens = _name_tokens(run)
    if len(tokens) < 2:
        return None  # a bare "hospital", "station" or a dropped station number is not a name here
    phrase = _norm(" ".join(tokens))
    if not phrase or not any(token.lower() in _FACILITY_WORDS for token in phrase.split()):
        return None
    return any(phrase in name for name in names)


def _fhsz_class(body: str) -> str | None:
    for match in _FHSZ_TOKEN.finditer(body):
        token = re.sub(r"\s+", " ", match.group(1))
        if re.fullmatch(r"Non[\s-]?Wildland", token):
            return "NonWildland"
        for known in _FHSZ_CLASSES:
            if token == known:
                return known
    return None


def unsupported_reasons(body: str, facts: dict[str, Any]) -> list[str]:
    """Reasons one narrative body is not backed by the facts. Empty means verified.

    Checks every number, compass direction, station or hospital name and FHSZ class in ``body``
    against the full facts JSON. Generic words ("hospital", "stations") pass; a named facility
    must be one the facts list.
    """
    reasons: list[str] = []
    known = facts_numbers(facts)
    for token in _NUMBER.findall(_strip_dates(body)):
        value = float(token)
        if not any(
            abs(value - fact) <= _NUMBER_TOLERANCE or abs(abs(value) - abs(fact)) <= _NUMBER_TOLERANCE
            for fact in known
        ):
            reasons.append(f"number {token}")
    known_dates = facts_dates(facts)
    for date in _DATE.findall(body):
        if date not in known_dates:
            reasons.append(f"date {date}")

    abbr, words = facts_directions(facts)
    for match in _ABBR.finditer(body):
        if match.group(1) not in abbr:
            reasons.append(f"direction {match.group(1)}")
    for match in _DIRECTION_WORD.finditer(body):
        token = match.group(1).lower()
        if token not in words and token.replace("-", "") not in words:
            reasons.append(f"direction {match.group(1)}")

    names = facility_names(facts)
    for match in _CAPITALISED_RUN.finditer(body):
        if _facility_mention(match.group(0), names) is False:
            reasons.append(f"facility {match.group(0).strip()}")
    for match in _STATION_NUMBER.finditer(body):
        if not _known_station(match.group(1), names):
            reasons.append(f"station {match.group(0).strip()}")

    claimed = _fhsz_class(body)
    actual = (facts.get("fuel") or {}).get("fhsz")
    if claimed is not None and claimed != actual:
        reasons.append(f"FHSZ class {claimed}")

    out: list[str] = []
    for reason in reasons:
        if reason not in out:
            out.append(reason)
    return out


def _known_station(number: str, names: list[str]) -> bool:
    """True when a facts facility name carries exactly that station number ("9" must not match "99")."""
    pattern = re.compile(rf"\bstation {re.escape(number.lower())}(?![\w])")
    return any(pattern.search(name) for name in names)


@dataclass(frozen=True)
class VerifierResult:
    """Verified lines plus what the verifier replaced. ``lines`` is always six facts-backed lines."""

    lines: list[str]
    replaced: list[str] = field(default_factory=list)
    details: dict[str, list[str]] = field(default_factory=dict)

    @property
    def lines_replaced(self) -> int:
        return len(self.replaced)

    def as_dict(self) -> dict[str, Any]:
        return {"lines_replaced": self.lines_replaced, "replaced": self.replaced, "details": self.details}


def verify_narrative(lines: list[str], facts: dict[str, Any]) -> VerifierResult:
    """Deterministic check of a six-line narrative against the facts.

    A supported line is kept (with glossary slips fixed); a line with any unsupported value is
    replaced by the facts-only line for the same section. Pure: no clock, no network, no model.
    """
    fallback = fact_lines(facts)
    out: list[str] = []
    replaced: list[str] = []
    details: dict[str, list[str]] = {}
    for index, line in enumerate(lines):
        label, _, body = line.partition(":")
        label = label.strip()
        fixed = apply_glossary(body).strip()
        reasons = unsupported_reasons(fixed, facts)
        if reasons or index >= len(fallback):
            replaced.append(label)
            details[label] = reasons or ["no facts-only line for this section"]
            out.append(fallback[index] if index < len(fallback) else line)
        else:
            out.append(f"{label}: {fixed}")
    return VerifierResult(lines=out, replaced=replaced, details=details)


# --------------------------------------------------------------------------- prompt


def _pick(row: dict[str, Any] | None, keys: tuple[str, ...]) -> dict[str, Any] | None:
    return None if row is None else {k: row.get(k) for k in keys}


def _rows(rows: list[dict[str, Any]] | None, keys: tuple[str, ...]) -> list[dict[str, Any]] | None:
    return None if rows is None else [{k: r.get(k) for k in keys} for r in rows]


def prompt_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """The subset of facts the model sees: what the six lines need, no ids or bookkeeping.

    A reasoning model checks every value it is shown; on 2026-11-09 the full facts made the
    cluster model spend its whole token budget verifying ids, zones and bearings.
    """
    loc = facts.get("location") or {}
    weather = facts.get("weather") or {}
    nearby = facts.get("nearby_fire_activity") or {}
    place = loc.get("nearest_place") or {}
    return {
        "target": _pick(facts.get("target"), ("name", "data_type", "lat", "lon")),
        "location": {
            "place": f"{place.get('city')}, {place.get('state')}" if place.get("city") else None,
            "nws_office": loc.get("nws_office"),
            "fire_weather_zone": loc.get("fire_weather_zone"),
        },
        "nearby_fire_activity": {
            "radius_km": nearby.get("radius_km"),
            "entities": _rows(
                nearby.get("entities"),
                ("name", "data_type", "distance_km", "direction", "target_inside_polygon"),
            ),
        },
        "fuel": facts.get("fuel"),
        "weather": {
            "periods": _rows(
                weather.get("periods"),
                (
                    "name",
                    "temperature",
                    "temperature_unit",
                    "wind_speed",
                    "wind_direction",
                    "precip_chance_pct",
                    "short_forecast",
                ),
            ),  # fmt: skip
            "alerts": _rows(weather.get("alerts"), ("event", "severity", "ends")),
        },
        "exposures": {"hospitals": _rows((facts.get("exposures") or {}).get("hospitals"), _FACILITY_KEYS)},
        "resources": {
            "fire_stations": _rows((facts.get("resources") or {}).get("fire_stations"), _FACILITY_KEYS)
        },
        "unknowns": facts.get("unknowns"),
    }


def build_messages(facts: dict[str, Any]) -> list[dict[str, str]]:
    """Chat messages for core.llm.chat_result: system rules plus the facts JSON, nothing else."""
    body = json.dumps(prompt_facts(facts), indent=1, sort_keys=True)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Facts JSON:\n{body}\n\nWrite the six-line tactical brief."},
    ]


def parse_narrative(text: str) -> list[str] | None:
    """Six labelled lines in LABELS order, or None when the model broke the format."""
    cleaned = _THINK.sub("", text or "")
    found: dict[str, str] = {}
    for raw in cleaned.splitlines():
        line = _LINE_PREFIX.sub("", raw).replace("**", "").strip()
        for label in LABELS:
            if line.upper().startswith(label + ":"):
                content = line[len(label) + 1 :].strip()
                if content and label not in found:
                    found[label] = content
                break
    if len(found) != len(LABELS):
        return None
    return [f"{label}: {found[label]}" for label in LABELS]


@dataclass
class Narrative:
    """One brief's narrative: what the model said, what the verifier kept, and its provenance."""

    status: str
    lines: list[str] = field(default_factory=list)
    reason: str | None = None
    verifier: VerifierResult | None = None
    model: str | None = None
    latency_s: float | None = None
    usage: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "lines": self.lines,
            "reason": self.reason,
            "verifier": self.verifier.as_dict() if self.verifier else None,
            "model": self.model,
            "latency_s": None if self.latency_s is None else round(self.latency_s, 3),
            "usage": self.usage,
        }


def narrative_from_text(
    text: str,
    facts: dict[str, Any],
    *,
    model: str | None = None,
    latency_s: float | None = None,
    usage: dict[str, Any] | None = None,
) -> Narrative:
    """Verify a model answer into a Narrative. Unsupported lines become facts-only lines."""
    lines = parse_narrative(text)
    if lines is None:
        return Narrative(
            status=NARRATIVE_REJECTED, reason="output did not contain the six labelled lines", model=model,
            latency_s=latency_s, usage=usage or {},
        )  # fmt: skip
    verified = verify_narrative(lines, facts)
    return Narrative(
        status=NARRATIVE_OK,
        lines=verified.lines,
        verifier=verified,
        model=model,
        latency_s=latency_s,
        usage=usage or {},
    )


def narrative_unavailable(reason: str) -> Narrative:
    return Narrative(status=NARRATIVE_UNAVAILABLE, reason=reason)


# --------------------------------------------------------------------------- facts as text


def _fmt(value: Any, unit: str = "") -> str:
    return "unknown" if value is None else f"{value}{unit}"


def _facility_text(rows: list[dict[str, Any]] | None, what: str) -> str:
    if rows is None:
        return f"{what}: lookup failed"
    if not rows:
        return f"{what}: none found in search area"
    return f"{what}: " + "; ".join(
        f"{r['name']} {r['distance_km']} km {r['direction']} ({r['bearing_deg']} deg)" for r in rows
    )


def fact_lines(facts: dict[str, Any]) -> list[str]:
    """Deterministic six-line summary rendered straight from the facts (no model)."""
    target = facts["target"]
    loc = facts.get("location") or {}
    place = loc.get("nearest_place") or {}
    nearby = (facts.get("nearby_fire_activity") or {}).get("entities")
    radius = (facts.get("nearby_fire_activity") or {}).get("radius_km")
    location = (
        f"{target['name']} ({_fmt(target['data_type'])}) at {target['lat']}, {target['lon']}"
        f"; NWS place {_fmt(place.get('city'))}, {_fmt(place.get('state'))}"
        f"; office {_fmt(loc.get('nws_office'))}, grid {_fmt(loc.get('grid'))}"
        f", fire weather zone {_fmt(loc.get('fire_weather_zone'))}"
    )
    if nearby is None:
        location += "; nearby Lattice fire activity: unavailable"
    elif not nearby:
        location += f"; no Lattice fire activity within {radius} km"
    else:
        location += "; nearby: " + "; ".join(
            f"{n['name']} [{n['data_type']}] "
            + (
                "target inside polygon"
                if n["target_inside_polygon"]
                else f"{n['distance_km']} km {n['direction']}"
            )
            for n in nearby[:5]
        )

    fuel = facts.get("fuel") or {}
    status = fuel.get("fhsz_status", "unknown")
    fuel_line = f"FHSZ {fuel['fhsz']} ({status})" if fuel.get("fhsz") else f"FHSZ {status}"

    weather = facts.get("weather") or {}
    periods = weather.get("periods") or []
    if periods:
        wx = "; ".join(
            f"{p['name']}: {_fmt(p['temperature'])}{p.get('temperature_unit') or ''}, wind "
            f"{_fmt(p['wind_direction'])} {_fmt(p['wind_speed'])}, precip {_fmt(p['precip_chance_pct'], '%')}, "
            f"{_fmt(p['short_forecast'])}"
            for p in periods
        )
    else:
        wx = "forecast unavailable"
    alerts = weather.get("alerts")
    if alerts is None:
        wx += "; alerts: unavailable"
    elif not alerts:
        wx += "; no active NWS alerts at point"
    else:
        wx += "; alerts: " + "; ".join(f"{a['event']} (until {_fmt(a['ends'])})" for a in alerts)

    exposures = _facility_text((facts.get("exposures") or {}).get("hospitals"), "hospitals")
    resources = _facility_text((facts.get("resources") or {}).get("fire_stations"), "fire stations")
    unknowns = "; ".join(facts.get("unknowns") or []) or "none listed"
    values = (location, fuel_line, wx, exposures, resources, unknowns)
    return [f"{label}: {value}" for label, value in zip(LABELS, values, strict=True)]


# --------------------------------------------------------------------------- objects


def object_paths(task_id: str) -> tuple[str, str]:
    """Object store paths (Markdown, JSON) for one task's brief."""
    return f"{BRIEF_PREFIX}/{task_id}.md", f"{BRIEF_PREFIX}/{task_id}.json"


def render_json(facts: dict[str, Any], narrative: Narrative) -> dict[str, Any]:
    md_path, json_path = object_paths(facts["task_id"]) if facts.get("task_id") else (None, None)
    return {
        "schema": BRIEF_SCHEMA,
        "task_id": facts.get("task_id"),
        "target_entity_id": facts["target"]["entity_id"],
        "generated_at": facts["generated_at"],
        "objects": {"markdown": md_path, "json": json_path},
        "narrative": narrative.as_dict(),
        "facts": facts,
    }


def render_markdown(facts: dict[str, Any], narrative: Narrative) -> str:
    target = facts["target"]
    lines = [
        f"# Kodros Analyst brief: {target['name']}",
        "",
        f"- Target: `{target['entity_id'] or 'operator point'}` ({_fmt(target['data_type'])})"
        f" at {target['lat']}, {target['lon']}",
        f"- Task: `{facts.get('task_id') or 'none'}`",
        f"- Generated (UTC): {facts['generated_at']}",
        "",
        "## Tactical brief",
        "",
    ]
    if narrative.status == NARRATIVE_OK:
        lines += [f"- {line}" for line in narrative.lines]
        lines += ["", f"_{narrative_footer(narrative)}_"]
    else:
        headline = UNAVAILABLE_LINE if narrative.status == NARRATIVE_UNAVAILABLE else REJECTED_LINE
        lines.append(
            f"**{headline}** ({narrative.reason}). No text was generated; the facts below are the brief."
        )
    lines += ["", "## Facts", ""]
    lines += [f"- {line}" for line in fact_lines(facts)]
    lines += ["", "## Sources", "", "| Source | URL | Fetched (UTC) | Note |", "|---|---|---|---|"]
    for s in facts.get("sources") or []:
        note = s["error"] or ("cache, stale" if s["stale"] else "cache" if s["from_cache"] else "live")
        lines.append(f"| {s['name']} | {s['url']} | {_fmt(s['fetched_at'])} | {note} |")
    lines.append("")
    return "\n".join(lines)


def narrative_footer(narrative: Narrative) -> str:
    """One line naming the server model, its latency and how many lines the verifier replaced."""
    served = narrative.model or "unrecorded model"
    latency = f"{narrative.latency_s:.1f} s" if narrative.latency_s is not None else "unknown latency"
    verifier = narrative.verifier
    replaced = verifier.lines_replaced if verifier else 0
    text = (
        f"Tactical brief written by the local Kodros cluster model ({served}) in {latency} "
        f"from the facts below only. Verifier: {replaced} lines replaced"
    )
    if verifier and verifier.replaced:
        text += f" ({', '.join(verifier.replaced)} -> facts-only lines)"
    return text + "."
