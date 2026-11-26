"""Split the submission's ordered X12 elements and render the stored v1 format."""

from claimlab import x837p


FORMAT_V1 = {"version": 1, "encoding": "ascii", "separator": "*", "terminator": "~"}
CONTROL_SEGMENTS = {"ISA", "GS", "ST", "BHT", "HL", "LX", "SE", "GE", "IEA"}
REDACTED_ELEMENTS = {"CLM": {0, 2}, "SV1": {0, 2, 3, 4}}


def freeze_837p(data, *, control_number, now):
    redacted = []
    protected = {}
    for index, segment in enumerate(x837p.build_segments(data, ctrl=control_number, test=True, now=now)):
        fields = segment.removesuffix("~").split("*")
        if fields[0] in CONTROL_SEGMENTS:
            redacted.append(fields)
            continue
        visible_positions = REDACTED_ELEMENTS.get(fields[0], {0})
        redacted.append([field if pos in visible_positions else None for pos, field in enumerate(fields)])
        concealed = [field for pos, field in enumerate(fields) if pos not in visible_positions]
        if concealed:
            protected[str(index)] = concealed
    return {**FORMAT_V1, "segments": redacted}, protected


def render_frozen_837p(billing, phi):
    # Keep v1 independent of the current exporter so old submissions remain reproducible.
    if {key: billing.get(key) for key in FORMAT_V1} != FORMAT_V1:
        raise ValueError("Unsupported frozen 837P format.")
    segments = []
    used = set()
    for index, fields in enumerate(billing["segments"]):
        key = str(index)
        protected = phi.get(key, [])
        if fields.count(None) != len(protected):
            raise ValueError("The billing and PHI snapshots do not match.")
        if protected:
            used.add(key)
        values = iter(protected)
        elements = [next(values) if field is None else field for field in fields]
        segments.append(billing["separator"].join(elements))
    if used != set(phi):
        raise ValueError("The billing and PHI snapshots do not match.")
    return (billing["terminator"].join(segments) + billing["terminator"]).encode(billing["encoding"])
