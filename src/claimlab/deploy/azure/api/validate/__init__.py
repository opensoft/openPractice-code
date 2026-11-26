from claimlab.sandbox import MAX_BYTES, simulate
from claimlab_http import error, response


def main(req):
    body = req.get_body()
    if not body:
        return error(400, "EMPTY_UPLOAD", "Supply an 837P file.")
    content_type = req.headers.get("Content-Type", "").partition(";")[0].strip().lower()
    if content_type != "application/edi-x12":
        return error(415, "EDI_REQUIRED", "Send raw file bytes with Content-Type: application/edi-x12.")
    if len(body) > MAX_BYTES:
        return error(413, "FILE_TOO_LARGE", "The maximum file size is 1 MiB.")
    return response(simulate(body))
