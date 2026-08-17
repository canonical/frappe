import frappe


def execute():
	if frappe.db.db_type in ("mariadb", "mysql"):
		frappe.db.sql_ddl("alter table `tabSingles` modify column `value` longtext")
