import base64
import re
import traceback
import os
from PIL import Image
import qrcode
import io
import logging 
import json
import requests
from collections import defaultdict
from lxml import etree
from odoo import models, api, fields, _
from odoo.exceptions import AccessError, ValidationError, UserError
from odoo.tools import float_compare
from odoo.addons.tijari_wms_18.controllers.controllers import WmsApiController
from odoo.tools.float_utils import float_is_zero

_logger = logging.getLogger(__name__)
_OPERATOR_VALIDATION_WRITE_TOKEN = object()
_BACKORDER_CREATE_TOKEN = object()

class StockPicking(models.Model):
    _name = 'stock.picking'
    _inherit = ['stock.picking', 'approval.mixin']

    @api.model
    def _get_operator_validation_workflow_context(self):
        """Allow trusted workflow and scan methods to carry internal writes.

        The identity token cannot be recreated from a client supplied context;
        this private helper is used by manufacturing workflow entry points.
        """
        if not self.env.user.has_group('tijari_wms_18.group_operator'):
            return {}
        elevated_groups = (
            'tijari_wms_18.group_warehouse_manager',
            'tijari_wms_18.group_warehouse_head',
            'tijari_wms_18.group_warehouse_admin',
            'tijari_wms_18.group_it_staff',
            'stock.group_stock_manager',
            'base.group_system',
        )
        if any(self.env.user.has_group(group) for group in elevated_groups):
            return {}
        return {
            '_tijari_operator_validation_write_token': _OPERATOR_VALIDATION_WRITE_TOKEN,
        }

    @api.model
    def get_view(self, view_id=None, view_type='form', **options):
        result = super().get_view(view_id=view_id, view_type=view_type, **options)
        restricted_list = self.env.ref(
            'tijari_wms_18.view_stock_picking_list_no_manual_create',
            raise_if_not_found=False,
        )
        if (
            view_type == 'list'
            and restricted_list
            and view_id == restricted_list.id
            and (
                self.env.user.has_group('stock.group_stock_manager')
                or self.env.user.has_group('base.group_system')
            )
        ):
            arch = etree.fromstring(result['arch'].encode())
            arch.set('create', 'true')
            result['arch'] = etree.tostring(arch, encoding='unicode')

        # Matrix read access applies to the picking document itself. Operators
        # may edit only the notebook pages granted to their role, except in the
        # dedicated Over-Receipt action, which allows document edits.
        elevated_groups = (
            'tijari_wms_18.group_warehouse_manager',
            'tijari_wms_18.group_warehouse_head',
            'tijari_wms_18.group_warehouse_admin',
            'tijari_wms_18.group_it_staff',
            'stock.group_stock_manager',
            'base.group_system',
        )
        is_limited_operator = (
            self.env.user.has_group('tijari_wms_18.group_operator')
            and not any(self.env.user.has_group(group) for group in elevated_groups)
        )
        # Creation on stock.picking and edits outside permitted notebook pages
        # stay restricted even when an action does not preserve its context.
        if view_type in ('list', 'form') and is_limited_operator:
            arch = etree.fromstring(result['arch'].encode())
            arch.set('create', 'false')
            if self.env.context.get('default_stock_lebih'):
                # The dedicated Over-Receipt action grants operators access
                # to edit these documents; other pickings retain the normal
                # operator restrictions below.
                arch.set('delete', 'false')
                arch.set('edit', 'true')
                result['arch'] = etree.tostring(arch, encoding='unicode')
                return result
            if view_type == 'list':
                arch.set('edit', 'false')
                arch.set('delete', 'false')
                for button in arch.xpath('.//header//button'):
                    button.set('invisible', '1')
                result['arch'] = etree.tostring(arch, encoding='unicode')
                return result

            writable_pages = {
                'page_picker', 'page_packer_operation', 'page_putaway',
                'detail_product', 'packed_items', 'note', 'Notes', 'extra',
                'extra_info', 'additional_info', 'page_notes',
            }
            writable_page_labels = {
                'Unpacked Item Details', 'Packed Item', 'Additional Info',
                'Additional Information', 'Notes',
            }
            for field_node in arch.xpath('.//field'):
                page = field_node.getparent()
                while page is not None and page.tag != 'page':
                    page = page.getparent()
                if page is None or (
                    page.get('name') not in writable_pages
                    and page.get('string') not in writable_page_labels
                ):
                    field_node.set('readonly', '1')
            for button in arch.xpath('.//header//button'):
                # Operators can validate a picking when the normal view
                # conditions allow it. Keep other workflow buttons hidden.
                if button.get('name') != 'button_validate':
                    button.set('invisible', '1')
            result['arch'] = etree.tostring(arch, encoding='unicode')
        return result

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        if (
            self.env.context.get('default_stock_lebih')
            and 'approval_type_id' in fields_list
            and not vals.get('approval_type_id')
        ):
            approval_type = self.env.ref(
                'tijari_wms_18.approval_type_over_receipt', raise_if_not_found=False,
            )
            if approval_type:
                vals['approval_type_id'] = approval_type.id
        if self.env.context.get('default_stock_lebih') and 'picking_type_id' in fields_list and not vals.get('picking_type_id'):
            # The Over-Receipt menu creates a normal inbound receipt.
            warehouse_id = (
                self.env.context.get('default_warehouse_id')
                or self.env.user.warehouse_id.id
            )
            domain = [
                ('code', '=', 'incoming'),
                ('name', '=', 'Receipts'),
                ('operation_category', '=', 'receive'),
                ('company_id', '=', self.env.company.id),
            ]
            PickingType = self.env['stock.picking.type']
            picking_type = PickingType.search(
                domain + ([('warehouse_id', '=', warehouse_id)] if warehouse_id else []),
                limit=1,
            )
            if not picking_type:
                picking_type = PickingType.search(domain, order='warehouse_id, id', limit=1)
            if picking_type:
                vals['picking_type_id'] = picking_type.id
        if (
            self.env.context.get('default_stock_lebih')
            and 'location_id' in fields_list
            and not vals.get('location_id')
            and not self.env.context.get('default_location_id')
        ):
            picking_type_id = (
                vals.get('picking_type_id')
                or self.env.context.get('default_picking_type_id')
            )
            picking_type = self.env['stock.picking.type'].browse(picking_type_id).exists()
            source_location = picking_type.default_location_src_id if picking_type else False
            if not source_location:
                source_location = self.env.ref(
                    'stock.stock_location_suppliers', raise_if_not_found=False,
                )
            if source_location:
                vals['location_id'] = source_location.id
        if self.env.context.get('default_stock_lebih') and 'location_dest_id' in fields_list and not self.env.context.get('default_location_dest_id'):
            warehouse_id = self.env.context.get('default_warehouse_id') or self.env.user.warehouse_id.id
            location_domain = [
                ('usage', '=', 'internal'),
                ('name', 'ilike', 'Finish Good'),
                ('company_id', 'in', [self.env.company.id, False]),
            ]
            if warehouse_id:
                location_domain.append(('warehouse_id', '=', warehouse_id))
            finish_goods = self.env['stock.location'].search(location_domain, order='id', limit=1)
            if finish_goods:
                vals['location_dest_id'] = finish_goods.id
        return vals

    @api.depends('picking_type_id', 'partner_id', 'stock_lebih')
    def _compute_location_id(self):
        super()._compute_location_id()
        for picking in self:
            if not picking.stock_lebih or picking.state in ('done', 'cancel'):
                continue
            warehouse_id = (
                picking.picking_type_id.warehouse_id.id
                or self.env.context.get('default_warehouse_id')
                or self.env.user.warehouse_id.id
            )
            domain = [
                ('usage', '=', 'internal'),
                ('name', 'ilike', 'Finish Good'),
                ('company_id', 'in', [picking.company_id.id, False]),
            ]
            if warehouse_id:
                domain.append(('warehouse_id', '=', warehouse_id))
            finish_goods = self.env['stock.location'].search(
                domain, order='id', limit=1,
            )
            if finish_goods:
                picking.location_dest_id = finish_goods

    stock_lebih = fields.Boolean(
        string='Stock Lebih',
        default=False,
        copy=False,
        index=True,
        help='Dokumen khusus untuk penerimaan SOTR dengan quantity berlebih.',
    )
    over_receipt_transaction_type = fields.Selection(
        [
            ('receipt_sotr', 'Receipt SOTR'),
            ('inventory_receive', 'Inventory Receive'),
        ],
        string='Tipe Transaksi Over-Receipt',
        copy=False,
        tracking=True,
    )
    ref_sotr = fields.Char(
        string='Ref. No. Document',
        copy=False,
        tracking=True,
    )
    transfer_shipment_discrepancy = fields.Boolean(
        string='Transfer Shipment Quantity Discrepancy',
        copy=False,
        tracking=True,
    )

    approval_type_id = fields.Many2one(
        'approval.type',
        string='Approval Type',
        domain="[('approval_model_id.model', '=', 'stock.picking')]",
    )

    # Memberi label unik untuk menghindari warning 'Same label: Type (Acumatica)'
    adt_receipt_nbr = fields.Char(string='Receipt Nbr (Acumatica)')
    adt_vendor_code = fields.Char(string='VendorID (Acumatica)')
    adt_partner_ref = fields.Char(string='Vendor Ref (Acumatica)')
    adt_branch_warehouse = fields.Char(string='Branch (Acumatica)')
    adt_actual_qty = fields.Float(string='Control Qty (Acumatica)')
    adt_is_hold = fields.Boolean(string='Hold (Acumatica)')
    adt_inventory_ref = fields.Char(string='Inventory Ref Nbr (Acumatica)')
    adt_acumatica_write_date = fields.Datetime(string='Last Modified (Acumatica)')
    adt_header_location_code = fields.Char(string='Header Location (Acumatica)')
    adt_post_period = fields.Char(string='Post Period (Acumatica)')
    adt_status = fields.Char(string='Status (Acumatica)')
    adt_total_qty = fields.Float(string='Qty Total (Acumatica)')
    
    adt_type = fields.Char(string='Tipe Transaksi')
    
    adt_external_ref = fields.Char(string='External Ref (Acumatica)')
    adt_warehouse_code = fields.Char(string='Warehouse (Acumatica)')
    adt_external_id = fields.Char(string='UUID Header Acumatica', index=True)
    adt_to_warehouse_code = fields.Char(string='To Warehouse (Acumatica)')
    adt_from_warehouse_code = fields.Char(string='From Warehouse (Acumatica)')
    adt_description = fields.Char(string='Description (Acumatica)')
    adt_order_type = fields.Char(string='Order Type (Acumatica)')
    adt_order_nbr = fields.Char(string='Order Nbr (Acumatica)')
    adt_change_item_ref_nbr = fields.Char(string='Change Item Ref. Nbr (Acumatica)')
    adt_is_document_only = fields.Boolean(string='Is Document Only', index=True, copy=False)
    operation_category = fields.Selection(related='picking_type_id.operation_category', string='Operation Category', store=True, readonly=True)
    adt_destination_location_address = fields.Char(string='Destination Address')
    qr_code_image = fields.Binary(string='Sign')

    adt_actual_amount = fields.Float(string='Control Amount (Acumatica)')
    adt_total_amount = fields.Float(string='Total Amount (Acumatica)')

    adt_receipt_type = fields.Char(string='Receipt Type (Acumatica)')
    adt_trans_type = fields.Char(string='Trans Type (Acumatica)')
    sync_state = fields.Selection([
        ('ready', 'Ready'),
        ('not_ready', 'Not Ready'),
    ], string='Sync State', default='ready', readonly=True, help='Data acumatica yg tidak sesuai dengan master yg ada di wms akan masuk ke state Not Ready, dan record tidak bisa di proses sampai data master nya sudah sinkron.')

    sync_message = fields.Char(string='Sync Message', readonly=True, help='Keterangan jika ada data acumatica yang tidak sesuai dengan master yang ada di wms.')
    adt_shipment_status = fields.Char(string='Shipment Status (Acumatica)')
    destination_location = fields.Char(string='to delete field')
    created_by = fields.Char(string='Created by')
    approved_by = fields.Char(string='Approved by')
    created_by_id = fields.Many2one("res.users", string='Created by')
    approved_by_id = fields.Many2one("res.users", string='Approved by')
    user_ids = fields.Many2many(
        comodel_name="res.users",
        relation="stock_picking_user_rel",
        column1="picking_id",
        column2="user_id",
        string="Responsible Users",
        domain=[("share", "=", False)],
        compute="_compute_user_ids",
        store=True,
    )


    pick_responsible_ids = fields.Many2many(
        comodel_name="res.users",
        relation="stock_picking_pick_responsible_rel",
        column1="picking_id",
        column2="user_id",
        string="Assigned Picker",
    )

    pack_responsible_ids = fields.Many2many(
        comodel_name="res.users",
        relation="stock_picking_pack_responsible_rel",
        column1="picking_id",
        column2="user_id",
        string="Assigned Packer",
    )

    do_responsible_ids = fields.Many2many(
        comodel_name="res.users",
        relation="stock_picking_do_responsible_rel",
        column1="picking_id",
        column2="user_id",
        string="Assigned Loader",
    )

    putaway_responsible_ids = fields.Many2many(
        comodel_name="res.users",
        relation="stock_picking_putaway_responsible_rel",
        column1="picking_id",
        column2="user_id",
        string="Assigned Putaway",
        domain=[("share", "=", False)],
    )

    show_picker_page = fields.Boolean(
        compute="_compute_show_picker_page",
    )

    show_packer_page = fields.Boolean(
        compute='_compute_show_packer_page',
    )

    show_delivery_page = fields.Boolean(
        compute='_compute_show_delivery_page',
    )
    
    show_putaway_page = fields.Boolean(
        compute="_compute_show_putaway_page",
    )

    can_edit_pack_responsible_ids = fields.Boolean(string='can edit')

    @api.depends("pick_responsible_ids")
    def _compute_show_picker_page(self):
        user = self.env.user
        is_warehouse_operator = user.has_group("tijari_wms_18.group_operator")
        for picking in self:
            picking.show_picker_page = (
                not is_warehouse_operator
                or user in picking.pick_responsible_ids
            )

    @api.depends('pack_responsible_ids')
    def _compute_show_packer_page(self):
        user = self.env.user
        is_warehouse_operator = user.has_group('tijari_wms_18.group_operator')
        for picking in self:
            picking.show_packer_page = (
                not is_warehouse_operator
                or user in picking.pack_responsible_ids
            )

    @api.depends('do_responsible_ids')
    def _compute_show_delivery_page(self):
        user = self.env.user
        is_warehouse_operator = user.has_group('tijari_wms_18.group_operator')
        for picking in self:
            picking.show_delivery_page = (
                not is_warehouse_operator
                or user in picking.do_responsible_ids
            )

    @api.depends("putaway_responsible_ids")
    def _compute_show_putaway_page(self):
        user = self.env.user
        is_warehouse_operator = user.has_group("tijari_wms_18.group_operator")
        for picking in self:
            picking.show_putaway_page = (
                not is_warehouse_operator
                or user in picking.putaway_responsible_ids
            )

    @api.model
    def _get_responsible_carry_over_fields(self):
        """Fields that describe "who is responsible" for this transfer.

            Kept in one place because they are carried forward to the
            next transfer(s) in the chain (e.g. Pick -> Pack -> Delivery Order).

            Putaway is intentionally excluded because it is assigned
            independently on the receiving/putaway process.
        """
        return [
            'pick_responsible_ids',
            'pack_responsible_ids',
            'do_responsible_ids',
            'user_ids',
        ]

    @api.onchange('pick_responsible_ids')
    def _onchange_pick_responsible_ids(self):
        for picking in self:
            if 'Pick' not in (picking.picking_type_name or '') :
                continue

            old_picker_ids = set(picking._origin.pick_responsible_ids.ids)
            new_picker_ids = set(picking.pick_responsible_ids.ids)
            packer_ids = set(picking.pack_responsible_ids.ids)

            # Picker ditambah → Packer ikut tambah
            packer_ids.update(new_picker_ids - old_picker_ids)

            # Picker dihapus → Packer ikut hapus
            packer_ids.difference_update(old_picker_ids - new_picker_ids)

            picking.pack_responsible_ids = [(6, 0, list(packer_ids))]

    @api.depends(
        "pick_responsible_ids",
        "pack_responsible_ids",
        "do_responsible_ids",
        "putaway_responsible_ids",
    )
    def _compute_user_ids(self):
        for picking in self:
            picking.user_ids = (
                picking.pick_responsible_ids
                | picking.pack_responsible_ids
                | picking.do_responsible_ids
                | picking.putaway_responsible_ids
            )

    all_moves_picked = fields.Boolean(
        compute='_compute_all_moves_picked',
    )
    allowed_putaway_product_ids = fields.Many2many(
        'product.product',
        compute='_compute_allowed_putaway_product_ids',
    )
    scan_product_button = fields.Boolean(string='Scan Product')
    scan_product_by_lot_button = fields.Boolean(
        string='Scan Product By Lot',
        compute='_compute_scan_product_by_lot_button',
    )
    putaway_product_id = fields.Many2one(
        'product.product', string="Product Name")
    putaway_scan_code = fields.Char(
        string="Scan Handheld",
        help="Bisa diisi dari scanner handheld atau diketik manual.",
    )
    allowed_putaway_lot_ids = fields.Many2many(
        'stock.lot',
        compute='_compute_allowed_putaway_lot_ids',
    )
    putaway_lot_id = fields.Many2one(
        comodel_name="stock.lot",
        string="LOT Number",
        readonly=False,
        domain="[('id', 'in', allowed_putaway_lot_ids)]",
    )
    putaway_move_line_ids = fields.Many2many(
        comodel_name="stock.move.line",
        string="Stock Move Lines",
        compute='_compute_putaway_values',
        readonly=False,
    )
    putaway_move_ids = fields.Many2many(
        comodel_name="stock.move",
        string="Putaway Stock Moves",
        compute='_compute_putaway_move_ids',
    )
    putaway_progress_status = fields.Selection(
        [
            ('finished', 'Finished'),
            ('unfinished', 'Unfinished'),
        ],
        string='Status',
        compute='_compute_putaway_progress_status',
    )
    putaway_qty_finish = fields.Float(
        string='Quantity Dipenuhi',
        compute='_compute_putaway_qty_finish',
    )
    allowed_picker_product_ids = fields.Many2many(
        'product.product',
        compute='_compute_allowed_picker_product_ids',
    )
    scan_picker_product_button = fields.Boolean(string='Scan Product')
    scan_picker_product_by_lot_button = fields.Boolean(
        string='Scan Product By Lot',
        compute='_compute_scan_product_by_lot_button',
    )
    picker_product_id = fields.Many2one(
        'product.product',
        string="Product Name",
    )
    picker_scan_code = fields.Char(
        string="Scan Handheld",
        help="Bisa diisi dari scanner handheld atau diketik manual.",
    )
    allowed_picker_lot_ids = fields.Many2many(
        'stock.lot',
        compute='_compute_allowed_picker_lot_ids',
    )
    picker_lot_id = fields.Many2one(
        'stock.lot',
        string="LOT Number",
        domain="[('id', 'in', allowed_picker_lot_ids)]",
        readonly=False,
    )
    picker_progress_status = fields.Selection(
        [
            ('finished', 'Finished'),
            ('unfinished', 'Unfinished'),
        ],
        string='Status',
        compute='_compute_picker_progress_status',
    )
    picker_qty_finish = fields.Float(
        string='Quantity Dipenuhi',
        compute='_compute_picker_qty_finish',
    )
    picker_move_ids = fields.Many2many(
        comodel_name="stock.move",
        string="Stock Moves",
        compute='_compute_picker_values',
        readonly=True,
    )

    filtered_move_ids = fields.One2many(
        comodel_name="stock.move",
        inverse_name="picking_id",
        string="Filtered Stock Moves",
        compute='_compute_filtered_move_ids',
    )

    allowed_unpacked_product_ids = fields.Many2many(
        comodel_name="product.product",
        compute="_compute_allowed_unpacked_product_ids",
    )
    allowed_unpacked_lot_ids = fields.Many2many(
        comodel_name="stock.lot",
        compute="_compute_allowed_unpacked_lot_ids",
    )
    unpacked_product_id = fields.Many2one(
        comodel_name="product.product",
        string="Filter Product",
        domain="[('id', 'in', allowed_unpacked_product_ids)]",
    )
    unpacked_lot_id = fields.Many2one(
        comodel_name="stock.lot",
        string="Filter Lot",
        domain="[('id', 'in', allowed_unpacked_lot_ids)]",
    )
    unpacked_lot_code = fields.Char(string="Filter Lot Code")
    unpacked_scan_code = fields.Char(string="Scan Handheld")
    filtered_unpacked_move_ids = fields.One2many(
        comodel_name="stock.move",
        inverse_name="picking_id",
        string="Filtered Unpacked Moves",
        compute="_compute_filtered_unpacked_move_ids",
    )

    filtered_picker_move_ids = fields.Many2many(
        comodel_name="stock.move",
        string="Ordered Picker Moves",
        compute='_compute_filtered_picker_move_ids',
    )

    def _compute_scan_product_by_lot_button(self):
        for picking in self:
            picking.scan_product_by_lot_button = True
            picking.scan_picker_product_by_lot_button = True

    available_package_ids = fields.Many2many(
        "stock.quant.package",
        compute="_compute_available_package_ids",
        string="Available Packages",
    )

    scanned_package_id = fields.Many2one(
        "stock.quant.package",
        string="Package Number",
    )

    package_scan_code = fields.Char(
        string="Scan Handheld",
        help="Masukkan kode package dari scanner handheld atau ketik manual.",
        store=False,
    )

    filtered_package_level_id = fields.Many2one(
        'stock.package_level',
        string='Filtered Package Level',
        readonly=True
    )

    @api.onchange('scanned_package_id')
    def _onchange_scanned_package_id(self):
        for picking in self:
            picking.filtered_package_level_id = False

            if not picking.scanned_package_id:
                continue

            package_level = picking.package_level_ids.filtered(
                lambda p: p.package_id == picking.scanned_package_id
            )[:1]

            if package_level:
                picking.filtered_package_level_id = package_level

    @api.onchange('package_scan_code')
    def _onchange_package_scan_code(self):
        scan_records = self.with_context(
            **self._get_operator_validation_workflow_context(),
        )
        for picking in scan_records:
            if not picking.package_scan_code:
                continue
            values = picking._get_scanned_package_values(picking.package_scan_code)
            picking.update(values['values'])
            picking.package_scan_code = False

    scan_package_button = fields.Boolean(string="Scan Package")

    filtered_package_level_ids = fields.One2many(
        "stock.package_level",
        compute="_compute_filtered_package_level_ids",
    )

    def _sync_adt_quantities_from_moves(self):
        for picking in self:
            picking.with_context(
                _tijari_operator_validation_write_token=_OPERATOR_VALIDATION_WRITE_TOKEN,
            ).update({
                'adt_total_qty': sum(picking.move_ids.mapped('product_uom_qty'))
                # 'adt_actual_qty': sum(picking.move_ids.mapped('quantity')),
            })

    def action_reset_scan_filters(self):
        """Clear scan inputs and selectors that filter picking operation lines."""
        self.write({
            'putaway_scan_code': False,
            'putaway_product_id': False,
            'putaway_lot_id': False,
            'picker_scan_code': False,
            'picker_product_id': False,
            'picker_lot_id': False,
            'unpacked_scan_code': False,
            'unpacked_product_id': False,
            'unpacked_lot_id': False,
            'unpacked_lot_code': False,
            'package_scan_code': False,
            'scanned_package_id': False,
            'filtered_package_level_id': False,
        })

    @api.depends("package_level_ids")
    def _compute_available_package_ids(self):
        for picking in self:
            picking.available_package_ids = (
                picking.package_level_ids.mapped("package_id")
            )

    @api.depends(
        "package_level_ids",
        "scanned_package_id",
    )
    def _compute_filtered_package_level_ids(self):

        for picking in self:

            if not picking.scanned_package_id:

                picking.filtered_package_level_ids = (
                    picking.package_level_ids
                )

            else:

                picking.filtered_package_level_ids = (
                    picking.package_level_ids.filtered(
                        lambda p:
                        p.package_id
                        == picking.scanned_package_id
                    )
                )

    def write(self, vals):
        import_audit_fields = {
            field_name for field_name in vals
            if field_name in self._fields and field_name not in {
                'write_uid', 'write_date', 'message_follower_ids', 'activity_ids',
            }
        }
        import_audit_before = {
            picking.id: {
                field_name: picking[field_name]
                for field_name in import_audit_fields
            }
            for picking in self
        }
        # if (
        #     'pick_responsible_ids' in vals
        #     and not self.env.context.get('tijari_responsible_sync')
        #     and self.env.user.has_group('tijari_wms_18.group_operator')
        # ):
        #     raise UserError(_(
        #         "Hanya user non-operator yang dapat mengisi/mengubah Penanggungjawab Pick."
        #     ))

        is_operator_validation_write = (
            self.env.context.get('_tijari_operator_validation_write_token')
            is _OPERATOR_VALIDATION_WRITE_TOKEN
        )
        if (
            not self.env.su
            and not is_operator_validation_write
            and self.env.user.has_group('tijari_wms_18.group_operator')
            and not any(self.env.user.has_group(group) for group in (
                'tijari_wms_18.group_warehouse_manager',
                'tijari_wms_18.group_warehouse_head',
                'tijari_wms_18.group_warehouse_admin',
                'tijari_wms_18.group_it_staff',
                'stock.group_stock_manager',
                'base.group_system',
            ))
        ):
            protected_pickings = self.filtered(
                lambda picking: (
                    not picking.internal_transfer_order_id
                    and not picking.stock_lebih
                )
            )
            allowed_operator_fields = {
                'note', 'desc_note', 'note_ba', 'carrier_id',
                'carrier_tracking_ref', 'weight', 'shipping_weight',
                'move_type', 'sale_id', 'batch_id', 'user_ids',
                'pick_responsible_ids', 'pack_responsible_ids',
                'do_responsible_ids', 'putaway_responsible_ids',
                'picker_scan_code', 'picker_product_id', 'picker_lot_id',
                'putaway_scan_code', 'putaway_product_id', 'putaway_lot_id',
                'putaway_move_line_ids',
                'unpacked_scan_code', 'unpacked_product_id', 'unpacked_lot_id',
                'unpacked_lot_code', 'package_scan_code', 'scanned_package_id',
                'filtered_package_level_id', 'filtered_move_ids', 'filtered_package_level_ids'
            }
            blocked_operator_fields = set(vals) - allowed_operator_fields
            if protected_pickings and blocked_operator_fields:
                _logger.warning(
                    "Warehouse Operator picking write blocked: uid=%s picking_ids=%s fields=%s",
                    self.env.uid,
                    protected_pickings.ids,
                    sorted(blocked_operator_fields),
                )
                raise AccessError(_(
                    'Warehouse Operator hanya dapat mengubah field pada notebook '
                    'yang diizinkan; field dokumen lainnya hanya dapat dibaca.'
                ))

        result = super().write(vals)
        if import_audit_fields and not self.env.context.get('skip_acumatica_import_log'):
            staging_models = (
                'acumatica.receipt.line', 'acumatica.inventory.recieved.line',
                'acumatica.transfer.line', 'acumatica.shipment.line',
                'acumatica.transfer.shipment.line', 'acumatica.transfer.one.step.line',
                'acumatica.transfer.two.step.line', 'acumatica.direct.transfer.staging',
                'acumatica.vendor.return.direct.transfer.staging',
                'acumatica.credit.memo.consignment.staging',
                'acumatica.invoice.consignment.staging', 'acumatica.inventory.issue.line',
                'acumatica.issue.line', 'acumatica.issue.po.return.line',
                'acumatica.kit.assembly', 'acumatica.shipment.gi.staging',
                'acumatica.shipment.completed.staging',
                'acumatica.sales.order.shipment.staging',
            )
            for picking in self:
                staging = self.env['acumatica.import.log.mixin'].browse()
                for model_name in staging_models:
                    try:
                        model = self.env[model_name]
                    except KeyError:
                        continue
                    if model and 'odoo_picking_id' in model._fields:
                        staging = model.search([('odoo_picking_id', '=', picking.id)], limit=1)
                        if staging:
                            break
                if not staging or not hasattr(staging, '_acumatica_post_import_log'):
                    continue
                changed_fields = []
                for field_name in import_audit_fields:
                    old_value = import_audit_before[picking.id].get(field_name)
                    new_value = picking[field_name]
                    field = picking._fields[field_name]
                    if field.type == 'many2one':
                        old_value = old_value.display_name if old_value else False
                        new_value = new_value.display_name if new_value else False
                    elif field.type in ('many2many', 'one2many'):
                        old_value = ', '.join(old_value.mapped('display_name'))
                        new_value = ', '.join(new_value.mapped('display_name'))
                    if old_value != new_value:
                        changed_fields.append({
                            'field_name': field_name,
                            'field_label': field.string,
                            'old_value': old_value,
                            'new_value': new_value,
                        })
                if changed_fields:
                    staging._acumatica_post_import_log(
                        'updated',
                        'Data stock.picking diperbarui dari import transaksi.',
                        picking,
                        changed_fields,
                    )
        if (
            not self.env.context.get('skip_user_ids_sync')
            and 'user_id' in vals
            and 'user_ids' not in vals
        ):
            self._sync_user_ids_from_user_id()
        
        if self._get_responsible_carry_over_fields_set() & set(vals):
            self._propagate_responsible_to_next_transfers()
        if 'adt_is_document_only' not in vals and ({'origin', 'adt_type'} & set(vals)):
            self._set_acumatica_document_only_flag()
        if not self.env.context.get('skip_document_only_picking_type_sync') and (
            {'adt_is_document_only', 'picking_type_id', 'origin', 'adt_type'} & set(vals)
        ):
            self._set_document_only_picking_type()
        return result

    def unlink(self):
        if (
            not self.env.su
            and self.env.user.has_group('tijari_wms_18.group_operator')
            and not any(self.env.user.has_group(group) for group in (
                'tijari_wms_18.group_warehouse_manager',
                'tijari_wms_18.group_warehouse_head',
                'tijari_wms_18.group_warehouse_admin',
                'tijari_wms_18.group_it_staff',
                'stock.group_stock_manager',
                'base.group_system',
            ))
            and self.filtered(lambda picking: not picking.internal_transfer_order_id)
        ):
            raise AccessError(_('Warehouse Operator tidak dapat menghapus dokumen picking.'))
        return super().unlink()

    def _sync_user_ids_from_user_id(self):
        for picking in self.filtered(lambda record: record.user_id and record.user_id not in record.user_ids):
            picking.with_context(skip_user_ids_sync=True).write({
                'user_ids': [(4, picking.user_id.id)],
            })

    @api.model
    def _get_responsible_carry_over_fields_set(self):
        return set(self._get_responsible_carry_over_fields())

    def _propagate_responsible_to_next_transfers(self):
        """Carry pick/pack/do responsible + user_ids forward onto the next
        transfer(s) already created in the chain (e.g. Pick -> Pack -> DO),
        so users don't have to re-enter them there.

        Only fills fields that are still empty on the target picking - it
        never overwrites values a user (or an earlier propagation) already
        set on the downstream transfer.
        """
        fields_to_copy = self._get_responsible_carry_over_fields()
        for picking in self:
            next_transfers = picking._get_next_transfers()
            if not next_transfers:
                continue
            for target in next_transfers:
                vals = {}
                for field_name in fields_to_copy:
                    source_ids = picking[field_name].ids
                    if not source_ids or target[field_name]:
                        continue
                    vals[field_name] = [(6, 0, source_ids)]
                if vals:
                    target.with_context(tijari_responsible_sync=True).write(vals)

    def action_detailed_operations(self):
        action = super().action_detailed_operations()
        action.setdefault('context', {})
        action['context']['picking_type_name'] = self.picking_type_name
        return action

    @staticmethod
    def _document_only_value(value):
        if isinstance(value, bool):
            return value
        if value in (False, None, ''):
            return False
        return str(value).strip().lower() in ('true', '1', 'yes', 'y', 'document only')

    def _set_acumatica_document_only_flag(self):
        staging_specs = [
            ('acumatica.receipt.line', 'receipt_nbr', 'document_only'),
            ('acumatica.transfer.line', 'reference_nbr', 'document_only'),
            ('acumatica.receipt.change.item.line', 'reference_nbr', 'document_only'),
            ('acumatica.inventory.recieved.line', 'reference_nbr', 'document_only'),
            ('acumatica.shipment.line', 'shipment_nbr', 'document_only'),
            ('acumatica.shipment.gi.staging', 'adt_ShipmentNbr', 'adt_DocumentOnly'),
            ('acumatica.issue.line', 'reference_nbr', 'document_only'),
            ('acumatica.inventory.issue.line', 'reference_nbr', 'document_only'),
            ('acumatica.issue.po.return.line', 'reference_nbr', 'document_only'),
            ('acumatica.transfer.shipment.line', 'shipment_nbr', 'document_only'),
            ('acumatica.transfer.one.step.line', 'reference_nbr', 'document_only'),
            ('acumatica.transfer.two.step.line', 'reference_nbr', 'document_only'),
            ('acumatica.direct.transfer.staging', 'adt_ReferenceNbr', 'adt_DocumentOnly'),
            ('acumatica.vendor.return.direct.transfer.staging', 'adt_ReferenceNbr', 'adt_DocumentOnly'),
            ('acumatica.receipt.external.line', 'reference_nbr', 'document_only'),
            ('acumatica.toko.pegang.do.line', 'reference_nbr', 'document_only'),
        ]
        for picking in self:
            if picking.adt_is_document_only or not picking.origin:
                continue
            is_document_only = False
            for model_name, reference_field, document_field in staging_specs:
                if model_name not in self.env.registry.models:
                    continue
                Model = self.env[model_name].sudo()
                if reference_field not in Model._fields or document_field not in Model._fields:
                    continue
                staging = Model.search([(reference_field, '=', picking.origin)], limit=1)
                if staging and self._document_only_value(staging[document_field]):
                    is_document_only = True
                    break
            if is_document_only:
                picking.sudo().with_context(skip_document_only_sync=True).write({'adt_is_document_only': True})
        self._set_document_only_picking_type()

    def _get_document_only_operation(self):
        self.ensure_one()
        picking_type_code = self.picking_type_id.code
        if picking_type_code == 'incoming':
            return 'direct_receipt'
        if picking_type_code == 'outgoing':
            return 'direct_delivery_order'

        adt_type = (self.adt_type or '').strip().lower()
        if 'po return' in adt_type:
            return 'direct_delivery_order'
        if any(token in adt_type for token in ('receipt', 'transfer in', 'return')):
            return 'direct_receipt'
        if any(token in adt_type for token in ('issue', 'shipment', 'delivery', 'transfer out')):
            return 'direct_delivery_order'
        return False

    def _set_document_only_picking_type(self):
        PickingType = self.env['stock.picking.type'].sudo()
        for picking in self:
            if not picking.adt_is_document_only:
                continue
            if not picking.picking_type_id.is_document_only_api:
                operation = picking._get_document_only_operation()
                if operation:
                    warehouse = (
                        picking.picking_type_id.warehouse_id
                        or picking.location_dest_id.warehouse_id
                        or picking.location_id.warehouse_id
                    )
                    document_only_type = PickingType.resolve_document_only_picking_type(
                        target_operation_type=operation,
                        document_only=True,
                        warehouse_code=warehouse.code if warehouse else False,
                    )
                    if document_only_type and document_only_type != picking.picking_type_id:
                        picking.with_context(
                            skip_document_only_picking_type_sync=True,
                            skip_document_only_sync=True,
                        ).sudo().write({
                            'picking_type_id': document_only_type.id,
                        })
            picking._adt_preserve_document_only_destination()

    def _adt_get_document_only_destination(self):
        self.ensure_one()
        Location = self.env['stock.location'].sudo()
        warehouse = (
            self.picking_type_id.warehouse_id
            or self.location_id.warehouse_id
            or self.location_dest_id.warehouse_id
        )
        domain = [
            ('name', '=', 'Document Only'),
            ('is_document_only', '=', True),
            ('usage', '=', 'internal'),
        ]
        if warehouse:
            domain.append(('warehouse_id', '=', warehouse.id))
            location = Location.search(domain, order='id', limit=1)
        else:
            domain.extend([
                ('company_id', 'in', [self.company_id.id, False]),
            ])
            locations = Location.search(domain, order='id', limit=2)
            if len(locations) > 1:
                raise UserError(_(
                    'Picking %s tidak terhubung ke warehouse dan memiliki lebih '
                    'dari satu lokasi Document Only. Hubungkan operation type '
                    'ke warehouse agar lokasi tujuan dapat ditentukan.'
                ) % self.display_name)
            location = locations[:1]

        if not location:
            warehouse_name = warehouse.display_name if warehouse else self.company_id.display_name
            raise UserError(_(
                'Lokasi tujuan Document Only dengan flag is_document_only '
                'tidak ditemukan untuk %s.'
            ) % (warehouse_name or self.display_name))
        return location

    def _adt_preserve_document_only_destination(self):
        for picking in self:
            if not picking.adt_is_document_only:
                continue
            document_only_location = picking._adt_get_document_only_destination()
            if picking._is_document_only_outbound():
                # Inventory Issue is outbound: Document Only is the source.
                # Keep the configured destination so stock actually leaves
                # the Document Only location.
                picking.with_context(
                    skip_document_only_sync=True,
                    skip_document_only_picking_type_sync=True,
                ).sudo().write({
                    'location_id': document_only_location.id,
                })
                picking.move_ids.sudo().with_context(
                    skip_document_only_sync=True,
                ).write({
                    'location_id': document_only_location.id,
                })
                picking.move_line_ids.sudo().write({
                    'location_id': document_only_location.id,
                })
                continue
            picking.with_context(
                skip_document_only_sync=True,
                skip_document_only_picking_type_sync=True,
            ).sudo().write({
                'location_dest_id': document_only_location.id,
                'adt_dest_location_id': document_only_location.id,
            })
            picking.move_ids.sudo().with_context(
                skip_document_only_sync=True,
            ).write({
                'location_dest_id': document_only_location.id,
                'adt_dest_location_id': document_only_location.id,
            })
            picking.move_line_ids.sudo().write({
                'location_dest_id': document_only_location.id,
            })

    def _is_document_only_outbound(self):
        self.ensure_one()
        adt_type = (self.adt_type or '').strip().lower()
        return (
            self.picking_type_id.code == 'outgoing'
            or any(token in adt_type for token in (
                'issue', 'shipment', 'delivery', 'transfer out',
            ))
        )

    def _check_document_only_outbound_stock(self):
        self.ensure_one()
        if not self.adt_is_document_only or not self._is_document_only_outbound():
            return True

        source_location = self._adt_get_document_only_destination()
        Quant = self.env['stock.quant'].sudo()
        moves = self.move_ids.filtered(lambda move: move.state not in ('done', 'cancel'))
        for product in moves.mapped('product_id'):
            product_moves = moves.filtered(lambda move: move.product_id == product)
            required = sum(product_moves.mapped('product_qty'))
            available = Quant._get_available_quantity(
                product,
                source_location,
                strict=False,
            )
            # Quant availability excludes this picking's own reservations.
            # Count only unpicked reservation lines; automatic validation can
            # prefill picked quantities, which are not proof that stock exists.
            for line in product_moves.mapped('move_line_ids').filtered(
                lambda item: not item.picked and item.location_id == source_location
            ):
                available += line.product_uom_id._compute_quantity(
                    line.quantity,
                    product.uom_id,
                )
            if float_compare(
                available,
                required,
                precision_rounding=product.uom_id.rounding,
            ) < 0:
                raise UserError(_(
                    'Stok Document Only tidak cukup untuk outbound %s. '
                    'Produk %s: perlu %s %s, tersedia %s %s.'
                ) % (
                    self.display_name,
                    product.display_name,
                    format(required, '.2f'),
                    product.uom_id.display_name,
                    format(available, '.2f'),
                    product.uom_id.display_name,
                ))
        return True


    wms_state = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('waiting', 'Waiting Another Operation'),
            ('confirmed', 'Waiting'),
            ('assigned', 'Ready'),
            ('waiting_response', 'Waiting For Shipment'),
            ('waiting_approval', 'Waiting For Approval'),
            ('return_shipment', 'Returned Shipment'),
            ('done', 'Done'),
            ('cancel', 'Cancelled'),
        ],
        string="WMS State",
        compute="_compute_wms_state",
        store=True,
        copy=False,
        tracking=True,
    )
    wms_state_over_receipt = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('waiting', 'Waiting Another Operation'),
            ('confirmed', 'Waiting'),
            ('assigned', 'Ready'),
            ('waiting_response', 'Waiting For Approval'),
            ('waiting_approval', 'Waiting For Approval'),
            ('return_shipment', 'Returned Shipment'),
            ('done', 'Done'),
            ('cancel', 'Cancelled'),
        ],
        string='Over-Receipt State',
        compute='_compute_wms_state_over_receipt',
    )

    shipment_information = fields.Selection([
        ('partial', 'Partial Shipment'),
        ('full_shipment', 'Full Shipment'),
        # ('full_return', 'Canceled'),
    ], string='Informasi TMS', help='Status shipment yang akan dikirimkan dari TMS.')

    attachment_ids = fields.Many2many(
        'ir.attachment',
        'stock_picking_shipment_attachment_rel',
        'picking_id',
        'attachment_id',
        string='Shipment Attachments',
        copy=False,
    )

    status_ba = fields.Selection([
        ('conform', 'Confirmed'),
        ('discrepancy', 'Discrepancy'),],
    string='Status BA',
    )

    transaction_type = fields.Selection([
        ('inbound', 'Inbound'),
        ('outbound', 'Outbound'),
        ('internal', 'Internal'),
    ], string='Type transaksi', readonly=True)

    def _get_putaway_base_move_lines(self):
        self.ensure_one()

        return self.move_line_ids.filtered(
                lambda line: (
                    line.quantity > 0
                    and (line.lot_id or line.lot_name)
                )
            )

    def _get_putaway_moves(self):
        self.ensure_one()

        moves = self.move_ids_without_package.filtered(
            lambda move: move.state != 'cancel' and move.product_id
        )
        if self.putaway_product_id:
            moves = moves.filtered(
                lambda move: move.product_id == self.putaway_product_id
            )
        if self.putaway_lot_id:
            moves = moves.filtered(
                lambda move: any(
                    line.lot_id == self.putaway_lot_id
                    or line.lot_name == self.putaway_lot_id.name
                    for line in move.move_line_ids
                )
            )
        return moves.sorted(
            key=lambda move: (move.putaway_sequence, move._origin.id or 0)
        )

    def _get_putaway_move_lines(self):
        self.ensure_one()

        move_lines = self._get_putaway_base_move_lines()
        if self.putaway_product_id:
            move_lines = move_lines.filtered(
                lambda line: line.product_id == self.putaway_product_id and line.quantity > 0
            )
        if self.putaway_lot_id:
            move_lines = move_lines.filtered(
                lambda line: line.lot_id == self.putaway_lot_id or line.lot_name == self.putaway_lot_id.name
            )

        return move_lines.sorted(
            key=lambda line: (
                line.putaway_status != 'unfinished',
                line._origin.id or 0,
            )
        )

    def _get_putaway_move_lines_for_product(self, product):
        self.ensure_one()

        move_lines = self._get_putaway_base_move_lines()
        if product:
            move_lines = move_lines.filtered(
                lambda line: line.product_id == product and line.quantity > 0
            )
        return move_lines.sorted(
            key=lambda line: (
                line.putaway_status != 'unfinished',
                line._origin.id or 0,
            )
        )

    def _get_lots_from_move_lines(self, move_lines, products=False):
        lots = move_lines.lot_id
        lot_names = move_lines.filtered(
            lambda line: line.lot_name and not line.lot_id
        ).mapped('lot_name')
        if lot_names:
            domain = [('name', 'in', lot_names)]
            if products:
                domain.append(('product_id', 'in', products.ids))
            lots |= self.env['stock.lot'].search(domain)
        return lots

    def _get_putaway_lots(self):
        self.ensure_one()

        move_lines = self._get_putaway_base_move_lines()
        if self.putaway_product_id:
            move_lines = move_lines.filtered(
                lambda line: line.product_id == self.putaway_product_id
            )
        return self._get_lots_from_move_lines(move_lines, move_lines.product_id)

    def _get_picker_base_moves(self):
        self.ensure_one()

        return self.move_ids_without_package.filtered(
            lambda move: (
                move._origin.id
                and move.state != 'cancel'
                and move.product_id
            )
        )

    def _get_unpacked_base_moves(self):
        self.ensure_one()
        return self.move_ids_without_package

    @api.depends(
        'move_ids_without_package',
        'move_ids_without_package.product_id',
        'move_ids_without_package.state',
    )
    def _compute_allowed_unpacked_product_ids(self):
        for picking in self:
            picking.allowed_unpacked_product_ids = (
                picking._get_unpacked_base_moves().filtered('product_id').product_id
            )

    @api.depends(
        'move_ids_without_package',
        'move_ids_without_package.move_line_ids.lot_id',
    )
    def _compute_allowed_unpacked_lot_ids(self):
        for picking in self:
            picking.allowed_unpacked_lot_ids = (
                picking._get_unpacked_base_moves().mapped('move_line_ids.lot_id')
            )

    @api.depends(
        'unpacked_product_id',
        'unpacked_lot_id',
        'unpacked_lot_code',
        'move_ids_without_package',
        'move_ids_without_package.product_id',
        'move_ids_without_package.state',
        'move_ids_without_package.move_line_ids.lot_id',
        'move_ids_without_package.move_line_ids.lot_name',
    )
    def _compute_filtered_unpacked_move_ids(self):
        for picking in self:
            moves = picking._get_unpacked_base_moves()
            if picking.unpacked_product_id:
                moves = moves.filtered(lambda move: move.product_id == picking.unpacked_product_id)
            if picking.unpacked_lot_id or picking.unpacked_lot_code:
                lot_code = picking.unpacked_lot_code or picking.unpacked_lot_id.name
                moves = moves.filtered(lambda move: move.move_line_ids.filtered(
                    lambda line: (
                        (picking.unpacked_lot_id and line.lot_id == picking.unpacked_lot_id)
                        or (lot_code and line.lot_name == lot_code)
                    )
                ))
            picking.filtered_unpacked_move_ids = moves

    @api.onchange('unpacked_product_id')
    def _onchange_unpacked_product_id(self):
        for picking in self:
            if picking.unpacked_lot_id and picking.unpacked_lot_id.product_id != picking.unpacked_product_id:
                picking.unpacked_lot_id = False
                picking.unpacked_lot_code = False

    @api.onchange('unpacked_lot_id')
    def _onchange_unpacked_lot_id(self):
        for picking in self:
            if not picking.unpacked_lot_id:
                picking.unpacked_lot_code = False
                continue
            picking.unpacked_lot_code = picking.unpacked_lot_id.name
            lot_moves = picking._get_unpacked_base_moves().filtered(
                lambda move: picking.unpacked_lot_id in move.move_line_ids.lot_id
            )
            if lot_moves:
                picking.unpacked_product_id = lot_moves.product_id[:1]

    @api.onchange('unpacked_scan_code')
    def _onchange_unpacked_scan_code(self):
        scan_records = self.with_context(
            **self._get_operator_validation_workflow_context(),
        )
        for picking in scan_records:
            scanned_code = (picking.unpacked_scan_code or '').strip()
            if not scanned_code:
                continue

            product, matched_by = picking._find_product_scan_match(scanned_code)
            if product:
                if product.id not in picking.allowed_unpacked_product_ids.ids:
                    raise ValidationError(
                        _("Product hasil scan tidak ada di dokumen ini.")
                    )
                picking.unpacked_product_id = product
                product_lines = picking._get_unpacked_base_moves().filtered(
                    lambda move: move.product_id == product
                ).mapped('move_line_ids')
                document_lot = product_lines.mapped('lot_id')[:1]
                lot_codes = product_lines.mapped('lot_name')
                lot_code = document_lot.name if document_lot else False
                if not lot_code and lot_codes:
                    lot_code = lot_codes[0]
                picking.unpacked_lot_id = document_lot
                picking.unpacked_lot_code = lot_code
                picking.unpacked_scan_code = False
                continue

            matching_lines = picking._get_unpacked_base_moves().mapped('move_line_ids').filtered(
                lambda line: (
                    (line.lot_id and line.lot_id.name == scanned_code)
                    or line.lot_name == scanned_code
                )
            )
            if not matching_lines:
                raise ValidationError(
                    _("Kode %s tidak ditemukan sebagai Barcode, SKU, atau Lot.") % scanned_code
                )
            products = matching_lines.product_id
            if len(products) > 1:
                raise ValidationError(_("Lot hasil scan mengarah ke lebih dari satu product."))

            picking.unpacked_product_id = products[:1]
            picking.unpacked_lot_id = matching_lines.mapped('lot_id').filtered(
                lambda lot: lot.name == scanned_code
            )[:1]
            picking.unpacked_lot_code = scanned_code
            picking.unpacked_scan_code = False

    def _get_picker_moves(self):
        self.ensure_one()

        moves = self._get_picker_base_moves()
        if self.picker_product_id:
            moves = moves.filtered(
                lambda move: move.product_id == self.picker_product_id
            )

        return moves.sorted(
            key=lambda move: (
                move.pick_status_sequence,
                move._origin.id or 0,
            )
        )

    def _get_picker_lots(self):
        self.ensure_one()

        moves = self._get_picker_moves()
        lots = moves.lot_ids | moves.move_line_ids.lot_id

        lot_names = moves.move_line_ids.filtered(
            lambda line: line.lot_name and not line.lot_id
        ).mapped('lot_name')
        if lot_names:
            domain = [('name', 'in', lot_names)]
            products = moves.product_id
            if products:
                domain.append(('product_id', 'in', products.ids))
            lots |= self.env['stock.lot'].search(domain)

        return lots

    @api.depends(
        "state",
        "stock_lebih",
        "approval_request_id",
        "adt_type",
        "internal_transfer_order_id",
        "internal_transfer_order_id.transfer_type",
        "internal_transfer_leg",
        "picking_type_id",
        "picking_type_name",
        "transfer_shipment_discrepancy",
        "approval_state",
        "shipment_information",
    )
    def _compute_wms_state(self):
        for picking in self:
            is_waiting_over_receipt = bool(
                picking.stock_lebih
                and picking.approval_request_id
                and picking.approval_state != 'rejected'
                and picking.state in ('confirmed', 'assigned')
            )
            is_ready_transfer_in_receipt = bool(
                picking.internal_transfer_order_id
                and picking.internal_transfer_leg == "receipt"
                and picking.state in ("confirmed", "assigned")
            )
            is_waiting_shipment = (
                picking.adt_type in ("Sales Order Shipment", "SO Shipment")
                or (
                    picking.internal_transfer_order_id
                    and picking.adt_type in (
                        "Shipment Consignment",
                        "Transfer Shipment",
                    )
                    and picking.internal_transfer_leg == "delivery"
                    and picking.picking_type_id.code == "internal"
                )
            )
            is_transfer_shipment_discrepancy = bool(
                picking.internal_transfer_order_id
                and picking.internal_transfer_leg == "delivery"
                and picking.picking_type_id.code == "internal"
                and picking.adt_type == "Transfer Shipment"
                and picking.transfer_shipment_discrepancy
                and picking.approval_state != "approved"
            )

            if is_transfer_shipment_discrepancy:
                picking.wms_state = "waiting_approval"
            elif is_waiting_over_receipt:
                picking.wms_state = 'waiting_response'
            elif is_ready_transfer_in_receipt:
                # A transfer-in receipt is ready to process after Check
                # Availability, even when its zero-qty working lines cannot
                # reserve transit stock like a vendor receipt. Do not prefill
                # the actual received quantity here.
                picking.wms_state = "assigned"
            elif not is_waiting_shipment or picking.picking_type_name not in ["Delivery Orders", "Internal Transfer"]:
                picking.wms_state = picking.state
            elif is_waiting_shipment and not picking.shipment_information:
                picking.wms_state = "assigned"       

    @api.depends('wms_state')
    def _compute_wms_state_over_receipt(self):
        for picking in self:
            picking.wms_state_over_receipt = picking.wms_state

    @api.depends(
        'move_line_ids',
        'move_line_ids.product_id',
        'move_line_ids.lot_id',
        'move_line_ids.quantity',
        'move_line_ids.confirmation_quantity',
        'move_line_ids.picker_status',
    )
    def _compute_allowed_putaway_product_ids(self):
        for picking in self:
            picking.allowed_putaway_product_ids = picking._get_putaway_base_move_lines().product_id

    @api.depends(
        'putaway_product_id',
        'putaway_lot_id',
        'move_line_ids',
        'move_line_ids.product_id',
        'move_line_ids.lot_id',
        'move_line_ids.lot_name',
        'move_line_ids.quantity',
        'move_line_ids.confirmation_quantity',
        'move_line_ids.picker_status',
    )
    def _compute_allowed_putaway_lot_ids(self):
        for picking in self:
            picking.allowed_putaway_lot_ids = picking._get_putaway_lots()

    @api.depends(
        'move_ids_without_package',
        'move_ids_without_package.product_id',
        'move_ids_without_package.state',
    )
    def _compute_allowed_picker_product_ids(self):
        for picking in self:
            picking.allowed_picker_product_ids = picking._get_picker_base_moves().product_id

    @api.depends(
        'picker_product_id',
        'move_ids_without_package',
        'move_ids_without_package.product_id',
        'move_ids_without_package.state',
        'move_ids_without_package.lot_ids',
        'move_line_ids.lot_id',
        'move_line_ids.lot_name',
    )
    def _compute_allowed_picker_lot_ids(self):
        for picking in self:
            picking.allowed_picker_lot_ids = picking._get_picker_lots()

    @api.depends(
        'picker_product_id',
        'picker_lot_id',
        'move_ids_without_package',
        'move_ids_without_package.product_uom_qty',
        'move_ids_without_package.quantity',
        'move_ids_without_package.product_uom',
        'move_ids_without_package.state',
        'move_ids_without_package.move_line_ids.picker_status',
        'move_ids_without_package.move_line_ids.quantity',
        'move_ids_without_package.move_line_ids.state',
    )
    def _compute_picker_progress_status(self):
        for picking in self:
            moves = picking._get_picker_moves()
            demand_qty = sum(moves.mapped('product_uom_qty'))
            picked_qty = sum(moves.mapped('quantity'))
            rounding = moves[:1].product_uom.rounding if moves else 0.01
            move_lines = moves.mapped('move_line_ids').filtered(
                lambda line: line.state != 'cancel' and line.quantity > 0
            )
            all_lines_finished = bool(move_lines) and all(
                line.picker_status == 'finished'
                for line in move_lines
            )
            picking.picker_progress_status = (
                'finished'
                if moves
                and all_lines_finished
                and float_compare(picked_qty, demand_qty, precision_rounding=rounding) == 0
                else 'unfinished'
            )

    @api.depends(
        'picker_product_id',
        'picker_lot_id',
        'move_ids_without_package',
        'move_ids_without_package.product_id',
        'move_ids_without_package.quantity',
        'move_ids_without_package.state',
    )
    def _compute_picker_qty_finish(self):
        for picking in self:
            picking.picker_qty_finish = sum(
                picking._get_picker_moves().mapped('quantity')
            )

    @api.depends(
        'putaway_product_id',
        'putaway_lot_id',
        'move_line_ids',
        'move_line_ids.product_id',
        'move_line_ids.lot_id',
        'move_line_ids.putaway_status',
        'move_line_ids.picker_status',
    )
    def _compute_putaway_values(self):
        for picking in self:
            move_lines = picking._get_putaway_move_lines()
            picking.putaway_move_line_ids = move_lines

    @api.depends(
        'putaway_product_id',
        'putaway_lot_id',
        'move_ids_without_package',
        'move_ids_without_package.product_id',
        'move_ids_without_package.state',
        'move_line_ids',
        'move_line_ids.move_id',
        'move_line_ids.product_id',
        'move_line_ids.lot_id',
        'move_line_ids.lot_name',
        'move_line_ids.quantity',
        'move_line_ids.confirmation_quantity',
        'move_line_ids.picker_status',
        'move_line_ids.state',
    )
    def _compute_putaway_move_ids(self):
        for picking in self:
            picking.putaway_move_ids = picking._get_putaway_moves()

    @api.depends(
        'putaway_product_id',
        'move_line_ids',
        'move_line_ids.product_id',
        'move_line_ids.quantity',
        'move_line_ids.confirmation_quantity',
        'move_line_ids.putaway_status',
        'move_line_ids.picker_status',
        'move_line_ids.product_uom_id',
    )
    def _compute_putaway_progress_status(self):
        for picking in self:
            move_lines = picking._get_putaway_move_lines()
            picked_qty = sum(move_lines.mapped('quantity'))
            confirmation_qty = sum(move_lines.mapped('confirmation_quantity'))
            rounding = move_lines[:1].product_uom_id.rounding if move_lines else 0.01
            all_lines_finished = bool(move_lines) and all(
                line.putaway_status == 'finished'
                for line in move_lines
            )
            picking.putaway_progress_status = (
                'finished'
                if all_lines_finished
                and float_compare(confirmation_qty, picked_qty, precision_rounding=rounding) == 0
                else 'unfinished'
            )

    @api.depends(
        'putaway_product_id',
        'putaway_lot_id',
        'move_line_ids',
        'move_line_ids.product_id',
        'move_line_ids.lot_id',
        'move_line_ids.lot_name',
        'move_line_ids.quantity',
        'move_line_ids.confirmation_quantity',
        'move_line_ids.state',
    )
    def _compute_putaway_qty_finish(self):
        for picking in self:
            picking.putaway_qty_finish = sum(
                picking._get_putaway_move_lines().mapped('confirmation_quantity')
            )

    @api.depends(
        'picker_product_id',
        'picker_lot_id',
        'move_ids_without_package',
        'move_ids_without_package.product_id',
        'move_ids_without_package.state',
    )
    def _compute_picker_values(self):
        for picking in self:
            picking.picker_move_ids = picking._get_picker_moves()

    @api.depends(
        'picker_product_id',
        'move_ids_without_package',
        'move_ids_without_package.move_line_ids.pick_status',
    )
    def _compute_filtered_move_ids(self):
        for picking in self:
            picking.filtered_move_ids = picking._get_picker_moves()

    @api.depends(
        'picker_product_id',
        'move_ids_without_package',
        'move_ids_without_package.state',
        'move_ids_without_package.product_id',
        'move_ids_without_package.move_line_ids.pick_status',
    )
    def _compute_filtered_picker_move_ids(self):
        for picking in self:
            picking.filtered_picker_move_ids = picking._get_picker_moves()



    @api.onchange('putaway_product_id')
    def _onchange_putaway_product_id(self):
        for picking in self:
            move_lines = picking._get_putaway_move_lines_for_product(picking.putaway_product_id)
            picking.putaway_move_line_ids = move_lines
            picking.putaway_lot_id = (
                move_lines.mapped('lot_id')[:1]
                if picking.putaway_product_id
                else False
            )

    @api.onchange('putaway_lot_id')
    def _onchange_putaway_lot_id(self):
        for picking in self:
            if not picking.putaway_lot_id:
                picking.putaway_product_id = False
                picking.putaway_move_line_ids = picking._get_putaway_move_lines()
                continue
            move_lines = picking._get_putaway_base_move_lines().filtered(
                lambda line: (
                    line.lot_id == picking.putaway_lot_id
                    or line.lot_name == picking.putaway_lot_id.name
                )
            )
            products = move_lines.product_id
            if products:
                picking.putaway_product_id = products[:1]
            picking.putaway_move_line_ids = picking._get_putaway_move_lines()

    @api.onchange('picker_product_id')
    def _onchange_picker_product_id(self):
        for picking in self:
            picking.picker_move_ids = picking._get_picker_moves()
            picking.picker_lot_id = picking._get_picker_lots()[:1] if picking.picker_product_id else False

    @api.onchange('picker_lot_id')
    def _onchange_picker_lot_id(self):
        for picking in self:
            if not picking.picker_lot_id:
                picking.picker_product_id = False
                picking.picker_move_ids = picking._get_picker_moves()
                continue
            moves = picking._get_picker_base_moves().filtered(
                lambda move: (
                    picking.picker_lot_id in move.lot_ids
                    or picking.picker_lot_id in move.move_line_ids.lot_id
                    or picking.picker_lot_id.name in move.move_line_ids.mapped('lot_name')
                )
            )
            products = moves.product_id
            if products:
                picking.picker_product_id = products[:1]
            picking.picker_move_ids = picking._get_picker_moves()

    def _set_document_only_done_quantities(self):
        MoveLine = self.env['stock.move.line'].sudo()
        for picking in self:
            for move in picking.move_ids.filtered(lambda line: line.state not in ('done', 'cancel')):
                quantity = move.product_uom_qty
                if not quantity:
                    continue

                move_lines = move.move_line_ids
                quantity_field = 'quantity' if 'quantity' in MoveLine._fields else 'qty_done'
                # move.write({
                #     'quantity': quantity,
                # })
                if move_lines:
                    done_lines = move_lines.filtered(lambda line: line[quantity_field] > 0)
                    if not done_lines:
                        done_lines = move_lines[-1]
                        done_lines.write({quantity_field: quantity})

                    for line in done_lines:
                        line_vals = {'picked': True}
                        if (
                            move.product_id.tracking != 'none'
                            and not line.lot_id
                            and not line.lot_name
                        ):
                            line_vals['lot_name'] = picking.origin or picking.name or 'DOCUMENT ONLY'
                        line.write(line_vals)
                else:
                    move_line_vals = {
                        'picking_id': picking.id,
                        'move_id': move.id,
                        'product_id': move.product_id.id,
                        'product_uom_id': move.product_uom.id,
                        'location_id': move.location_id.id,
                        'location_dest_id': move.location_dest_id.id,
                        quantity_field: quantity,
                        'picked': True,
                    }
                    if move.product_id.tracking != 'none':
                        move_line_vals['lot_name'] = picking.origin or picking.name or 'DOCUMENT ONLY'
                    MoveLine.create(move_line_vals)

                move.move_line_ids.flush_recordset([quantity_field, 'picked'])
                move.flush_recordset(['quantity', 'picked'])
                self.env.cr.execute(
                    "UPDATE stock_move_line SET picked = TRUE WHERE move_id = %s",
                    [move.id],
                )
                self.env.cr.execute(
                    "UPDATE stock_move SET picked = TRUE WHERE id = %s",
                    [move.id],
                )
                move.move_line_ids.invalidate_recordset(['picked'])
                move.invalidate_recordset(['picked'])

    def _process_document_only_validation_action(self, action):
        if not isinstance(action, dict):
            return

        model_name = action.get('res_model')
        context = action.get('context') or {}
        if model_name == 'stock.immediate.transfer':
            wizard = self.env[model_name].with_context(context).create({})
            wizard.process()
        elif model_name == 'stock.backorder.confirmation':
            wizard = self.env[model_name].with_context(context).create({})
            wizard.process_cancel_backorder()

    def action_validate_document_only_import(self, force=False):
        for picking in self:
            if force and not picking.adt_is_document_only:
                picking.sudo().write({'adt_is_document_only': True})
            if not picking.adt_is_document_only or picking.state in ('done', 'cancel'):
                continue
            if not picking.move_ids:
                raise UserError(_("Document only picking %s has no lines to validate.") % picking.display_name)

            picking._adt_preserve_document_only_destination()
            picking.action_confirm()
            picking.action_assign()
            picking.button_validate()
            picking._adt_preserve_document_only_destination()
            # picking._set_document_only_done_quantities()
            # action = picking.with_context().button_validate()
            # picking._process_document_only_validation_action(action)
        return True


    def _create_backorder_picking(self):
        """Mark the picking copy as part of Odoo's backorder workflow.

        Backorders are created by copying an existing picking during validation;
        they must pass the API/manual-document guard without opening general
        stock.picking creation to the acting user.
        """
        self.ensure_one()
        return super(StockPicking, self.with_context(
            _tijari_backorder_create_token=_BACKORDER_CREATE_TOKEN,
        ))._create_backorder_picking()


