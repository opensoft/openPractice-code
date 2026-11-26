from claimlab.sandbox import EXAMPLES, example
from claimlab_http import error, response


def main(req):
    name = req.route_params.get("name")
    if name not in EXAMPLES:
        return error(404, "NOT_FOUND", "This example does not exist.")
    return response(example(name), filename=name)
