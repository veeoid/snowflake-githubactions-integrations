"""Deploy Snowflake objects (per-env folders) including the LMA framework.

Object types and how each deploys:
  tables   -> execute DDL (CREATE OR ALTER). hash-skip if unchanged.
  views    -> execute DDL (CREATE OR REPLACE). hash-skip if unchanged.
  inserts  -> DATA STEP. Before running: back up the target table (if it
              exists) into DEV_PREP_DB.BACKUPS. Then run the insert. Skipped
              on no-op redeploy (hash unchanged) so we don't reload data or
              pile up identical backups.
  tasks    -> CREATE TASK IF NOT EXISTS: only creates when absent. hash-skip.
  catalog  -> refresh the QUERY_CATALOG_TBL row: DELETE the query_id then
              INSERT the new one, every deploy that changed (hash-skip).

An "inserts" file names its target table on the first line as a directive:
    -- TARGET: MY_PROJECT_DEV_PREP_DB.BASE_MODEL.CUSTOMER
so the engine knows what to back up. The converter rewrites the env in that
line for free (it's a DB name).
"""

import argparse
import pathlib
import re
import sys

import yaml

import executor
import manifest

REPO_ROOT = pathlib.Path(__file__).parent.parent
TARGET_RE = re.compile(r"^--\s*TARGET:\s*([A-Z0-9_.]+)", re.IGNORECASE | re.MULTILINE)


def load_config(env):
    cfg = yaml.safe_load((REPO_ROOT / "config" / "base.yml").read_text()) or {}
    cfg.update(yaml.safe_load((REPO_ROOT / "config" / f"{env}.yml").read_text()) or {})
    return cfg


def object_key(path, sql):
    """Stable identity for history/hash. For tables/views use the object name;
    for procedural files use the repo-relative path (they have no single name)."""
    otype = manifest.object_type(path)
    if otype in {"tables", "views"}:
        return manifest.extract_object_name(sql)
    return str(path.relative_to(REPO_ROOT))


def deploy_one(conn, cfg, path, sql):
    """Run the right action for this file's object type. Returns an action word."""
    otype = manifest.object_type(path)
    env = cfg["env"]

    if otype in {"tables", "views"}:
        executor.execute(conn, sql)
        return "DEPLOYED"

    if otype == "inserts":
        m = TARGET_RE.search(sql)
        if not m:
            raise SystemExit(
                f"{path.relative_to(REPO_ROOT)}: inserts file needs a "
                f"'-- TARGET: DB.SCHEMA.TABLE' directive on its own line."
            )
        target = m.group(1).upper()
        backup = executor.backup_table(conn, target, env)
        if backup:
            print(f"backed up -> {backup} ... ", end="", flush=True)
        else:
            print("no backup (target absent) ... ", end="", flush=True)
        # strip the directive comment before running
        body = TARGET_RE.sub("", sql, count=1)
        executor.execute(conn, body)
        return "DEPLOYED"

    if otype == "tasks":
        # File is written as CREATE TASK IF NOT EXISTS, so re-running is safe.
        executor.execute(conn, sql)
        return "DEPLOYED"

    if otype == "catalog":
        # File is a self-contained DELETE + INSERT keyed on query_id.
        # env-as-DATA (not a DB name) is filled here, never hardcoded in the file:
        #   __ENV__  -> this deploy's env (DEV/TST/PRD)
        #   __USER__ -> created-by, from LMA_UPDATED_BY env var (falls back to CURRENT_USER)
        import os

        user = os.environ.get("LMA_UPDATED_BY", "")
        body = sql.replace("__ENV__", env.upper())
        if user:
            body = body.replace("__USER__", user)
        else:
            # no env var: let Snowflake stamp the session user
            body = body.replace("'__USER__'", "CURRENT_USER()")
        # catalog files are DELETE + INSERT (two statements) -> run each
        executor.execute_multi(conn, body)
        return "DEPLOYED"

    raise SystemExit(f"{path.relative_to(REPO_ROOT)}: unknown object type {otype!r}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--env", required=True, choices=["dev", "tst", "prd"])
    p.add_argument("--target", help="path substring to scope the deploy")
    p.add_argument("--force", action="store_true")
    p.add_argument("--validate-only", action="store_true")
    args = p.parse_args()

    cfg = load_config(args.env)
    files = manifest.build_manifest(cfg, target=args.target)
    if not files:
        sys.exit(f"No SQL files for env {cfg['env']}.")

    # Phase 1: validate
    for path in files:
        raw = path.read_text()
        manifest.validate_no_replace_table(path, raw)
        manifest.validate_file(path, raw, cfg["env"])

    if args.validate_only:
        print(f"Validated {len(files)} objects for {cfg['env']}. No deploy.")
        return

    print(f"Validated {len(files)} objects. Deploying to {cfg['env']}\n")

    conn = executor.connect(cfg)
    sha = executor.git_sha()
    deployed = skipped = 0
    try:
        last = executor.get_last_hashes(conn, cfg)
        for path in files:
            rel = str(path.relative_to(REPO_ROOT))
            sql = path.read_text()
            otype = manifest.object_type(path)
            key = object_key(path, sql)
            h = executor.file_hash(sql)

            if not args.force and last.get(key) == h:
                executor.record_history(conn, cfg, key, otype, rel, h, sha, "SKIPPED")
                print(f"  skipping  {rel} (unchanged)")
                skipped += 1
                continue

            print(f"  deploying {rel} ... ", end="", flush=True)
            try:
                action = deploy_one(conn, cfg, path, sql)
            except Exception:
                executor.record_history(conn, cfg, key, otype, rel, h, sha, "FAILED")
                print("FAILED")
                raise
            executor.record_history(conn, cfg, key, otype, rel, h, sha, action)
            print("ok")
            deployed += 1
    finally:
        conn.close()

    print(f"\nDone. {deployed} deployed, {skipped} skipped.")


if __name__ == "__main__":
    main()
