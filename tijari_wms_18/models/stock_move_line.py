
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare
from odoo.tools.float_utils import float_is_zero
import logging
import traceback


_logger = logging.getLogger(__name__)

class StockMoveLine(models.Model):
    _inherit  = 'stock.move.line'
    _description = "Stock Move Line"
    wave_picking_type_id = fields.Many2one(
        'stock.picking.type',
        related='picking_id.picking_type_id',
        string='Operation Type',
        store=True,
        index=True,
        readonly=True,
    )
    batch_adt_type = fields.Char(
        related='picking_id.adt_type', string='Tipe Transaksi', readonly=True,
    )
    batch_origin = fields.Char(
        related='picking_id.origin', string='No. Transaksi', readonly=True,
    )
    # Kept stored so databases with the previous embedded-list ordering can
    # validate their existing view architecture during the next module upgrade.
    picker_batch_sort_key = fields.Char(
        string='Batch Picker Sort Key',
        compute='_compute_picker_batch_sort_key',
        store=True,
    )
    picker_location_sort_key = fields.Char(
        string='Picker Location Sort Key',
        compute='_compute_picker_client_sort_keys',
    )
    picker_product_sort_key = fields.Char(
        string='Picker Product Sort Key',
        compute='_compute_picker_client_sort_keys',
    )
    picker_default_sort_key = fields.Char(
        string='Picker Default Sort Key',
        compute='_compute_picker_client_sort_keys',
    )
    # Bisa dihapus sebelum golive
    adt_dest_location_id = fields.Many2one(
        "stock.location",
        related="move_id.adt_dest_location_id",
        store=True,
        readonly=True,
    )

    adt_dest_location = fields.Char(
        string="Mapped Location",
        related="move_id.adt_dest_location",
        store=True,
        readonly=True,
    )

    remaining_picker_quantity = fields.Float(
        string="Sisa Qty",
        related="move_id.remaining_picker_quantity",
        readonly=True,
    )
    lot_button = fields.Char(string="Lot Button", compute="_compute_lot_button")
    auto_input_code = fields.Boolean(string="Auto Input Code")

    confirmation_location_code = fields.Char(
        string='Confirmation Location Code',
        help='Scanned / input location code for confirmation.',
        store=True
    )
    handheld_scan_code = fields.Char(
        string='Scan Handheld',
        help='Masukkan kode lot, lokasi, atau package dari scanner handheld atau ketik manual.',
        store=False,
    )
    putaway_scan_code = fields.Char(
        string='Putaway Scan Code',
        related='picking_id.putaway_scan_code',
    )
    confirmation_lot_code = fields.Char(
        string='Confirmation Lot Code',
        help='Scanned / input lot code for confirmation.',
        store=True
    )
    confirmation_package_code = fields.Char(
        string='Confirmation Package Code',
        help='Scanned / input package code for destination package.',
        store=True,
    )
    # matching = fields.Selection([
    #     ('match', 'Match'),
    #     ('not_match', 'Not Match')
    # ], string='Matching Status',
    #    store=True)

    matching = fields.Selection(
        [
            ('match', 'Match'),
            ('not_match', 'Not Match')
        ],
        store=True,
        default="not_match"
    )
    putaway_status = fields.Selection(
        [
            ('finished', 'Finished'),
            ('unfinished', 'Unfinished')
        ],
        string='Putaway Status',
        default='unfinished',
    )

    picker_status = fields.Selection(
        [
            ('finished', 'Finished'),
            ('unfinished', 'Unfinished')
        ],
        string='Picker Status',
        default='unfinished',
    )
    location_putaway_id = fields.Many2one(
        "stock.location",
        string="Putaway Location",
    )

    operation_category = fields.Selection(related='picking_id.picking_type_id.operation_category', string='Operation Category', store=True, readonly=True)

    allocation_storage = fields.Many2one(
        "stock.location",
        string="Allocation Storage",
    )

    allocation_id = fields.Char(string='Allocation ID')
    allocation_note = fields.Text(string='Allocation Note')
    allocation_Description = fields.Text(string='Allocation Description')
    allocation_InventoryID = fields.Char(string='Allocation Inventory ID')
    allocation_LocationID = fields.Char(string='Allocation Location ID')
    allocation_Quantity = fields.Float(string='Allocation Quantity')
    allocation_SplitLineNbr = fields.Integer(string='Allocation Split Line Number')
    allocation_UOM = fields.Char(string='Allocation UOM')
    product_uom_qty = fields.Float(
        string='Demand',
        related='move_id.product_uom_qty',
        readonly=True,
    )
    remaining_putaway_quantity = fields.Float(
        string='Sisa Qty',
        related='move_id.remaining_putaway_quantity',
        readonly=True,
    )
    confirmation_quantity = fields.Float(string='Confirmation Qty')
    so_confirmation_quantity = fields.Float(string='Qty Dikirim')
    mo_confirmation_quantity = fields.Float(string='Qty Digunakan')
    quantity_shadow = fields.Float(string='Qty Digunakan')

    picking_code = fields.Selection(
        related='picking_id.picking_type_code',
        store=False,
    )

    origin = fields.Char(
        related='picking_id.origin',
        string='Source Document',
        readonly=True,
        store=True,
    )
    matching_lot_id = fields.Many2one(
        comodel_name="stock.lot",
        string="Confirm Lot",
        domain="[('id', 'in', allowed_matching_lot_ids)]",
        store=True
    )
    matching_loc_dest_id = fields.Many2one(
        comodel_name="stock.location",
        string="Confirm Location",
        domain="[('id', 'child_of', adt_dest_location_id)]",
        store=True
    )
    allowed_matching_lot_ids = fields.Many2many(
        comodel_name="stock.lot",
        string="Allowed Matching Lots",
        compute="_compute_allowed_matching_lot_ids",
    )

    unfiltered_dest = fields.Boolean(
        string="Unfiltered Destination",
        default=False,
    )

    source_location_id = fields.Many2one(
        "stock.location",
        string="Source Location",
        domain="[('id', 'child_of', location_id)]",
    )

    product_lot_id = fields.Many2one(
        "stock.lot",
        string="Lot",
        domain="[('id', 'in', temp_lot_ids)]",
    )

    selected_quant_id = fields.Many2one(
        "stock.quant",
        string="Selected Quant",
        domain="[('id', 'in', temp_quant_ids)]",
        readonly=True,
    )

    temp_quant_ids = fields.Many2many(
        "stock.quant",
        string="Temporary Quants",
        compute="_compute_temp_quant_ids",
        store=False,
    )
    temp_lot_ids = fields.Many2many(
        "stock.lot",
        string="Temporary Lots",
        compute="_compute_temp_quant_ids",
        store=False,
    )

    available_lot_ids = fields.Many2many(
        "stock.lot",
        compute="_compute_available_quant_fields",
    )

    available_location_ids = fields.Many2many(
        "stock.location",
        compute="_compute_available_quant_fields",
    )

    picking_type_name = fields.Char(
        related="move_id.picking_type_name",
        store=False,
    )
   
    allowed_lot_ids = fields.Many2many(
        "stock.lot",
        string="Allowed Lots",
        compute="_compute_allowed_lot_ids",
    )

    second_matching = fields.Char(string="Status")

    adt_owner_id = fields.Many2one(
        "res.partner",
        related="move_id.owner_id",
        store=True,
        readonly=True,
    )

    mo_status = fields.Selection(
        [
            ('finished', 'Finished'),
            ('unfinished', 'Unfinished')
        ],
        string='MO Status',
        default='unfinished',
    )

    pick_status = fields.Selection(
        [
            ('finished', 'Finished'),
            ('unfinished', 'Unfinished')
        ],
        string='Pick Status',
        default='unfinished',
    )

    status_check = fields.Selection(
        [
            ('finished', 'Finished'),
            ('unfinished', 'Unfinished')
        ],
        string='Unpack Status',
        default='unfinished',
    )

    description_picking = fields.Text(
        string='Description of Picking',
        compute='_compute_description_picking_from_product',
        store=False,
    )

    @api.depends('move_id', 'move_id.description_picking')
    def _compute_description_picking_from_product(self):
        for move in self:
            move.description_picking = move.move_id.description_picking or False

    is_manufacturing = fields.Boolean(
        compute="_compute_is_manufacturing",
    )

    @api.depends(
        "move_id.raw_material_production_id",
        "move_id.production_id",
    )
    def _compute_is_manufacturing(self):
        for line in self:
            line.is_manufacturing = bool(
                line.move_id.raw_material_production_id
                or line.move_id.production_id
            )

    @api.depends(
        "move_id",
        "move_id.move_line_ids.lot_id",
    )

    def _compute_allowed_lot_ids(self):
        print('def _compute_allowed_lot_ids')
        for line in self:
            if not line.move_id:
                line.allowed_lot_ids = False
                continue

            lots = line.move_id.move_line_ids.mapped("lot_id")
            line.allowed_lot_ids = lots
            

    @api.depends(
        "picking_id.move_line_ids.lot_id",
        "picking_id.move_ids.move_line_ids.lot_id",
        "move_id.move_line_ids.lot_id",
    )
    def _compute_allowed_matching_lot_ids(self):
        for line in self:
            line.allowed_matching_lot_ids = (
                line.picking_id.move_line_ids.mapped("lot_id")
                | line.move_id.move_line_ids.mapped("lot_id")
            )

    def _get_owner_quant_domain(self):
        self.ensure_one()
        domain = [
            ("product_id", "=", self.product_id.id),
            ("location_id", "child_of", self.source_location_id.id),
            ("quantity", ">", 0),
            ("lot_id", "!=", False),
        ]
        if self.move_id.picking_id.adt_type == 'Toko Pegang DO' and self.move_id.owner_id:
            domain.append(("owner_id", "=", self.move_id.owner_id.id))
        return domain

    @api.depends("product_id", "source_location_id", "move_id.owner_id", "move_id.picking_id.adt_type")
    def _compute_temp_quant_ids(self):
        print('def _compute_temp_quant_ids(self):')
        for line in self:
            if line.move_id.transaction_type == 'outbound' and line.move_id.picking_type_name == 'Pack':
                print('Auto matching source_location_id and product_lot_id in Pack transaction')
                print(line.location_id.id)
                print(line.lot_id.id)
                line.source_location_id = line.location_id
                line.product_lot_id = line.lot_id.id
                line.lot_name = line.lot_id.name

        # _logger.info("Running Onchange on pick type: %s", self.picking_type_name)
        Quant = self.env["stock.quant"]

        for line in self:

            line.temp_quant_ids = False
            line.temp_lot_ids = False

            if not line.product_id or not line.source_location_id:
                continue

            domain = line._get_owner_quant_domain()

            # ===========================
            # TAMBAHAN UNTUK TOKO PEGANG DO
            # ===========================
            print(line.move_id.picking_id.adt_type)
            if line.move_id.picking_id.adt_type == 'Toko Pegang DO' and line.move_id.owner_id:
                print(f"Running domain with owner : {line.move_id.owner_id.name} ")
            # ===========================

            quants = Quant.search(domain)

            line.temp_quant_ids = [(6, 0, quants.ids)]
            line.temp_lot_ids = [(6, 0, quants.mapped("lot_id").ids)]

            for item in line.temp_quant_ids:
                print(f"Quant domained: {item.lot_id.name}")

            print(f"Quant domain: {line.temp_quant_ids}")

    @api.onchange("lot_id", "location_id")
    def _onchange_quant_fields(self):

        return self._refresh_quant()

    @api.onchange("product_id", "source_location_id")
    def _onchange_source_location(self):
        print('def _onchange_source_location(self)')
        print(f"Search product: {self.product_id.name} in loc: {self.source_location_id.name}")
        self.product_lot_id = False
        self.selected_quant_id = False
        # self.quant_id = False

        if not self.product_id or not self.source_location_id:
            return {
                "domain": {
                    "product_lot_id": [("id", "=", False)]
                }
            }

        quants = self.env["stock.quant"].search(self._get_owner_quant_domain())

        lot_ids = quants.mapped("lot_id").ids
        print(f"Lot found: {lot_ids}")

        return {
            "domain": {
                "product_lot_id": [("id", "in", lot_ids)]
            }
        }
    
    @api.onchange("product_lot_id")
    def _onchange_product_lot(self):
        print('def _onchange_product_lot(self)')
        self.selected_quant_id = False
        # self.quant_id = False

        if not (
            self.product_id
            and self.source_location_id
            and self.product_lot_id
        ):
            return

        quant = self.env["stock.quant"].search(
            self._get_owner_quant_domain() + [("lot_id", "=", self.product_lot_id.id)],
            order="quantity desc",
            limit=1,
        )
        print(f"Quant found: {quant}")

        self.selected_quant_id = quant
        self.quant_id = quant

    @api.model_create_multi
    def create(self, vals_list):
        move_lines = super().create(vals_list)
        move_lines.mapped('picking_id')._sync_adt_quantities_from_moves()
        # if not move_lines.parent_location_id:
        #     move_lines.parent_location_id = move_lines.adt_dest_location_id
        # if not move_lines.parent_location_id:
        #     for line in move_lines:
        #         line.parent_location_id = line.adt_dest_location_id

        return move_lines
        

    def write(self, vals):
        pickings = self.mapped('picking_id')
        res = super().write(vals)
        pickings |= self.mapped('picking_id')

        if {'quantity', 'move_id', 'picking_id'} & set(vals):
            pickings._sync_adt_quantities_from_moves()

        # matching_inputs = {
        #     'matching_lot_id', 'matching_loc_dest_id',
        #     'source_location_id', 'product_lot_id',
        #     'location_id', 'location_dest_id', 'lot_id',
        # }
        # if not self.env.context.get('skip_matching_sync') and matching_inputs & set(vals):
        #     for line in self:
        #         matching = line._get_matching_status() if not line.move_id.is_manufacturing else line._get_matching_status_mo()
        #         status_vals = {'matching': matching}
        #         if line.picking_id.picking_type_id.name in ('Pick', 'Pick Internal', 'Internal Transfers') or line.move_id.is_manufacturing:
        #             status_vals['second_matching'] = (
        #                 'Match' if matching == 'match' else 'Not Match'
        #             )
                
        #         if any(line[field] != value for field, value in status_vals.items()):
        #               line.with_context(skip_matching_sync=True).write(status_vals)
        
        self._update_move_picked()

        return res

    def unlink(self):
        finished_putaway_lines = self.filtered(
            lambda line: line.putaway_status == 'finished'
        )
        finished_pick_lines = self.filtered(
            lambda line: line.pick_status == 'finished'
        )
        if finished_putaway_lines:
            raise ValidationError(_(
                "Data tidak bisa dihapus karena Putaway sudah Finished. "
                "Rollback Putaway terlebih dahulu."
            ))

        if finished_pick_lines:
            raise ValidationError(_(
                "Data tidak bisa dihapus karena Pick sudah Finished. "
                "Rollback Pick terlebih dahulu."
            ))

        pickings = self.mapped('picking_id')
        res = super().unlink()
        pickings._sync_adt_quantities_from_moves()
        return res

    # @api.onchange('confirmation_lot_code', 'confirmation_location_code', 'matching', 'auto_input_code')
    # def _compute_matching(self):
    #     print(">>> Computing matching status...") # Cek di terminal Odoo
    #     for line in self:
    #         picking_type = line.picking_id.picking_type_id

    #         # 1. Validasi awal: pastikan input user tidak kosong atau hanya berisi spasi
    #         conf_loc = str(line.confirmation_location_code or '').strip()
    #         conf_lot = str(line.confirmation_lot_code or '').strip()

    #         loc_ok = False
    #         lot_ok = False
            
    #         if not conf_loc or not conf_lot:
    #             line.matching = 'not_match'
    #             continue
            
    #         if picking_type and picking_type.name in ('Pick'):
    #             print('run validate code for pick')
    #             loc_code = str(line.location_id.unique_code or '').strip()
    #             product_code = str(line.lot_id.name or '').strip()

    #             loc_ok = bool(loc_code and conf_loc.lower() == loc_code.lower())
    #             lot_ok = bool(product_code and conf_lot.lower() == product_code.lower())

    #         if picking_type and picking_type.name in ('Receipts'):
    #             print('run validate code for receipt')
    #             loc_code = str(line.location_dest_id.unique_code or '').strip()
    #             product_code = str(line.lot_id.name or '').strip()

    #             loc_ok = bool(loc_code and conf_loc.lower() == loc_code.lower())
    #             lot_ok = bool(product_code and conf_lot.lower() == product_code.lower())


    #         if not picking_type or picking_type.name not in ('Pick', 'Pack', 'Receipts'):
    #             raise ValueError('validation is not config yet')

    #         line.matching = 'match'

    #         # # Untuk PACK → cek lot + unique_code (aku anggap SOURCE location)
    #         # elif operation_category == 'delivery':
    #         #     print(f">>> picking_type_code: {line.picking_type_id.code if line.picking_id and line.picking_id.picking_type_id else 'N/A'}") # Cek di terminal Odoo
    #         #     print(f">>> Operation Category: DELIVERY") # Cek di terminal Odoo
    #         #     print(f">>> Evaluating line {line.id} - Product: {line.product_id.display_name}, Lot Name: {line.lot_id.name if line.lot_id else 'N/A'}") # Cek di terminal Odoo
    #         #     # kalau belum ada input sama sekali, invisible
    #         #     if line.confirmation_location_code or line.confirmation_lot_code:
    #         #         loc_code = line.location_id.unique_code if line.location_id else (line.quant_id.location_id.unique_code if line.quant_id and line.quant_id.location_id else 'N/A')
    #         #         lot_code = line.lot_id.name if line.lot_id else (line.quant_id.lot_id.name if line.quant_id and line.quant_id.lot_id else 'N/A')
    #         #         print(f">>> Information to match : Lot Code = {lot_code}, Location Code = {loc_code}") # Cek di terminal Odoo
    #         #         # src_loc = line.location_id
    #         #         # src_code = src_loc.unique_code if src_loc else False
    #         #         # lot = line.lot_id
    #         #         # src_code = line.quant_id.location_id.unique_code if line.quant_id.location_id else False
    #         #         # lot = line.quant_id.lot_id.name

    #         #         # kondisi match
    #         #         loc_ok = bool(loc_code and line.confirmation_location_code == loc_code)
    #         #         lot_ok = bool(lot_code and line.confirmation_lot_code == lot_code)

    #         #         if loc_ok and lot_ok:
    #         #             matching = 'match'
    #         #         else:
    #         #             matching = 'not_match'

    #         #         print(f">>> Location Match: {loc_ok}, Lot Match: {lot_ok}, Overall Matching Status: {matching}") # Cek di terminal Odoo

    #         # if line.matching_lot_id.id == line.lot_id.id and line.matching_loc_dest_id.id == line.location_dest_id.id:
    #         #     matching = 'match'

    #         # line.matching = matching


    @api.onchange('product_id')
    def _onchange_product_copy_previous_lot(self):
        _logger.info('def _onchange_product_copy_previous_lot(self)')

        for line in self:
            if not line.move_id:
                continue
            if line.picking_type_name != 'Receipts':
                continue

            previous_lines = line.move_id.move_line_ids.filtered(
                lambda l: l != line
            )
            last_lot_line = previous_lines.filtered(lambda l: l.lot_id)

            if last_lot_line:
                line.lot_id = last_lot_line[-1].lot_id

            if not line.env.context.get('copy_previous_picker_location'):
                continue

            last_location_line = previous_lines.filtered(
                lambda l: l.location_id or l.location_dest_id or l.source_location_id
            )
            if not last_location_line:
                continue

            previous_line = last_location_line[-1]
            if previous_line.location_id:
                line.location_id = previous_line.location_id
            if previous_line.location_dest_id:
                line.location_dest_id = previous_line.location_dest_id
            if previous_line.source_location_id:
                line.source_location_id = previous_line.source_location_id
                
    @api.onchange('quant_id')
    def _onchange_move_line_ids(self):
        for line in self:
            if not line.quant_id:
                continue

            quant = line.quant_id

            # print(
            #     f">>> Quant selected: {quant.id} | "
            #     f"lot={quant.lot_id.name if quant.lot_id else 'N/A'} | "
            #     f"package={quant.package_id.name if quant.package_id else 'N/A'} | "
            #     f"location={quant.location_id.display_name}"
            # )

            line.location_id = quant.location_id
            line.lot_id = quant.lot_id
            # line.package_id = quant.package_id
            # line.result_package_id = quant.package_id
            line.owner_id = quant.owner_id

            line.lot_name = (
                quant.lot_id.name
                if quant.lot_id
                else line.lot_name
            )
            
    @api.onchange('auto_input_code')
    def _compute_auto_input_code(self):
        print('def _compute_auto_input_code(self)')
        for line in self:
            if line.auto_input_code:
                print(f">>> Auto input code triggered for line {line.id}") # Cek di terminal Odoo
                line.matching = 'match'
                sml = self.env['stock.move.line'].browse(line._origin.id)
                sml.write({'matching': 'match'})


    def action_generate_selected_lot(self):
        for line in self:
            line.generate_lot()

        return True
        
    def generate_lot(self):
        for line in self:
            print(f"BEFORE product={line.product_id.display_name} move={line.move_id.product_id.display_name}")

            vals = line.action_generate_or_pick_lot()

            print(f"AFTER product={line.product_id.display_name} move={line.move_id.product_id.display_name}")

            if vals.get('lot_id'):
                line.lot_id = vals['lot_id']

            if vals.get('lot_name'):
                line.lot_name = vals['lot_name']

            # if vals.get('confirmation_location_code'):
            #     line.confirmation_location_code = vals['confirmation_location_code']

            # if vals.get('confirmation_lot_code'):
            #     line.confirmation_lot_code = vals['confirmation_lot_code']

    @api.onchange('lot_button')
    def _onchange_lot_button(self):
        print('def _onchange_lot_button(self)')
        print(">>> TOMBOL GENERATE LOT DIKLIK!") # Cek di terminal Odoo
        for line in self:
            if line.lot_button:
                vals = line.action_generate_or_pick_lot()
                print(f">>> Values returned from action_generate_or_pick_lot: {vals}") # Cek di terminal Odoo
                print(f">>> Line confirmation_location_code after generate/pick: {line.confirmation_location_code}") # Cek di terminal Odoo
                print(f">>> Line confirmation_lot_code after generate/pick: {line.confirmation_lot_code}") # Cek di terminal Odoo
                print(f">>> Line operation_category: {line.operation_category}") # Cek di terminal Odoo
                print(f">>> Lot ID assigned: {line.lot_id}") # Cek di terminal Odoo
                print(f">>> Lot Name assigned: {line.lot_name}") # Cek di terminal Odoo
                print(f">>> Confirmation Location Code assigned: {line.confirmation_location_code}") # Cek di terminal Odoo
                print(f">>> Confirmation Lot Code assigned: {line.confirmation_lot_code}") # Cek di terminal Odoo

                if vals.get('lot_id'):
                    line.lot_id = vals['lot_id']
                    print(f">>> Lot ID assigned: {line.lot_id}") # Cek di terminal Odoo
                if vals.get('lot_name'):
                    line.lot_name = vals['lot_name']
                    print(f">>> Lot Name assigned: {line.lot_name}") # Cek di terminal Odoo
                if vals.get('confirmation_location_code'):
                    line.confirmation_location_code = vals['confirmation_location_code']
                    print(f">>> Confirmation Location Code assigned: {line.confirmation_location_code}") # Cek di terminal Odoo
                if vals.get('confirmation_lot_code'):
                    line.confirmation_lot_code = vals['confirmation_lot_code']
                    print(f">>> Confirmation Lot Code assigned: {line.confirmation_lot_code}") # Cek di terminal Odoo
                line.lot_button = False



    def _compute_lot_button(self):
        # field dummy, tidak dipakai
        for rec in self:
            rec.lot_button = " "

    # ---------- Helpers ----------

    def _get_base_quant_domain(self):
            self.ensure_one()
    
            domain = [
                ("product_id", "=", self.product_id.id),
                ("location_id", "child_of", self.adt_dest_location_id.id),
                ("quantity", ">", 0),
                ("lot_id", "!=", False),
            ]
    
            if (
                self.move_id.picking_id.adt_type == "Toko Pegang DO"
                and self.move_id.owner_id
            ):
                domain.append(
                    ("owner_id", "=", self.move_id.owner_id.id)
                )
    
            return domain
    
    @api.depends(
        "product_id",
        "source_location_id",
        "lot_id",
        "location_id",
    )
    def _compute_available_quant_fields(self):

        Quant = self.env["stock.quant"]

        for line in self:

            line.available_lot_ids = False
            line.available_location_ids = False

            if (
                not line.product_id
                or not line.location_id
            ):
                continue

            # ----------------------------------
            # Available Lots
            # ----------------------------------

            lot_domain = line._get_base_quant_domain()

            lots = Quant.search(lot_domain).mapped("lot_id")

            line.available_lot_ids = [(6, 0, lots.ids)]

            # ----------------------------------
            # Available Locations
            # ----------------------------------
            
            Location = self.env['stock.location']
            # location_domain = line._get_base_quant_domain()

            locations = Location.search(
                [('id', 'child_of', line.adt_dest_location_id.id)]
            )

            line.available_location_ids = [
                (6, 0, locations.ids)
            ]

    def _refresh_quant(self):
        self.ensure_one()

        Quant = self.env['stock.quant']
        quants = self._get_base_quant_domain()
        available_lots = Quant.search(quants).mapped("lot_id")
        available_locations = self.env['stock.location'].search(
            [('id', 'child_of', self.adt_dest_location_id.id)]
        )

        # ------------------------------------------------------
        # Sync Quant
        # ------------------------------------------------------

        # self.quant_id = False

        # if self.lot_id and self.location_id:

        #     quant = self.env["stock.quant"].search([
        #         ("product_id", "=", self.product_id.id),
        #         ("lot_id", "=", self.lot_id.id),
        #         ("location_id", "=", self.location_id.id),
        #         ("quantity", ">", 0),
        #     ], limit=1)

        #     self.quant_id = quant

        return {
            "domain": {
                "lot_id": [
                    ("id", "in", available_lots.ids)
                ],
                "location_id": [
                    ("id", "in", available_locations.ids)
                ],
            }
        }

    def _tjr_is_incoming_tracked(self):
        print(f">>> Checking if line {self.id} is incoming tracked...") # Cek di terminal Odoo
        self.ensure_one()
        print(f">>> Line {self.id} - Product: {self.product_id.display_name} - Lot id {self.lot_id.name if self.lot_id else 'N/A'} - Stock Quantity: {self.quantity}") # Cek di terminal Odoo
        # Cek tipe picking dari berbagai sumber (picking_id atau dari context)
        picking_type = self.picking_id.picking_type_id or self.move_id.picking_type_id
        print(f">>> Line {self.id} - Picking Type: {picking_type.name if picking_type else 'N/A'}") # Cek di terminal Odoo
        is_internal_transfer_receipt = (
            self.picking_id
            and 'internal_transfer_leg' in self.picking_id._fields
            and self.picking_id.internal_transfer_leg == 'receipt'
        )
        is_incoming = (
            picking_type.code == 'incoming'
            or self._context.get('default_picking_type_code') == 'incoming'
            or picking_type.name in ('Receipts', 'Receipt Internal', 'Storage', 'Store Finished Product')
            or is_internal_transfer_receipt
        )
        
        return (
            is_incoming
            and self.product_id
            and self.product_id.tracking != 'none'
        )

    # def _tjr_is_incoming_tracked(self):
    #     self.ensure_one()
    #     return (
    #         self.picking_id
    #         and self.picking_id.picking_type_code == 'incoming'
    #         and self.product_id
    #         and self.product_id.tracking != 'none'
    #     )

    def _tjr_is_outgoing_or_internal_tracked(self):
        self.ensure_one()
        return (
            self.picking_id
            and self.picking_id.picking_type_code in ('outgoing', 'internal')
            and self.product_id
            and self.product_id.tracking != 'none'
        )

    def _tjr_get_po_number(self):
        self.ensure_one()
        po = self.move_id.purchase_line_id.order_id if self.move_id and self.move_id.purchase_line_id else False
        return po.name if po else (self.picking_id.origin or self.picking_id.name or 'PO')

    def _tjr_build_lot_name(self):
        self.ensure_one()
        # Ambil nomor PO
        po_number = self._tjr_get_po_number()

        # Ambil ID purchase line jika ada, fallback ke stock move line id
        po_line_id = (
            self.move_id.purchase_line_id.id
            if self.move_id and self.move_id.purchase_line_id
            else self.id
        )

        # Ambil nomor sequence unik
        seq = self.env['ir.sequence'].next_by_code('tijari.lot.serial') \
            or fields.Datetime.now().strftime('%Y%m%d%H%M%S')

        return f"{po_number} - {po_line_id} - {seq}"

    # ---------- Main button ----------
    def action_generate_or_pick_lot(self):
        """
        Incoming (receipt): generate lot_name (lot_id=False).
        Outgoing/Internal : pick FIFO lot_id from stock.quant (source location).
        Fields lot_id & lot_name dibuat readonly di view; perubahan via tombol ini.
        """

        Quant = self.env['stock.quant']
        for line in self:

            is_incoming = line._tjr_is_incoming_tracked()
            
            # print(f">>> Is Incoming: {is_incoming}") # Cek di terminal Odoo

            if is_incoming:
                kode_supplier = self.picking_id.partner_id.company_registry or (self.picking_id.partner_id.ref if self.picking_id.partner_id and self.picking_id.partner_id.ref else False)
                print(f">>> Kode Supplier: {kode_supplier}") # Cek di terminal Odoo
                if not kode_supplier:
                    kode_supplier = "SUPP"  # Fallback jika supplier tidak memiliki kode

                # 2. Ambil TANGGAL MASUK (Format: MMDDYY)
                # Odoo menyimpan waktu dalam UTC, kita ambil tanggal hari ini
                tanggal_masuk = fields.Date.today().strftime('%m%d%y')
                print(f">>> Tanggal Masuk: {tanggal_masuk}") # Cek di terminal Odoo

                # 3. Ambil KODE WAREHOUSE Tujuan
                # Mengambil warehouse dari destinasi lokasi penerimaan
                des_parent_code = self.location_dest_id.location_id.name if self.location_dest_id and self.location_dest_id.location_id else False
                print(f">>> Lokasi Tujuan: {des_parent_code}") # Cek di terminal Odoo

                warehouse = line.picking_id.picking_type_id.warehouse_id
                kode_warehouse = des_parent_code or (warehouse.code if warehouse else False)
                if not kode_warehouse:
                    kode_warehouse = "WH"  # Fallback jika gudang tidak teridentifikasi

                # 4. Ambil SEQUENCE (Berdasarkan jumlah lot yang dibuat hari ini)
                # Mencari lot dengan awalan yang sama untuk menghitung urutan
                kode_product = line.product_id.default_code or f"PROD{line.product_id.id}"
                source_document = line.picking_id.origin or f"PICK{line.picking_id.id}"
                # prefix = f"{source_document}-{tanggal_masuk}-{kode_product}"
                prefix = f"{kode_product}-{tanggal_masuk}-{source_document}"

                # lot_count = self.env['stock.lot'].search_count([
                #     ('name', '=like', f"{prefix}%"),
                #     ('product_id', '=', line.product_id.id)
                # ])

                # KOMPOSISI AKHIR LOT
                new_lot_name = f"{prefix}"
                
                # Masukkan ke dalam UI menggunakan update
                print(f">>> Line Confirmation Location Code before update: {line.confirmation_location_code}") # Cek di terminal Odoo
                print(f">>> Line Confirmation Lot Code before update: {line.confirmation_lot_code}") # Cek di terminal Odoo
                line.update({
                    'lot_id': False,
                    # 'confirmation_location_code': des_parent_code,
                    # 'confirmation_lot_code': new_lot_name,
                })
                # line.confirmation_location_code = des_parent_code  # Update confirmation_location_code untuk menampilkan di UI
                # line.confirmation_lot_code = new_lot_name  # Update confirmation_lot_code untuk menampilkan di UI
                line.lot_name = new_lot_name  # Update lot_name untuk menampilkan di UI

                print(f">>> Line Confirmation Location Code after update: {line.confirmation_location_code}") # Cek di terminal Odoo
                print(f">>> Line Confirmation Lot Code after update: {line.confirmation_lot_code}")
                
                print(f">>> SUCCESS: Generated lot name -> {new_lot_name}")
                lot_data = {
                    'name': line.lot_name,               # Nomor Lot
                    'product_id': line.product_id.id,              # ID Produk
                    'product_uom_id': line.product_id.uom_id.id,           # ID Satuan Unit
                    'company_id': False,           # [NULL] di Odoo diwakili False
                    'location_id': line.location_id.id,              # Lokasi awal/default (opsional tergantung versi)
                    'ref': False,                  # Internal Reference [NULL]
                    'note': False,                 # Catatan [NULL]
                }
                search_lot = self.env['stock.lot'].search([('name', '=', line.lot_name)])
                lot_exist = search_lot
                if not search_lot:
                    new_lot = line.env['stock.lot'].create(lot_data)
                    lot_exist = new_lot
                    # search_lot

                
                line.lot_id = lot_exist.id
                print(f">>> Created new lot with ID: {lot_exist.id} and Name: {lot_exist.name}") # Cek di terminal Odoo
                line.update({
                    'lot_id': lot_exist.id,
                })
                print(f">>> Lot ID assigned: {line.lot_id}") # Cek di terminal Odoo

                continue

            # === Incoming: generate lot_name ===
            if line._tjr_is_incoming_tracked():
                print(">>> Detected incoming tracked product. Generating lot name...") # Cek di terminal Odoo
                new_name = line._tjr_build_lot_name()
                # biarkan Odoo membuat lot saat validasi jika lot_id kosong
                line.update({
                    'lot_id': False,
                    'lot_name': new_name,
                })
                print(f">>> Generated lot name: {new_name}")

                # line.write({'lot_id': False, 'lot_name': new_name})
                continue

            # === Outgoing/Internal: pilih lot via FIFO ===
            if not line.location_id:
                raise UserError(_("Source Location is empty on the move line."))

            # Berapa kuantitas yang mau di-assign?
            # - Jika qty_done sudah diisi, pakai itu.
            # - Jika kosong: untuk serial -> 1; untuk lot -> sisa qty di line (product_uom_qty/demand).
            uom_line = line.product_uom_id or line.product_id.uom_id
            qty_needed_line_uom = line.quantity
            print(f">>> Initial quantity needed (line UoM): {qty_needed_line_uom}") # Cek di terminal Odoo
            # print(f">>> Product Tracking: {line.product_id.tracking}") # Cek di terminal Odoo   
            if not qty_needed_line_uom:
                print(">>> Quantity needed is zero, calculating fallback...") # Cek di terminal Odoo
                if line.product_id.tracking == 'serial':
                    qty_needed_line_uom = 1.0
                else:
                    qty_needed_line_uom = line.quantity or line.qty_reserved or 0.0
                    if not qty_needed_line_uom:
                        qty_needed_line_uom = 1.0  # fallback

            # konversi ke UoM produk (base) untuk hitung available quant
            qty_needed_base = uom_line._compute_quantity(qty_needed_line_uom, line.product_id.uom_id, rounding_method='HALF-UP')
            print(f">>> Quantity needed (product UoM): {qty_needed_base}") # Cek di terminal Odoo

            # Ambil quants dengan lot yang punya stok available > 0 di lokasi sumber (FIFO)
            domain = [
                ('product_id', '=', line.product_id.id),
                ('location_id', 'child_of', line.location_id.id),
                ('lot_id', '!=', False),
                ('quantity', '>', 0),
            ]
            if line.owner_id:
                domain.append(('owner_id', '=', line.owner_id.id))
            if line.package_id:
                domain.append(('package_id', '=', line.package_id.id))

            quants = Quant.search(domain, order='in_date asc, id asc')

            chosen = False
            for q in quants:
                # hitung available di lokasi quant (strict ke lokasi quant)
                print(f"Product ID - {line.product_id.id}, Location ID - {q.location_id.id}, Lot ID - {q.lot_id.id if q.lot_id else 'N/A'}, Package ID - {q.package_id.id if q.package_id else 'N/A'}, Owner ID - {q.owner_id.id if q.owner_id else 'N/A'}") # Cek di terminal Odoo
                avail = Quant._get_available_quantity(
                    line.product_id,
                    q.location_id,
                    lot_id=q.lot_id,
                    package_id=q.package_id or None,
                    owner_id=q.owner_id or None,
                    strict=True,
                )
                print(f">>> Evaluating quant {q.id} - Lot: {q.lot_id.name if q.lot_id else 'N/A'} - Available Qty: {avail}") # Cek di terminal Odoo                
                if avail <= 0:
                    continue

                if avail >= qty_needed_base or line.product_id.tracking == 'serial':
                    # gunakan lot ini
                    # updates = {'lot_id': q.lot_id.id, 'lot_name': False}
                    print(f">>> Using lot: {q.lot_id.name}") # Cek di terminal Odoo
                    line.lot_id = q.lot_id
                    line.lot_name = False

                    # Kalau qty_done 0, isi qty_done minimal 1 untuk serial, atau min(avail, qty_needed)
                    if not line.quantity:
                        print(f">>> Line quantity before assignment: {line.quantity}") # Cek di terminal Odoo
                        print(f">>> Calculating quantity to assign for line {line.id}") # Cek di terminal Odoo
                        if line.product_id.tracking == 'serial':
                            # qty_to_write = 1.0
                            line.quantity = 1.0
                        else:
                            # convert avail (base) ke uom line
                            # qty_to_write = line.product_id.uom_id._compute_quantity(
                            #     min(avail, qty_needed_base),
                            #     uom_line,
                            #     rounding_method='HALF-UP'
                            # )
                            line.quantity = line.product_id.uom_id._compute_quantity(
                                min(avail, qty_needed_base),
                                uom_line)
                            print(f">>> Assigned quantity: {line.quantity} {uom_line.name}") # Cek di terminal Odoo
                    #     updates['qty_done'] = qty_to_write
                    # line.write(updates)
                    chosen = True
                    break
                      
            if not chosen:
                print(f">>> chosen {chosen}") # Cek di terminal Odoo
                # print(f">>> chosen lot {q.lot_id.name} - Product: {line.product_id.display_name}") # Cek di terminal Odoo
                # Tidak ada lot yg cukup → pilih lot tertua yang punya sedikit stok (sebagai hint)
                partial = next((q for q in quants if Quant._get_available_quantity(
                    line.product_id, q.location_id, lot_id=q.lot_id,
                    package_id=q.package_id or None, owner_id=q.owner_id or None, strict=True) > 0), None)
                print(f">>> No sufficient lot found for line {line.id}. Partial suggestion: {partial.lot_id.name if partial else 'None'}") # Cek di terminal Odoo
                if partial:
                    line.write({'lot_id': partial.lot_id.id, 'lot_name': False})
                    # quantity bila kosong isi seminimal mungkin agar user sadar (1 untuk serial, else 0)
                    if not line.quantity and line.product_id.tracking == 'serial':
                        line.quantity = 1.0
                else:
                    raise UserError(_("No available lots found for product %s in %s.") % (line.product_id.display_name, line.location_id.display_name))

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Done'),
                'message': _('Lot generated for receipts or picked by FIFO for issues/transfers.'),
                'type': 'success',
                'sticky': False,
            }
        }

    def action_prepare_scan_lot(self):
        self.ensure_one()
        if self.picking_type_name in ('Storage', 'Store Finished Product'):
            self._check_storage_pick_completed()
        elif not self.lot_id:
            raise ValidationError("Lot kosong, Silahkan generate lot terlebih dahulu")
        return {
            'type': 'ir.actions.client',
            'tag': 'adt_scan_qr_to_move_line_field',
            'params': {
                'field_name': 'confirmation_lot_code',
                'move_line_id': self.id,
                'title': _('Scan Lot QR Code'),
            },
        }

    def action_prepare_scan_location(self):
        self.ensure_one()
        if self.picking_type_name in ('Storage', 'Store Finished Product'):
            self._check_storage_pick_completed()
        elif not self.lot_id:
            raise ValidationError("Lot kosong, Silahkan generate lot terlebih dahulu")
        return {
            'type': 'ir.actions.client',
            'tag': 'adt_scan_qr_to_move_line_field',
            'params': {
                'field_name': 'confirmation_location_code',
                'move_line_id': self.id,
                'title': _('Scan Location QR Code'),
            },
        }

    def action_open_putaway_wizard(self):
        if not self.exists():
            return {'type': 'ir.actions.client', 'tag': 'reload'}
        self.ensure_one()
        wizard = self.env['putaway.move.line.wizard'].create(
            self._prepare_putaway_move_line_wizard_values()
        )
        return {
            'name': _('Putaway'),
            'type': 'ir.actions.act_window',
            'res_model': 'putaway.move.line.wizard',
            'res_id': wizard.id,
            'view_mode': 'form',
            'target': 'new',
        }

    @api.onchange('matching')
    def _onchange_matching(self):
        for line in self:
            line._update_move_picked()

    def action_apply_scanned_field(self, field_name, scanned_code):
        allowed_fields = {
            'confirmation_lot_code',
            'confirmation_location_code',
            'confirmation_package_code',
            'matching_lot_id',
            'matching_loc_dest_id',
            'result_package_id',
        }
        if field_name not in allowed_fields:
            raise ValidationError(_("Field scan tidak valid."))

        result = {}
        for line in self:
            picking_type_name = line.picking_type_name
            vals = {}
            if field_name in ('confirmation_lot_code', 'matching_lot_id'):
                lot = line._find_lot_by_scan_code(scanned_code)
                if not lot:
                    raise ValidationError(_("Barcode tidak terdaftar."))
                if field_name == 'confirmation_lot_code':
                    vals['confirmation_lot_code'] = scanned_code
                vals['matching_lot_id'] = lot.id
            elif field_name in ('confirmation_location_code', 'matching_loc_dest_id'):
                location = line._find_loc_by_scan_code(scanned_code)
                if not location:
                    raise ValidationError(_("Barcode tidak terdaftar."))
                if (
                    picking_type_name in ('Receipts', 'Receipt Internal', 'Storage', 'Store Finished Product')
                    and field_name == 'confirmation_location_code'
                ):
                    line._check_putaway_location_mapping(location)
                if field_name == 'confirmation_location_code':
                    vals['confirmation_location_code'] = scanned_code
                if picking_type_name in ['Receipts', 'Receipt Internal', 'Storage', 'Store Finished Product'] and field_name == 'confirmation_location_code':
                    vals['location_putaway_id'] = location.id
                    vals['location_dest_id'] = location.id
                else:
                    vals['matching_loc_dest_id'] = location.id
            elif field_name in ('confirmation_package_code', 'result_package_id'):
                package = line._find_package_by_scan_code(scanned_code)
                if not package:
                    raise ValidationError(
                        _("Barcode package %s tidak terdaftar.") % scanned_code
                    )
                if package.package_cat != 'box':
                    raise ValidationError(
                        _("Package %s harus dalam bentuk box.") % package.display_name
                    )
                if not package.is_empty:
                    raise ValidationError(
                        _("Package %s telah digunakan.") % package.display_name
                    )
                vals['confirmation_package_code'] = scanned_code
                vals['result_package_id'] = package.id

            line.write(vals)
            if (
                picking_type_name in ('Pick', 'Pick Internal', 'Pick Components', 'Internal Transfers', 'Internal Transfer', 'Transfer Internal')
                or line.picking_id.is_ito_internal_transfer
            ):
                matching = line._get_matching_status()
                line.write({
                    'matching': matching,
                    'second_matching': 'Match' if matching == 'match' else 'Not Match',
                })
            elif picking_type_name in ('Storage', 'Store Finished Product') and field_name in (
                'confirmation_lot_code', 'matching_lot_id'
            ):
                matching = 'match' if line._is_storage_lot_match(line.matching_lot_id) else 'not_match'
                line.write({
                    'matching': matching,
                    'second_matching': 'Match' if matching == 'match' else 'Not Match',
                })

            elif line.move_id.raw_material_production_id:
                line.matching = line._get_matching_status_mo()
                line.second_matching = (
                    'Match' if line.matching == 'match' else 'Not Match'
                )

            matching_labels = dict(line._fields['matching'].selection)
            status_labels = dict(line._fields['putaway_status'].selection)
            result = {
                'id': line.id,
                field_name: scanned_code
                # 'matching': line.matching,
                # 'matching_label': matching_labels.get(line.matching, line.matching or ''),
                # 'matching_lot_id': [
                #     line.matching_lot_id.id,
                #     line.matching_lot_id.display_name,
                # ] if line.matching_lot_id else False,
            }

            if picking_type_name in ['Receipts', 'Receipt Internal', 'Storage', 'Store Finished Product']:
                result['putaway_status'] = line.putaway_status
                result['putaway_status_label'] = status_labels.get(line.putaway_status, line.putaway_status or '')
                result['location_putaway_id'] = [
                    line.location_putaway_id.id,
                    line.location_putaway_id.name,
                ] if line.location_putaway_id else False
                result['location_dest_id'] = [
                    line.location_dest_id.id,
                    line.location_dest_id.name,
                ] if line.location_dest_id else False
                result['matching'] = line.matching
                result['matching_label'] = matching_labels.get(line.matching, line.matching or '')
                result['matching_lot_id'] = [
                    line.matching_lot_id.id,
                    line.matching_lot_id.display_name,
                ] if line.matching_lot_id else False
            else:
                result['matching'] = line.matching
                result['matching_label'] = matching_labels.get(line.matching, line.matching or '')
                result['matching_lot_id'] = [
                    line.matching_lot_id.id,
                    line.matching_lot_id.display_name,
                ] if line.matching_lot_id else False
                result['matching_loc_dest_id'] = [
                    line.matching_loc_dest_id.id,
                    line.matching_loc_dest_id.display_name,
                ] if line.matching_loc_dest_id else False
                result['second_matching'] = line.second_matching

            if field_name in ('confirmation_package_code', 'result_package_id'):
                result['confirmation_package_code'] = line.confirmation_package_code
                result['result_package_id'] = [
                    line.result_package_id.id,
                    line.result_package_id.display_name,
                ] if line.result_package_id else False

        return result

    def action_mark_putaway_finished(self):
        if self.picking_type_name in ('Storage', 'Store Finished Product'):
            self._check_storage_pick_completed()
            if self.product_id.tracking != 'none':
                expected_lots = self._get_storage_expected_lots()
                confirmed_lot = self.matching_lot_id
                if not confirmed_lot and self.confirmation_lot_code:
                    confirmed_lot = self._find_lot_by_scan_code(
                        self.confirmation_lot_code
                    )
                if not confirmed_lot or not expected_lots:
                    self.write({
                        'matching': 'not_match',
                        'second_matching': 'Not Match',
                    })
                    raise ValidationError(_(
                        "Lot Storage belum dikonfirmasi atau lot Pick tidak ditemukan."
                    ))
                if confirmed_lot not in expected_lots:
                    self.write({
                        'matching': 'not_match',
                        'second_matching': 'Not Match',
                    })
                    raise ValidationError(_(
                        "Lot %(lot)s tidak sesuai dengan lot yang dikirim dari Pick."
                    ) % {'lot': confirmed_lot.display_name})

        if self.picking_type_name in ('Receipts', 'Receipt Internal', 'Storage', 'Store Finished Product') and self.location_putaway_id:
            self._check_putaway_location_mapping(self.location_putaway_id)
        if not self.location_putaway_id and self.confirmation_quantity == 0:
            raise ValidationError(_("Location putaway dan Quantity tidak boleh kosong, silahkan input location putaway dan quantity terlebih dahulu"))
        if self.confirmation_quantity == 0:
            raise ValidationError(_("Quantity tidak boleh 0, silahkan input quantity terlebih dahulu"))
        if not self.location_putaway_id:
            raise ValidationError(_("Location putaway tidak boleh kosong, silahkan input location putaway terlebih dahulu"))
        if self.confirmation_quantity > self.quantity:
            raise ValidationError(_("Stored Qty tidak boleh lebih besar dari Picked Qty, silahkan input quantity yang sesuai"))
        if self.confirmation_quantity < self.quantity:
            raise ValidationError(_("Stored Qty tidak boleh kurang dari Picked Qty, silahkan input quantity yang sesuai"))
        vals = {'putaway_status': 'finished'}
        if self.picking_type_name in ('Storage', 'Store Finished Product'):
            vals.update({
                'matching': 'match',
                'second_matching': 'Match',
            })
            if self.matching_lot_id and not self.lot_id:
                vals['lot_id'] = self.matching_lot_id.id
        self.write(vals)
        return self.move_id.action_open_move_putaway()

    def action_mark_putaway_unfinished(self):
        self.write({'putaway_status': 'unfinished'})
        return self.move_id.action_open_move_putaway()


    def action_mark_picker_finished(self):
        self._check_picker_quantity_vs_demand()
        if not self.lot_id:
            raise ValidationError(_("Lot kosong, silahkan generate lot terlebih dahulu"))
        if self.quantity == 0:
            raise ValidationError(_("Quantity tidak boleh 0, silahkan input quantity terlebih dahulu"))
        self.write({'picker_status': 'finished'})
        return {'type': 'ir.actions.act_window_close'}

    def _check_picker_quantity_vs_demand(self):
        for line in self:
            move = line.move_id
            if not move:
                continue

            total_picker_qty = sum(move.move_line_ids.mapped('quantity'))
            demand_qty = move.product_uom_qty
            rounding = move.product_uom.rounding if move.product_uom else 0.01
            if float_compare(
                total_picker_qty,
                demand_qty,
                precision_rounding=rounding,
            ) > 0:
                raise ValidationError(_(
                    "Qty Picker tidak boleh lebih besar dari Qty Total.\n"
                    "Product: %(product)s\n"
                    "Qty Picker: %(picked)s\n"
                    "Qty Total: %(demand)s"
                ) % {
                    'product': move.product_id.display_name,
                    'picked': total_picker_qty,
                    'demand': demand_qty,
                })

    def action_save_picker_details(self):
        self.ensure_one()
        is_batch_view = bool(self.picking_id and self.picking_id.batch_id)

        if (
            self.picking_id
            and self.picking_id.picking_type_id.name == 'Receipts'
        ):
            self.action_mark_picker_finished()

        elif (
            self.picking_id
            and "Pick" in (self.picking_id.picking_type_id.name or "")
        ):
            self.action_mark_check_pick_finished()

        elif (
            self.picking_id
            and "Pack" in (self.picking_id.picking_type_id.name or "")
        ):
            if not is_batch_view:
                return self.move_id.action_open_move_operation()

        elif (
            self.picking_id
            and "Delivery Orders" in (self.picking_id.picking_type_id.name or "")
        ):
            self.action_mark_check_unpack_finished()
            if not is_batch_view:
                return self.move_id.action_open_unpack_detail()

        elif self.is_manufacturing:
            self.action_mark_check_finished_mo()
            return self.move_id.action_open_move_mo()

        else:
            self._check_picker_quantity_vs_demand()
            if not self.lot_id:
                raise ValidationError(_("Lot kosong, silahkan generate lot terlebih dahulu"))
            if self.quantity == 0:
                raise ValidationError(_("Quantity tidak boleh 0, silahkan input quantity terlebih dahulu"))
            self.write({'picker_status': 'finished'})

        if is_batch_view:
            return
        return self.move_id.action_open_move_pick()
    
    def action_rollback_details(self):
        self.ensure_one()

        if (
            self.picking_id
            and self.picking_id.picking_type_id.name == 'Receipts'
        ):
            return self.action_mark_picker_unfinished_form()

        elif (
            self.picking_id
            and "Pick" in (self.picking_id.picking_type_id.name or "")
        ):
            return self.action_mark_check_pick_unfinished_form()
        
        elif (
            self.picking_id
            and "Delivery Orders" in (self.picking_id.picking_type_id.name or "")
        ):
            return self.action_mark_check_unpack_unfinished_form()
            # return self.move_id.action_open_unpack_detail()

        elif self.is_manufacturing:
            return self.action_mark_check_unfinished_mo_form()
            # return self.move_id.action_open_move_mo()
            
        else:
            self.write({'picker_status': 'finished'})

        return self.action_open_move_pick()

    def action_discard_details(self):
        self.ensure_one()

        if (
            self.picking_id
            and self.picking_id.picking_type_id.name == 'Receipts'
        ):
            return self.action_picker_discard_form()

        elif (
            self.picking_id
            and "Pick" in (self.picking_id.picking_type_id.name or "")
        ):
            return self.move_id.action_open_move_pick()

        elif (
            self.picking_id
            and "Pack" in (self.picking_id.picking_type_id.name or "")
        ):
            return self.move_id.action_open_move_operation()
        
        elif (
            self.picking_id
            and "Delivery Orders" in (self.picking_id.picking_type_id.name or "")
        ):
            return self.move_id.action_open_unpack_detail()

        elif self.is_manufacturing:
            return self.move_id.action_open_move_mo()
        
        return {"type": "ir.actions.act_window_close"}

    def action_cancel_picker_details(self):
        self.ensure_one()

        if self.env.context.get('picker_new_line'):
            move = self.move_id
            self.unlink()
        else:
            move = self.move_id

        return {
            'type': 'ir.actions.act_window',
            'name': _('Stock Move'),
            'res_model': 'stock.move',
            'res_id': move.id,
            'view_mode': 'form',
            'view_id': self.env.ref(
                'tijari_wms_18.stock_move_pick_view'
            ).id,
            'target': 'new',
        }

    def action_mark_picker_unfinished(self):
        if self.putaway_status == 'finished':
            raise ValidationError(_(
                "Picker tidak dapat di-rollback karena proses Putaway telah selesai."
            ))
        self.write({'picker_status': 'unfinished'})
        return self.move_id.action_open_move_pick()

    def action_mark_picker_unfinished_form(self):
        if self.putaway_status == 'finished':
            raise ValidationError(_(
                "Picker tidak dapat di-rollback karena proses Putaway telah selesai."
            ))
        self.write({'picker_status': 'unfinished'})
        return self.action_open_move_pick()

    def action_picker_discard_form(self):
        self.ensure_one()
        return self.action_open_pick_detail()

    def action_delete_picker_line(self):
        self.ensure_one()

        move = self.move_id
        if self.state == 'done' or move.state in ('done', 'cancel'):
            raise ValidationError(_("Line yang sudah Done atau Cancelled tidak boleh dihapus."))

        if self.picking_id.picking_type_id.name in ('Receipts', 'Receipt Internal', 'Storage', 'Store Finished Product'):
            if self.putaway_status == 'finished':
                raise ValidationError(_(
                    "Penerimaan barang tidak dapat dihapus karena proses penyimpanan telah selesai"
                ))

        elif "Pick"  in (self.picking_id.picking_type_id.name or ""):
            if self.pick_status == 'finished':
                raise ValidationError(_(
                    "Pick barang tidak dapat dihapus karena proses telah selesai"
                ))

        self.unlink()

        return {
            'type': 'ir.actions.act_window',
            'name': _('Stock Move'),
            'res_model': 'stock.move',
            'res_id': move.id,
            'view_mode': 'form',
            'view_id': self.env.ref(
                'tijari_wms_18.stock_move_pick_detail_view'
            ).id,
            'target': 'new',
        }

    def action_delete_packer_line(self):
        self.ensure_one()

        move = self.move_id
        if self.state == 'done' or move.state in ('done', 'cancel'):
            raise ValidationError(_("Line yang sudah Done atau Cancelled tidak boleh dihapus."))

        self.unlink()

        return {
            'type': 'ir.actions.act_window',
            'name': _('Stock Move'),
            'res_model': 'stock.move',
            'res_id': move.id,
            'view_mode': 'form',
            'view_id': self.env.ref(
                'tijari_wms_18.stock_move_pick_detail_view'
            ).id,
            'target': 'new',
        }

    def action_open_picker_details(self):
        self.ensure_one()

        return {
            'type': 'ir.actions.act_window',
            'name': _('Picker Details'),
            'res_model': 'stock.move.line',
            'res_id': self.id,
            'view_mode': 'form',
            'view_id': self.env.ref(
                'tijari_wms_18.wizard_picker_view_mobile'
            ).id,
            'target': 'new',
        }


    def action_mark_check_pick_finished(self):
        if self.filtered(lambda line: line.picking_id.batch_id):
            return self._mark_check_pick_finished(reload_action=False)
        return self._mark_check_pick_finished(reload_action='reload')

    def action_mark_check_pick_finished_batch(self):
        """Finish a picker line from a batch without reloading the client."""
        return self._mark_check_pick_finished(reload_action=False)

    def _mark_check_pick_finished(self, reload_action):
        self._validate_before_mark_pick_finished()
        if self.matching == 'not_match':
            self._check_matching_status_pick()

        self.write({'pick_status': 'finished'})
        # Refresh every move in the picking so the list order reflects status
        # changes and any older rows with stale sequence values.
        self.mapped('move_id.picking_id.move_ids_without_package')._refresh_pick_status_sequence()
        if reload_action:
            return {'type': 'ir.actions.client', 'tag': reload_action}

    def action_mark_check_pick_unfinished(self):
        self.write({'pick_status': 'unfinished'})
        self.mapped('move_id.picking_id.move_ids_without_package')._refresh_pick_status_sequence()
        if self.filtered(lambda line: line.picking_id.batch_id):
            return
        return self.move_id.action_open_move_pick()

    def action_mark_check_pick_unfinished_batch(self):
        """Undo a finished picker line from a batch without reloading the client."""
        self.write({'pick_status': 'unfinished'})
        self.mapped('move_id.picking_id.move_ids_without_package')._refresh_pick_status_sequence()

    def action_mark_check_pick_unfinished_form(self):
        self.write({'pick_status': 'unfinished'})
        self.mapped('move_id.picking_id.move_ids_without_package')._refresh_pick_status_sequence()
        return self.action_open_move_pick()

    def action_open_move_pick(self):
        self.ensure_one()

        return {
            'type': 'ir.actions.act_window',
            'name': 'Move Line',
            'res_model': 'stock.move.line',
            'res_id': self.id,
            'view_mode': 'form',
            'views': [
                (
                    self.env.ref(
                        'stock.view_move_line_mobile_form'
                    ).id,
                    'form',
                ),
            ],
            'target': 'new',
        }

    def action_open_note_wizard(self):
        self.ensure_one()
        return self.move_id.action_open_note_wizard()

    @api.depends(
        'pick_status', 'location_id.name', 'product_id.display_name',
        'move_id.sequence', 'picking_id.batch_id.picker_sort_by_location',
        'picking_id.batch_id.picker_sort_by_product',
    )
    def _compute_picker_batch_sort_key(self):
        for line in self:
            batch = line.picking_id.batch_id
            if batch.picker_sort_by_location:
                primary = (line.location_id.name or '').casefold()
            elif batch.picker_sort_by_product:
                primary = (line.product_id.display_name or '').casefold()
            else:
                primary = ''
            line.picker_batch_sort_key = '%s|%s|%010d|%010d' % (
                '1' if line.pick_status == 'finished' else '0',
                primary,
                line.move_id.sequence or 0,
                line._origin.id or 0,
            )

    @api.depends(
        'pick_status', 'location_id.name', 'product_id.display_name',
        'move_id.sequence',
    )
    def _compute_picker_client_sort_keys(self):
        for line in self:
            status = '1' if line.pick_status == 'finished' else '0'
            sequence = line.move_id.sequence or 0
            line_id = line._origin.id or 0
            tie_breaker = '%010d|%010d' % (sequence, line_id)
            line.picker_location_sort_key = '%s|%s|%s' % (
                status, (line.location_id.name or '').casefold(), tie_breaker,
            )
            line.picker_product_sort_key = '%s|%s|%s' % (
                status, (line.product_id.display_name or '').casefold(), tie_breaker,
            )
            line.picker_default_sort_key = '%s|%s' % (status, tie_breaker)

    def action_open_pick_detail(self):
        self.ensure_one()

        move = self.move_id
        return {
            'type': 'ir.actions.act_window',
            'name': _('Stock Move'),
            'res_model': 'stock.move',
            'res_id': move.id,
            'view_mode': 'form',
            'view_id': self.env.ref(
                'tijari_wms_18.stock_move_pick_detail_view'
            ).id,
            'target': 'new',
        }


    def _validate_before_mark_pick_finished(self):
        confirmation_quantity = self.confirmation_quantity
        quantity = self.quantity

        rounding = self.product_uom_id.rounding if self.product_uom_id else 0.01
        comparison = float_compare(
            confirmation_quantity,
            quantity,
            precision_rounding=rounding,
        )

        if float_is_zero(
            confirmation_quantity,
            precision_rounding=rounding,
        ):
            raise UserError(f"Qty Diambil lot {self.lot_id.name} tidak boleh 0.")

        if comparison > 0:
            raise UserError("Qty Diambil tidak boleh melebihi Available Qty")

        return comparison

    def action_mark_check_unpack_finished(self):
        self._validate_before_mark_unpack_finished()

        self.write({'status_check': 'finished'})
        return self.move_id.action_open_unpack_detail()

    def action_mark_check_unpack_unfinished(self):
        self.write({'status_check': 'unfinished'})
        return self.move_id.action_open_unpack_detail()

    def action_mark_check_unpack_unfinished_form(self):
        self.write({'status_check': 'unfinished'})
        return self.action_open_move_pick()

    def _validate_before_mark_unpack_finished(self):
        confirmation_quantity = self.so_confirmation_quantity
        quantity = self.quantity

        rounding = self.product_uom_id.rounding if self.product_uom_id else 0.01
        comparison = float_compare(
            confirmation_quantity,
            quantity,
            precision_rounding=rounding,
        )

        if float_is_zero(
            confirmation_quantity,
            precision_rounding=rounding,
        ):
            raise UserError(f"Qty Dikirim lot {self.lot_id.name} tidak boleh 0.")

        if comparison > 0:
            raise UserError("Qty Dikirim tidak boleh melebihi Qty Unpack")

        return comparison
                
    def _check_matching_status_pick(self):
        expected_location = self.source_location_id or self.location_id
        if not self.matching_loc_dest_id:
            raise UserError(f"Pastikan melakukan konfirmasi lokasi pada lot {self.lot_id.name}")
        if self.matching_loc_dest_id != expected_location:
            raise UserError(
                _(
                    "Lot: %s\n"
                    "Expected Location: %s\n"
                    "Confirmation Location: %s\n\n"
                    "Confirmation Location tidak sesuai dengan Expected Location."
                ) % (
                    self.lot_id.name or "-",
                    expected_location.name,
                    self.matching_loc_dest_id.name,
                )
            )

        expected_lot = self.product_lot_id or self.lot_id
        current_lot = self.matching_lot_id or self.lot_id
        if not self.matching_lot_id:
            raise UserError(f"Pastikan melakukan konfirmasi lot {self.lot_id.name}")
        if self.matching_lot_id != expected_lot:
            raise UserError(
                _(
                    "Expected Lot: %s\n"
                    "Confirmation Lot: %s\n"
                    "Confirmation Lot tidak sesuai dengan Expected Lot."
                ) % (
                    expected_lot.name,
                    self.matching_lot_id.name,
                )
            )

    def action_mark_check_finished_mo(self):
        self._validate_before_mark_finished_mo()

        self.write({
            'mo_status': 'finished'
        })

        # lines = self.move_id.move_line_ids
        # self.move_id.status_mo_check = (
        #     'finished' if lines and all(line.mo_status == 'finished' for line in lines)
        #     else 'unfinished'
        # )

        return {'type': 'ir.actions.client', 'tag': 'soft_reload'}

    def action_mark_check_unfinished_mo(self):
        self.write({'mo_status': 'unfinished'})
        return self.move_id.action_open_move_mo()

    def action_mark_check_unfinished_mo_form(self):
        self.write({'mo_status': 'unfinished'})
        return self.action_open_move_pick()

    def _validate_before_mark_finished_mo(self):
        for line in self:
            move = line.move_id
            if not move:
                continue

            total_used_qty = sum(move.move_line_ids.mapped('quantity'))
            demand_qty = move.product_uom_qty
            rounding = move.product_uom.rounding if move.product_uom else 0.01
            if float_compare(
                total_used_qty,
                demand_qty,
                precision_rounding=rounding,
            ) > 0:
                raise ValidationError(_(
                    "Qty Digunakan tidak boleh lebih besar dari To Consume.\n"
                    "Product: %(product)s\n"
                    "Qty Digunakan: %(used)s\n"
                    "To Consume: %(demand)s"
                ) % {
                    'product': move.product_id.display_name,
                    'used': total_used_qty,
                    'demand': demand_qty,
                })


    @api.onchange('location_putaway_id')
    def _onchange_location_putaway_id(self):
        for line in self:

            if line.location_putaway_id:
                line.location_dest_id = line.location_putaway_id.id

    @api.onchange('confirmation_location_code')
    def _onchange_confirmation_location_code(self):
        print('def _onchange_confirmation_location_code(self)')
        for wizard in self:

            location = wizard._find_loc_by_scan_code(wizard.confirmation_location_code)
            if not wizard.confirmation_location_code or not location:
                wizard.matching_loc_dest_id = False
                wizard.matching = 'not_match'

            if location and wizard.picking_type_name in ('Receipts', 'Receipt Internal', 'Storage', 'Store Finished Product'):
                wizard._check_putaway_location_mapping(location)

            wizard.matching_loc_dest_id = location.id

    @api.onchange('handheld_scan_code')
    def _onchange_handheld_scan_code(self):
        for line in self:
            values = line._get_handheld_scan_values(line.handheld_scan_code)
            if not values:
                continue
            line.update(values)
            line.handheld_scan_code = False

    def _get_handheld_scan_values(self, scanned_code):
        self.ensure_one()
        code = (scanned_code or '').strip()
        if not code:
            return {}

        if self.picking_type_name in ('Storage', 'Store Finished Product'):
            self._check_storage_pick_completed()

        if self.picking_type_name in ('Pack', 'Pack Internal'):
            package = self._find_package_by_scan_code(code)
            if not package:
                raise ValidationError(
                    _("Barcode package %s tidak terdaftar.") % code
                )
            if package.package_cat != 'box':
                raise ValidationError(
                    _("Package %s harus dalam bentuk box.") % package.display_name
                )
            if not package.is_empty:
                raise ValidationError(
                    _("Package %s telah digunakan.") % package.display_name
                )
            return {
                'confirmation_package_code': code,
                'result_package_id': package.id,
            }

        lot = self._find_lot_by_scan_code(code)
        # Resolve an exact lot match before trying locations.  The allowed
        # lot relation can be temporarily stale while an editable move line
        # is being changed; treating a valid lot as a location then produces
        # the misleading "lot atau lokasi tidak terdaftar" error.
        if lot:
            values = {
                'confirmation_lot_code': code,
                'matching_lot_id': lot.id,
            }
            if self.picking_type_name in ('Storage', 'Store Finished Product'):
                values.update({
                    'matching': 'match' if self._is_storage_lot_match(lot) else 'not_match',
                    'second_matching': 'Match' if self._is_storage_lot_match(lot) else 'Not Match',
                })
        else:
            location = self._find_loc_by_scan_code(code)
            if not location:
                raise ValidationError(_('Kode lot atau lokasi tidak terdaftar.'))
            if self.picking_type_name in ('Receipts', 'Receipt Internal', 'Storage', 'Store Finished Product'):
                self._check_putaway_location_mapping(location)
            values = {'confirmation_location_code': code}
            if self.picking_type_name in ('Receipts', 'Receipt Internal', 'Storage', 'Store Finished Product'):
                values.update({
                    'location_putaway_id': location.id,
                    'location_dest_id': location.id,
                })
            else:
                values['matching_loc_dest_id'] = location.id

        if self.picking_type_name not in ('Receipts', 'Receipt Internal', 'Storage', 'Store Finished Product'):
            matching_lot = values.get('matching_lot_id', self.matching_lot_id.id)
            matching_location = values.get(
                'matching_loc_dest_id', self.matching_loc_dest_id.id
            )
            if matching_lot and matching_location:
                expected_location = self.source_location_id or self.location_id
                expected_lot = self.product_lot_id or self.lot_id
                values['matching'] = (
                    'match'
                    if expected_location.id == matching_location
                    and expected_lot.id == matching_lot
                    else 'not_match'
                )
                values['second_matching'] = (
                    'Match' if values['matching'] == 'match' else 'Not Match'
                )
        return values

    def action_apply_handheld_scan_code(self, scanned_code):
        self.ensure_one()
        values = self._get_handheld_scan_values(scanned_code)
        if not values:
            return {}
        self.write(values)
        return {
            'id': self.id,
            'handheld_scan_code': False,
            **{
                field_name: values[field_name]
                for field_name in (
                    'confirmation_lot_code',
                    'matching_lot_id',
                    'confirmation_location_code',
                    'matching_loc_dest_id',
                    'confirmation_package_code',
                    'result_package_id',
                    'matching',
                    'second_matching',
                )
                if field_name in values
            },
        }

    def _find_lot_by_scan_code(self, lot_code):
        Lot = self.env['stock.lot']
        lot_code = (lot_code or '').strip()
        if not lot_code:
            return Lot.browse()

        return Lot.search([('name', '=', lot_code)], limit=1)

    def _check_putaway_location_mapping(self, location):
        self.ensure_one()
        mapping = self.adt_dest_location_id or self.move_id.location_dest_id
        if not mapping:
            raise ValidationError(_(
                "Location Mapping belum diatur untuk product %(product)s."
            ) % {
                'product': self.product_id.display_name,
            })

        is_allowed = bool(self.env['stock.location'].search_count([
            ('id', '=', location.id),
            ('id', 'child_of', mapping.id),
        ]))
        if not is_allowed:
            raise ValidationError(_(
                "Lokasi %(location)s berada di luar Location Mapping %(mapping)s."
            ) % {
                'location': location.name,
                'mapping': mapping.name,
            })

    def _check_storage_pick_completed(self):
        picking = self.picking_id
        if picking and hasattr(picking, '_check_storage_pick_completed'):
            picking._check_storage_pick_completed()

    def _get_storage_expected_lots(self):
        self.ensure_one()
        lots = self.lot_id

        source_moves = self.move_id.move_orig_ids
        if source_moves:
            lots |= source_moves.move_line_ids.filtered(
                lambda line: line.product_id == self.product_id and line.lot_id
            ).mapped('lot_id')

        order = self.picking_id.internal_transfer_order_id
        if order:
            pickings = order._get_related_pickings().filtered(
                lambda picking: (
                    picking.internal_transfer_leg == 'pick'
                    and picking.state == 'done'
                )
            )
            lots |= pickings.move_line_ids.filtered(
                lambda line: line.product_id == self.product_id and line.lot_id
            ).mapped('lot_id')
        return lots

    def _is_storage_lot_match(self, lot):
        self.ensure_one()
        expected_lots = self._get_storage_expected_lots()
        if not expected_lots:
            return self.product_id.tracking == 'none' and not lot
        return bool(lot and lot in expected_lots)

    @api.onchange('confirmation_lot_code')
    def _onchange_confirmation_lot_code(self):
        print('def _onchange_confirmation_lot_code(self)')
        for wizard in self:

            lot = wizard._find_lot_by_scan_code(wizard.confirmation_lot_code)
            if not lot:
                wizard.matching_lot_id = False
                wizard.matching = 'not_match'
                continue

            wizard.matching_lot_id = lot.id
            if wizard.picking_type_name in ('Storage', 'Store Finished Product'):
                wizard.matching = (
                    'match' if wizard._is_storage_lot_match(lot) else 'not_match'
                )
                wizard.second_matching = (
                    'Match' if wizard.matching == 'match' else 'Not Match'
                )
    def _find_loc_by_scan_code(self, location_code):
        Loc = self.env['stock.location']
        location_code = (location_code or '').strip()
        if not location_code:
            return Loc.browse()

        search_fields = ['unique_code', 'barcode', 'complete_name', 'name']
        for field_name in search_fields:
            if field_name not in Loc._fields:
                continue
            loc = Loc.search([(field_name, '=', location_code)], limit=1)
            if loc:
                return loc

        # Some handhelds scan the location display name, for example
        # ``WH33/Finish Good (FG)``, instead of unique_code/barcode.
        name_search_result = Loc.with_context(
            show_location_short_name=True,
        ).name_search(
            name=location_code,
            operator='=',
            limit=1,
        )
        if name_search_result:
            return Loc.browse(name_search_result[0][0])

        # The mobile display format may be ``WAREHOUSE/LOCATION NAME``.
        if '/' in location_code:
            warehouse_code, location_name = location_code.split('/', 1)
            loc = Loc.search([
                ('name', '=', location_name),
                ('warehouse_id.code', '=', warehouse_code),
            ], limit=1)
            if loc:
                return loc

        return Loc.browse()

    def _find_package_by_scan_code(self, package_code):
        Package = self.env['stock.quant.package']
        package_code = (package_code or '').strip()
        if not package_code:
            return Package.browse()

        domain_base = []
        if self.company_id:
            domain_base = [('company_id', 'in', [self.company_id.id, False])] + domain_base

        for field_name in ('name', 'barcode'):
            if field_name not in Package._fields:
                continue
            package = Package.search(domain_base + [(field_name, '=', package_code)], limit=1)
            if package:
                return package

        return Package.browse()

    # @api.onchange('matching_lot_id', 'matching_loc_dest_id')
    # def _onchange_update_matching_status(self):
    #     for line in self:
    #         if not line.matching_lot_id or not line.matching_loc_dest_id:
    #             continue

    #         lot_match = line.matching_lot_id.id == line.lot_id.id
    #         location_match = line.matching_loc_dest_id.id == line.location_dest_id.id

    #         line.matching = 'match' if lot_match and location_match else 'not_match'

    def _update_move_picked(self):
        for move in self.mapped('move_id'):

            # Odoo's inventory adjustment moves have no picking and are
            # already marked picked by _get_inventory_move_values(). They do
            # not use the WMS matching workflow; changing picked to False
            # makes stock.move._action_done() skip applying their quantities.
            if move.is_inventory and not move.picking_id:
                move.picked = True
                continue

            # Return picking tidak melalui proses matching WMS.
            if move.picking_id.return_id:
                move.picked = True
                continue

            # Raw material moves are consumed by manufacturing and do not use
            # the transfer matching flow to determine whether they were picked.
            if move.raw_material_production_id:
                move.picked = True
                continue

            # Finished product dari Manufacturing Order
            # tidak menggunakan proses matching.
            if move.production_id:
                continue

            if move.picking_id.picking_type_id.name in ("Storage", "Store Finished Product"):
                # Storage is the putaway leg. Picker status belongs to the
                # preceding operation and must not make a fully stored move
                # look unpicked to Odoo's backorder check.
                move.picked = bool(
                    move.move_line_ids
                    and all(
                        line.putaway_status == "finished"
                        for line in move.move_line_ids
                    )
                )
                continue

            if move.picking_id.picking_type_id.name in ("Receipts", "Receipt Internal"):
                move.picked = bool(
                    move.move_line_ids
                    and all(
                        line.picker_status == "finished"
                        and line.putaway_status == "finished"
                        for line in move.move_line_ids
                    )
                )
                continue

            # Transfer biasa dan raw material manufacturing
            # harus lolos matching.
            move.picked = bool(
                move.move_line_ids
                and all(
                    line.matching == "match"
                    for line in move.move_line_ids
                )
            )

    def _get_matching_status(self):
        """Return the matching result using the reference fields for each operation."""
        self.ensure_one()
        picking_name = self.picking_id.picking_type_id.name
        if not self.matching_lot_id or not self.matching_loc_dest_id:
            return 'not_match'

        if (
            picking_name in ('Pick', 'Pick Internal', 'Pick Components', 'Internal Transfers', 'Internal Transfer', 'Transfer Internal')
            or self.picking_id.is_ito_internal_transfer
        ):
            # expected_location = self.source_location_id or self.location_id
            expected_location = self.location_id
            expected_lot = self.product_lot_id or self.lot_id
        elif picking_name in ('Receipts', 'Receipt Internal', 'Storage', 'Store Finished Product'):
            expected_location = self.location_dest_id
            expected_lot = self.lot_id
        else:
            return 'not_match'

        return (
            'match'
            if expected_location == self.matching_loc_dest_id
            and expected_lot == self.matching_lot_id
            else 'not_match'
        )

    #ONLY LOT CONFIRMATION
    def _get_matching_status_mo(self):
            """Return the matching result using the reference fields for each operation."""
            self.ensure_one()
            if not self.matching_lot_id:
                return 'not_match'
    
            expected_lot = self.lot_id
            
            return (
                'match'
                if expected_lot == self.matching_lot_id
                else 'not_match'
            )
    

    @api.onchange('confirmation_quantity', 'auto_input_code', 'matching_lot_id', 'matching_loc_dest_id')
    def _compute_matching(self):
        for line in self:
            if line.picking_id:
                if (
                    line.picking_id.picking_type_id.name in ('Pick', 'Pick Internal', 'Pick Components', 'Internal Transfers', 'Internal Transfer', 'Transfer Internal')
                    or line.picking_id.is_ito_internal_transfer
                ):
                    line.matching = line._get_matching_status()
                    line.second_matching = (
                        'Match' if line.matching == 'match' else 'Not Match'
                    )
                elif line.picking_id.picking_type_id.name in ('Internal Transfers') and line.matching == 'match':
                    line.matching = line._get_matching_status()
                    line.picker_status = 'finished'
                elif line.picking_id.picking_type_id.name in ('Receipt Internal', 'Storage', 'Store Finished Product'):
                    line.matching = line._get_matching_status_mo()
                    line.second_matching = (
                        'Match' if line.matching == 'match' else 'Not Match'
                                    )
            elif line.move_id.raw_material_production_id:
                line.matching = line._get_matching_status_mo()
                line.second_matching = (
                    'Match' if line.matching == 'match' else 'Not Match'
                )
                
                 
    @api.onchange('auto_input_code')
    def _compute_auto_input_code(self):
        print('def _compute_auto_input_code(self)')
        print(f"Self: {self}")
        for line in self:
            picking_name = line.move_id.picking_id.picking_type_id.name
            print(f"validate stock move line with picking type name: {picking_name}")

            if picking_name and picking_name in ( 'Pick', 'Pick Components', 'Pick Internal'):
                loc_code = str(line.location_id.unique_code or '').strip()
                product_code = str(line.lot_id.name or '').strip()

                line.confirmation_location_code = loc_code
                line.confirmation_lot_code = product_code
                line.matching_lot_id = line.product_lot_id.id
                line.matching_loc_dest_id = line.source_location_id.id
                line.matching = 'match'
                line.second_matching = 'Match'

            if picking_name and picking_name == 'Receipt':
                loc_code = str(line.location_dest_id.unique_code or '').strip()
                product_code = str(line.lot_id.name or '').strip()

                line.confirmation_location_code = loc_code
                line.confirmation_lot_code = product_code
                line.matching = 'match'
            #     # loc_ok = bool(loc_code and conf_loc.lower() == loc_code.lower())
            #     # lot_ok = bool(product_code and conf_lot.lower() == product_code.lower())
                
                

            # if line.auto_input_code:
            #     print(f">>> Auto input code triggered for line {line.id}") # Cek di terminal Odoo
            #     line.matching = 'match'
            #     sml = self.env['stock.move.line'].browse(line._origin.id)
            #     sml.write({'matching': 'match'})
    
    def _copy_quant_info(self, vals):
        quant = self.env['stock.quant'].browse(vals.get('quant_id', 0))

        line_data = {
            'product_id': quant.product_id.id,
            'lot_id': quant.lot_id.id,
            # 'package_id': quant.package_id.id,
            # 'result_package_id': quant.package_id.id,
            'location_id': quant.location_id.id,
            'owner_id': quant.owner_id.id,
        }

        return line_data

    def action_open_putaway_move_line_wizard(self):
        if not self.exists():
            return {'type': 'ir.actions.client', 'tag': 'reload'}
        self.ensure_one()
        wizard = self.env['putaway.move.line.wizard'].create(
            self._prepare_putaway_move_line_wizard_values()
        )

        return {
            'type': 'ir.actions.act_window',
            'name': _('Putaway'),
            'res_model': 'putaway.move.line.wizard',
            'res_id': wizard.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_open_putaway_details(self):
        if not self.exists():
            return {'type': 'ir.actions.client', 'tag': 'reload'}
        self.ensure_one()
        if self.picking_type_name in (
            'Receipts', 'Receipt Internal', 'Storage', 'Store Finished Product'
        ):
            return self.action_open_putaway_move_line_wizard()

        return {
            'type': 'ir.actions.act_window',
            'name': _('Putaway Details'),
            'res_model': 'stock.move.line',
            'res_id': self.id,
            'view_mode': 'form',
            'view_id': self.env.ref(
                'tijari_wms_18.view_move_line_form_inherit_tijari'
            ).id,
            'target': 'new',
        }

    def _prepare_putaway_move_line_wizard_values(self):
        self.ensure_one()
        return {
            'move_line_id': self.id,
            'location_putaway_id': self.location_putaway_id.id,
            'original_location_putaway_id': self.location_putaway_id.id,
            'confirmation_location_code': self.confirmation_location_code,
            'original_confirmation_location_code': self.confirmation_location_code,
            'quantity': self.quantity,
            'confirmation_quantity': self.confirmation_quantity,
        }
