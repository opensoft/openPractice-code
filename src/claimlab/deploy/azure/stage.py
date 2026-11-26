"""Stage the ClaimLab runtime files for an Azure Static Web Apps upload."""

import argparse
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory


AZURE_SOURCE = Path(__file__).resolve().parent
CLAIMLAB_SOURCE = AZURE_SOURCE.parents[1]
STATIC_FILES = ("index.html", "styles.css", "app.js")
RUNTIME_MODULES = ("sandbox.py", "synthetic.py", "x837p.py")
FUNCTION_FILES = (
    "claimlab_http.py",
    "host.json",
    "requirements.txt",
    "examples/__init__.py",
    "examples/function.json",
    "validate/__init__.py",
    "validate/function.json",
)


def stage(destination):
    """Copy the runtime allowlist into a fresh app and API bundle."""
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError(f"Choose a new deployment directory: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="claimlab-swa-", dir=destination.parent) as temporary:
        bundle = Path(temporary) / "bundle"
        for source, target, names in (
            (CLAIMLAB_SOURCE / "sandbox_ui", bundle / "app", STATIC_FILES),
            (AZURE_SOURCE, bundle / "app", ("staticwebapp.config.json",)),
            (AZURE_SOURCE / "api", bundle / "api", FUNCTION_FILES),
            (CLAIMLAB_SOURCE, bundle / "api" / "claimlab", RUNTIME_MODULES),
        ):
            for name in names:
                path = target / name
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / name, path)
        bundle.rename(destination)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="A new directory for the deployment bundle.")
    args = parser.parse_args()
    try:
        destination = stage(args.destination)
    except OSError as error:
        parser.exit(1, f"{error}\n")
    print(destination)


if __name__ == "__main__":
    main()
