"""Capture an LMA unit from DEV into the repo (interactive).

An LMA unit for one QUERY_ID produces up to four files, all in DEV folders:
    tables/<obj>.sql    the target table DDL         (GET_DDL)
    inserts/<obj>.sql   the transform, with a TARGET directive
    tasks/<tsk>.sql     CREATE TASK IF NOT EXISTS     (from Snowflake or prompted)
    catalog/<obj>.sql   DELETE+INSERT of the QUERY_CATALOG_TBL row (placeholdered)

Usage:
    export LMA_UPDATED_BY="$USER"          # created-by; also used at deploy time
    python3 deploy/capture_lma.py --query-id MY_PROJECT_CUSTOMER

It reads the catalog row for the query_id from DEV's QUERY_CATALOG_TBL to learn
the target table and the transform SQL. INDEX_VALUE is NOT automated: if the
row is new, capture prompts for it. If no task exists for the query_id, capture
prompts for the schedule and creates a CREATE TASK IF NOT EXISTS file.

The created-by NAME is taken from the LMA_UPDATED_BY env var, never typed.
"""

import argparse
import os
import re
import sys

import executor
import manifest
from deploy import load_config, REPO_ROOT

IDENT = re.compile(r"^[A-Z0-9_]+$")


def q(conn, sql, params=None):
    cur = conn.cursor()
    try:
        cur.execute(sql, params or ())
        return cur.fetchall()
    finally:
        cur.close()


def get_catalog_row(conn, cfg, query_id):
    md = f"MY_PROJECT_{cfg['env'].upper()}_MD_DB.CATALOG.QUERY_CATALOG_TBL"
    rows = q(
        conn,
        f"SELECT TGT_DB_SCHEMA_TBL, SRC_DB_SCHEMA_TBL, QUERY_STATEMENT, "
        f"INDEX_VALUE, PROJECT FROM {md} WHERE QUERY_ID = %s "
        f"QUALIFY ROW_NUMBER() OVER (ORDER BY LAST_UPDATED_TS DESC) = 1",
        (query_id,),
    )
    return rows[0] if rows else None


def get_table_ddl(conn, target_fqtn):
    rows = q(conn, "SELECT GET_DDL('TABLE', %s, TRUE)", (target_fqtn,))
    ddl = rows[0][0]
    return re.sub(
        r"CREATE\s+OR\s+REPLACE\s+TABLE",
        "CREATE OR ALTER TABLE",
        ddl,
        flags=re.IGNORECASE,
    )


def find_task_for_query(conn, cfg, query_id):
    """Return the CREATE TASK DDL if a task in this env references the query_id."""
    env = cfg["env"].upper()
    schema = f"MY_PROJECT_{env}_PREP_DB.BASE_MODEL"
    tasks = q(conn, f"SHOW TASKS IN SCHEMA {schema}")
    # SHOW TASKS columns: name is col 1; definition is available via GET_DDL
    for row in tasks:
        name = row[1]
        fq = f"{schema}.{name}"
        ddl_rows = q(conn, "SELECT GET_DDL('TASK', %s, TRUE)", (fq,))
        ddl = ddl_rows[0][0]
        if query_id in ddl:
            # ensure idempotent verb
            return re.sub(
                r"CREATE\s+(OR\s+REPLACE\s+)?TASK",
                "CREATE TASK IF NOT EXISTS",
                ddl,
                flags=re.IGNORECASE,
            ), name
    return None, None


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    header = f"-- {path.relative_to(REPO_ROOT)}\n"
    path.write_text(header + text.strip() + "\n")
    print(f"  wrote {path.relative_to(REPO_ROOT)}")


DEFAULT_SCHEDULE = "using cron 45 4 * * * America/Matamoros"
SCHED_RE = re.compile(
    r"^\s*(using\s+cron\s+\S+.*|\d+\s+(minute|minutes|m))\s*$", re.IGNORECASE
)


