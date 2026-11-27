FROM frappe/erpnext:v15.127.0@sha256:5892977201bf5c40146f39f5ecaa84c43a626d1652153c2187ef3b645f859f67

WORKDIR /home/frappe/frappe-bench
ARG INCLUDE_OPENHR=0
RUN if [ "$INCLUDE_OPENHR" = "1" ]; then \
      git init -q apps/hrms && \
      git -C apps/hrms fetch --depth 1 https://github.com/opensoft/openHR.git 0a54d2c7b8b6043e2705b6f557b6ed65260670dd && \
      git -C apps/hrms checkout -q --detach FETCH_HEAD && \
      env/bin/pip install -e apps/hrms; \
    fi
COPY --chown=1000:1000 . apps/open_practice/
RUN env/bin/pip install --no-deps -e apps/open_practice
