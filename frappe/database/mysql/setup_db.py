"""
MySQL 8 database bootstrap and setup for Frappe.

Reuses MariaDB's framework SQL (compatible with MySQL 8 with minor fixes)
and sets up the user/database with MySQL-compatible commands.
"""

import sys

import click

import frappe
from frappe.database.db_manager import DbManager
from frappe.database.mariadb.setup_db import (
	drop_user_and_database,
	get_root_connection,
	import_db_from_sql,
)

# Re-export so callers can import from this module
__all__ = ["bootstrap_database", "drop_user_and_database", "setup_database"]


def get_mysql_variables():
	return frappe._dict(frappe.db.sql("SHOW VARIABLES"))


def get_mysql_version(version_string: str = "") -> tuple[str, str]:
	version_string = version_string or get_mysql_variables().get("version", "")
	# Strip suffixes like '-community', '-log', etc.
	version = version_string.split("-", 1)[0]
	parts = version.rsplit(".", 1)
	return (parts[0], parts[1]) if len(parts) == 2 else (version, "")


def setup_database(force, verbose, mariadb_user_host_login_scope=None):
	frappe.local.session = frappe._dict({"user": "Administrator"})

	db_user = frappe.conf.db_user
	db_name = frappe.local.conf.db_name
	root_conn = get_root_connection()
	dbman = DbManager(root_conn)
	dbman_kwargs = {}

	if mariadb_user_host_login_scope is not None:
		dbman_kwargs["host"] = mariadb_user_host_login_scope

	dbman.create_user(db_user, frappe.conf.db_password, **dbman_kwargs)
	if verbose:
		print(f"Created or updated user {db_user}")

	if force or (db_name not in dbman.get_database_list()):
		dbman.drop_database(db_name)
	else:
		print(f"Database {db_name} already exists, please drop it manually or pass `--force`.")
		sys.exit(1)

	dbman.create_database(db_name)
	if verbose:
		print(f"Created database {db_name}")

	dbman.grant_all_privileges(db_name, db_user, **dbman_kwargs)
	dbman.flush_privileges()
	if verbose:
		print(f"Granted privileges to user {db_user} and database {db_name}")

	root_conn.close()


def bootstrap_database(verbose, source_sql=None):
	frappe.connect()
	check_compatible_versions()
	import_db_from_sql(source_sql, verbose)

	frappe.connect()
	if "tabDefaultValue" not in frappe.db.get_tables(cached=False):
		from click import secho

		secho(
			"Table 'tabDefaultValue' missing in the restored site. "
			"This happens when the backup fails to restore. Please check that the file is valid.",
			fg="red",
		)
		sys.exit(1)


def check_compatible_versions():
	try:
		version = get_mysql_version()
		version_tuple = tuple(int(v) for v in version[0].split("."))

		if version_tuple < (8, 0):
			click.secho(
				f"Warning: MySQL version {version} is older than 8.0 which is not supported by Frappe",
				fg="yellow",
			)
		elif version_tuple > (9, 9):
			click.secho(
				f"Warning: MySQL version {version} is newer than 9.x which is not yet tested with Frappe Framework.",
				fg="yellow",
			)
	except Exception:
		click.secho(
			"MySQL version compatibility checks failed, make sure you're running MySQL 8.0+.",
			fg="yellow",
		)
