import json

import azure.functions as func

from claimlab.sandbox import issue


def response(body, status=200, *, filename=None):
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    mimetype = "application/json"
    if filename:
        mimetype = "application/edi-x12"
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    else:
        body = json.dumps(body)
    return func.HttpResponse(body, status_code=status, headers=headers, mimetype=mimetype)


def error(status, code, message):
    return response({"error": issue(code, message)}, status)
