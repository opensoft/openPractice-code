# claimlab

Throwaway test app for learning the X12 837P claim file format. This is not
openPractice and produces draft files only.

## Usage
    python -m src.claimlab.x837p build src/claimlab/fixtures/sample_claim.json out.txt
    python -m src.claimlab.x837p validate out.txt
    python -m src.claimlab.x837p validate --novitas colorado-test.837
    python -m src.claimlab.x837p diff baseline.txt changed.txt

## Rules
- Synthetic data only. Never commit PHI or payer credentials.
- ISA15 stays T (test) until Novitas test submission passes.
- Field knowledge: free companion guides + PC-ACE diffs, never X12 guide text.
