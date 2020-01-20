# -*- coding: utf-8 -*-
# Copyright (c) 2019, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from __future__ import unicode_literals

from decimal import Decimal
import json
import re
import traceback
import zipfile
import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_field
from frappe.model.document import Document
from frappe.model.naming import getseries, revert_series_if_last
from frappe.utils.data import format_datetime
from bs4 import BeautifulSoup as bs
from erpnext import encode_company_abbr
from erpnext.accounts.doctype.account.chart_of_accounts.chart_of_accounts import create_charts

PRIMARY_ACCOUNT = "Primary"
VOUCHER_CHUNK_SIZE = 500


class TallyMigration(Document):
	def autoname(self):
		if not self.name:
			self.name = "Tally Migration on " + format_datetime(self.creation)

	def get_collection(self, data_file):
		def sanitize(string):
			return re.sub("&#4;", "", string)

		def emptify(string):
			string = re.sub(r"<\w+/>", "", string)
			string = re.sub(r"<([\w.]+)>\s*<\/\1>", "", string)
			string = re.sub(r"\r\n", "", string)
			return string

		master_file = frappe.get_doc("File", {"file_url": data_file})

		with zipfile.ZipFile(master_file.get_full_path()) as zf:
			encoded_content = zf.read(zf.namelist()[0])
			try:
				content = encoded_content.decode("utf-8-sig")
			except UnicodeDecodeError:
				content = encoded_content.decode("utf-16")

		master = bs(sanitize(emptify(content)), "xml")
		collection = master.BODY.IMPORTDATA.REQUESTDATA
		return collection

	def dump_processed_data(self, data):
		for key, value in data.items():
			f = frappe.get_doc({
				"doctype": "File",
				"file_name":  key + ".json",
				"attached_to_doctype": self.doctype,
				"attached_to_name": self.name,
				"content": json.dumps(value)
			}).insert()
			setattr(self, key, f.file_url)

	def _process_master_data(self):
		def get_company_name(collection):
			return collection.find_all("REMOTECMPINFO.LIST")[0].REMOTECMPNAME.string

		def get_coa_customers_suppliers(collection):
			root_type_map = {
				"Application of Funds (Assets)": "Asset",
				"Expenses": "Expense",
				"Income": "Income",
				"Source of Funds (Liabilities)": "Liability"
			}
			roots = set(root_type_map.keys())
			accounts = list(get_groups(collection.find_all("GROUP"))) + list(get_ledgers(collection.find_all("LEDGER")))
			children, parents = get_children_and_parent_dict(accounts)
			group_set =  [acc[1] for acc in accounts if acc[2]]
			children, customers, suppliers = remove_parties(parents, children, group_set)
			coa = traverse({}, children, roots, roots, group_set)

			for account in coa:
				coa[account]["root_type"] = root_type_map[account]

			return coa, customers, suppliers

		def get_groups(accounts):
			for account in accounts:
				if account["NAME"] in (self.tally_creditors_account, self.tally_debtors_account):
					yield get_parent(account), account["NAME"], 0
				else:
					yield get_parent(account), account["NAME"], 1

		def get_ledgers(accounts):
			for account in accounts:
				# If Ledger doesn't have PARENT field then don't create Account
				# For example "Profit & Loss A/c"
				if account.PARENT:
					yield account.PARENT.string, account["NAME"], 0

		def get_parent(account):
			if account.PARENT:
				return account.PARENT.string
			return {
				("Yes", "No"): "Application of Funds (Assets)",
				("Yes", "Yes"): "Expenses",
				("No", "Yes"): "Income",
				("No", "No"): "Source of Funds (Liabilities)",
			}[(account.ISDEEMEDPOSITIVE.string, account.ISREVENUE.string)]

		def get_children_and_parent_dict(accounts):
			children, parents = {}, {}
			for parent, account, is_group in accounts:
				children.setdefault(parent, set()).add(account)
				parents.setdefault(account, set()).add(parent)
				parents[account].update(parents.get(parent, []))
			return children, parents

		def remove_parties(parents, children, group_set):
			customers, suppliers = set(), set()
			for account in parents:
				if self.tally_creditors_account in parents[account]:
					children.pop(account, None)
					if account not in group_set:
						suppliers.add(account)
				elif self.tally_debtors_account in parents[account]:
					children.pop(account, None)
					if account not in group_set:
						customers.add(account)
			return children, customers, suppliers

		def traverse(tree, children, accounts, roots, group_set):
			for account in accounts:
				if account in group_set or account in roots:
					if account in children:
						tree[account] = traverse({}, children, children[account], roots, group_set)
					else:
						tree[account] = {"is_group": 1}
				else:
					tree[account] = {}
			return tree

		def get_parties_addresses(collection, customers, suppliers):
			parties, addresses = [], []
			for account in collection.find_all("LEDGER"):
				party_type = None
				if account.NAME.string in customers:
					party_type = "Customer"
					parties.append({
						"doctype": party_type,
						"customer_name": account.NAME.string,
						"tax_id": account.INCOMETAXNUMBER.string if account.INCOMETAXNUMBER else None,
						"customer_group": "All Customer Groups",
						"territory": "All Territories",
						"customer_type": "Individual",
					})
				elif account.NAME.string in suppliers:
					party_type = "Supplier"
					parties.append({
						"doctype": party_type,
						"supplier_name": account.NAME.string,
						"pan": account.INCOMETAXNUMBER.string if account.INCOMETAXNUMBER else None,
						"supplier_group": "All Supplier Groups",
						"supplier_type": "Individual",
					})
				if party_type:
					address = "\n".join([a.string for a in account.find_all("ADDRESS")])
					addresses.append({
						"doctype": "Address",
						"address_line1": address[:140].strip(),
						"address_line2": address[140:].strip(),
						"country": account.COUNTRYNAME.string if account.COUNTRYNAME else None,
						"state": account.LEDSTATENAME.string if account.LEDSTATENAME else None,
						"gst_state": account.LEDSTATENAME.string if account.LEDSTATENAME else None,
						"pin_code": account.PINCODE.string if account.PINCODE else None,
						"mobile": account.LEDGERPHONE.string if account.LEDGERPHONE else None,
						"phone": account.LEDGERPHONE.string if account.LEDGERPHONE else None,
						"gstin": account.PARTYGSTIN.string if account.PARTYGSTIN else None,
						"links": [{"link_doctype": party_type, "link_name": account["NAME"]}],
					})
			return parties, addresses

		def get_stock_items_uoms(collection):
			uoms = []
			for uom in collection.find_all("UNIT"):
				uoms.append({"doctype": "UOM", "uom_name": uom.NAME.string})

			items = []
			for item in collection.find_all("STOCKITEM"):
				items.append({
					"doctype": "Item",
					"item_code" : item.NAME.string,
					"stock_uom": item.BASEUNITS.string,
					"is_stock_item": 0,
					"item_group": "All Item Groups",
					"item_defaults": [{"company": self.erpnext_company}]
				})
			return items, uoms


		self.publish("Process Master Data", _("Reading Uploaded File"), 1, 5)
		collection = self.get_collection(self.master_data)

		company = get_company_name(collection)
		self.tally_company = company
		self.erpnext_company = company

		self.publish("Process Master Data", _("Processing Chart of Accounts and Parties"), 2, 5)
		chart_of_accounts, customers, suppliers = get_coa_customers_suppliers(collection)
		self.publish("Process Master Data", _("Processing Party Addresses"), 3, 5)
		parties, addresses = get_parties_addresses(collection, customers, suppliers)
		self.publish("Process Master Data", _("Processing Items and UOMs"), 4, 5)
		items, uoms = get_stock_items_uoms(collection)
		data = {"chart_of_accounts": chart_of_accounts, "parties": parties, "addresses": addresses, "items": items, "uoms": uoms}
		self.publish("Process Master Data", _("Done"), 5, 5)

		self.dump_processed_data(data)
		self.is_master_data_processed = 1
		self.status = ""
		self.save()

	def publish(self, title, message, count, total):
		frappe.publish_realtime("tally_migration_progress_update", {"title": title, "message": message, "count": count, "total": total})

	def _import_master_data(self):
		def create_company_and_coa(coa_file_url):
			coa_file = frappe.get_doc("File", {"file_url": coa_file_url})
			frappe.local.flags.ignore_chart_of_accounts = True
			company = frappe.get_doc({
				"doctype": "Company",
				"company_name": self.erpnext_company,
				"default_currency": "INR",
				"enable_perpetual_inventory": 0,
			}).insert()
			frappe.local.flags.ignore_chart_of_accounts = False
			create_charts(company.name, custom_chart=json.loads(coa_file.get_content()))
			company.create_default_warehouses()

		def create_parties_and_addresses(parties_file_url, addresses_file_url):
			parties_file = frappe.get_doc("File", {"file_url": parties_file_url})
			for party in json.loads(parties_file.get_content()):
				try:
					frappe.get_doc(party).insert()
				except:
					self.log(party)
			addresses_file = frappe.get_doc("File", {"file_url": addresses_file_url})
			for address in json.loads(addresses_file.get_content()):
				try:
					frappe.get_doc(address).insert(ignore_mandatory=True)
				except:
					try:
						gstin = address.pop("gstin", None)
						frappe.get_doc(address).insert(ignore_mandatory=True)
						self.log({"address": address, "message": "Invalid GSTIN: {}. Address was created without GSTIN".format(gstin)})
					except:
						self.log(address)


		def create_items_uoms(items_file_url, uoms_file_url):
			uoms_file = frappe.get_doc("File", {"file_url": uoms_file_url})
			for uom in json.loads(uoms_file.get_content()):
				if not frappe.db.exists(uom):
					try:
						frappe.get_doc(uom).insert()
					except:
						self.log(uom)

			items_file = frappe.get_doc("File", {"file_url": items_file_url})
			for item in json.loads(items_file.get_content()):
				try:
					frappe.get_doc(item).insert()
				except:
					self.log(item)

		self.publish("Import Master Data", _("Creating Company and Importing Chart of Accounts"), 1, 4)
		create_company_and_coa(self.chart_of_accounts)
		self.publish("Import Master Data", _("Importing Parties and Addresses"), 2, 4)
		create_parties_and_addresses(self.parties, self.addresses)
		self.publish("Import Master Data", _("Importing Items and UOMs"), 3, 4)
		create_items_uoms(self.items, self.uoms)
		self.publish("Import Master Data", _("Done"), 4, 4)
		self.status = ""
		self.is_master_data_imported = 1
		self.save()

	def _process_day_book_data(self):
		def get_vouchers(collection):
			vouchers = []
			for voucher in collection.find_all("VOUCHER"):
				if voucher.ISCANCELLED.string == "Yes":
					continue
				inventory_entries = voucher.find_all("INVENTORYENTRIES.LIST") + voucher.find_all("ALLINVENTORYENTRIES.LIST") + voucher.find_all("INVENTORYENTRIESIN.LIST") + voucher.find_all("INVENTORYENTRIESOUT.LIST")
				if voucher.VOUCHERTYPENAME.string not in ["Journal", "Receipt", "Payment", "Contra"] and inventory_entries:
					function = voucher_to_invoice
				else:
					function = voucher_to_journal_entry
				try:
					processed_voucher = function(voucher)
					if processed_voucher:
						vouchers.append(processed_voucher)
				except:
					self.log(voucher)
			return vouchers

		def voucher_to_journal_entry(voucher):
			accounts = []
			ledger_entries = voucher.find_all("ALLLEDGERENTRIES.LIST") + voucher.find_all("LEDGERENTRIES.LIST")
			for entry in ledger_entries:
				account = {"account": encode_company_abbr(entry.LEDGERNAME.string, self.erpnext_company), "cost_center": self.default_cost_center}
				if entry.ISPARTYLEDGER.string == "Yes":
					party_details = get_party(entry.LEDGERNAME.string)
					if party_details:
						party_type, party_account = party_details
						account["party_type"] = party_type
						account["account"] = party_account
						account["party"] = entry.LEDGERNAME.string
				amount = Decimal(entry.AMOUNT.string)
				if amount > 0:
					account["credit_in_account_currency"] = str(abs(amount))
				else:
					account["debit_in_account_currency"] = str(abs(amount))
				accounts.append(account)

			journal_entry = {
				"doctype": "Journal Entry",
				"tally_guid": voucher.GUID.string,
				"posting_date": voucher.DATE.string,
				"company": self.erpnext_company,
				"accounts": accounts,
			}
			return journal_entry

		def voucher_to_invoice(voucher):
			if voucher.VOUCHERTYPENAME.string in ["Sales", "Credit Note"]:
				doctype = "Sales Invoice"
				party_field = "customer"
				account_field = "debit_to"
				account_name = encode_company_abbr(self.tally_debtors_account, self.erpnext_company)
				price_list_field = "selling_price_list"
			elif voucher.VOUCHERTYPENAME.string in ["Purchase", "Debit Note"]:
				doctype = "Purchase Invoice"
				party_field = "supplier"
				account_field = "credit_to"
				account_name = encode_company_abbr(self.tally_creditors_account, self.erpnext_company)
				price_list_field = "buying_price_list"
			else:
				# Do not handle vouchers other than "Purchase", "Debit Note", "Sales" and "Credit Note"
				# Do not handle Custom Vouchers either
				return

			invoice = {
				"doctype": doctype,
				party_field: voucher.PARTYNAME.string,
				"tally_guid": voucher.GUID.string,
				"posting_date": voucher.DATE.string,
				"due_date": voucher.DATE.string,
				"items": get_voucher_items(voucher, doctype),
				"taxes": get_voucher_taxes(voucher),
				account_field: account_name,
				price_list_field: "Tally Price List",
				"set_posting_time": 1,
				"disable_rounded_total": 1,
				"company": self.erpnext_company,
			}
			return invoice

		def get_voucher_items(voucher, doctype):
			inventory_entries = voucher.find_all("INVENTORYENTRIES.LIST") + voucher.find_all("ALLINVENTORYENTRIES.LIST") + voucher.find_all("INVENTORYENTRIESIN.LIST") + voucher.find_all("INVENTORYENTRIESOUT.LIST")
			if doctype == "Sales Invoice":
				account_field = "income_account"
			elif doctype == "Purchase Invoice":
				account_field = "expense_account"
			items = []
			for entry in inventory_entries:
				qty, uom = entry.ACTUALQTY.string.strip().split()
				items.append({
					"item_code": entry.STOCKITEMNAME.string,
					"description": entry.STOCKITEMNAME.string,
					"qty": qty.strip(),
					"uom": uom.strip(),
					"conversion_factor": 1,
					"price_list_rate": entry.RATE.string.split("/")[0],
					"cost_center": self.default_cost_center,
					"warehouse": self.default_warehouse,
					account_field: encode_company_abbr(entry.find_all("ACCOUNTINGALLOCATIONS.LIST")[0].LEDGERNAME.string, self.erpnext_company),
				})
			return items

		def get_voucher_taxes(voucher):
			ledger_entries = voucher.find_all("ALLLEDGERENTRIES.LIST") + voucher.find_all("LEDGERENTRIES.LIST")
			taxes = []
			for entry in ledger_entries:
				if entry.ISPARTYLEDGER.string == "No":
					tax_account = encode_company_abbr(entry.LEDGERNAME.string, self.erpnext_company)
					taxes.append({
						"charge_type": "Actual",
						"account_head": tax_account,
						"description": tax_account,
						"tax_amount": entry.AMOUNT.string,
						"cost_center": self.default_cost_center,
					})
			return taxes

		def get_party(party):
			if frappe.db.exists({"doctype": "Supplier", "supplier_name": party}):
				return "Supplier", encode_company_abbr(self.tally_creditors_account, self.erpnext_company)
			elif frappe.db.exists({"doctype": "Customer", "customer_name": party}):
				return "Customer", encode_company_abbr(self.tally_debtors_account, self.erpnext_company)

		self.publish("Process Day Book Data", _("Reading Uploaded File"), 1, 3)
		collection = self.get_collection(self.day_book_data)
		self.publish("Process Day Book Data", _("Processing Vouchers"), 2, 3)
		vouchers = get_vouchers(collection)
		self.publish("Process Day Book Data", _("Done"), 3, 3)
		self.dump_processed_data({"vouchers": vouchers})
		self.status = ""
		self.is_day_book_data_processed = 1
		self.save()

	def _import_day_book_data(self):
		def create_fiscal_years(vouchers):
			from frappe.utils.data import add_years, getdate
			earliest_date = getdate(min(voucher["posting_date"] for voucher in vouchers))
			oldest_year = frappe.get_all("Fiscal Year", fields=["year_start_date", "year_end_date"], order_by="year_start_date")[0]
			while earliest_date < oldest_year.year_start_date:
				new_year = frappe.get_doc({"doctype": "Fiscal Year"})
				new_year.year_start_date = add_years(oldest_year.year_start_date, -1)
				new_year.year_end_date = add_years(oldest_year.year_end_date, -1)
				if new_year.year_start_date.year == new_year.year_end_date.year:
					new_year.year = new_year.year_start_date.year
				else:
					new_year.year = "{}-{}".format(new_year.year_start_date.year, new_year.year_end_date.year)
				new_year.save()
				oldest_year = new_year

		def create_custom_fields(doctypes):
			for doctype in doctypes:
				df = {
					"fieldtype": "Data",
					"fieldname": "tally_guid",
					"read_only": 1,
					"label": "Tally GUID"
				}
				create_custom_field(doctype, df)

		def create_price_list():
			frappe.get_doc({
				"doctype": "Price List",
				"price_list_name": "Tally Price List",
				"selling": 1,
				"buying": 1,
				"enabled": 1,
				"currency": "INR"
			}).insert()

		frappe.db.set_value("Account", encode_company_abbr(self.tally_creditors_account, self.erpnext_company), "account_type", "Payable")
		frappe.db.set_value("Account", encode_company_abbr(self.tally_debtors_account, self.erpnext_company), "account_type", "Receivable")
		frappe.db.set_value("Company", self.erpnext_company, "round_off_account", self.round_off_account)

		vouchers_file = frappe.get_doc("File", {"file_url": self.vouchers})
		vouchers = json.loads(vouchers_file.get_content())

		create_fiscal_years(vouchers)
		create_price_list()
		create_custom_fields(["Journal Entry", "Purchase Invoice", "Sales Invoice"])

		total = len(vouchers)
		is_last = False
		for index in range(0, total, VOUCHER_CHUNK_SIZE):
			if index + VOUCHER_CHUNK_SIZE >= total:
				is_last = True
			frappe.enqueue_doc(self.doctype, self.name, "_import_vouchers", queue="long", timeout=3600, start=index+1, total=total, is_last=is_last)

	def _import_vouchers(self, start, total, is_last=False):
		frappe.flags.in_migrate = True
		vouchers_file = frappe.get_doc("File", {"file_url": self.vouchers})
		vouchers = json.loads(vouchers_file.get_content())
		chunk = vouchers[start: start + VOUCHER_CHUNK_SIZE]

		for index, voucher in enumerate(chunk, start=start):
			try:
				doc = frappe.get_doc(voucher).insert()
				doc.submit()
				self.publish("Importing Vouchers", _("{} of {}").format(index, total), index, total)
			except:
				self.log(voucher)

		if is_last:
			self.status = ""
			self.is_day_book_data_imported = 1
			self.save()
			frappe.db.set_value("Price List", "Tally Price List", "enabled", 0)
		frappe.flags.in_migrate = False

	def process_master_data(self):
		self.status = "Processing Master Data"
		self.save()
		frappe.enqueue_doc(self.doctype, self.name, "_process_master_data", queue="long", timeout=3600)

	def import_master_data(self):
		self.status = "Importing Master Data"
		self.save()
		frappe.enqueue_doc(self.doctype, self.name, "_import_master_data", queue="long", timeout=3600)

	def process_day_book_data(self):
		self.status = "Processing Day Book Data"
		self.save()
		frappe.enqueue_doc(self.doctype, self.name, "_process_day_book_data", queue="long", timeout=3600)

	def import_day_book_data(self):
		self.status = "Importing Day Book Data"
		self.save()
		frappe.enqueue_doc(self.doctype, self.name, "_import_day_book_data", queue="long", timeout=3600)

	def log(self, data=None):
		message = "\n".join(["Data", json.dumps(data, default=str, indent=4), "Exception", traceback.format_exc()])
		return frappe.log_error(title="Tally Migration Error", message=message)
