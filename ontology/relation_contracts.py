"""Source FK names used to validate current FM business relationships.

Portal's FM replica capture records physical SQL columns on relation edges;
its entity observations expose semantic attribute names. Both must identify
the same current FK before an edge can support business work.
"""

from __future__ import annotations


FM_SOURCE_FK = {
    "fm_ordered_by": ("idcust", "customerId"),
    "fm_line_of_sales_order": ("idoe", "salesOrderId"),
    "fm_sales_line_product": ("idfp", "productId"),
    "fm_sales_line_bulk_product": ("idbulk", "bulkProductId"),
    "fm_assembly_link_for_line": ("idoeitem", "salesOrderLineId"),
    "fm_linked_assembly_order": ("idassembly", "assemblyOrderId"),
    "fm_item_of_assembly_order": ("assembly_order_id", "assemblyOrderId"),
    "fm_task_of_assembly_order": ("mo_id", "assemblyOrderId"),
    "fm_status_of_assembly_order": ("assembly_order_id", "assemblyOrderId"),
    "fm_damage_of_assembly_order": ("assembly_order_id", "assemblyOrderId"),
    "fm_damage_for_assembly_item": ("assembly_item_id", "assemblyItemId"),
    "fm_line_of_purchase_order": ("idpogen", "purchaseOrderId"),
    "fm_receipt_for_purchase_line": ("idgenpoitem", "purchaseOrderLineId"),
    "fm_raw_receipt_detail_for_purchase_line": ("idpogenitem", "purchaseOrderLineId"),
    "fm_packaging_receipt_detail_for_purchase_line": ("idpogenitem", "purchaseOrderLineId"),
    "fm_po_line_raw_supplier": ("idproductsupp", "supplierProductId"),
    "fm_po_line_packaging_supplier": ("idproductsupp", "supplierProductId"),
    "fm_ordered_from": ("idsupplier", "supplierId"),
    "fm_raw_supplier_material": ("idraw", "rawMaterialId"),
    "fm_packaging_supplier_material": ("idpack", "packagingId"),
    "fm_ao_item_packaging": ("packaging_id", "packagingId"),
    "fm_ao_item_bulk": ("bulk_product_id", "bulkProductId"),
    "fm_ao_item_finished": ("finished_product_id", "finishedProductId"),
}


def semantic_fk_field(relation: str, source_field: str) -> str:
    names = FM_SOURCE_FK.get(relation)
    return names[1] if names and source_field == names[0] else source_field
