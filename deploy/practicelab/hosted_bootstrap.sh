#!/usr/bin/env bash
# Run in the dedicated QA PracticeLab backend container.
set -euo pipefail

practice=practicelab.opensoft.dev
hr=openhr.practicelab.internal

: "${PRACTICELAB_COMPANY_KEY:?Missing synthetic connector key}"
: "${PRACTICELAB_COMPANY_SECRET:?Missing synthetic connector secret}"
: "${PRACTICELAB_SMOKE_PASSWORD:?Missing synthetic browser password}"

append_app() {
  if ! grep -Fxq "$1" sites/apps.txt; then
    printf '%s\n' "$1" >> sites/apps.txt
  fi
}

for app in erpnext hrms; do
  append_app "$app"
done

create_site() {
  local site="$1"
  if [ ! -f "sites/$site/site_config.json" ]; then
    bench new-site "$site" --mariadb-user-host-login-scope='%' \
      --db-root-password "$MYSQL_ROOT_PASS" --admin-password "$ADMIN_PASS"
  fi
}

install_app() {
  local apps
  apps=$(bench --site "$1" list-apps)
  if ! grep -q "^$2[[:space:]]" <<<"$apps"; then
    bench --site "$1" install-app "$2"
  fi
}

create_site "$practice"
bench --site "$practice" set-config -p open_practice_practicelab 1
install_app "$practice" open_practice
bench --site "$practice" migrate

create_site "$hr"
install_app "$hr" erpnext
install_app "$hr" hrms
bench --site "$hr" migrate

env/bin/python -m open_practice.practicelab_onboarding_smoke configure-openhr "$hr"
export PRACTICELAB_OPENHR_TEST_SITE_NAME="$hr"
export PRACTICELAB_OPENHR_CONNECTOR_URL=http://127.0.0.1:8000
bench --site "$practice" execute open_practice.practicelab_onboarding_smoke.configure_practicelab
