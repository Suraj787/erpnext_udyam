# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

# ERPNext - web based ERP (http://erpnext.com)
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe, unittest
from frappe.utils import flt, nowdate, nowtime
from erpnext.accounts.utils import get_stock_and_account_difference
from erpnext.stock.doctype.purchase_receipt.test_purchase_receipt import set_perpetual_inventory
from erpnext.stock.stock_ledger import get_previous_sle, update_entries_after
from erpnext.stock.doctype.stock_reconciliation.stock_reconciliation import EmptyStockReconciliationItemsError, get_items
from erpnext.stock.doctype.warehouse.test_warehouse import create_warehouse
from erpnext.stock.doctype.item.test_item import create_item
from erpnext.stock.utils import get_stock_balance, get_incoming_rate, get_available_serial_nos, get_stock_value_on
from erpnext.stock.doctype.serial_no.serial_no import get_serial_nos

class TestStockReconciliation(unittest.TestCase):
	@classmethod
	def setUpClass(self):
		create_batch_or_serial_no_items()
		frappe.db.set_value("Stock Settings", None, "allow_negative_stock", 1)
		insert_existing_sle()

	def test_reco_for_fifo(self):
		self._test_reco_sle_gle("FIFO")

	def test_reco_for_moving_average(self):
		self._test_reco_sle_gle("Moving Average")

	def _test_reco_sle_gle(self, valuation_method):
		set_perpetual_inventory()
		# [[qty, valuation_rate, posting_date,
		#		posting_time, expected_stock_value, bin_qty, bin_valuation]]
		input_data = [
			[50, 1000, "2012-12-26", "12:00"],
			[25, 900, "2012-12-26", "12:00"],
			["", 1000, "2012-12-20", "12:05"],
			[20, "", "2012-12-26", "12:05"],
			[0, "", "2012-12-31", "12:10"]
		]

		for d in input_data:
			set_valuation_method("_Test Item", valuation_method)

			last_sle = get_previous_sle({
				"item_code": "_Test Item",
				"warehouse": "_Test Warehouse - _TC",
				"posting_date": d[2],
				"posting_time": d[3]
			})

			# submit stock reconciliation
			stock_reco = create_stock_reconciliation(qty=d[0], rate=d[1],
				posting_date=d[2], posting_time=d[3])

			# check stock value
			sle = frappe.db.sql("""select * from `tabStock Ledger Entry`
				where voucher_type='Stock Reconciliation' and voucher_no=%s""", stock_reco.name, as_dict=1)

			qty_after_transaction = flt(d[0]) if d[0] != "" else flt(last_sle.get("qty_after_transaction"))

			valuation_rate = flt(d[1]) if d[1] != "" else flt(last_sle.get("valuation_rate"))

			if qty_after_transaction == last_sle.get("qty_after_transaction") \
				and valuation_rate == last_sle.get("valuation_rate"):
					self.assertFalse(sle)
			else:
				self.assertEqual(sle[0].qty_after_transaction, qty_after_transaction)
				self.assertEqual(sle[0].stock_value, qty_after_transaction * valuation_rate)

				# no gl entries
				self.assertTrue(frappe.db.get_value("Stock Ledger Entry",
					{"voucher_type": "Stock Reconciliation", "voucher_no": stock_reco.name}))
				self.assertFalse(get_stock_and_account_difference(["_Test Account Stock In Hand - _TC"]))

			stock_reco.cancel()

			self.assertFalse(frappe.db.get_value("Stock Ledger Entry",
				{"voucher_type": "Stock Reconciliation", "voucher_no": stock_reco.name}))

			self.assertFalse(frappe.db.get_value("GL Entry",
				{"voucher_type": "Stock Reconciliation", "voucher_no": stock_reco.name}))

			set_perpetual_inventory(0)

	def test_get_items(self):
		create_warehouse("_Test Warehouse Group 1", {"is_group": 1})
		create_warehouse("_Test Warehouse Ledger 1",
			{"is_group": 0, "parent_warehouse": "_Test Warehouse Group 1 - _TC"})

		create_item("_Test Stock Reco Item", is_stock_item=1, valuation_rate=100,
			warehouse="_Test Warehouse Ledger 1 - _TC", opening_stock=100)

		items = get_items("_Test Warehouse Group 1 - _TC", nowdate(), nowtime(), "_Test Company")

		self.assertEqual(["_Test Stock Reco Item", "_Test Warehouse Ledger 1 - _TC", 100],
			[items[0]["item_code"], items[0]["warehouse"], items[0]["qty"]])

	def test_stock_reco_for_serialized_item(self):
		set_perpetual_inventory()

		to_delete_records = []
		to_delete_serial_nos = []

		# Add new serial nos
		serial_item_code = "Stock-Reco-Serial-Item-1"
		serial_warehouse = "_Test Warehouse for Stock Reco1 - _TC"

		sr = create_stock_reconciliation(item_code=serial_item_code,
			warehouse = serial_warehouse, qty=5, rate=200)

		# print(sr.name)
		serial_nos = get_serial_nos(sr.items[0].serial_no)
		self.assertEqual(len(serial_nos), 5)

		args = {
			"item_code": serial_item_code,
			"warehouse": serial_warehouse,
			"posting_date": nowdate(),
			"posting_time": nowtime(),
			"serial_no": sr.items[0].serial_no
		}

		valuation_rate = get_incoming_rate(args)
		self.assertEqual(valuation_rate, 200)

		to_delete_records.append(sr.name)

		sr = create_stock_reconciliation(item_code=serial_item_code,
			warehouse = serial_warehouse, qty=5, rate=300, serial_no = '\n'.join(serial_nos))

		# print(sr.name)
		serial_nos1 = get_serial_nos(sr.items[0].serial_no)
		self.assertEqual(len(serial_nos1), 5)

		args = {
			"item_code": serial_item_code,
			"warehouse": serial_warehouse,
			"posting_date": nowdate(),
			"posting_time": nowtime(),
			"serial_no": sr.items[0].serial_no
		}

		valuation_rate = get_incoming_rate(args)
		self.assertEqual(valuation_rate, 300)

		to_delete_records.append(sr.name)
		to_delete_records.reverse()

		for d in to_delete_records:
			stock_doc = frappe.get_doc("Stock Reconciliation", d)
			stock_doc.cancel()

		for d in serial_nos + serial_nos1:
			if frappe.db.exists("Serial No", d):
				frappe.delete_doc("Serial No", d)

	def test_stock_reco_for_batch_item(self):
		set_perpetual_inventory()

		to_delete_records = []
		to_delete_serial_nos = []

		# Add new serial nos
		item_code = "Stock-Reco-batch-Item-1"
		warehouse = "_Test Warehouse for Stock Reco2 - _TC"

		sr = create_stock_reconciliation(item_code=item_code,
			warehouse = warehouse, qty=5, rate=200, do_not_submit=1)
		sr.save(ignore_permissions=True)
		sr.submit()

		self.assertTrue(sr.items[0].batch_no)
		to_delete_records.append(sr.name)

		sr1 = create_stock_reconciliation(item_code=item_code,
			warehouse = warehouse, qty=6, rate=300, batch_no=sr.items[0].batch_no)

		args = {
			"item_code": item_code,
			"warehouse": warehouse,
			"posting_date": nowdate(),
			"posting_time": nowtime(),
		}

		valuation_rate = get_incoming_rate(args)
		self.assertEqual(valuation_rate, 300)
		to_delete_records.append(sr1.name)


		sr2 = create_stock_reconciliation(item_code=item_code,
			warehouse = warehouse, qty=0, rate=0, batch_no=sr.items[0].batch_no)

		stock_value = get_stock_value_on(warehouse, nowdate(), item_code)
		self.assertEqual(stock_value, 0)
		to_delete_records.append(sr2.name)

		to_delete_records.reverse()
		for d in to_delete_records:
			stock_doc = frappe.get_doc("Stock Reconciliation", d)
			stock_doc.cancel()


