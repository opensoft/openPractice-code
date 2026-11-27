FROM frappe/erpnext:v15.127.0@sha256:5892977201bf5c40146f39f5ecaa84c43a626d1652153c2187ef3b645f859f67

WORKDIR /home/frappe/frappe-bench
COPY --chown=1000:1000 . apps/open_practice/
RUN env/bin/pip install --no-deps -e apps/open_practice