# Function to foward adt_type from parent picking
    @api.model_create_multi
    def create(self, vals_list):
        is_backorder_workflow_create = (
            self.env.context.get('_tijari_backorder_create_token')
            is _BACKORDER_CREATE_TOKEN
        )
        if (
            not self.env.su
            and not self.env.user.has_group('base.group_system')
            and not is_backorder_workflow_create
        ):
            can_create_over_receipt = any(self.env.user.has_group(group_xmlid) for group_xmlid in (
                'tijari_wms_18.group_warehouse_manager',
                'tijari_wms_18.group_warehouse_head',
                'tijari_wms_18.group_warehouse_admin',
                'tijari_wms_18.group_it_staff',
                'stock.group_stock_manager',
            ))
            default_vals = self.default_get(['stock_lebih', 'picking_type_id'])
            for vals in vals_list:
                is_over_receipt = vals.get(
                    'stock_lebih',
                    self.env.context.get('default_stock_lebih', default_vals.get('stock_lebih')),
                )
                picking_type_id = vals.get(
                    'picking_type_id',
                    self.env.context.get('default_picking_type_id', default_vals.get('picking_type_id')),
                )
                picking_type = self.env['stock.picking.type'].browse(picking_type_id).exists()
                if not (
                    can_create_over_receipt
                    and is_over_receipt
                    and picking_type
                    and picking_type.code == 'incoming'
                    and picking_type.operation_category == 'receive'
                ):
                    raise AccessError(_(
                        'Stock picking hanya dapat dibuat melalui API Data Hub. '
                        'Pengecualian manual hanya berlaku untuk dokumen Over-Receipt '
                        'dan Internal Transfer Order yang dibuat melalui alurnya.'
                    ))
        # print("=" * 100)
        # print("CREATE PICKING")
        # traceback.print_stack(limit=15)        
        print('=' * 100)
        print("@api.model_create_multi")

        print(f"Self: {self}")
        print(f"Vals List: {vals_list}")

        # Karena vals_list adalah sebuah LIST, kita harus melakukan looping
        # for vals in vals_list:
        #     # Ambil nilai 'origin' dengan aman dari dictionary
        #     origin = vals.get('origin')
            
        #     if origin:
        #         # Perbaikan pencarian: menggunakan titik 'stock.picking' dan format domain yang benar
        #         parent_picking = self.env['stock.picking'].search([('origin', '=', origin)], limit=1)
                
        #         # Jika parent_picking ditemukan, salin nilai adt_type ke data baru
        #         if parent_picking and parent_picking.adt_type:
        #             vals['adt_type'] = parent_picking.adt_type

        # # Panggil fungsi super() dengan melewatkan vals_list yang sudah diperbarui
        pickings = super(
            StockPicking,
            self.with_context(mail_create_nolog=True)
        ).create(vals_list)
        for picking, vals in zip(pickings, vals_list):
            description = picking.adt_type or ""
            if 'user_ids' not in vals:
                picking._sync_user_ids_from_user_id()
            
            if self._get_responsible_carry_over_fields_set() & set(vals):
                picking._propagate_responsible_to_next_transfers()

            picking.with_context(
                mail_create_nosubscribe=True,
                mail_post_autofollow=False,
            ).message_post(
                body=f"{description} created",
                message_type='notification',
                subtype_xmlid='mail.mt_note',
            )
            
        pickings._set_acumatica_document_only_flag()
        pickings._set_document_only_picking_type()
        print(pickings)
        return pickings


    def action_put_in_pack(self, move_lines_to_pack=False):
        self.ensure_one()

        if self.env.context.get('sml_specific_default'):
            self = self.with_context(
                clean_context(self.env.context)
            )

        if self.state in ('done', 'cancel'):
            return super().action_put_in_pack(
                move_lines_to_pack=move_lines_to_pack
            )

        if (
            self.picking_type_id.name in (
                'Receipts', 'Receipt Internal', 'Storage', 'Store Finished Product'
            )
            and not self.is_ito_internal_transfer
        ):
            self._check_putaway_status_requirements()

        move_line_ids = self._package_move_lines(
            move_lines_to_pack=move_lines_to_pack
        )

        if not move_line_ids:
            raise UserError((
                "There is nothing eligible to put in a pack."
            ))

        # packages = self.env['stock.quant.package']
        # for move in move_line_ids.mapped('move_id'):

        #     lines = move_line_ids.filtered(
        #         lambda ml: ml.move_id == move
        #     )

        #     package = self._put_in_pack(lines)

        #     packages |= package

        # return True

        move_line_ids = move_line_ids.filtered(
            lambda line:
                line.quantity > 0
                and not line.result_package_id
        )

        if not move_line_ids:
            raise UserError(
                _("There is nothing eligible to put in a pack.")
            )

        # Group move line berdasarkan destination location.
        packages = self.env["stock.quant.package"]

        # Satu move line diproses satu per satu.
        for move_line in move_line_ids:
            package = self._put_in_pack(move_line)
            packages |= package

        return packages[-1] if packages else False
    
    def _put_in_pack(self, move_line_ids):
        """
        Create a package and package level for move lines that have
        the same destination location.

        Custom behavior:
        - Preserve the existing `location_dest_id`.
        - Do not recalculate putaway strategy during packing.
        """
        self.ensure_one()

        if not move_line_ids.location_dest_id:
            raise UserError(
                (
                    "Destination location is missing for product %s."
                ) % move_line_ids.product_id.display_name
            )

        package = self._get_put_in_pack_package()

        package_type = (
            move_line_ids.move_id
            .product_packaging_id
            .package_type_id
        )

        if package_type:
            package.package_type_id = package_type

        move_line_ids.write({
            "result_package_id": package.id,
        })
        # Buat package level untuk package tersebut.
        self.env["stock.package_level"].with_context(
            from_put_in_pack=True
        ).create({
            "package_id": package.id,
            "picking_id": self.id,
            "location_id": False,
            "location_dest_id": move_line_ids.location_dest_id.id,
            "move_line_ids": [
                (6, 0, move_line_ids.ids)
            ],
            "company_id": self.company_id.id,
        })

        return package

    def _get_put_in_pack_package(self):
        return self.env['stock.quant.package'].create({
            'package_cat': 'palet',
        })

    def _check_sync_state(self):
        for picking in self:
            if picking.sync_state == 'not_ready':
                raise UserError(
                    f'Picking {picking.name} tidak bisa diproses karena data tidak lengkap: {picking.sync_message}'
                )

    # def action_confirm(self):
    #     self._check_sync_state()
    #     return super().action_confirm()
    
    def action_confirm(self):
        _logger.info(">>> action_confirm CALLED %s", self.ids)
        # for picking in self:
        #     if (
        #         picking.picking_type_name == 'Internal Transfers'
        #         and picking.location_id
        #         and picking.location_dest_id
        #         and picking.location_id == picking.location_dest_id
        #     ):
        #         raise ValidationError(
        #             _("Source Location dan Destination Location tidak boleh sama.")
        #         )

        marked_as_todo = self.filtered(lambda picking: picking.state == 'draft')
        over_receipt_moves = marked_as_todo.filtered(
            lambda picking: picking.stock_lebih
        ).mapped('move_ids').filtered(lambda move: move.product_uom_qty <= 0)
        if over_receipt_moves:
            raise ValidationError(_(
                "Product quantity must be greater than zero before marking an Over Receipt as To Do."
            ))
        res = super().action_confirm()

        # Manufacturing component moves already carry the authoritative
        # mapped destination. Copy that exact value to the picking after the
        # moves have been assigned to it; do not derive it from picking type
        # defaults or from location_dest_id.
        for picking in self:
            component_locations = picking.move_ids.filtered(
                lambda move: move.raw_material_production_id
                and move.adt_dest_location_id
            ).mapped('adt_dest_location_id')
            if len(component_locations) == 1:
                picking.adt_dest_location_id = component_locations

        self._adt_apply_dest_location_to_move_lines()
        self.filtered(
            lambda picking: picking.picking_type_id.name in ('Receipts', 'Storage', 'Store Finished Product')
        )._remove_qty_move_line()

        # Transfer IN: ketika receipt di-Mark as Todo, otomatis
        # proses leg delivery yang menjadi pasangan transfer-nya.
        transfer_in_receipts = marked_as_todo.filtered(
            lambda picking: (
                picking.state in ('confirmed', 'waiting', 'assigned')
                and picking.internal_transfer_order_id
                and picking.internal_transfer_order_id.transfer_type == 'transfer_in'
                and picking.internal_transfer_leg == 'receipt'
            )
        )
        for receipt in transfer_in_receipts:
            receipt.internal_transfer_order_id.action_auto_validate_transfer_in_delivery()
            active_moves = receipt.move_ids.filtered(
                lambda move: move.state not in ('done', 'cancel')
            )
            active_moves._recompute_state()
            if active_moves:
                receipt.action_assign()
        
        for picking in self:
                    for move in picking.move_ids:
                        for line in move.move_line_ids:
                            print(line)
                            if line.lot_id:
                                print(line.lot_id)
                                print(f"write source_location: {line.lot_id.location_id.id} product_lot_id: {line.lot_id.id,}")
                                line.write({
                                    'source_location_id': line.lot_id.location_id.id or line.location_id.id,
                                    'product_lot_id': line.lot_id.id,
                                })        

        return res

    def _adt_apply_dest_location_to_move_lines(self):
        for picking in self:
            if not picking.adt_dest_location_id:
                continue

            for move in picking.move_ids_without_package:
                final_dest_location = move._adt_get_final_dest_location()
                if not final_dest_location:
                    continue

                if not move.adt_dest_location_id:
                    move.location_dest_id = picking.adt_dest_location_id
                    move.adt_dest_location_id = picking.adt_dest_location_id

                if move.move_line_ids:
                    move.move_line_ids.write({
                        'location_dest_id': final_dest_location.id,
                    })

    def _remove_qty_move_line(self):
        # for move in self.move_ids:
        #     move.action_generate_lot()
        #     for line in move.move_line_ids:
        #         line.quantity = 0
        for move in self.move_line_ids:
            move.quantity = 0

    adt_dest_location_id = fields.Many2one(
        "stock.location",
        domain="[('usage', '=', 'internal')]",
        string="Mapped Location",
    )
    # Compatibility for database custom views that still reference the former
    # Internal Transfer Final Location field. It no longer drives locations.
    internal_transfer_adt_dest_location_id = fields.Many2one(
        "stock.location",
        string="Mapped Location (Legacy)",
        readonly=True,
        copy=False,
    )
    internal_transfer_destination_location_id = fields.Many2one(
        'stock.location',
        string='Lokasi Tujuan',
        related='internal_transfer_order_id.location_dest_id',
        readonly=True,
    )
    show_internal_transfer_destination_location = fields.Boolean(
        compute='_compute_show_internal_transfer_destination_location',
    )
    note_ba = fields.Text(string="Notes For Official Report")
    desc_note = fields.Char(string="Description")
    picking_type_name = fields.Char(string="Picking Type Name", related="picking_type_id.name", store=True)

    @api.depends(
        'internal_transfer_order_id',
        'internal_transfer_order_id.transfer_type',
        'internal_transfer_order_id.transaction_type',
        'internal_transfer_order_id.adt_transaction_type',
        'internal_transfer_order_id.location_id',
        'internal_transfer_order_id.location_dest_id',
        'internal_transfer_order_id.move_ids.adt_type',
        'internal_transfer_leg',
        'picking_type_id.code',
    )
    def _compute_show_internal_transfer_destination_location(self):
        for picking in self:
            order = picking.internal_transfer_order_id
            if not order:
                picking.show_internal_transfer_destination_location = False
                continue

            transaction_api = (
                (order.adt_transaction_type or '')
                .strip()
                .lower()
                .replace('_', ' ')
            )
            is_one_step_same_location = (
                order.transaction_type == 'intra_warehouse'
                and order.location_id == order.location_dest_id
                and (
                    transaction_api in ('1-step', '1 step', 'transfer one step')
                    or any(
                        'transfer one step' in (move.adt_type or '').strip().lower()
                        for move in order.move_ids
                    )
                )
            )
            auto_validated = (
                (order.transfer_type == 'transfer_in'
                 and picking.internal_transfer_leg == 'delivery')
                or (order.transfer_type == 'transfer_out'
                    and picking.internal_transfer_leg == 'receipt')
                or (
                    picking.internal_transfer_leg == 'receipt'
                    and order.adt_transaction_type in (
                        'Shipment Consignment', 'Transfer Shipment',
                    )
                    and picking.picking_type_id.code == 'internal'
                )
                or (
                    is_one_step_same_location
                    and picking.internal_transfer_leg in ('delivery', 'receipt')
                )
            )
            picking.show_internal_transfer_destination_location = not auto_validated

    def _allow_custom_push_destination(self):
        self.ensure_one()
        # Untuk picking type incomine, kalo dibutuhkan untuk internal, manufacturing, dan lain lain bisa ditambahkan "self.picking_type_id.code in ["incoming", "internal", "manufacturing"]" atau yang lain sesuai kebutuhan
        return self.picking_type_id.code == "incoming"

    def action_send_email_hasil_picking(self):
        self.ensure_one()

        # partner = self.partner_id
        # if not partner or not partner.email:
        #     raise ValidationError('Partner belum memiliki email.')

        # Generate PDF dari report Odoo, lebih baik daripada panggil controller langsung
        controller = WmsApiController()
        response = controller.PrintWarehouseReceipt(id=self.id)

        if response.status_code != 200:
            raise UserError(f"Gagal mendapatkan PDF. Kode status: {response.status_code}")

        pdf_content = response.data
        pdf_base64 = base64.b64encode(pdf_content)

        attachment_new = self.env['ir.attachment'].sudo().create({
            'name': f"{self.origin or self.name}.pdf",
            'datas': pdf_base64.decode('utf-8'),
            'res_model': 'stock.picking',
            'res_id': self.id,
            'type': 'binary',
            'mimetype': 'application/pdf',
        })

        template = self.env.ref(
            'tijari_wms_18.email_template_stock_picking_custom',
            raise_if_not_found=False
        )

        return {
            'name': 'Kirim Email Berita Acara',
            'type': 'ir.actions.act_window',
            'res_model': 'mail.compose.message',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_model': 'stock.picking',
                'default_res_ids': [self.id],
                'default_use_template': bool(template),
                'default_template_id': template.id if template else False,
                'default_composition_mode': 'comment',
                'default_attachment_ids': [(6, 0, [attachment_new.id])],
                'tijari_hasil_picking_email': True,
            },
        }

    def action_print_warehouse_receipt(self):
        action = {}
        action['type'] = 'ir.actions.act_url'
        action['url'] = '/print/warehouse_receipt?id=' + str(self.id)
        action['target'] = 'new'
        return action

    def action_print_warehouse_pick(self):
        action = {}
        action['type'] = 'ir.actions.act_url'
        action['url'] = '/print/warehouse_pick?id=' + str(self.id)
        action['target'] = 'new'
        return action

    def action_print_warehouse_pack(self):
        action = {}
        action['type'] = 'ir.actions.act_url'
        action['url'] = '/print/warehouse_pack?id=' + str(self.id)
        action['target'] = 'new'
        return action

    def delivery_slip_pack(self):
        action = {}
        action['type'] = 'ir.actions.act_url'
        action['url'] = '/print/delivery_slip_pack_?id=' + str(self.id)
        action['target'] = 'new'
        return action

    def action_print_warehouse_transfer(self):
        action = {}
        action['type'] = 'ir.actions.act_url'
        action['url'] = '/print/warehouse_transfer?id=' + str(self.id)
        action['target'] = 'new'
        return action

    def do_print_picking(self):
        action = {}
        action['type'] = 'ir.actions.act_url'
        action['url'] = '/print/warehouse_receipt?id=' + str(self.id)
        action['target'] = 'new'
        return action

    def action_open_stock_move_scan_wizard(self):
        self.ensure_one()

        wizard = self.env['stock.move.scan.wizard'].create({
            'picking_id': self.id,
        })

        return {
            'type': 'ir.actions.act_window',
            'name': ('Scan Product'),
            'res_model': 'stock.move.scan.wizard',
            'view_mode': 'form',
            'res_id': wizard.id,
            'target': 'new',
        }

    @api.depends('move_ids.picked')
    def _compute_all_moves_picked(self):
        for picking in self:
            moves = picking.move_ids
            picking.all_moves_picked = (
                bool(moves) and all(move.picked for move in moves)
            )

    def action_prepare_scan_product(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'adt_scan_qr_to_picking_product',
            'params': {
                'picking_id': self.id,
                'title': 'Scan Product QR Code',
            },
        }

    def action_prepare_scan_package(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'adt_scan_qr_to_picking_package',
            'params': {
                'picking_id': self.id,
                'title': 'Scan Package QR Code',
            },
        }

    @api.model
    def action_open_by_scanned_origin(self, scanned_code):
        origin = (scanned_code or '').strip()
        if not origin:
            raise ValidationError(_("Origin hasil scan kosong."))

        pickings = self.search([('origin', '=', origin)], order='id desc', limit=1)
        if not pickings:
            raise ValidationError(_("Stock Picking tidak ditemukan untuk nomor dokumen: %s") % origin)

        action = {
            'type': 'ir.actions.act_window',
            'name': _('Stock Picking: %s') % origin,
            'res_model': 'stock.picking',
            'view_mode': 'list,form',
            'domain': [('id', 'in', pickings.ids)],
            'context': {
                'search_default_origin': origin,
            },
        }
        if len(pickings) == 1:
            action.update({
                'view_mode': 'form',
                'views': [(False, 'form')],
                'res_id': pickings.id,
            })
        return action

    def _find_product_by_scan_code(self, scan_code):
        product, _matched_by = self._find_product_scan_match(scan_code)
        return product

    def _find_product_scan_match(self, scan_code):
        Product = self.env['product.product']
        scan_code = (scan_code or '').strip()

        product = Product.search([('barcode', '=', scan_code)], limit=1)
        if product:
            return product, _('Barcode')

        product = Product.search([('default_code', '=', scan_code)], limit=1)
        if product:
            return product, _('SKU')

        return Product.browse(), False

    def _find_putaway_product_by_lot_scan_code(self, scan_code):
        self.ensure_one()
        StockLot = self.env['stock.lot']

        lots = StockLot.search([('name', '=', scan_code)])
        move_lines = self._get_putaway_base_move_lines().filtered(
            lambda line: (
                (lots and line.lot_id in lots)
                or (line.lot_name and line.lot_name == scan_code)
            )
        )

        if not move_lines:
            if lots:
                raise ValidationError(
                    _("Lot %s ditemukan, tetapi tidak ada di dokumen ini.")
                    % scan_code
                )
            raise ValidationError(
                _("Kode %s tidak ditemukan sebagai Barcode, SKU, atau Lot.")
                % scan_code
            )

        products = move_lines.product_id
        if len(products) > 1:
            raise ValidationError(_("Lot hasil scan mengarah ke lebih dari satu product."))

        return products[:1], move_lines, lots[:1]

    def _find_picker_product_by_lot_scan_code(self, scan_code):
        self.ensure_one()
        StockLot = self.env['stock.lot']

        lots = StockLot.search([('name', '=', scan_code)])
        moves = self._get_picker_base_moves().filtered(
            lambda move: (
                (lots and move.lot_ids and bool(move.lot_ids & lots))
                or move.move_line_ids.filtered(
                    lambda line: (
                        (lots and line.lot_id in lots)
                        or (line.lot_name and line.lot_name == scan_code)
                    )
                )
            )
        )

        if not moves:
            if lots:
                raise ValidationError(
                    _("Lot %s ditemukan, tetapi tidak ada di dokumen ini.")
                    % scan_code
                )
            raise ValidationError(
                _("Kode %s tidak ditemukan sebagai Barcode, SKU, atau Lot.")
                % scan_code
            )

        products = moves.product_id
        if len(products) > 1:
            raise ValidationError(_("Lot hasil scan mengarah ke lebih dari satu product."))

        return products[:1], moves, lots[:1]

    def action_apply_scanned_product(self, scanned_code):
        self.ensure_one()
        scanned_code = (scanned_code or '').strip()
        product = self._find_product_by_scan_code(scanned_code)

        if not product:
            raise ValidationError("Product tidak ditemukan untuk kode scan: %s" % scanned_code)

        move_lines = self._get_putaway_move_lines_for_product(product)
        self.write({
            'putaway_product_id': product.id,
            'putaway_lot_id': move_lines.mapped('lot_id')[:1].id,
        })

        return {
            'product_id': product.id,
            'display_name': product.display_name,
        }

    def action_apply_scanned_product_by_lot(self, scanned_code):
        self.ensure_one()
        scanned_code = (scanned_code or '').strip()
        product, move_lines, lot = self._find_putaway_product_by_lot_scan_code(scanned_code)

        self.write({
            'putaway_product_id': product.id,
            'putaway_lot_id': lot.id,
        })

        return {
            'product_id': product.id,
            'display_name': product.display_name,
            'lot_id': lot.id,
            'move_line_ids': move_lines.ids,
        }

    def _get_putaway_scan_values(self, scanned_code):
        self.ensure_one()
        scanned_code = (scanned_code or '').strip()
        if not scanned_code:
            raise ValidationError(_("Kode scan tidak boleh kosong."))

        move_lines = self._get_putaway_base_move_lines()
        product, matched_by = self._find_product_scan_match(scanned_code)
        if product:
            matching_lines = move_lines.filtered(
                lambda line: line.product_id == product
            )
            if matching_lines:
                lot = self._get_lots_from_move_lines(
                    matching_lines, matching_lines.product_id
                )[:1]
                return {
                    'values': {
                        'putaway_product_id': product.id,
                        'putaway_lot_id': lot.id,
                    },
                    'result': {
                        'product_id': product.id,
                        'display_name': product.display_name,
                        'lot_id': lot.id,
                        'move_line_ids': matching_lines.ids,
                    },
                }
            raise ValidationError(
                _(
                    "%s %s dikenali sebagai Product %s, tetapi product tersebut "
                    "tidak ada di dokumen ini."
                ) % (matched_by, scanned_code, product.display_name)
            )

        product, matching_lines, lot = self._find_putaway_product_by_lot_scan_code(
            scanned_code
        )
        return {
            'values': {
                'putaway_product_id': product.id,
                'putaway_lot_id': lot.id,
            },
            'result': {
                'product_id': product.id,
                'display_name': product.display_name,
                'lot_id': lot.id,
                'move_line_ids': matching_lines.ids,
            },
        }

    def action_apply_scanned_putaway_code(self, scanned_code):
        self.ensure_one()
        values = self._get_putaway_scan_values(scanned_code)
        self.write(values['values'])
        return values['result']

    @api.onchange('putaway_scan_code')
    def _onchange_putaway_scan_code(self):
        scan_records = self.with_context(
            **self._get_operator_validation_workflow_context(),
        )
        for picking in scan_records:
            if not picking.putaway_scan_code:
                continue
            values = picking._get_putaway_scan_values(picking.putaway_scan_code)
            picking.update(values['values'])
            picking.putaway_scan_code = False

    def action_apply_scanned_picker_product_by_lot(self, scanned_code):
        self.ensure_one()
        scanned_code = (scanned_code or '').strip()
        product, picker_moves, lot = self._find_picker_product_by_lot_scan_code(scanned_code)

        self.write({
            'picker_product_id': product.id,
            'picker_lot_id': lot.id,
        })

        return {
            'product_id': product.id,
            'display_name': product.display_name,
            'move_ids': picker_moves.ids,
        }

    def action_apply_scanned_picker_product(self, scanned_code):
        self.ensure_one()
        scanned_code = (scanned_code or '').strip()
        product = self._find_product_by_scan_code(scanned_code)

        if not product:
            raise ValidationError("Product tidak ditemukan untuk kode scan: %s" % scanned_code)

        picker_moves = self._get_picker_base_moves().filtered(
            lambda move: move.product_id == product
        )
        if not picker_moves:
            raise ValidationError(_("Product hasil scan tidak ada di dokumen ini."))

        self.write({
            'picker_product_id': product.id,
            'picker_lot_id': picker_moves.lot_ids[:1].id or picker_moves.move_line_ids.lot_id[:1].id,
        })

        return {
            'product_id': product.id,
            'display_name': product.display_name,
            'move_ids': picker_moves.ids,
        }

    def action_apply_scanned_picker_code(self, scanned_code):
        """Apply a generic scanner input that may identify a product or a lot."""
        self.ensure_one()
        values = self._get_picker_scan_values(scanned_code)
        self.write(values['values'])
        return values['result']

    def _get_picker_scan_values(self, scanned_code):
        self.ensure_one()
        scanned_code = (scanned_code or '').strip()
        if not scanned_code:
            raise ValidationError(_("Kode scan tidak boleh kosong."))

        picker_moves = self._get_picker_base_moves()
        product, matched_by = self._find_product_scan_match(scanned_code)
        if product:
            matching_moves = picker_moves.filtered(
                lambda move: move.product_id == product
            )
            if matching_moves:
                lot = matching_moves.lot_ids[:1] or matching_moves.move_line_ids.lot_id[:1]
                return {
                    'values': {
                        'picker_product_id': product.id,
                        'picker_lot_id': lot.id,
                    },
                    'result': {
                        'product_id': product.id,
                        'display_name': product.display_name,
                        'lot_id': lot.id,
                        'move_ids': matching_moves.ids,
                        'matched_by': 'product',
                    },
                }
            raise ValidationError(
                _(
                    "%s %s dikenali sebagai Product %s, tetapi product tersebut "
                    "tidak ada di dokumen ini."
                ) % (matched_by, scanned_code, product.display_name)
            )

        product, matching_moves, lot = self._find_picker_product_by_lot_scan_code(
            scanned_code
        )
        return {
            'values': {
                'picker_product_id': product.id,
                'picker_lot_id': lot.id,
            },
            'result': {
                'product_id': product.id,
                'display_name': product.display_name,
                'lot_id': lot.id,
                'move_ids': matching_moves.ids,
                'matched_by': 'lot',
            },
        }

    @api.onchange('picker_scan_code')
    def _onchange_picker_scan_code(self):
        scan_records = self.with_context(
            **self._get_operator_validation_workflow_context(),
        )
        for picking in scan_records:
            if not picking.picker_scan_code:
                continue
            values = picking._get_picker_scan_values(picking.picker_scan_code)
            picking.update(values['values'])
            picking.picker_scan_code = False

    def action_apply_scanned_package(self, scanned_code):
        self.ensure_one()

        values = self._get_scanned_package_values(scanned_code)
        package = values['package']
        package_level = values['package_level']

        self.write(values['values'])

        wizard = False

        if package_level:
            wizard = self.env[
                "package.level.detail.wizard"
            ].create({
                "package_level_id": package_level.id,
                "status": (
                    "finished"
                    if package_level.status == "finished"
                    else "unfinished"
                ),
                "package_id": package_level.package_id.id,
                "move_line_ids": [
                    (6, 0, package_level.move_line_ids.ids)
                ],
            })

        return {
            "package_id": package.id,
            "package_level_id": package_level.id or False,
            "package_detail_wizard_id": wizard.id if wizard else False,
            "display_name": package.display_name,
        }

    def _get_scanned_package_values(self, scanned_code):
        self.ensure_one()
        scanned_code = (scanned_code or '').strip()

        package = self.available_package_ids.filtered(
            lambda available_package: available_package.name == scanned_code
        )[:1]

        if not package:
            raise ValidationError(
                "Package tidak ditemukan untuk kode scan: %s" % scanned_code
            )

        package_level = self.package_level_ids.filtered(
            lambda package_level: package_level.package_id == package
        )[:1]

        return {
            'values': {
                "scanned_package_id": package.id,
                "filtered_package_level_id": package_level.id or False,
            },
            'package': package,
            'package_level': package_level,
        }
    
    def action_mark_as_done(self):
        self.ensure_one()
        
        if self.wms_state != "return_shipment":
            raise UserError(
                _("Only documents in Returned Items state can be completed.")
            )
    
        return_picking = self.env["stock.picking"].search([
            ("return_id", "=", self.id)
        ], limit=1)
    
        if not return_picking:
            raise UserError(
                _("Return document has not been created yet.")
            )
    
        if return_picking.state != "done":
            raise UserError(
                _("Please validate the Return document first.")
            )
    
        self.wms_state = "done"
    
        return True
    

    # @api.onchange("state")
    # def _onchange_state_update_wms_state(self):
    #     for picking in self:
    #         if picking.state == 'done':
    #             continue
    #         picking.wms_state = picking.state        


    def _format_datetime(self, dt):
        if not dt:
            return None

        if isinstance(dt, str):
            dt = Datetime.from_string(dt)

        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    # ==========================================================
    # Prepare Payload
    # ==========================================================

    # def _prepare_loginext_payload(self):
    #     self.ensure_one()
    #     print(self)

    #     print(self.origin)

    #     header_to_send = self.env["loginext.staging.data"].search(
    #         [("odoo_shipment_number", "=", self.origin)],
    #         limit=1,
    #     )
    #     print(header_to_send)

    #     if not header_to_send:
    #         raise UserError("Data staging TMS tidak dapat ditemukan")
        
    #     return {
    #         "orderNo": header_to_send.loginext_order_no,
    #         "shipmentOrderDt": self._format_datetime(
    #             header_to_send.loginext_shipment_order_dt
    #         ),
    #         "shipmentOrderTypeCd": header_to_send.loginext_shipment_order_type_cd,
    #         "orderState": header_to_send.loginext_order_state,
    #         "distributionCenter": header_to_send.loginext_distribution_center,
    #         "returnBranch": header_to_send.loginext_return_branch,
    #         "packageWeight": str(header_to_send.loginext_package_weight or 0),
    #         "packageVolume": str(header_to_send.loginext_package_volume or 0),
    #         "preparationTime": 30,
    #         "paymentType": header_to_send.loginext_payment_type,
    #         "packageValue": str(header_to_send.loginext_package_value or 0),
    #         "numberOfItems": header_to_send.loginext_number_of_items or 0,
    #         "partialDeliveryAllowedFl": "Y" if header_to_send.loginext_partial_delivery_allowed_fl else "N",
    #         "returnAllowedFl": "Y" if header_to_send.loginext_return_allowed_fl else "N",
    #         "cancellationAllowedFl": "Y" if header_to_send.loginext_cancellation_allowed_fl else "N",
    #         "deliverServiceTime": '30',
    #         "deliverStartTimeWindow": self._format_datetime(header_to_send.loginext_deliver_start_time_window),
    #         "deliverEndTimeWindow": self._format_datetime(header_to_send.loginext_deliver_end_time_window),
    #         "deliveryType": header_to_send.loginext_delivery_type,
    #         "deliverBranch": header_to_send.loginext_deliver_branch,
    #         "deliverAccountCode": header_to_send.loginext_deliver_account_code,
    #         "deliverAccountName": header_to_send.loginext_deliver_account_name,
    #         "deliverApartment": header_to_send.loginext_deliver_apartment,
    #         "deliverStreetName": header_to_send.loginext_deliver_street_name,
    #         "deliverLocality": header_to_send.loginext_deliver_locality,
    #         "deliverCity": header_to_send.loginext_deliver_city,
    #         "deliverState": header_to_send.loginext_deliver_state,
    #         "deliverCountry": header_to_send.loginext_deliver_country,
    #         "deliverPinCode": header_to_send.loginext_deliver_pin_code,
    #         "deliverAddressTimezone": header_to_send.loginext_deliver_address_timezone,
    #         "deliverNotes": header_to_send.loginext_deliver_notes,
    #         "shipmentCrateMappings": [
    #             {
    #                 "crateCd": crate.loginext_crate_cd,
    #                 "crateName": crate.loginext_crate_name,
    #                 "crateType": crate.loginext_crate_type,
    #                 "crateWeight": crate.loginext_crate_weight,
    #                 "shipmentlineitems": [
    #                     {
    #                         "itemCd": item.loginext_item_cd,
    #                         "itemName": item.loginext_item_name,
    #                         "itemPrice": item.loginext_item_price,
    #                         "itemQuantity": item.loginext_item_quantity,
    #                         "itemType": item.loginext_item_type,
    #                         "itemWeight": item.loginext_item_weight,
    #                     }
    #                     for item in crate.item_ids
    #                 ],
    #             }
    #             for crate in header_to_send.crate_ids
    #         ],
    #     }

    # ==========================================================
    # POST API
    # ==========================================================

    def action_post_loginext(self):
        # print("=" * 80)
        # print("Sent LOGINEXT REQUEST")
        # self.wms_state = 'waiting_response'

        # LOGIN_URL = "https://api.loginextsolutions.com/LoginApp/login/authenticate"
        # CREATE_URL = "https://api.loginextsolutions.com/ShipmentApp/mile/v2/create"

        # USERNAME = "api.tms@aditya.co.id"
        # PASSWORD = "9CE5Q8Jz_#xXR"

        # body = [
        #     rec._prepare_loginext_payload()
        #     for rec in self
        # ]

        # print("=" * 80)
        # print("LOGINEXT REQUEST")
        # print("=" * 80)
        # print(json.dumps(body, indent=4))

        # session = requests.Session()

        # try:
        #     # ======================================================
        #     # LOGIN
        #     # ======================================================

        #     login_payload = {
        #         "userName": USERNAME,
        #         "password": PASSWORD,
        #         "sessionExpiryTimeout": "1",
        #     }

        #     login_response = session.post(
        #         LOGIN_URL,
        #         json=login_payload,
        #         timeout=30,
        #     )

        #     print("=" * 80)
        #     print("LOGINEXT LOGIN RESPONSE")
        #     print("=" * 80)
        #     print(f"Status : {login_response.status_code}")
        #     print(login_response.text)


        #     login_response.raise_for_status()
        #     www_authenticate = login_response.headers.get("WWW-Authenticate")
        #     # ======================================================
        #     # CREATE SHIPMENT
        #     # ======================================================

        #     headers = {
        #         "Content-Type": "application/json",
        #         "Accept": "application/json",
        #     }

        #     if www_authenticate:
        #         headers["WWW-Authenticate"] = www_authenticate

        #     print("Sent Response to Loginext")
        #     response = session.post(
        #         CREATE_URL,
        #         headers=headers,
        #         json=body,
        #         timeout=1,
        #     )

        #     print("=" * 80)
        #     print("LOGINEXT CREATE RESPONSE")
        #     print("=" * 80)
        #     print(f"Status : {response.status_code}")
        #     print(response.text)

        #     response.raise_for_status()

        #     print("=" * 80)
        #     print("END LOGINEXT REQUEST")
        #     print("=" * 80)

            res = self.button_validate()

            return res


        # except requests.exceptions.RequestException as e:
        #     _logger.exception(e)
        #     raise UserError(f"Loginext API Error\n\n{e}")

    def action_assign(self):
        _logger.info(">>> action_assign CALLED %s", self.ids)

        transfer_out_pickings = self.filtered(
            lambda picking: (
                picking.internal_transfer_order_id
                and picking.internal_transfer_order_id.transfer_type == 'transfer_out'
                and picking.internal_transfer_leg == 'delivery'
                and picking.picking_type_id.code == 'internal'
                and picking.state not in ('done', 'cancel')
            )
        )
        package_transfer_pickings = self.filtered(
            lambda picking: (
                picking.internal_transfer_order_id
                and picking.internal_transfer_leg == 'delivery'
                and picking.picking_type_id.code == 'internal'
                and picking.state not in ('done', 'cancel')
            )
        )
        transfer_in_receipt_pickings = self.filtered(
            lambda picking: (
                picking.internal_transfer_order_id
                and picking.internal_transfer_order_id.transfer_type == 'transfer_in'
                and picking.internal_transfer_leg == 'receipt'
                and picking.state not in ('done', 'cancel')
            )
        )
        for picking in transfer_out_pickings:
            picking.internal_transfer_order_id._prepare_transfer_out_delivery(picking)
        for picking in transfer_in_receipt_pickings:
            picking.internal_transfer_order_id._prepare_transfer_in_receipt(picking)

        bad_stock_pickings = self.filtered(
            lambda picking: picking._is_manual_bad_stock_pick()
        )
        pickings_to_assign = self - bad_stock_pickings
        res = (
            super(StockPicking, pickings_to_assign).action_assign()
            if pickings_to_assign
            else True
        )

        for picking in self:
            is_transfer_in_receipt = bool(
                picking.internal_transfer_order_id
                and picking.internal_transfer_order_id.transfer_type == 'transfer_in'
                and picking.internal_transfer_leg == 'receipt'
            )
            if picking.picking_type_id.name == 'Receipts' or is_transfer_in_receipt:
                if is_transfer_in_receipt:
                    move_lines_to_create = []
                    for move in picking.move_ids.filtered(
                        lambda item: item.state not in ('done', 'cancel')
                        and not item.move_line_ids.filtered(
                            lambda line: line.state not in ('done', 'cancel')
                        )
                    ):
                        move_lines_to_create.append({
                            'company_id': picking.company_id.id,
                            'picking_id': picking.id,
                            'move_id': move.id,
                            'product_id': move.product_id.id,
                            'product_uom_id': move.product_uom.id,
                            'location_id': move.location_id.id,
                            'location_dest_id': move.location_dest_id.id,
                            'source_location_id': move.location_id.id,
                            'quantity': 0.0,
                            'picked': False,
                        })
                    if move_lines_to_create:
                        self.env['stock.move.line'].create(move_lines_to_create)

                for move in picking.move_ids:
                    active_lines = move.move_line_ids.filtered(
                        lambda line: line.state not in ('done', 'cancel')
                    )
                    if len(active_lines) > 1:
                        # A Receipt uses one working line per stock move.
                        active_lines[1:].unlink()

                # Check Availability must not prefill the received quantity.
                # The actual receipt quantity is entered by the warehouse user.
                picking.move_line_ids.filtered(
                    lambda line: line.state not in ('done', 'cancel')
                ).write({'quantity': 0.0})

            for move in picking.move_ids:
                for line in move.move_line_ids:
                    print(line)
                    if line.lot_id:
                        print(line.lot_id)
                        # vals = {
                        #     'source_location_id': line.lot_id.location_id.id or line.location_id.id,
                        # }
                        # vals['product_lot_id'] = line.lot_id.id
                        # line.write(vals)
                        print(f"write source_location: {line.lot_id.location_id.id} product_lot_id: {line.lot_id.id,}")
                        line.write({
                            'source_location_id': line.lot_id.location_id.id or line.location_id.id,
                            'product_lot_id': line.lot_id.id,
                        })

        for picking in package_transfer_pickings:
            picking.internal_transfer_order_id._sync_transfer_package_levels(
                picking
            )

        # Match the PO Receipt operator flow: once Check Availability runs,
        # a transfer-in receipt is ready to process even though its actual
        # received quantity remains zero until entered by the operator.
        self.filtered(
            lambda picking: (
                picking.internal_transfer_order_id
                and picking.internal_transfer_leg == "receipt"
            )
        )._compute_wms_state()

        return res

    def action_mark_todo_and_check_availability(self):
        to_confirm = self.filtered(lambda picking: picking.state == 'draft')
        if to_confirm:
            to_confirm.action_confirm()
            pickings_to_assign = to_confirm.filtered(
                lambda picking: not picking._is_manual_bad_stock_pick()
            )
            if pickings_to_assign:
                pickings_to_assign.action_assign()

        return True

    def _is_manual_bad_stock_pick(self):
        self.ensure_one()
        order = self.internal_transfer_order_id
        return bool(
            order
            and self.internal_transfer_leg == 'pick'
            and order._is_bad_stock_destination()
        )

    def action_confirm_shipment(self):
        self.ensure_one()

        is_transfer_shipment_ito_delivery = bool(
            self.internal_transfer_order_id
            and self.internal_transfer_order_id.adt_transaction_type == 'Transfer Shipment'
            and self.internal_transfer_leg == 'delivery'
            and self.picking_type_id.code == 'internal'
            and self.adt_type == 'Transfer Shipment'
        )
        has_transfer_shipment_quantity_difference = bool(
            is_transfer_shipment_ito_delivery
            and any(
                float_compare(
                    move.shipped_qty,
                    move.quantity,
                    precision_rounding=move.product_uom.rounding,
                ) != 0
                for move in self.move_ids
            )
        )
        from_transfer_shipment_staging = self.env.context.get(
            'transfer_shipment_completed_from_staging'
        )
        if (
            is_transfer_shipment_ito_delivery
            and not from_transfer_shipment_staging
            and self.transfer_shipment_discrepancy
            and self.approval_state != 'approved'
        ):
            raise UserError(_(
                'Transfer Shipment discrepancy harus disetujui sebelum shipment dapat diselesaikan.'
            ))
        if (
            is_transfer_shipment_ito_delivery
            and not from_transfer_shipment_staging
            and not self.transfer_shipment_discrepancy
            and (self.shipment_information == 'partial' or has_transfer_shipment_quantity_difference)
        ):
            raise UserError(_(
                'Periksa dokumen pada Shipment Transfer Completed dengan tombol Complete Shipment sebelum menyelesaikan quantity yang berbeda.'
            ))

        if not self.shipment_information:
            raise UserError(_("Shipment Information is required."))

        if self.shipment_information == "full_shipment":
            self.wms_state = "done"
            if (
                self.internal_transfer_order_id
                and self.internal_transfer_order_id.adt_transaction_type in ('Shipment Consignment', 'Transfer Shipment')
                and self.internal_transfer_leg == 'delivery'
                and self.picking_type_id.code == 'internal'
            ):
                self.internal_transfer_order_id.action_auto_validate_transfer_out_receipt(
                    shipment_picking=self,
                )
            return

        is_transfer_shipment_ito_delivery = bool(
            self.internal_transfer_order_id
            and self.internal_transfer_order_id.adt_transaction_type == 'Transfer Shipment'
            and self.internal_transfer_leg == 'delivery'
            and self.picking_type_id.code == 'internal'
        )
        if is_transfer_shipment_ito_delivery:
            # A shortage creates a draft return for the unshipped quantity,
            # while the transfer receipt is completed at the reported quantity.
            self._generate_return_from_actual_qty()
            self.wms_state = "return_shipment"
            self.internal_transfer_order_id.action_auto_validate_transfer_out_receipt(
                shipment_picking=self,
            )
            return

        self._generate_return_from_actual_qty()

        self.wms_state = "return_shipment"
        if (
            self.internal_transfer_order_id
            and self.internal_transfer_order_id.adt_transaction_type in ('Shipment Consignment', 'Transfer Shipment')
            and self.internal_transfer_leg == 'delivery'
            and self.picking_type_id.code == 'internal'
        ):
            self.internal_transfer_order_id.action_auto_validate_transfer_out_receipt(
                shipment_picking=self,
            )

    def _get_over_receipt_warehouse_partner(self):
        self.ensure_one()
        ref_no = (self.ref_sotr or '').strip()
        if not ref_no:
            return self.env['res.partner'].browse()

        transaction_type = self.over_receipt_transaction_type or 'receipt_sotr'
        if transaction_type == 'receipt_sotr':
            staging_model, reference_field = 'acumatica.transfer.line', 'receipt_nbr'
        elif transaction_type == 'inventory_receive':
            staging_model, reference_field = 'acumatica.inventory.recieved.line', 'reference_nbr'
        else:
            return self.env['res.partner'].browse()

        staging = self.env[staging_model].search(
            [(reference_field, '=', ref_no), ('stock_lebih', '=', True)],
            order='id desc',
            limit=1,
        )
        if not staging:
            return self.env['res.partner'].browse()

        # Supplier / Customer partner follows the source Warehouse, not
        # Warehouse Dest (which is the receipt destination warehouse).
        source_warehouse_code = (staging.warehouse_code or '').strip().upper()
        if not source_warehouse_code:
            return self.env['res.partner'].browse()
        warehouse_code = source_warehouse_code.split('-', 1)[0].strip()
        if not warehouse_code:
            return self.env['res.partner'].browse()

        LocationMapping = self.env['stock.location.acumatica']
        mapping = LocationMapping.search([
            ('code', '=', source_warehouse_code),
            ('is_active', '=', True),
            ('odoo_building_id', '!=', False),
        ], limit=1)
        warehouse = mapping.odoo_building_id
        if not warehouse:
            warehouse = self.env['stock.warehouse'].search([
                ('code', '=', warehouse_code),
                ('company_id', '=', self.company_id.id),
            ], limit=1)

        partner = warehouse.partner_id if warehouse else self.env['res.partner'].browse()
        if partner and partner.partner_type == 'warehouse':
            return partner
        return self.env['res.partner'].search([
            ('name', '=', warehouse_code),
            ('partner_type', '=', 'warehouse'),
            ('company_id', 'in', [self.company_id.id, False]),
        ], limit=1)

    def action_update_over_receipt(self):
        self.ensure_one()
        if not self.stock_lebih:
            raise UserError(_('Update Receipt hanya tersedia untuk dokumen Over-Receipt.'))
        if self.approval_state != 'approved':
            raise UserError(_('Dokumen harus disetujui sebelum Update Receipt.'))
        if not (self.ref_sotr or '').strip():
            raise UserError(_('Ref. No. Document wajib diisi sebelum Update Receipt.'))
        if self.wms_state != 'waiting_response':
            raise UserError(_('Dokumen tidak sedang menunggu approval/penerimaan.'))

        transaction_type = self.over_receipt_transaction_type or 'receipt_sotr'
        if transaction_type == 'receipt_sotr':
            staging_model = 'acumatica.transfer.line'
            reference_field = 'receipt_nbr'
            source_label = _('SOTR')
        elif transaction_type == 'inventory_receive':
            staging_model = 'acumatica.inventory.recieved.line'
            reference_field = 'reference_nbr'
            source_label = _('Inventory Receive')
        else:
            raise UserError(_('Pilih Tipe Transaksi Over-Receipt terlebih dahulu.'))

        Staging = self.env[staging_model]
        staging = Staging.search(
            [(reference_field, '=', self.ref_sotr.strip()), ('stock_lebih', '=', True)],
            order='id desc',
            limit=1,
        )
        if not staging:
            return self._over_receipt_quantity_warning(
                _('Data %s Over-Receipt %s belum ditemukan di staging.')
                % (source_label, self.ref_sotr)
            )

        api_quantities = {}
        for line in staging.product_line_ids:
            inventory_id = (line.inventory_id or '').strip()
            if inventory_id:
                api_quantities[inventory_id] = (
                    api_quantities.get(inventory_id, 0.0)
                    + staging._parse_float(line.product_uom_qty)
                )

        document_quantities = {}
        for move in self.move_ids:
            inventory_id = (move.product_id.default_code or '').strip()
            if inventory_id:
                document_quantities[inventory_id] = (
                    document_quantities.get(inventory_id, 0.0) + move.quantity
                )

        mismatches = []
        inventory_ids = set(api_quantities) | set(document_quantities)
        for inventory_id in sorted(inventory_ids):
            product_moves = self.move_ids.filtered(
                lambda move: (move.product_id.default_code or '').strip() == inventory_id
            )
            product_name = (
                product_moves[:1].product_id.default_code
                if product_moves else inventory_id
            )
            rounding = product_moves[:1].product_uom.rounding if product_moves else 0.01
            api_qty = api_quantities.get(inventory_id, 0.0)
            document_qty = document_quantities.get(inventory_id, 0.0)
            if float_compare(api_qty, document_qty, precision_rounding=rounding) != 0:
                mismatches.append(
                    _('Product: %(product)s\nQty di API: %(api_qty)s\nQty di Dokumen: %(document_qty)s')
                    % {
                        'product': product_name,
                        'api_qty': f'{api_qty:g}',
                        'document_qty': f'{document_qty:g}',
                    }
                )

        if mismatches:
            message = _(
                'Dokumen ini tidak bisa di-update karena ada perbedaan quantity:\n\n%s'
            ) % '\n\n'.join(mismatches)
            return self._over_receipt_quantity_warning(message)

        return self.with_context(over_receipt_update=True).button_validate()

    def action_approve(self):
        self.ensure_one()
        if self.stock_lebih and not (self.ref_sotr or '').strip():
            raise UserError(_('Ref. No. Document wajib diisi sebelum dokumen Over-Receipt dapat disetujui.'))
        if self.transfer_shipment_discrepancy and not self.attachment_ids:
            raise UserError(_(
                'Lampiran wajib diisi sebelum discrepancy quantity Transfer Shipment dapat disetujui.'
            ))
        return super().action_approve()

    def _approval_approved(self):
        result = super()._approval_approved()
        for picking in self.filtered('transfer_shipment_discrepancy'):
            if not picking.attachment_ids:
                raise UserError(_(
                    'Lampiran wajib diisi sebelum discrepancy quantity Transfer Shipment dapat disetujui.'
                ))
            if picking.shipment_information not in ('partial', 'full_shipment'):
                raise UserError(_(
                    'Shipment Information belum ditentukan untuk Transfer Shipment discrepancy.'
                ))
            picking.action_confirm_shipment()
        return result

    def _submit_transfer_shipment_discrepancy_approval(self):
        self.ensure_one()
        approval_type = self.approval_type_id or self.env.ref(
            'tijari_wms_18.approval_type_over_receipt', raise_if_not_found=False,
        )
        if not approval_type:
            raise UserError(_(
                'Approval Type Discrepancy Quantity belum dikonfigurasi.'
            ))

        if not self.approval_type_id:
            self.approval_type_id = approval_type
        if not approval_type._get_approval_lines(0.0):
            raise UserError(_(
                'Tambahkan approver pada Approval Type %s terlebih dahulu.'
            ) % approval_type.display_name)
        self.action_submit_approval()

    def action_rollback_over_receipt_approval(self):
        self.ensure_one()
        if not self.stock_lebih:
            raise UserError(_('Rollback approval hanya tersedia untuk dokumen Over-Receipt.'))
        if not self.env.user.has_group('stock.group_stock_manager'):
            raise UserError(_('Hanya Stock Manager yang dapat melakukan rollback approval.'))
        if self.state in ('done', 'cancel'):
            raise UserError(_('Approval tidak dapat di-rollback setelah dokumen selesai atau dibatalkan.'))
        request = self.approval_request_id
        if not request or request.state != 'approved':
            raise UserError(_('Rollback hanya dapat dilakukan pada dokumen yang sudah disetujui.'))

        request.line_ids.write({'state': 'waiting', 'action_date': False})
        request.write({'state': 'waiting'})
        self.message_post(body=_('Approval di-rollback. Ref. No. Document dapat diperiksa dan diperbarui.'))
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    @api.model
    def _over_receipt_quantity_warning(self, message):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Update Receipt belum dapat diproses'),
                'message': message,
                'type': 'warning',
                'sticky': True,
            },
        }

    def button_validate(self):
        # Validation performs internal writes on the picking (for example,
        # updating adt_actual_qty and the picking state). Keep those writes
        # separate from direct Operator edits, which remain restricted to the
        # permitted notebook fields in write().
        self = self.with_context(
            _tijari_operator_validation_write_token=_OPERATOR_VALIDATION_WRITE_TOKEN,
        )
        if not self.env.context.get('over_receipt_update'):
            over_receipts = self.filtered('stock_lebih')
            for picking in over_receipts:
                if picking.state in ('done', 'cancel'):
                    continue
                if picking.approval_state == 'approved':
                    raise UserError(_(
                        'Gunakan tombol Update Receipt untuk menyelesaikan dokumen Over-Receipt.'
                    ))
                if picking.approval_request_id:
                    if picking.approval_state != 'rejected':
                        raise UserError(_(
                            'Dokumen Over-Receipt sudah dikirim untuk approval dan belum disetujui.'
                        ))
                    # Keep the rejected request as history, then let Validate
                    # create a fresh request for the corrected quantities.
                    picking.approval_request_id = False

                approval_type = picking.approval_type_id or self.env.ref(
                    'tijari_wms_18.approval_type_over_receipt', raise_if_not_found=False,
                )
                if not approval_type:
                    raise UserError(_('Approval Type Discrepancy Quantity belum dikonfigurasi.'))
                if not picking.approval_type_id:
                    picking.approval_type_id = approval_type
                if not approval_type._get_approval_lines(0.0):
                    raise UserError(_(
                        'Tambahkan approver pada Approval Type %s terlebih dahulu.'
                    ) % approval_type.display_name)
                picking.action_submit_approval()

            if over_receipts:
                return {'type': 'ir.actions.client', 'tag': 'reload'}

        document_only_pickings = self.filtered(
            lambda picking: picking.adt_is_document_only
        )
        for picking in document_only_pickings.filtered(lambda item: item.state not in ('done', 'cancel')):
            picking._adt_preserve_document_only_destination()
            picking._check_document_only_outbound_stock()
            if picking.state == 'draft':
                picking.action_confirm()
            if picking.state in ('confirmed', 'waiting', 'assigned'):
                picking.action_assign()
            picking._adt_preserve_document_only_destination()
            picking._set_document_only_done_quantities()

        for picking in self:
            
            # if picking.adt_type == 'Toko Pegang DO':
            #     if not picking.owner_id:
            #         raise UserError(_("Owner is required for Toko Pegang DO."))

            if picking.adt_is_document_only:
                continue
            # Pick harus diblokir sebelum validasi standar Odoo memproses
            # perpindahan stok apabila masih ada line yang belum Match.
            if "Pick" in (picking.picking_type_id.name or ""):
                picking._check_validate_requirements()
                picking._check_pick_quantity_vs_demand()
                picking._check_pick_confirmation_quantity()
                picking._apply_confirmation_qty_to_quantity_pick()

            if picking.picking_type_id.name in ('Pack'):
                picking._check_pick_quantity_vs_demand()

            if picking.picking_type_id.name in ('Delivery Orders', 'Internal Transfer'):
                picking._check_validate_delivery_requirements()
                picking._apply_confirmation_qty_to_quantity_delivery()

             # update actual qty
            picking.update({
                'adt_actual_qty': sum(picking.move_ids.mapped('quantity'))
            })

            if picking.picking_type_id.name in ("Pick", "Pick Components", "Pack", "Delivery Orders"):
                picking._assign_status_ba()
                
                    
            
            # Storage validation only depends on putaway completion. It does
            # not use the receipt/picker status checks.
            if picking.picking_type_id.name in ('Storage', 'Store Finished Product'):
                picking._check_putaway_status_requirements()
                # Refresh Odoo's picked quantity flag from the Storage
                # putaway status before its standard backorder check runs.
                picking.move_line_ids._update_move_picked()
            
            if (
                picking.picking_type_id.name in ('Receipts', 'Receipt Internal')
                and not picking.is_ito_internal_transfer
            ):
                picking._check_putaway_status_requirements()
                picking._check_picker_status_requirements()

            if picking.picking_type_id.name not in ('Pick', 'Receipts', 'Receipt Internal', 'Storage', 'Store Finished Product'):
                print(f"Run validate all except pick and receipt")
                # Lakukan perulangan untuk setiap stock.move yang ada di dalam picking
                for move in picking.move_ids:
                    # Jika move memiliki move_line_ids, lakukan update massal di level DB
                    for line in move.move_line_ids:
                        loc_code = str(line.location_id.unique_code or '').strip()
                        product_code = str(line.lot_id.name or '').strip()
                        vals = {
                            'confirmation_location_code': loc_code,
                            'confirmation_lot_code': product_code,
                            'matching': 'match',
                        }
                        print(f"Write vals = {vals}")

                        line.write(vals)

                        print(f"After write = {line.matching}")

                        print(f"After write line {line.id} = {line.matching} for picking: {picking.id}")

        for picking in self.filtered(
            lambda item: (
                not item.adt_is_document_only
                and item.state not in ('done', 'cancel')
                and item.picking_type_id.name == 'Receipts'
            )
        ):
            move_lines_to_pack = picking._package_move_lines()
            if move_lines_to_pack:
                picking.action_put_in_pack(
                    move_lines_to_pack=move_lines_to_pack
                )

        if document_only_pickings:
            res = super(StockPicking, self.with_context(skip_sanity_check=True)).button_validate()
        else:
            res = super().button_validate()
        document_only_pickings._adt_preserve_document_only_destination()

        for rec in self:

            is_document_only = rec.adt_is_document_only
            if is_document_only:
                continue
            print(rec.picking_type_id.name)
            is_receipt_putaway = (
                rec.picking_type_id.name in ('Receipts', 'Receipt Internal', 'Storage', 'Store Finished Product')
                and not rec.is_ito_internal_transfer
            )
            for line in rec.move_line_ids:
                print(f">>> Validating line {line.id} - Product: {line.product_id.display_name}, Lot Name: {line.lot_id},Picking id: {line.picking_id.id}, Picking Type: {line.picking_id.picking_type_id.name}, Status product: {line.picking_id.picking_type_id.operation_category}, Matching Status: {line.matching}") # Cek di terminal Odoo
                if not is_receipt_putaway:
                    line.lot_name = line.lot_id.name if line.lot_id else line.lot_name
                if not is_receipt_putaway and not line.lot_name:
                    print(f"Missing lot: {line}")
                    raise UserError(_("Validation blocked.\n Lot not Found Please generate lot for all lines before validating the picking."))
                
            if is_receipt_putaway:
                rec._check_putaway_status_requirements()
                if rec.picking_type_id.name not in ('Storage', 'Store Finished Product'):
                    rec._check_picker_status_requirements()
            else:
                rec._check_validate_requirements()
            operation_category = rec.picking_type_id.operation_category
            print(f">>> Validating picking {rec.id} - Picking Type: {rec.picking_type_id.name}, Operation Category: {operation_category}") # Cek di terminal Odoo

            # Hanya enforce untuk PUT dan DELIVERY
            if not is_receipt_putaway and operation_category in ['put', 'delivery']:
                for line in rec.move_line_ids:
                    # Jika belum dicek (invisible), tidak dianggap ok
                    print(f">>> Checking line {line.matching} - Product: {line.product_id.display_name}, Lot Name: {line.lot_id.name if line.lot_id else 'N/A'}") # Cek di terminal Odoo
                    # line.matching = 'match'
                    print(f">>> Matching status for line {line.id}: {line.matching}") # Cek di terminal Odoo
                    if line.matching != 'match':
                        raise UserError(_(
                            "Validation blocked.\n\n"
                            "Line:\n"
                            f"- Product: {line.product_id.display_name}\n"
                            f"- Qty: {line.quantity or line.product_uom_qty}\n\n"
                            "Status: ❌ NOT MATCH\n\n"
                            "Silakan pastikan scan location dan/atau lot sudah benar."
                        ))

            if (
                "Pick Components" in (rec.picking_type_name or "")
                and rec.state == 'done'
                and (rec.backorder_id or rec.backorder_ids)
            ):
                # Raw material moves belong to the MO and may not have a
                # picking_id. Detect the backorder from this picking, then
                # create a separate working line on each affected MO move.
                component_moves = rec.move_ids.move_dest_ids.filtered(
                    lambda move: bool(move.raw_material_production_id)
                )
                for move in component_moves:
                    finished_lines = move.move_line_ids.filtered(
                        lambda line: line.mo_status == 'finished'
                    )
                    if not finished_lines:
                        continue

                    working_lines = move.move_line_ids.filtered(
                        lambda line: (
                            line.state not in ('done', 'cancel')
                            and line.mo_status != 'finished'
                        )
                    )

                    source_moves = rec.move_ids.filtered(
                        lambda source_move: move in source_move.move_dest_ids
                    )
                    lot_values = {}
                    for source_line in source_moves.move_line_ids:
                        lot_id = source_line.lot_id.id or False
                        lot_name = source_line.lot_id.name or source_line.lot_name or False
                        lot_values[(lot_id, lot_name)] = True

                    # If the transfer line has no lot, preserve the lot from
                    # the finished MO line so the new line remains traceable.
                    if not lot_values:
                        for finished_line in finished_lines:
                            lot_id = finished_line.lot_id.id or False
                            lot_name = finished_line.lot_id.name or finished_line.lot_name or False
                            lot_values[(lot_id, lot_name)] = True

                    for lot_id, lot_name in lot_values:
                        matching_working_line = working_lines.filtered(
                            lambda line: (
                                line.lot_id.id == lot_id
                                and (line.lot_id.name or line.lot_name or False) == lot_name
                            )
                        )
                        if matching_working_line:
                            continue
                        line_vals = move._prepare_move_line_vals(quantity=0.0)
                        line_vals.update({
                            'quantity': 0.0,
                            'lot_id': lot_id,
                            'lot_name': lot_name,
                            'mo_status': 'unfinished',
                            'picked': False,
                        })
                        self.env['stock.move.line'].create(line_vals)

            elif "Pick" in (rec.picking_type_name or "") and "Components" not in (rec.picking_type_name or ""):
                next_pickings = rec.move_ids.move_dest_ids.mapped("picking_id").filtered(
                    lambda picking: picking.picking_type_name == "Pack"
                )
                next_pickings.write({
                    # 'adt_actual_qty': rec.adt_actual_qty,
                    'adt_actual_qty': 0.0,
                    
                })

                for move in rec.move_ids:
                    confirmation_qty = move.confirmation_quantity

                    for dest_move in move.move_dest_ids.filtered(
                        lambda m: m.picking_id.picking_type_name == "Pack"
                    ):
                        dest_move.write({
                            'product_uom_qty': confirmation_qty,
                        })
            
            elif 'Pack' in (rec.picking_type_name or ""):
                next_pickings = rec.move_ids.move_dest_ids.mapped("picking_id").filtered(
                    lambda picking: picking.picking_type_name == "Delivery Orders"
                )
                next_pickings.write({
                    # 'adt_actual_qty': rec.adt_actual_qty,
                    'adt_actual_qty': 0.0
                })

                for move in rec.move_ids:
                    for dest_move in move.move_dest_ids.filtered(
                        lambda m: m.picking_id.picking_type_name == "Delivery Orders"
                    ):
                        packaged_qty = sum(
                            ml.quantity
                            for ml in dest_move.move_line_ids
                            if ml.package_id
                        )
                        
                        dest_move.write({
                            "unpack_quantity": dest_move.quantity - packaged_qty
                        })

            elif 'Delivery Orders' in (rec.picking_type_name or ""):
                for move in rec.move_ids:
                    move._compute_remaining_package_qty()

            if rec.picking_type_name in ['Delivery Orders'] and rec.adt_type in ['Sales Order Shipment', 'SO Shipment']:
                rec.wms_state = 'waiting_response'

            if (
                rec.internal_transfer_order_id
                and rec.internal_transfer_leg == 'delivery'
                and rec.adt_type in ('Shipment Consignment', 'Transfer Shipment')
                and rec.picking_type_id.code == 'internal'
                and rec.state == 'done'
            ):
                rec.wms_state = 'waiting_response'

            if (
                rec.state == "done"
                and rec.return_id
                and rec.return_id.wms_state == "return_shipment"
            ):
                
                rec.return_id.wms_state = "done"
                                             
            pickings = self.env["stock.picking"].search([])
            if rec.picking_type_id.code != "incoming":
                continue

            rec._check_putaway_quantity_vs_demand()

            storage_pickings = self.env["stock.picking"].search([
                ("origin", "ilike", rec.origin or rec.name),
                ("picking_type_id.code", "=", "internal"),
            ], limit=1)

            total_qty = sum(rec.move_ids.mapped('product_uom_qty'))

            rec.write({
                'adt_total_qty': total_qty
            })

            storage_pickings.write({
                "adt_dest_location_id": rec.adt_dest_location_id,
                "adt_type": rec.adt_type,
                "note" : rec.note,
            })

            for storage in storage_pickings:
                for storage_move in storage.move_ids:
                    receipt_move = rec.move_ids.filtered(
                        lambda m: m.product_id == storage_move.product_id
                    )[:1]

                    if receipt_move:
                        storage_move.write({
                            "product_note": receipt_move.product_note,
                        })

        if self.env.context.get('over_receipt_update'):
            completed_over_receipts = self.filtered(
                lambda picking: picking.stock_lebih and picking.state == 'done'
            )
            for picking in completed_over_receipts:
                ref_no = (picking.ref_sotr or '').strip()
                vals = {}
                if ref_no and picking.origin != ref_no:
                    vals['origin'] = ref_no
                if not picking.adt_type:
                    transaction_type_labels = {
                        'receipt_sotr': _('Receipt SOTR'),
                        'inventory_receive': _('Inventory Receive'),
                    }
                    transaction_type_label = transaction_type_labels.get(
                        picking.over_receipt_transaction_type
                    )
                    if transaction_type_label:
                        vals['adt_type'] = transaction_type_label
                if not picking.partner_id:
                    warehouse_partner = picking._get_over_receipt_warehouse_partner()
                    if warehouse_partner:
                        vals['partner_id'] = warehouse_partner.id
                if vals:
                    picking.sudo().write(vals)

        return res

    def _apply_confirmation_qty_to_quantity_pick(self):
        for move in self.move_ids:
            for line in move.move_line_ids:
                confirmation_qty = line.confirmation_quantity

                line.quantity_shadow = line.quantity
                line.quantity = confirmation_qty

    def _apply_confirmation_qty_to_quantity_delivery(self):
        for move in self.move_ids:
            for line in move.move_line_ids:
                confirmation_qty = line.so_confirmation_quantity

                line.quantity_shadow = line.quantity
                line.quantity = confirmation_qty
                
    def _check_pick_quantity_vs_demand(self):
        for picking in self:
            # has_discrepancy = False
            for move in picking.move_ids.filtered(lambda item: item.state != 'cancel'):
                diff_qty = move.difference_quantity
                demand_qty = move.product_uom_qty
                picked_qty = move.quantity
                rounding = move.product_uom.rounding if move.product_uom else 0.01
                comparison = float_compare(
                    picked_qty,
                    demand_qty,
                    precision_rounding=rounding,
                )
                if comparison > 0:
                    raise UserError(
                        _(
                            "Product: %s\n"
                            "Lot: %s\n"
                            "Stored Qty: %s\n"
                            "Demand: %s\n\n"
                            "Quantity exceeds demand."
                        ) % (
                            move.product_id.display_name,
                            move.lot_ids.name or "-",
                            picked_qty,
                            demand_qty,
                        )
                    )
                
            #     if not float_is_zero(
            #         diff_qty,
            #         precision_rounding=rounding,
            #     ):
            #         has_discrepancy = True

            # picking.status_ba = 'discrepancy' if has_discrepancy else 'conform'

    def _check_pick_confirmation_quantity(self):
        for picking in self:

            for line in picking.move_line_ids:
                if line.confirmation_quantity <= 0:
                    raise UserError(_(
                        "Qty Diambil tidak boleh 0.\n\n"
                        "Product: %s"
                    ) % line.product_id.display_name)

                if line.confirmation_quantity > line.quantity:
                    raise UserError(_(
                        "Qty Diambil tidak boleh lebih besar "
                        "dari Qty Diterima.\n\n"
                        "Product: %s\n"
                        "Qty Diterima: %s\n"
                        "Qty Diambil: %s"
                    ) % (
                        line.product_id.display_name,
                        line.quantity,
                        line.confirmation_quantity,
                    ))

    def _check_delivery_quantity_vs_demand(self):
        for picking in self:
            has_discrepancy = False
            for move in picking.move_ids.filtered(lambda item: item.state != 'cancel'):
                diff_qty = move.product_uom_qty - move.so_confirmation_quantity
                demand_qty = move.product_uom_qty
                delivered_qty = move.so_confirmation_quantity
                rounding = move.product_uom.rounding if move.product_uom else 0.01
                comparison = float_compare(
                    delivered_qty,
                    demand_qty,
                    precision_rounding=rounding,
                )
                if comparison > 0:
                    raise UserError(
                        _(
                            "Product: %s\n"
                            "Lot: %s\n"
                            "Qty Dikirim: %s\n"
                            "Qty Total: %s\n\n"
                            "Qty Dikirim melebihi Qty Total."
                        ) % (
                            move.product_id.display_name,
                            move.lot_ids.name or "-",
                            delivered_qty,
                            demand_qty,
                        )
                    )
                
                if not float_is_zero(
                    diff_qty,
                    precision_rounding=rounding,
                ):
                    has_discrepancy = True

            picking.status_ba = 'discrepancy' if has_discrepancy else 'conform'

    def _assign_status_ba(self):
        for picking in self:
            demand_qty = picking.adt_total_qty
            picked_qty = picking.adt_actual_qty
            rounding = 0.01
            diff_qty = demand_qty - picked_qty

            if not float_is_zero(
                diff_qty,
                precision_rounding=rounding,
            ):
                picking.status_ba = 'discrepancy'
            else:
                picking.status_ba = 'conform'    
    
    def _check_putaway_quantity_vs_demand(self):
        for picking in self:
            if picking.picking_type_id.name not in ('Receipts', 'Receipt Internal', 'Storage', 'Store Finished Product'):
                continue

            has_discrepancy = False
            for move in picking.move_ids.filtered(lambda item: item.state != 'cancel'):
                demand_qty = move.product_uom_qty
                putaway_qty = move.confirmation_quantity
                rounding = move.product_uom.rounding if move.product_uom else 0.01
                comparison = float_compare(
                    putaway_qty,
                    demand_qty,
                    precision_rounding=rounding,
                )
                if comparison > 0:
                    raise UserError(
                        _(
                            "Product: %s\n"
                            "Lot: %s\n"
                            "Stored Qty: %s\n"
                            "Demand: %s\n\n"
                            "Quantity exceeds demand."
                        ) % (
                            move.product_id.display_name,
                            move.lot_ids.name or "-",
                            putaway_qty,
                            demand_qty,
                        )
                    )
                if comparison < 0:
                    has_discrepancy = True

            picking.status_ba = 'discrepancy' if has_discrepancy else 'conform'

    def _check_validate_requirements(self):
        self.ensure_one()
        _logger.info("Check Validate on = %s", self.id)
        # The ITO delivery leg replaces the old Delivery Order. It is an
        # internal stock move, not a receiving/putaway operation, regardless
        # of whether the configured operation type still has a legacy name.
        if self.is_ito_internal_transfer:
            return
        if self.picking_type_id.name in ('Storage', 'Store Finished Product'):
            return self._check_putaway_status_requirements()
        if self.picking_type_id.name in ('Receipts', 'Receipt Internal', 'Internal Transfers'):
            return self._check_putaway_status_requirements() and self._check_picker_status_requirements()

        if self.picking_type_name not in ['Pick', 'Pick Internal']:
            return

        active_moves = self.move_ids.filtered(
            lambda move: move.state != "cancel"
        )

        unpicked_moves = active_moves.filtered(
            lambda move: not move.picked
        )

        unmatched_move_lines = active_moves.move_line_ids.filtered(
            lambda line: line.matching != "match"
        )

        unfinished_move_lines = active_moves.move_line_ids.filtered(
            lambda line: line.pick_status != "finished"
        )

        error_messages = []

        if unpicked_moves:
            products = "\n".join(
                f"- {move.product_id.display_name}"
                for move in unpicked_moves
            )

            error_messages.append(
                (
                    "Semua product harus dalam kondisi picked.\n"
                    "Produk yang belum Terambil:\n%s"
                )
                % products
            )

        if unmatched_move_lines:
            lines = "\n".join(
                "- %s | Lot/Serial: %s | Matching: %s"
                % (
                    line.product_id.display_name,
                    line.lot_id.name or line.lot_name or "-",
                    dict(line._fields["matching"].selection).get(
                        line.matching,
                        line.matching or "-"
                    ),
                )
                for line in unmatched_move_lines
            )

            error_messages.append(
                (
                    "Semua product harus memiliki status matching "
                    "'Match'.\n"
                    "Detail produk yang belum match:\n%s"
                )
                % lines
            )

        if unfinished_move_lines:
            lines = "\n".join(
                "- %s | Lot/Serial: %s | Status: %s"
                % (
                    line.product_id.display_name,
                    line.lot_id.name or line.lot_name or "-",
                    dict(line._fields["pick_status"].selection).get(
                        line.pick_status,
                        line.pick_status or "-"
                    ),
                )
                for line in unfinished_move_lines
            )

            error_messages.append(
                (
                    "Pastikan semua status barang Finished "
                    "\n"
                    "Detail produk yang belum Finished:\n%s"
                )
                % lines
            )
        

        if error_messages:
            raise UserError("\n\n".join(error_messages))

    def _check_validate_delivery_requirements(self):
        self.ensure_one()

        active_moves = self.move_ids.filtered(
            lambda move: move.state != "cancel"
        )

        unfinished_unpacked_items = active_moves.filtered(
            lambda move: move.remaining_package_qty != 0 and move.status_check != "finished"
        )

        unfinished_packed_items = self.package_level_ids.filtered(
            lambda line:  line.status != "finished"
        )

        error_messages = []

        # if unfinished_unpacked_items:
        #     products = "\n".join(
        #         f"- {move.product_id.display_name}"
        #         for move in unfinished_unpacked_items
        #     )

        #     error_messages.append(
        #         (
        #             "Pastikan semua status item unpacked sudah 'Finished'.\n"
        #             "Unpacked item yang belum divalidasi:\n%s"
        #         )
        #         % products
        #     )

        if unfinished_packed_items:
            packages = "\n".join(
                f"- {line.package_id.name}"
                for line in unfinished_packed_items
            )

            error_messages.append(
                (
                    "Pastikan semua status package sudah 'Finished'.\n"
                    "Package belum divalidasi:\n%s"
                )
                % packages
            )

        if error_messages:
            raise UserError("\n\n".join(error_messages))
    

    def _get_acumatica_config(self):

        config = self.env["ir.config_parameter"].sudo()

        return {
            "auth_url": config.get_param("tijari.acumatica_auth_url"),
            "api_url": config.get_param("tijari.acumatica_api_url"),
            "login_endpoint": config.get_param("tijari.acumatica_endpoint_login"),
            "shipment_endpoint": config.get_param("tijari.acumatica_endpoint_shipment"),
            "username": config.get_param("tijari.acumatica_username"),
            "password": config.get_param("tijari.acumatica_password"),
            "company": config.get_param("tijari.acumatica_company"),
        }

    def action_login_acumatica(self):
        """Authenticate to Acumatica and return authenticated session."""

        config = self._get_acumatica_config()

        required = (
            "auth_url",
            "login_endpoint",
            "username",
            "password",
            "company",
        )

        for key in required:
            if not config.get(key):
                raise UserError(
                    _("Acumatica configuration '%s' is missing.") % key
                )

        login_url = (
            config["auth_url"]
            + config["login_endpoint"]
        )

        payload = {
            "name": config["username"],
            "password": config["password"],
            "company": config["company"],
        }

        session = requests.Session()

        _logger.info("=" * 80)
        _logger.info("ACUMATICA LOGIN")
        _logger.info("POST %s", login_url)

        try:

            response = session.post(
                login_url,
                json=payload,
                timeout=60,
            )

            _logger.info("Status Code : %s", response.status_code)
            _logger.info("Response    : %s", response.text)
            _logger.info("=" * 80)

            response.raise_for_status()

            return session

        except requests.exceptions.RequestException as exc:

            error_message = ""

            try:
                error_message = response.text
            except Exception:
                pass

            raise UserError(_(
                "Failed to login to Acumatica.\n\n%s"
            ) % (error_message or str(exc)))

    def action_get_shipment_completed_staging(self):
        self.ensure_one()

        ShipmentCompleted = self.env['acumatica.shipment.completed.staging']
        shipment_nbr = self.origin

        staging_data = ShipmentCompleted.search([('shipment_nbr','=',shipment_nbr)], limit=1)
        staging_data_line = staging_data.line_ids

        all_match = True
        for move in self.move_ids:
            staging_lines_for_product = staging_data_line.filtered(
                lambda line:
                    line.line_inventory_id == move.product_id.default_code
            )

            if not staging_lines_for_product:
                staging_qty = 0.0
                all_match = False

            staging_qty = sum(
                staging_lines_for_product.mapped('line_quantity')
            )

            if not staging_qty:
                staging_qty = 0.0
                all_match = False
                # return {
                #     'type': 'ir.actions.client',
                #     'tag': 'display_notification',
                #     'params': {
                #         'title': 'Gagal Melakukan Update',
                #         'message': f'Quantity {move.product_id.display_name} yang diupdate tidak memiliki quantity',
                #         'type': 'warning',
                #         'sticky': False,
                #     }
                # }

            if staging_qty < move.quantity:
                all_match = False
            
            elif staging_qty > move.quantity:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': 'Gagal Melakukan Update',
                        'message': f'Quantity {move.product_id.display_name} yang diupdate tidak boleh melebihi quatity barang yang dikirim',
                        'type': 'warning',
                        'sticky': False,
                    }
                }
            
            move.shipped_qty = staging_qty

        self.shipment_information = (
            "full_shipment"
            if all_match
            else "partial"
        )

        self.action_confirm_shipment()   
    # transfer shipment lama
    # def action_complete_transfer_shipment(self):
    #     self.ensure_one()
    #     if not (
    #         self.internal_transfer_order_id
    #         and self.internal_transfer_leg == 'delivery'
    #         and self.picking_type_id.code == 'internal'
    #         and self.adt_type == 'Transfer Shipment'
    #         and self.wms_state == 'waiting_response'
    #     ):
    #         raise UserError(_('Shipment completion is only available for a Transfer Shipment ITO delivery waiting for shipment.'))

    #     staging_data = self.env['acumatica.shipment.transfer.completed.staging'].search(
    #         [('shipment_nbr', '=', self.origin)], limit=1,
    #     )
    #     if not staging_data:
    #         raise UserError(_(
    #             'Dokumen %(document)s tidak ditemukan di staging Shipment Transfer Completed.'
    #         ) % {'document': self.origin})

    #     staging_quantities = {}
    #     staging_matches_document = bool(staging_data)
    #     if staging_data:
    #         # Acumatica can return multiple detail rows for the same product.
    #         # Merge those rows by InventoryID before comparing with the moves.
    #         for line in staging_data.line_ids:
    #             inventory_id = (line.line_inventory_id or '').strip()
    #             if inventory_id:
    #                 staging_quantities[inventory_id] = (
    #                     staging_quantities.get(inventory_id, 0.0)
    #                     + line.line_quantity
    #                 )

    #     move_quantities = {}
    #     for move in self.move_ids:
    #         inventory_id = (move.product_id.default_code or '').strip()
    #         move_quantities[inventory_id] = (
    #             move_quantities.get(inventory_id, 0.0) + move.quantity
    #         )

    #     if not staging_data:
    #         staging_matches_document = False
    #     elif set(staging_quantities) != set(move_quantities):
    #         staging_matches_document = False
    #     for inventory_id, move_qty in move_quantities.items():
    #         staging_qty = staging_quantities.get(inventory_id, 0.0)
    #         product_moves = self.move_ids.filtered(
    #             lambda move: (move.product_id.default_code or '').strip() == inventory_id
    #         )
    #         rounding = product_moves[:1].product_uom.rounding
    #         if not staging_qty or float_compare(
    #             staging_qty,
    #             move_qty,
    #             precision_rounding=rounding,
    #         ) != 0:
    #             staging_matches_document = False

    #     if staging_matches_document:
    #         if not self.approval_request_id:
    #             self.transfer_shipment_discrepancy = False
    #         for move in self.move_ids:
    #             # The staging total was checked per product above. Distribute
    #             # the confirmed total to each move using its own document qty.
    #             move.shipped_qty = move.quantity
    #         self.shipment_information = 'full_shipment'
    #         return self.action_confirm_shipment()

    #     all_match = True
    #     for move in self.move_ids:
    #         if move.shipped_qty < 0:
    #             raise UserError(_('Shipped Qty tidak boleh kurang dari nol.'))
    #         if float_compare(
    #             move.shipped_qty,
    #             move.quantity,
    #             precision_rounding=move.product_uom.rounding,
    #         ) != 0:
    #             all_match = False

    #     self.shipment_information = 'full_shipment' if all_match else 'partial'
    #     self.transfer_shipment_discrepancy = True
    #     self.wms_state = 'waiting_approval'
    #     if not self.approval_request_id:
    #         self._submit_transfer_shipment_discrepancy_approval()
    #     return {'type': 'ir.actions.client', 'tag': 'reload'}

    def action_get_transfer_shipment_completed_staging(self):
        self.ensure_one()
        if not (
            self.internal_transfer_order_id
            and self.internal_transfer_leg == 'delivery'
            and self.picking_type_id.code == 'internal'
            and self.adt_type == 'Transfer Shipment'
            and self.wms_state == 'waiting_response'
        ):
            raise UserError(_('Shipment completion is only available for a Transfer Shipment ITO delivery waiting for shipment.'))

        staging_data = self.env['acumatica.shipment.transfer.completed.staging'].search(
            [('shipment_nbr', '=', self.origin)], limit=1,
        )
        if not staging_data:
            raise UserError(_(
                'Dokumen %(document)s tidak ditemukan di staging Shipment Transfer Completed.'
            ) % {'document': self.origin})

        staging_quantities = {}
        # Acumatica can return multiple detail rows for a product. Aggregate
        # by InventoryID before distributing the reported quantity to moves.
        for line in staging_data.line_ids:
            inventory_id = (line.line_inventory_id or '').strip()
            if inventory_id:
                staging_quantities[inventory_id] = (
                    staging_quantities.get(inventory_id, 0.0)
                    + line.line_quantity
                )

        move_quantities = {}
        for move in self.move_ids:
            inventory_id = (move.product_id.default_code or '').strip()
            move_quantities[inventory_id] = (
                move_quantities.get(inventory_id, 0.0) + move.quantity
            )

        # Match the normal Shipment Completed behavior: compare every
        # document product, while ignoring staging-only extra product rows.
        staging_matches_document = True
        for inventory_id, move_qty in move_quantities.items():
            staging_qty = staging_quantities.get(inventory_id, 0.0)
            product_moves = self.move_ids.filtered(
                lambda move: (move.product_id.default_code or '').strip() == inventory_id
            )
            rounding = product_moves[:1].product_uom.rounding
            if float_compare(
                staging_qty,
                move_qty,
                precision_rounding=rounding,
            ) != 0:
                staging_matches_document = False
            if float_compare(
                staging_qty,
                move_qty,
                precision_rounding=rounding,
            ) > 0:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _('Gagal Melakukan Update'),
                        'message': _(
                            'Quantity %(product)s yang diupdate tidak boleh melebihi quantity barang yang dikirim.'
                        ) % {'product': product_moves[:1].product_id.display_name},
                        'type': 'warning',
                        'sticky': False,
                    },
                }

        # Persist the actual shipped quantity from staging, including zero for
        # products missing from the response.
        for move in self.move_ids:
            inventory_id = (move.product_id.default_code or '').strip()
            product_moves = self.move_ids.filtered(
                lambda candidate: (candidate.product_id.default_code or '').strip() == inventory_id
            )
            total_move_qty = sum(product_moves.mapped('quantity'))
            total_staging_qty = staging_quantities.get(inventory_id, 0.0)
            move.shipped_qty = (
                move.quantity * total_staging_qty / total_move_qty
                if total_move_qty
                else 0.0
            )

        self.shipment_information = 'full_shipment' if staging_matches_document else 'partial'
        self.transfer_shipment_discrepancy = False
        return self.with_context(
            transfer_shipment_completed_from_staging=True,
        ).action_confirm_shipment()

    # Keep existing RPC entrypoints compatible while sharing one completion flow.
    def action_complete_transfer_shipment(self):
        return self.action_get_transfer_shipment_completed_staging()

    def action_confirm_shipment_manually(self):
        return self.action_complete_transfer_shipment()

    def action_fetch_actual_qty(self):
        self.ensure_one()

        data = self._get_actual_qty_data()

        self._apply_actual_qty(data)

    def action_fetch_partial_shipment(self):
        self.ensure_one()

        data = self._get_actual_qty_data(is_match=False)

        self._apply_actual_qty(data)


    def _get_actual_qty_data(self, is_match=True):
        """
        Temporary mock implementation.
        Replace with Acumatica API once available.
        """

        import random

        result = []

        for move in self.move_ids:

            planned_qty = move.product_uom_qty

            # is_match = random.random() < 0.5

            if is_match:
                actual_qty = planned_qty
            else:
                # shortage = random.randint(
                #     1,
                #     min(5, int(planned_qty))
                # )
                actual_qty = planned_qty - 1

            result.append({
                "inventory_id": move.product_id.default_code,
                "actual_qty": actual_qty,
            })

        return result
    
    def _apply_actual_qty(self, data):

        mapping = {
            item["inventory_id"]: item
            for item in data
        }

        all_match = True

        for move in self.move_ids:

            api = mapping.get(move.product_id.default_code)

            if not api:
                continue

            actual_qty = api["actual_qty"]

            move.shipped_qty = actual_qty

            if actual_qty != move.product_uom_qty:
                all_match = False

        self.shipment_information = "full_shipment" if all_match else "partial"

    def _generate_return_from_actual_qty(self):
        ReturnWizard = self.env["stock.return.picking"]

        wizard = ReturnWizard.with_context(
            active_id=self.id,
            active_ids=self.ids,
            active_model="stock.picking",
        ).create({})

        for line in wizard.product_return_moves:
            move = line.move_id
            actual = move.shipped_qty
            planned = move.quantity
            line.quantity = max(planned - actual, 0)

        result = wizard.with_context(
            return_from_actual_qty=True
        ).sudo().action_create_returns()

        return result

    def _create_return(self):
        for return_move in self.product_return_moves.move_id:
            return_move.move_dest_ids.filtered(
                lambda m: m.state not in ('done', 'cancel')
            )._do_unreserve()

        new_picking = self.picking_id.copy(
            self._prepare_picking_default_values()
        )

        new_picking.user_id = False

        new_picking.message_post_with_source(
            'mail.message_origin_link',
            render_values={
                'self': new_picking,
                'origin': self.picking_id,
            },
            subtype_xmlid='mail.mt_note',
        )

        returned_lines = False

        for return_line in self.product_return_moves:
            if return_line._process_line(new_picking):
                returned_lines = True

        if not returned_lines:
            raise UserError(
                _("Tidak boleh ada quantity nol.")
            )

        # Hanya return dari _generate_return_from_actual_qty
        # yang langsung tetap draft.
        if not self.env.context.get('return_from_actual_qty'):
            new_picking.action_confirm()
            new_picking.action_assign()

        new_picking.origin = f"Return of {self.picking_id.origin if self.picking_id.origin else self.picking_id.name}"

        return new_picking

    def _check_putaway_status_requirements(self):
        self.ensure_one()
        move_lines = self.move_line_ids.filtered(
            lambda line: line.state != "cancel" and line.quantity > 0
        )
        unfinished_lines = move_lines.filtered(
            lambda line: line.putaway_status != "finished"
        )

        if not unfinished_lines:
            return

        lines = "\n".join(
            "- %s | Qty: %s | Status: %s"
            % (
                line.product_id.display_name,
                line.quantity,
                dict(line._fields["putaway_status"].selection).get(
                    line.putaway_status,
                    line.putaway_status or "-"
                ),
            )
            for line in unfinished_lines
        )
        raise UserError(_(
            "Validation blocked.\n\n"
            "Semua move line putaway harus memiliki status 'Finished'.\n"
            "Move line yang belum finish:\n%s"
        ) % lines)

    def _check_storage_pick_completed(self):
        self.ensure_one()
        order = self.internal_transfer_order_id
        if not (
            order
            and order.transaction_type == 'intra_warehouse'
            and self.internal_transfer_leg == 'receipt'
        ):
            return

        pickings = order._get_related_pickings().filtered(
            lambda picking: picking.internal_transfer_leg == 'pick'
        )
        if not pickings or any(picking.state != 'done' for picking in pickings):
            raise UserError(_(
                "Storage belum bisa diproses sebelum Pick selesai."
            ))

    def _check_picker_status_requirements(self):
        self.ensure_one()
        move_lines = self.move_line_ids.filtered(
            lambda line: line.state != "cancel" and line.quantity > 0
        )
        unfinished_lines = move_lines.filtered(
            lambda line: line.picker_status != "finished"
        )

        if not unfinished_lines:
            return

        lines = "\n".join(
            "- %s | Qty: %s | Status: %s"
            % (
                line.product_id.display_name,
                line.quantity,
                dict(line._fields["picker_status"].selection).get(
                    line.picker_status,
                    line.picker_status or "-"
                ),
            )
            for line in unfinished_lines
        )
        raise UserError(_(
            "Validation blocked.\n\n"
            "Semua move line picker harus memiliki status 'Finished'.\n"
            "Move line yang belum finish:\n%s"
        ) % lines)


    # ================================================================================================
