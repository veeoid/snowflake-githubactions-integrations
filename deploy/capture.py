"""Capture Snowflake DEV objects into the DEV repo folder (real names).

    python3 deploy/capture.py --objects MY_PROJECT_DEV_PREP_DB.BASE_MODEL.ORDERS

Writes MY_PROJECT_DEV_PREP_DB/BASE_MODEL/tables/orders.sql with real DEV names.
The pipeline generates the TST and PRD folders from this via convert_env.py.
DEV ONLY.
"""

import argparse
import re
import sys

import executor
import manifest
from deploy import load_config, REPO_ROOT

REPLACE_TABLE = re.compile(r"CREATE\s+OR\s+REPLACE\s+TABLE", re.IGNORECASE)
TYPE_FOLDER = {"TABLE": "tables", "VIEW": "views"}
IDENT = re.compile(r"^[A-Z0-9_]+$")


def object_type_of(conn, object_name):
    db, schema, name = object_name.upper().split(".")
    for part in (db, schema, name):
        if not IDENT.match(part):
            raise SystemExit(f"{object_name}: invalid identifier {part!r}")
    cur = conn.cursor()
    try:
        cur.execute(
            f"SELECT TABLE_TYPE FROM {db}.INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
            (schema, name),
        )
        row = cur.fetchone()
        if not row:
            raise SystemExit(f"{object_name}: not found in {db}")
        return "VIEW" if row[0].upper() == "VIEW" else "TABLE"
    finally:
        cur.close()


def fix_table_verb(ddl):
    return REPLACE_TABLE.sub("CREATE OR ALTER TABLE", ddl)


def get_ddl(conn, object_type, object_name):
    cur = conn.cursor()
    try:
        cur.execute("SELECT GET_DDL(%s, %s, TRUE)", (object_type, object_name))
        return cur.fetchone()[0]
    finally:
        cur.close()


def capture(conn, cfg, object_name):
    obj_type = object_type_of(conn, object_name)
    ddl = get_ddl(conn, obj_type, object_name)
    if obj_type == "TABLE":
        ddl = fix_table_verb(ddl)
    path = manifest.derive_path_from_name(
        object_name, cfg["env"], TYPE_FOLDER[obj_type]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    header = f"-- {path.relative_to(REPO_ROOT)}\n"
    path.write_text(header + ddl.strip() + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(
        description="Capture DEV objects into the DEV repo folder"
    )
    parser.add_argument("--objects", nargs="+", required=True)
    args = parser.parse_args()

    cfg = load_config("dev")
    conn = executor.connect(cfg)
    try:
        for object_name in args.objects:
            if object_name.count(".") != 2:
                sys.exit(f"{object_name}: need DB.SCHEMA.OBJECT")
            path = capture(conn, cfg, object_name)
            print(f"  captured {object_name}")
            print(f"        -> {path.relative_to(REPO_ROOT)}")
    finally:
        conn.close()

    print(
        "\nNext: python3 deploy/deploy.py --env dev --validate-only, then commit + PR"
    )


if __name__ == "__main__":
    main()