def insert_existing_sle():
	from erpnext.stock.doctype.stock_entry.test_stock_entry import make_stock_entry

	make_stock_entry(posting_date="2012-12-15", posting_time="02:00", item_code="_Test Item",
		target="_Test Warehouse - _TC", qty=10, basic_rate=700)

	make_stock_entry(posting_date="2012-12-25", posting_time="03:00", item_code="_Test Item",
		source="_Test Warehouse - _TC", qty=15)

	make_stock_entry(posting_date="2013-01-05", posting_time="07:00", item_code="_Test Item",
		target="_Test Warehouse - _TC", qty=15, basic_rate=1200)

def create_batch_or_serial_no_items():
	create_warehouse("_Test Warehouse for Stock Reco1",
		{"is_group": 0, "parent_warehouse": "_Test Warehouse Group - _TC"})

	create_warehouse("_Test Warehouse for Stock Reco2",
		{"is_group": 0, "parent_warehouse": "_Test Warehouse Group - _TC"})

	serial_item_doc = create_item("Stock-Reco-Serial-Item-1", is_stock_item=1)
	if not serial_item_doc.has_serial_no:
		serial_item_doc.has_serial_no = 1
		serial_item_doc.serial_no_series = "SRSI.####"
		serial_item_doc.save(ignore_permissions=True)

	batch_item_doc = create_item("Stock-Reco-batch-Item-1", is_stock_item=1)
	if not batch_item_doc.has_batch_no:
		batch_item_doc.has_batch_no = 1
		batch_item_doc.create_new_batch = 1
		serial_item_doc.batch_number_series = "BASR.#####"
		batch_item_doc.save(ignore_permissions=True)

def create_stock_reconciliation(**args):
	args = frappe._dict(args)
	sr = frappe.new_doc("Stock Reconciliation")
	sr.posting_date = args.posting_date or nowdate()
	sr.posting_time = args.posting_time or nowtime()
	sr.set_posting_time = 1
	sr.company = args.company or "_Test Company"
	sr.expense_account = args.expense_account or \
		("Stock Adjustment - _TC" if frappe.get_all("Stock Ledger Entry") else "Temporary Opening - _TC")
	sr.cost_center = args.cost_center or "_Test Cost Center - _TC"
	sr.append("items", {
		"item_code": args.item_code or "_Test Item",
		"warehouse": args.warehouse or "_Test Warehouse - _TC",
		"qty": args.qty,
		"valuation_rate": args.rate,
		"serial_no": args.serial_no,
		"batch_no": args.batch_no
	})

	try:
		if not args.do_not_submit:
			sr.submit()
	except EmptyStockReconciliationItemsError:
		pass
	return sr

def set_valuation_method(item_code, valuation_method):
	frappe.db.set_value("Item", item_code, "valuation_method", valuation_method)

	for warehouse in frappe.get_all("Warehouse", filters={"company": "_Test Company"}, fields=["name", "is_group"]):
		if not warehouse.is_group:
			update_entries_after({
				"item_code": item_code,
				"warehouse": warehouse.name
			}, allow_negative_stock=1)

test_dependencies = ["Item", "Warehouse"]

