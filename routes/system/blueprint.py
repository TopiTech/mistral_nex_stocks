"""システム系 Blueprint。`routes.api_system` からの純粋な移動。"""

from flask import Blueprint

# import_name は原文 (`routes.api_system` の __name__) と同一にする。
# endpoint 名 (`api_system.*`) は第1引数で決まるため URL 契約は不変。
api_system_bp = Blueprint("api_system", "routes.api_system")
