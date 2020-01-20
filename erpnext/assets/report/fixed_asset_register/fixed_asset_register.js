// Copyright (c) 2016, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt
/* eslint-disable */

frappe.query_reports["Fixed Asset Register"] = {
	"filters": [
		{
			fieldname:"company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
			reqd: 1
		},
		{
			fieldname:"status",
			label: __("Status"),
			fieldtype: "Select",
			options: "In Store\nDisposed",
			default: 'In Store',
			reqd: 1
		},
		{
			fieldname:"finance_book",
			label: __("Finance Book"),
			fieldtype: "Link",
			options: "Finance Book"
		},
	]
};