# Temp Storage function create move line qr code        
# ================================================================================================

    def action_print_lot_number_line(self):
        self.ensure_one()

        if not self.lot_ids:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Peringatan',
                    'message': 'Lot Number belum tersedia.',
                    'type': 'warning',
                    'sticky': False,
                }
            }

        lot_names = list({
            lot.name for lot in self.lot_ids if lot.name
        })

        if not lot_names:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Peringatan',
                    'message': 'Lot Number belum tersedia.',
                    'type': 'warning',
                    'sticky': False,
                }
            }

        self.create_qrcode_name()

        return {
            'type': 'ir.actions.act_url',
            'url': '/print/lot_number_move_line?ids=%s' % self.id,
            'target': 'new',
        }

    def create_qrcode_name(self):
        for rec in self:
            lot_names = list(set(
                rec.lot_ids.mapped('name')
            ))

            text = ', '.join(filter(None, lot_names))

            if text:
                rec.qr_code_image = rec.create_qrcode(text)

    def create_qrcode(self, text):
        logo_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), '../static/src/img/logo-company.png')
        )

        logo = Image.open(logo_path)

        basewidth = 80
        wpercent = basewidth / float(logo.size[0])
        hsize = int(float(logo.size[1]) * wpercent)

        logo = logo.resize((basewidth, hsize))

        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_H
        )

        qr.add_data(text)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white").convert('RGB')
        img = img.resize((300, 300))

        pos = (
            (img.size[0] - logo.size[0]) // 2,
            (img.size[1] - logo.size[1]) // 2
        )

        img.paste(logo, pos)

        buffer = io.BytesIO()
        img.save(buffer, format="PNG")

        return base64.b64encode(buffer.getvalue()).decode()

    def action_test_function(self):
        return True
    
