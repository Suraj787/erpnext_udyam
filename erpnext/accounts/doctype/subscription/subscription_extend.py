from __future__ import unicode_literals
import frappe
import erpnext
from frappe.utils import flt, nowdate, add_days, cint
from erpnext.accounts.doctype.subscription_plan.subscription_plan import get_plan_rate
from frappe.utils.data import nowdate, getdate, cint, add_days, date_diff, get_last_day, add_to_date, flt
from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import get_accounting_dimensions
from frappe import _

def generate_delivery():
	print('###################')
	subscription_list=frappe.get_all('Subscription',{'status':('!=','cancelled'),'creation':('>','2020-01-03')})  
	for s in subscription_list:
		customer=frappe.get_doc('Subscription',s)
		create_delivery(customer,0)

def create_delivery(customer,prorate):
	print('****************1')
	delivery = frappe.new_doc('Delivery Note')
	delivery.set_posting_time = 1
	delivery.posting_date = customer.start
	delivery.set_warehouse='Stores - up'
	delivery.customer = customer.customer
	print(delivery)
	accounting_dimensions = get_accounting_dimensions()

	for dimension in accounting_dimensions:
		if customer.get(dimension):
			delivery.update({
				dimension: customer.get(dimension)
			})
	print('*****************2')
	items_list = get_items_from_plans(customer,customer.plans, prorate)
	print('****************3')
	for item in items_list:
		delivery.append('items',item)
	
	if customer.tax_template:
		delivery.taxes_and_charges = customer.tax_template
		delivery.set_taxes()

	if customer.additional_discount_percentage:
		delivery.additional_discount_percentage = customer.additional_discount_percentage

	if customer.additional_discount_amount:
		delivery.discount_amount = customer.additional_discount_amount

	if customer.additional_discount_percentage or customer.additional_discount_amount:
		discount_on = customer.apply_additional_discount
		delivery.apply_additional_discount = discount_on if discount_on else 'Grand Total'

	delivery.from_date = customer.current_invoice_start
	delivery.to_date = customer.current_invoice_end

	delivery.flags.ignore_mandatory = True
	delivery.save()

def get_items_from_plans(customer, plans, prorate=0):
	print('88888888888888')
	if prorate:
		prorate_factor = get_prorata_factor(customer.current_invoice_end, customer.current_invoice_start)

	items = []
	customer = customer.customer
	for plan in plans:
		item_code = frappe.db.get_value("Subscription Plan", plan.plan, "item")
		if not prorate:
			items.append({'item_code': item_code, 'qty': plan.qty, 'rate': get_plan_rate(plan.plan, plan.qty, customer)})
		else:
			items.append({'item_code': item_code, 'qty': plan.qty, 'rate': (get_plan_rate(plan.plan, plan.qty, customer) * prorate_factor)})

	return items

def get_prorata_factor(period_end, period_start):
	diff = flt(date_diff(nowdate(), period_start) + 1)
	plan_days = flt(date_diff(period_end, period_start) + 1)
	prorate_factor = diff / plan_days

	return prorate_factor

def subscriber_next_scheduled_date():
	subscription_list=frappe.get_all('Subscription',{'status':('!=','cancelled'),'creation':('>','2020-01-03')})  
	for s in subscription_list:
		customer=frappe.get_doc('Subscription',s)
		plan=customer.plans[0].plan
		frq=frappe.db.get_value('Subscription Plan',plan,'frequency')
		rp=frappe.db.get_value('Subscription Plan',plan,'repeat_on_day')
		old_scheduled_date=frappe.db.get_value('Subscription',customer.name,'next_scheduled_date')
		if old_scheduled_date < frappe.utils.nowdate():
			frappe.db.set_value('subscription',customer.name,'next_scheduled_date','')
		if not frappe.db.get_value('Subscription',customer.name,'next_scheduled_date'): 
			next_date = get_next_schedule_date(customer.next_scheduled_date,frq or 'Monthly',rp or 0 )
			end_date=datetime.strptime(customer.current_invoice_end, '%Y-%m-%d').date()
			if next_date<end_date:
				frappe.db.set_value('subscription',customer.name,'next_scheduled_date',next_date)


