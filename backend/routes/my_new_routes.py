from flask import Blueprint, jsonify, request
from .. import auth
from ._util import body, fail

bp = Blueprint("my_new", __name__, url_prefix="/api")

@bp.get("/my-feature")
@auth.require("viewer")          # optional – requires login
def my_feature():
    return jsonify({"status": "working", "data": [1, 2, 3]})

@bp.post("/my-feature")
@auth.require("admin")
def create_feature():
    data = body()
    # your logic here
    return jsonify({"created": data}), 201
