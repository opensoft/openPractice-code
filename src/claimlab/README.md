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
Medicare Part B validation rules. The 25 synthetic subscribers are
TAYLOR DEMO with member IDs `SYNTH00001` through `SYNTH00025` and
birth dates January 1 through January 25, 1960 respectively. The batch
example includes all 25 records so developers can reuse their details.

The API returns JSON simulations of a TA1 interchange acknowledgment,
999 file acknowledgment, and 277CA claim acknowledgment. Issue codes
describe ClaimLab checks. A separate test with Novitas and its supported
test data verifies payer acceptance.

Reference examples:
[CMS professional claim edits](https://www.cms.gov/Regulations-and-Guidance/Guidance/Transmittals/Downloads/R1440OTN.pdf),
[Wisconsin ForwardHealth 999 guide](https://www.dhs.wisconsin.gov/publications/p0/p00268.pdf),
and [Novitas 277CA training](https://www.novitas-solutions.com/webcenter/content/conn/UCM_Repository/uuid/dDocName:00004758).

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