class StockReturnPicking(models.TransientModel):
    _inherit = 'stock.return.picking'

    def _prepare_picking_default_values(self):
        vals = super()._prepare_picking_default_values()
        source_origin = self.picking_id.origin or ''
        vals['origin'] = _("Return of %(origin)s", origin=source_origin)
        return vals


class StockRule(models.Model):
    _inherit = "stock.rule"

    # Override method _push_prepare_move_copy_values untuk mengubah lokasi tujuan push berdasarkan adt_dest_location_id di stock.picking jika picking type incoming dan adt_dest_location_id terisi, sehingga bisa push ke lokasi selain location_dest_id yang sudah ditentukan di stock.rule
    def _push_prepare_move_copy_values(self, move_to_copy, new_date,):
        vals = super(StockRule, self)._push_prepare_move_copy_values(move_to_copy, new_date)
        print('='*300)
        print("_push_prepare_move_copy_values")
        print(f"Vals to create new picking: {vals}")
        # print(f"Parent Picking: {move_to_copy}")

        picking = move_to_copy.picking_id

        print(picking)
        if picking:
            # print("=== DAFTAR FIELD DAN NILAI ===")
            # for field_name, field_object in picking._fields.items():
            #     # Mengambil nilai field
            #     val = picking[field_name]
            
            #     if field_object.type == 'many2one' and val:
            #         print(f"{field_name}: {val.display_name} (ID: {val.id})")
            #     else:
            #         print(f"{field_name}: {val}")

            adt_type = picking.adt_type


            if adt_type:
                vals.update({
                    "adt_type": picking.adt_type if picking.adt_type else picking.name,
                    "transaction_type": picking.transaction_type,
                    # "adt_type": picking.adt_type
                })

        if (picking and picking.adt_dest_location_id and picking._allow_custom_push_destination()):
            vals.update({
                "location_dest_id": picking.adt_dest_location_id.id,
                "adt_dest_location_id": picking.adt_dest_location_id.id,
                # "adt_dest_location_id": picking.move_ids.adt_dest_location_id.id,
                "origin": picking.origin if picking.origin else picking.name
            })

        if move_to_copy.production_id and not move_to_copy.raw_material_production_id:
            mapped_destination = self.env["stock.location"].browse(
                vals.get("location_dest_id") or self.location_dest_id.id
            )
            if mapped_destination:
                vals["adt_dest_location_id"] = mapped_destination.id

        print(f"vals to update: {vals}")

        return vals


