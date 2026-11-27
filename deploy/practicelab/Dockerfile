FROM frappe/erpnext:v15.127.0

WORKDIR /home/frappe/frappe-bench
COPY --chown=frappe:frappe . apps/open_practice/
RUN env/bin/pip install --no-deps -e apps/open_practice
