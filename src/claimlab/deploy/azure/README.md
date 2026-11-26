# Deploy ClaimLab on Azure Static Web Apps

The public ClaimLab site uses the static uploader and two HTTP-triggered
managed Azure Functions on Python 3.11. The Functions call the same simulator
and example generator as the local ClaimLab server.

## Build the upload bundle

From the repository root, stage the site into a new directory outside
the checkout. Install the API dependencies for the Linux runtime because
the Static Web Apps CLI uploads a prebuilt API.

```sh
python src/claimlab/deploy/azure/stage.py ../claimlab-swa
python -m pip install --only-binary=:all: --platform manylinux2014_x86_64 \
  --python-version 3.11 --no-compile \
  --target ../claimlab-swa/api/.python_packages/lib/site-packages \
  -r ../claimlab-swa/api/requirements.txt
```

The staging command copies an explicit allowlist of application files.
Store the CMS archives, generated claims, and expected-results manifest
outside the deployment bundle.

## Publish

The [ARM template](arm-template.json) describes the deployed Free-tier
Static Web App and its resource group. For releases to the existing site,
use its deployment token through the CLI's
`SWA_CLI_DEPLOYMENT_TOKEN` environment variable. From the staged bundle,
run the following command.

```sh
swa deploy ./app --app-location . --api-location ./api \
  --api-language python --api-version 3.11 --env production
```

## Verify

With Playwright installed, test all six examples and submit a synthetic
file through the browser. Use `--browser-channel msedge` for an installed
Microsoft Edge browser or install Playwright's Chromium browser.

```sh
python src/claimlab/deploy/azure/smoke.py https://claimlab.opensoft.dev
```
