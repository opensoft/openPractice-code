frappe.ui.form.on("OpenPractice Claim Batch", {
    setup(frm) {
        frm.set_query("billing_case", "billing_cases", () => ({
            filters: { docstatus: 1 }
        }));
    },
    refresh(frm) {
        if (frm.doc.docstatus === 1) {
            frm.add_custom_button(__("Download test 837P"), () => {
                const route = "open_practice.open_practice.doctype.openpractice_claim_batch.openpractice_claim_batch";
                window.location.assign(`/api/method/${route}.download_837p?name=${encodeURIComponent(frm.doc.name)}`);
            });
        }
    }
});
