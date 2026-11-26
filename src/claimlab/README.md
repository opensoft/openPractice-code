# ClaimLab

ClaimLab builds, compares, and validates synthetic 837P files. The sandbox
adds a browser uploader and an HTTP API for the same checks.

## Run the sandbox

From the repository root with Python 3.10 or newer:

```
python -m src.claimlab.sandbox
```

Open http://127.0.0.1:8765/. Choose an 837P file or try one of the three
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
| `file_ack.status` | `accepted` or `rejected`. |
| `claim_ack.status` | `accepted`, `rejected`, `mixed`, or `not_generated`. |

`file_ack.issues` and `claim_ack.claims` describe why a file or claim
failed. Check with `accepted.837`, `file-rejected.837`, and
`claim-rejected.837` under `/api/v1/examples/`.

The prototype supports one ISA/IEA, GS/GE, and ST/SE for an 837P
005010X222A1 batch using the * : ~ ^ delimiters and the existing
primary Medicare Part B validation rules. The synthetic subscriber is
`SYNTH00001`, TAYLOR DEMO, born 1960-01-01. Claim checks use that
local record and require a service line.

The responses are JSON simulations of a file-level 999 and claim-level
277CA. Issue codes describe ClaimLab checks. Novitas portal acceptance
requires a separate test with Novitas and the submitter's supported
test data.

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
