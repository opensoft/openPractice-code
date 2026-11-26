# ClaimLab

ClaimLab builds, compares, and validates synthetic 837P files. The sandbox
adds a browser uploader and an HTTP API for the same checks.

## Run the sandbox

From the repository root with Python 3.10 or newer:

```
python -m src.claimlab.sandbox
```

Open http://127.0.0.1:8765/. Choose an 837P file or try one of the six
downloadable examples. Pass `--port` to change the port.

Use synthetic data and set `ISA15=T`. ClaimLab processes the upload in
memory. The service binds to loopback and does not save or forward files.

## HTTP API

```sh
curl -o accepted.837 http://127.0.0.1:8765/api/v1/examples/accepted.837
curl --data-binary @accepted.837 -H "Content-Type: application/edi-x12" http://127.0.0.1:8765/api/v1/validate
```

The API accepts the original ASCII bytes (up to 1 MiB). It returns HTTP
200 for completed simulation runs, including rejected files or claims.
Other status codes describe request errors such as an empty upload
(400), an oversized file (413), or an unsupported content type (415).

| Field | Values |
| --- | --- |
| `simulated` | Always `true`. |
| `accepted` | `true` when the file and every claim pass. |
| `interchange_ack.status` | `rejected` for a TA1 failure or `not_generated`. |
| `file_ack.status` | `accepted`, `rejected`, or `not_generated` after a TA1 failure. |
| `claim_ack.status` | `accepted`, `rejected`, `mixed`, or `not_generated`. |

`interchange_ack.issues`, `file_ack.issues`, and `claim_ack.claims`
describe the outcome at each stage. Download these files from
`/api/v1/examples/`:

| File | Expected simulation |
| --- | --- |
| `accepted.837` | Accepted 999 and one accepted claim. |
| `accepted-25.837` | Accepted 999 and 25 accepted claims with distinct synthetic subscribers and claim IDs. |
| `file-rejected.837` | TA1 rejection, code `001`. ISA13 and IEA02 differ. No 999 or 277CA is generated. |
| `missing-clm02.837` | 999 rejection for a missing claim total. No 277CA is generated. |
| `invalid-total-charge.837` | Accepted 999 and rejected 277CA. The claim total does not match its service-line charges. |
| `claim-rejected.837` | Accepted 999 and rejected 277CA for an unknown synthetic subscriber. |

The prototype supports one ISA/IEA, GS/GE, and ST/SE for an 837P
005010X222A1 batch using the * : ~ ^ delimiters and selected primary
Medicare Part B validation rules. The sandbox knows 260 synthetic
subscribers, SYNTH00001 through SYNTH00260. The first 25 are TAYLOR DEMO
with birth dates January 1 through January 25, 1960 respectively. The
25-claim example includes the original set. The seeded test corpus
uses all 260 subscribers.

The API returns JSON simulations of a TA1 interchange acknowledgment,
999 file acknowledgment, and 277CA claim acknowledgment. Issue codes
describe ClaimLab checks. A separate test with Novitas and its supported
test data verifies payer acceptance.

Reference examples:
[CMS professional claim edits](https://www.cms.gov/Regulations-and-Guidance/Guidance/Transmittals/Downloads/R1440OTN.pdf),
[Wisconsin ForwardHealth 999 guide](https://www.dhs.wisconsin.gov/publications/p0/p00268.pdf),
and [Novitas 277CA training](https://www.novitas-solutions.com/webcenter/content/conn/UCM_Repository/uuid/dDocName:00004758).

## Seeded test corpus

Generate both suites into a new folder outside the checkout. The CLI writes
the X12 files and `manifest.json` together.

```sh
python -m src.claimlab.corpus generate ../claimlab-test-data --seed 837
python -m src.claimlab.corpus verify ../claimlab-test-data
python -m src.claimlab.corpus verify ../claimlab-test-data --api http://127.0.0.1:8765
```

The last command uses a running ClaimLab sandbox. Use `--suite regression`
or `--suite bulk` on `generate` to build a single suite. The seed controls
the bulk claim order, dates, codes, charges, and placement of failures.
Given the same seed and Carrier profile, the bulk fixtures are reproducible.
The regression fixtures have fixed contents.

- **Bulk.** 35,000 claims in 350 files of 100 claims. The varied cohort has
  250 subscribers with 100 claims each. The repeated cohort has 10
  subscribers with 1,000 claims each. Exactly 30% of each subscriber's
  claims are rejected at the 277CA stage. The four equally represented
  failures are an unknown subscriber, a wrong date of birth, missing
  service lines, and an unbalanced total charge.
- **Regression.** 11 files cover a passing control and 10 failure
  scenarios across TA1, 999, and 277CA. Every 277CA failure sits beside a
  passing claim. The manifest counts claims stopped by TA1 or 999 as
  blocked.

The manifest gives each file's SHA-256 digest, interchange control number,
and expected acknowledgment statuses. Each claim has an ID, position,
synthetic subscriber ID before any mutation, named scenario, and expected
outcome and issue codes. Verification checks the fixture bytes and
compares the sandbox results to those expectations. The generator uses
original fictional identities shared with the sandbox and works offline.
For bulk suites the manifest also records the name, source, and SHA-256
digest of the packaged Carrier profile.

### CMS Carrier service-line patterns

Bulk claims draw line counts from records with one to 13 positive allowed
amounts paired with HCPCS codes in both Carrier archives of CMS DE-SynPUF
Sample 1. The profile keeps common combinations of office and lab codes and
uses shorter observed combinations plus other observed office and lab codes
when longer combinations are scarce. Per-code $5 bands of historical allowed
amounts shape the generated test charges. These charges are synthetic.
The exporter assigns the office place of service, diagnosis, dates, and
identifiers independently of the CMS records. The profile contains aggregate
counts and source archive digests. See the [CMS Carrier sample downloads](https://www.cms.gov/data-research/statistics-trends-and-reports/medicare-claims-synthetic-public-use-files/cms-2008-2010-data-entrepreneurs-synthetic-public-use-file-de-synpuf/de10-sample-1).

Download the two Carrier ZIPs outside the checkout. Rebuild the profile with
the following command.

```sh
python -m src.claimlab.carrier_profile /path/to/DE1_0_2008_to_2010_Carrier_Claims_Sample_1A.zip /path/to/DE1_0_2008_to_2010_Carrier_Claims_Sample_1B.zip --output src/claimlab/fixtures/carrier-profile.json
```

Run the small suite with

```sh
python -m unittest src.claimlab.test_corpus src.claimlab.test_sandbox
```

## CLI

```sh
python -m src.claimlab.x837p build src/claimlab/fixtures/sample_claim.json out.txt
python -m src.claimlab.x837p validate out.txt
python -m src.claimlab.x837p validate --novitas colorado-test.837
python -m src.claimlab.x837p diff baseline.txt changed.txt
python -m unittest src.claimlab.test_sandbox
```

Use public companion guides and PC-ACE comparisons for additional rules.
Keep patient data and payer credentials out of the repository.
