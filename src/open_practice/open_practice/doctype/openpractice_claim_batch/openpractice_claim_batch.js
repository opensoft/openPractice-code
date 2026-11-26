frappe.ui.form.on("OpenPractice Claim Batch", {
    setup(frm) {
        frm.set_query("billing_case", "billing_cases", () => ({
            filters: { docstatus: 1 }
        }));
    },
    refresh(frm) {
        const canReconstruct = frappe.session.user === "Administrator" ||
            frappe.user.has_role("OpenPractice Billing Evidence");
        if (frm.doc.docstatus === 1 && frm.doc.phi_snapshot_ref && canReconstruct) {
            frm.add_custom_button(__("Download test 837P"), () => {
                const route = "open_practice.open_practice.doctype.openpractice_claim_batch.openpractice_claim_batch";
                open_url_post(`/api/method/${route}.download_837p`, {name: frm.doc.name});
            });
        }
    }
});