def prompt_schedule():
    """Ask for a task schedule; Enter accepts the default. Loops until valid.

    Accepts either 'USING CRON <5 fields> <tz>' or an interval like '5 MINUTE'.
    Pressing Enter with no input uses DEFAULT_SCHEDULE, so a task is never
    written with an empty schedule.
    """
    while True:
        val = input(f"Schedule [Enter for default: {DEFAULT_SCHEDULE}]: ").strip()
        if not val:
            print(f"  using default: {DEFAULT_SCHEDULE}")
            return DEFAULT_SCHEDULE
        if not SCHED_RE.match(val):
            print(
                "  That does not look like a valid schedule. Use "
                "'USING CRON <min> <hr> <dom> <mon> <dow> <timezone>' or "
                "'<n> MINUTE', or press Enter for the default."
            )
            continue
        return val


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query-id", required=True)
    args = ap.parse_args()
    query_id = args.query_id.upper()

    if not os.environ.get("LMA_UPDATED_BY"):
        sys.exit(
            'Set LMA_UPDATED_BY (the created-by name), e.g. export LMA_UPDATED_BY="$USER"'
        )

    cfg = load_config("dev")
    conn = executor.connect(cfg)
    try:
        row = get_catalog_row(conn, cfg, query_id)
        if not row:
            sys.exit(
                f"No catalog row for {query_id} in DEV. Create it in Snowsight first."
            )
        target, source, statement, index_value, project = row
        target = target.upper()
        print(f"Target table : {target}")
        print(f"Source       : {source}")

        # INDEX_VALUE is not automated: confirm/prompt
        if index_value is None:
            index_value = input("INDEX_VALUE (not set) — enter a number: ").strip()
        else:
            entered = input(
                f"INDEX_VALUE [{index_value}] — Enter to keep or type new: "
            ).strip()
            index_value = entered or index_value

        db, schema, obj = target.split(".")
        base = obj.lower()

        # 1. table DDL
        table_dir = manifest.REPO_ROOT / db / schema / "tables"
        write(table_dir / f"{base}.sql", get_table_ddl(conn, target))

        # 2. insert (transform) with TARGET directive
        insert_body = f"-- TARGET: {target}\n{statement.strip()}"
        write(manifest.REPO_ROOT / db / schema / "inserts" / f"{base}.sql", insert_body)

        # 3. task: reuse if present, else prompt to create
        task_ddl, task_name = find_task_for_query(conn, cfg, query_id)
        if not task_ddl:
            print(f"No task found for {query_id}.")
            make = input("Create a task? [y/N]: ").strip().lower()
            if make == "y":
                sched = prompt_schedule()
                task_name = f"REFRESH_{obj}_TSK"
                md = f"MY_PROJECT_{cfg['env'].upper()}_MD_DB.CATALOG"
                log = f"MY_PROJECT_{cfg['env'].upper()}_LOG_DB.APP"
                task_ddl = (
                    f"CREATE TASK IF NOT EXISTS {db}.{schema}.{task_name}\n"
                    f"    WAREHOUSE = {cfg['warehouse']}\n"
                    f"    SCHEDULE = '{sched}'\n"
                    f"AS CALL {md}.QUERY_EXECUTION_GENERIC_SP(\n"
                    f"    '{query_id}', '{project}', '{md}', '{log}'\n);"
                )
        if task_ddl:
            write(
                manifest.REPO_ROOT / db / schema / "tasks" / f"{task_name.lower()}.sql",
                task_ddl,
            )

        # 4. catalog row as DELETE+INSERT with __ENV__ / __USER__ placeholders
        md_db = f"MY_PROJECT_{cfg['env'].upper()}_MD_DB.CATALOG.QUERY_CATALOG_TBL"
        stmt_escaped = statement.replace("'", "''")
        catalog_sql = (
            f"DELETE FROM {md_db} WHERE QUERY_ID = '{query_id}';\n\n"
            f"INSERT INTO {md_db}\n"
            f"(QUERY_ID, PROJECT, SRC_DB_SCHEMA_TBL, TGT_DB_SCHEMA_TBL, QUERY_STATEMENT,\n"
            f" INDEX_VALUE, IS_ACTIVE, DB_ENV, LAST_UPDATED_BY, LAST_UPDATED_TS)\n"
            f"VALUES (\n"
            f" '{query_id}', '{project}',\n"
            f" '{source}',\n"
            f" '{target}',\n"
            f" '{stmt_escaped}',\n"
            f" {index_value}, 'Y', '__ENV__', '__USER__', current_timestamp(3)\n);"
        )
        cat_db = f"MY_PROJECT_{cfg['env'].upper()}_MD_DB"
        write(
            manifest.REPO_ROOT / cat_db / "CATALOG" / "catalog" / f"{base}.sql",
            catalog_sql,
        )

    finally:
        conn.close()

    print(
        "\nNext: python3 deploy/deploy.py --env dev --validate-only, then commit + PR"
    )


if __name__ == "__main__":
    main()
