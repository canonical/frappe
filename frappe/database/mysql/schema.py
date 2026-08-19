"""
MySQL 8 schema management.

Overrides MariaDB-specific DDL that MySQL 8 rejects:
  - ADD UNIQUE INDEX IF NOT EXISTS  → pre-check then plain ADD UNIQUE INDEX
  - TEXT/BLOB/JSON DEFAULT ''        → omit DEFAULT clause (MySQL error 1101)
  - UUID column type                → varchar(36) (MySQL has no native UUID type)
"""

import re

import frappe
from frappe.database.mariadb.schema import MariaDBTable
from frappe.database.schema import DbColumn
from frappe.utils.defaults import get_not_null_defaults


class MySQLTable(MariaDBTable):
	"""MariaDB table schema handler adapted for MySQL 8."""

	def get_columns_from_docfields(self):
		"""Override to use MySQLDbColumn instead of DbColumn for all column objects."""
		fields = self.meta.get_fieldnames_with_value(with_field_meta=True)

		if not self.meta.get("istable"):
			for fieldname in frappe.db.OPTIONAL_COLUMNS:
				fields.append({"fieldname": fieldname, "fieldtype": "Text"})
			if self.meta.get("track_seen"):
				fields.append({"fieldname": "_seen", "fieldtype": "Text"})

		for field in fields:
			if field.get("is_virtual"):
				continue
			self.columns[field.get("fieldname")] = MySQLDbColumn(
				table=self,
				fieldname=field.get("fieldname"),
				fieldtype=field.get("fieldtype"),
				length=field.get("length"),
				default=field.get("default"),
				set_index=field.get("search_index"),
				options=field.get("options"),
				unique=field.get("unique"),
				precision=field.get("precision"),
				not_nullable=field.get("not_nullable"),
			)

	# ── CREATE TABLE ───────────────────────────────────────────────────

	def create(self):
		"""Override to replace MariaDB UUID column type with varchar(36)."""
		# Monkeypatch sql_ddl temporarily to intercept and fix the query
		_original_sql_ddl = frappe.db.sql_ddl

		def _patched_sql_ddl(query, *args, **kwargs):
			# Replace 'name uuid primary key' with 'name varchar(36) primary key'
			query = re.sub(
				r"\bname\s+uuid\s+primary\s+key\b",
				"name varchar(36) primary key",
				query,
				flags=re.IGNORECASE,
			)
			return _original_sql_ddl(query, *args, **kwargs)

		frappe.db.sql_ddl = _patched_sql_ddl
		try:
			super().create()
		finally:
			frappe.db.sql_ddl = _original_sql_ddl

	# ── ALTER TABLE ────────────────────────────────────────────────────

	def alter(self):
		"""
		Override alter() to fix MySQL incompatibilities:
		  1. ADD UNIQUE INDEX IF NOT EXISTS → pre-check + plain ADD UNIQUE INDEX
		  2. UUID column type → varchar(36)
		"""
		for col in self.columns.values():
			col.build_for_alter_table(self.current_columns.get(col.fieldname.lower()))

		add_column_query = [f"ADD COLUMN `{col.fieldname}` {col.get_definition()}" for col in self.add_column]
		columns_to_modify = set(self.change_type + self.set_default + self.change_nullability)
		modify_column_query = [
			f"MODIFY `{col.fieldname}` {col.get_definition(for_modification=True)}"
			for col in columns_to_modify
		]
		if alter_pk := self.alter_primary_key():
			modify_column_query.append(alter_pk)

		# MySQL 8: ADD UNIQUE INDEX IF NOT EXISTS is not supported.
		# Pre-check and skip if the index already exists.
		for col in self.add_unique:
			if not frappe.db.get_column_index(self.table_name, col.fieldname, unique=True):
				modify_column_query.append(f"ADD UNIQUE INDEX `{col.fieldname}` (`{col.fieldname}`)")

		add_index_query = [
			f"ADD INDEX `{col.fieldname}_index`(`{col.fieldname}`)"
			for col in self.add_index
			if not frappe.db.get_column_index(self.table_name, col.fieldname, unique=False)
		]

		if self.meta.sort_field == "modified" and not frappe.db.get_column_index(
			self.table_name, "modified", unique=False
		):
			add_index_query.append("ADD INDEX `modified`(`modified`)")

		# Drop unique constraints for columns removed from doctype
		meta_columns = set(self.columns.keys())
		db_columns = set(self.current_columns.keys())

		for col in db_columns:
			if (
				col not in meta_columns
				and col not in frappe.db.DEFAULT_COLUMNS
				and col not in frappe.db.OPTIONAL_COLUMNS
			):
				has_unique = frappe.db.get_column_index(self.table_name, col, unique=True)
				if not has_unique:
					continue
				current_col = self.current_columns.get(col)
				deleted_col = DbColumn(
					table=self,
					fieldname=current_col.name,
					fieldtype=current_col.type,
					length=None,
					default=None,
					set_index=current_col.index,
					options=None,
					unique=False,
					precision=None,
					not_nullable=current_col.not_nullable,
				)
				self.drop_unique.append(deleted_col)

		drop_index_query = []
		for col in {*self.drop_index, *self.drop_unique}:
			if col.fieldname == "name":
				continue
			current_column = self.current_columns.get(col.fieldname.lower())
			unique_constraint_changed = current_column.unique != col.unique
			if unique_constraint_changed and not col.unique:
				if unique_index := frappe.db.get_column_index(self.table_name, col.fieldname, unique=True):
					drop_index_query.append(f"DROP INDEX `{unique_index.Key_name}`")
			index_constraint_changed = current_column.index != col.set_index
			if index_constraint_changed and not col.set_index:
				if index_record := frappe.db.get_column_index(self.table_name, col.fieldname, unique=False):
					drop_index_query.append(f"DROP INDEX `{index_record.Key_name}`")

		for col in self.change_nullability:
			if col.not_nullable:
				try:
					table = frappe.qb.DocType(self.doctype)
					frappe.qb.update(table).set(
						col.fieldname, col.default or get_not_null_defaults(col.fieldtype)
					).where(table[col.fieldname].isnull()).run()
				except Exception:
					print(f"Failed to update data in {self.table_name} for {col.fieldname}")
					raise

		try:
			for query_parts in [add_column_query, modify_column_query, add_index_query, drop_index_query]:
				if query_parts:
					query_body = ", ".join(query_parts)
					query = f"ALTER TABLE `{self.table_name}` {query_body}"
					# nosemgrep
					frappe.db.sql_ddl(query)

		except Exception as e:
			if query := locals().get("query"):
				print(f"Failed to alter schema using query: {query}")
			if frappe.db.is_duplicate_entry(e):
				from frappe import _

				fieldname = str(e).split("'")[-2]
				frappe.throw(
					_(
						"{0} field cannot be set as unique in {1}, as there are non-unique existing values"
					).format(fieldname, self.table_name)
				)
			raise

	def alter_primary_key(self) -> str | None:
		"""
		MySQL 8 has no native UUID column type.
		Map 'uuid' autoname to varchar(36) instead.
		"""
		autoname = self.meta.autoname
		current_type = frappe.db.get_column_type(self.doctype, "name")

		# Migrating to UUID autoname: target varchar(36) instead of 'uuid'
		if autoname == "UUID" and current_type not in ("uuid", "varchar(36)"):
			if not frappe.db.get_value(self.doctype, {}, order_by=None):
				return "MODIFY name varchar(36)"
			else:
				from frappe import _

				frappe.throw(
					_("Primary key of doctype {0} can not be changed as there are existing values.").format(
						self.doctype
					)
				)

		# Reverting from UUID to normal varchar
		if autoname != "UUID" and current_type in ("uuid", "varchar(36)"):
			return f"MODIFY name varchar({frappe.db.VARCHAR_LEN})"

		return None


# Field types that map to TEXT/BLOB/JSON in MySQL and cannot have DEFAULT values
_TEXT_LIKE_FIELDTYPES = frozenset(
	(
		"Small Text",
		"Long Text",
		"Code",
		"Text Editor",
		"Markdown Editor",
		"HTML Editor",
		"Text",
		"Password",
		"Attach",
		"Attach Image",
		"Signature",
		"Barcode",
		"Geolocation",
		"JSON",
	)
)


class MySQLDbColumn(DbColumn):
	"""Override column definition to fix MySQL TEXT/BLOB DEFAULT restrictions."""

	def get_definition(self, for_modification=False):
		definition = super().get_definition(for_modification=for_modification)
		# MySQL error 1101: BLOB/TEXT/JSON columns can't have a DEFAULT value.
		# Strip any DEFAULT clause from TEXT-like columns.
		if self.fieldtype in _TEXT_LIKE_FIELDTYPES:
			definition = re.sub(r"\s+DEFAULT\s+(?:'[^']*'|\S+)", "", definition, flags=re.IGNORECASE)
		return definition
