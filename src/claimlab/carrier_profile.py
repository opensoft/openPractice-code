"""Build and sample an aggregate profile of CMS DE-SynPUF Carrier claims."""

import argparse
import csv
import hashlib
import io
import json
import re
from collections import Counter, defaultdict
from decimal import Decimal
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from zipfile import ZipFile


SOURCE_PAGE = (
    "https://www.cms.gov/data-research/statistics-trends-and-reports/"
    "medicare-claims-synthetic-public-use-files/"
    "cms-2008-2010-data-entrepreneurs-synthetic-public-use-file-de-synpuf/de10-sample-1"
)
PROFILE_NAME = "cms-desynpuf-carrier-1"
CHARGE_BAND_DOLLARS = 5
MIN_PATTERN_COUNT = 10
VISITS = {"99211", "99212", "99213", "99214", "99215"}
OFFICE_LAB_CODES = VISITS | {
    "36415", "80048", "80053", "80061", "81001", "81002", "81003", "82043",
    "82565", "82570", "82947", "82962", "83036", "84443", "85025", "85027",
}
HCPCS = re.compile(r"(?:[A-Z][0-9]{4}|[0-9]{4}[A-Z0-9])")
ZERO_AMOUNTS = {"", "0", "0.0", "0.00"}


def build_profile(archives):
    """Read only procedure/allowed-amount pairs and publish aggregate counts."""
    line_weights = Counter()
    pattern_weights = Counter()
    charge_weights = defaultdict(Counter)
    sources = []
    for path in sorted(archives, key=lambda path: path.name):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        row_count = 0
        with ZipFile(path) as archive:
            member, = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            with io.TextIOWrapper(archive.open(member), encoding="utf-8-sig", newline="") as stream:
                rows = csv.reader(stream)
                header = next(rows)
                columns = [
                    (header.index(f"HCPCS_CD_{slot}"), header.index(f"LINE_ALOWD_CHRG_AMT_{slot}"))
                    for slot in range(1, 14)
                ]
                for row in rows:
                    row_count += 1
                    line_count = 0
                    office_lab = set()
                    for code_index, charge_index in columns:
                        code, amount = row[code_index].strip(), row[charge_index].strip()
                        if amount in ZERO_AMOUNTS or amount.startswith("-") or not HCPCS.fullmatch(code):
                            continue
                        if float(amount) <= 0:
                            continue
                        line_count += 1
                        if code in OFFICE_LAB_CODES:
                            office_lab.add(code)
                            bucket = int(Decimal(amount) // CHARGE_BAND_DOLLARS)
                            charge_weights[code][bucket] += 1

                    if line_count:
                        line_weights[line_count] += 1
                    if office_lab and len(office_lab & VISITS) <= 1:
                        pattern_weights[tuple(sorted(office_lab))] += 1

        sources.append({"archive": path.name, "sha256": digest.hexdigest(), "rows": row_count})

    return {
        "profile": PROFILE_NAME,
        "source": SOURCE_PAGE,
        "archives": sources,
        "charge_band_dollars": CHARGE_BAND_DOLLARS,
        "minimum_pattern_count": MIN_PATTERN_COUNT,
        "priced_line_counts": sorted(line_weights.items()),
        "office_lab_patterns": [
            [list(pattern), count] for pattern, count in sorted(pattern_weights.items())
            if count >= MIN_PATTERN_COUNT
        ],
        "allowed_charge_bands": {code: sorted(weights.items()) for code, weights in sorted(charge_weights.items())},
    }


class CarrierProfile:
    def __init__(self, profile, digest):
        self.reference = {
            "profile": profile["profile"],
            "sha256": digest,
            "source": profile["source"],
        }
        self.line_counts, self.line_weights = zip(*profile["priced_line_counts"])
        self.patterns = defaultdict(list)
        for codes, weight in profile["office_lab_patterns"]:
            self.patterns[len(codes)].append((codes, weight))
        self.pattern_sizes = sorted(self.patterns)
        self.bands = profile["allowed_charge_bands"]
        self.code_weights = {
            code: sum(weight for _, weight in bands) for code, bands in self.bands.items()
        }
        self.band_cents = profile["charge_band_dollars"] * 100

    def lines(self, rng, service_date):
        line_count = rng.choices(self.line_counts, self.line_weights)[0]
        pattern_size = max((size for size in self.pattern_sizes if size <= line_count), default=0)
        if pattern_size:
            patterns, weights = zip(*self.patterns[pattern_size])
            codes = list(rng.choices(patterns, weights)[0])
        else:
            codes = []
        while len(codes) < line_count:
            include_visit = VISITS.isdisjoint(codes)
            candidates = [code for code in self.code_weights if code not in codes and (include_visit or code not in VISITS)]
            codes.append(rng.choices(candidates, [self.code_weights[code] for code in candidates])[0])

        lines = []
        for code in codes:
            bands, weights = zip(*self.bands[code])
            start = rng.choices(bands, weights)[0] * self.band_cents
            cents = rng.randrange(max(1, start), start + self.band_cents)
            lines.append({"cpt": code, "charge": Decimal(cents) / 100, "date": service_date})
        return lines


@lru_cache(maxsize=1)
def load_profile():
    raw = files(__package__).joinpath("fixtures").joinpath("carrier-profile.json").read_bytes()
    return CarrierProfile(json.loads(raw), hashlib.sha256(raw).hexdigest())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", type=Path, nargs="+")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    profile = build_profile(args.archives)
    if not profile["priced_line_counts"] or not profile["office_lab_patterns"]:
        parser.exit(1, "The input did not contain enough priced office/lab lines for a profile.\n")
    args.output.write_bytes((json.dumps(profile, separators=(",", ":")) + "\n").encode("utf-8"))
    print(json.dumps({
        "rows": sum(source["rows"] for source in profile["archives"]),
        "priced_claims": sum(weight for _, weight in profile["priced_line_counts"]),
        "office_lab_patterns": len(profile["office_lab_patterns"]),
    }))


if __name__ == "__main__":
    main()