class StockPickingAcumaticaError(models.Model):
    _name = 'stock.picking.acumatica.error'
    _description = 'Error log untuk data picking yang diimport dari Acumatica'

    partner = fields.Char(string='Contact')
    operation_type = fields.Selection([
        ('receipt', 'Receipt'),
        ('delivery', 'Delivery'),
        ('internal', 'Internal'),
        ('manufacturing', 'Manufacturing'),
        ('inventory', 'Inventory'),
    ], string='Operation Type')

    location = fields.Char(string='Source Location')
    location_dest = fields.Char(string='Destination Location')
    scheduled_date = fields.Datetime(string='Scheduled Date')
    origin = fields.Char(string='Source Document')
    adt_total_qty = fields.Float(string='Qty Total (Acumatica)')
    adt_actual_qty = fields.Float(string='Actual Qty (Acumatica)')
    state = fields.Selection([
        ('new', 'New'),
        ('resolved', 'Resolved')
    ], default='new')

    api_acumatica = fields.Selection([
        ('po_receipt', 'PO Receipt'),
        ('so_transfer', 'SOTR Receipt'),
        ('receipt_change_item', 'Receipt Type Change Item'),
        ('inventory_received', 'Inventory Receive'),
        ('shipment_so', 'Shipment SO Return'),
        ('so_shipment', 'Sales Order Shipment'),
        ('transfer_shipment', 'Transfer Shipment'),
        ('issue_change_item', 'Issue Type Change Item'),
        ('inventory_issue', 'Inventory Issue'),
        ('issue_po_return', 'Issue Type PO Return'),
        ('transfer_one_step', 'Transfer Pinjaman (1 Step)'),
        ('transfer_two_step', 'Transfer Pinjaman (2 Step)'),
        ('direct_transfer', 'Direct Transfer'),
        ('vendor_return', 'Vendor Return'),
        ('credit_memo_consignment', 'Credit Memo Consignment'),
        ('invoice_consignment', 'Invoice Consignment'),
        ('toko_pegang_do', 'Toko Pegang DO'),
        ('inventory_receipt_transfer_wh_in', 'Inventory Receipt Transfer WH In'),
        ('kit_assembly', 'Kit Assembly'),
        ('inventory_stock', 'Inventory Stock'),
    ], string='API Acumatica', required=True)

    error_message = fields.Text(string='Error Message')
    line_ids = fields.One2many('stock.picking.acumatica.error.line', 'error_id', string='Error Lines')
    payload = fields.Json()

class StockPickingAcumaticaErrorLine(models.Model):
    _name = "stock.picking.acumatica.error.line"

    error_id = fields.Many2one(
        "stock.picking.acumatica.error",
        ondelete="cascade"
    )
    inventory_id = fields.Char(string="Product")
    product_name = fields.Char()
    qty = fields.Float(string="Demand")
    uom = fields.Char()
    payload = fields.Json()


class ApprovalRequest(models.Model):
    _inherit = 'approval.request'

    def action_approve(self):
        self.ensure_one()
        if self.source_model == 'stock.picking':
            picking = self.env['stock.picking'].browse(self.source_id).exists()
            if picking and picking.stock_lebih and not (picking.ref_sotr or '').strip():
                raise UserError(
                    _('Ref. No. Document wajib diisi sebelum dokumen Over-Receipt dapat disetujui.')
                )
            if picking and picking.transfer_shipment_discrepancy and not picking.attachment_ids:
                raise UserError(_(
                    'Lampiran wajib diisi sebelum discrepancy quantity Transfer Shipment dapat disetujui.'
                ))
        return super().action_approve()
