"""Build and sample an aggregate profile from CMS Carrier files."""

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
FREQUENCY_SOURCE = (
    "https://www.cms.gov/data-research/statistics-trends-and-reports/"
    "basic-stand-alone-medicare-claims-public-use-files/bsa-carrier-line-items-puf"
)
PROFILE_NAME = "cms-carrier-combined-1"
CHARGE_BAND_DOLLARS = 5
MIN_PATTERN_COUNT = 10
VISITS = {"99211", "99212", "99213", "99214", "99215"}
OFFICE_LAB_CODES = VISITS | {
    "36415", "80048", "80053", "80061", "81001", "81002", "81003", "82043",
    "82565", "82570", "82947", "82962", "83036", "84443", "85025", "85027",
}
HCPCS = re.compile(r"(?:[A-Z][0-9]{4}|[0-9]{4}[A-Z0-9])")
ZERO_AMOUNTS = {"", "0", "0.0", "0.00"}


def archive_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def procedure_pos_frequencies(archives):
    """Count actual Carrier lines by procedure and POS in either public file layout."""
    weights = Counter()
    sources = []
    for path in sorted(archives, key=lambda path: path.name):
        row_count = line_count = 0
        with ZipFile(path) as archive:
            member, = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            with io.TextIOWrapper(archive.open(member), encoding="utf-8-sig", newline="") as stream:
                rows = csv.reader(stream)
                header = next(rows)
                code_column = header.index("CAR_LINE_HCPCS_CD")
                pos_column = header.index("CAR_LINE_PLACE_OF_SRVC_CD")
                # The 2010 CSV collapses identical lines. The 2008 layout has one line per row.
                count_column = header.index("CAR_LINE_CNT") if "CAR_LINE_CNT" in header else None
                if count_column is None and "CAR_LINE_ID" not in header:
                    raise ValueError(f"{path.name}: expected a Carrier line count or line ID column.")
                for row in rows:
                    if not row:
                        continue
                    weight = 1 if count_column is None else int(row[count_column])
                    row_count += 1
                    line_count += weight
                    code = row[code_column].strip()
                    pos = row[pos_column].strip().zfill(2)
                    # BSA uses 00 for invalid and 01 for the retired office code.
                    if code in OFFICE_LAB_CODES and re.fullmatch(r"[0-9]{2}", pos) and pos not in {"00", "01"}:
                        weights[code, pos] += weight
        sources.append({
            "archive": path.name, "sha256": archive_sha256(path), "rows": row_count, "lines": line_count,
        })
    return weights, sources


def build_profile(archives, frequency_archives):
    """Aggregate DE-SynPUF co-occurrence and BSA procedure and POS frequencies."""
    line_weights = Counter()
    pattern_weights = Counter()
    charge_weights = defaultdict(Counter)
    sources = []
    for path in sorted(archives, key=lambda path: path.name):
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

        sources.append({"archive": path.name, "sha256": archive_sha256(path), "rows": row_count})

    frequency_weights, frequency_sources = procedure_pos_frequencies(frequency_archives)
    return {
        "profile": PROFILE_NAME,
        "source": SOURCE_PAGE,
        "archives": sources,
        "frequency_source": FREQUENCY_SOURCE,
        "frequency_archives": frequency_sources,
        "procedure_pos_counts": [
            [code, pos, weight] for (code, pos), weight in sorted(frequency_weights.items())
            if code in charge_weights and weight > 0
        ],
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
            "frequency_source": profile["frequency_source"],
        }
        self.line_counts, self.line_weights = zip(*profile["priced_line_counts"])
        self.pairs, self.pair_weights = zip(*(
            ((code, pos), weight) for code, pos, weight in profile["procedure_pos_counts"]
        ))
        positions = defaultdict(set)
        for code, pos in self.pairs:
            positions[code].add(pos)
        self.patterns = defaultdict(lambda: defaultdict(list))
        for codes, weight in profile["office_lab_patterns"]:
            shared_positions = set.intersection(*(positions[code] for code in codes))
            for pos in shared_positions:
                for code in codes:
                    self.patterns[code, pos][len(codes)].append((codes, weight))
        self.bands = profile["allowed_charge_bands"]
        self.band_cents = profile["charge_band_dollars"] * 100

    def service(self, rng, service_date):
        anchor, pos = rng.choices(self.pairs, self.pair_weights)[0]
        target_size = rng.choices(self.line_counts, self.line_weights)[0]
        patterns_by_size = self.patterns.get((anchor, pos), {})
        size = max((size for size in patterns_by_size if size <= target_size), default=0)
        codes = [anchor]
        if size > 1:
            patterns, weights = zip(*patterns_by_size[size])
            codes.extend(code for code in rng.choices(patterns, weights)[0] if code != anchor)

        lines = []
        for code in codes:
            bands, weights = zip(*self.bands[code])
            start = rng.choices(bands, weights)[0] * self.band_cents
            cents = rng.randrange(max(1, start), start + self.band_cents)
            lines.append({"cpt": code, "charge": Decimal(cents) / 100, "date": service_date})
        return pos, lines


@lru_cache(maxsize=1)
def load_profile():
    raw = files(__package__).joinpath("fixtures").joinpath("carrier-profile.json").read_bytes()
    return CarrierProfile(json.loads(raw), hashlib.sha256(raw).hexdigest())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", type=Path, nargs="+", help="DE-SynPUF Carrier ZIPs")
    parser.add_argument("--frequency-archive", required=True, action="append", type=Path,
                        help="BSA Carrier Line Items ZIP. Repeat to pool input files.")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    profile = build_profile(args.archives, args.frequency_archive)
    if not all(profile[key] for key in ("priced_line_counts", "office_lab_patterns", "procedure_pos_counts")):
        parser.exit(1, "The inputs did not contain enough overlapping office/lab services for a profile.\n")
    args.output.write_bytes((json.dumps(profile, separators=(",", ":")) + "\n").encode("utf-8"))
    print(json.dumps({
        "rows": sum(source["rows"] for source in profile["archives"]),
        "priced_claims": sum(weight for _, weight in profile["priced_line_counts"]),
        "office_lab_patterns": len(profile["office_lab_patterns"]),
        "procedure_pos_pairs": len(profile["procedure_pos_counts"]),
    }))


if __name__ == "__main__":
    main()
