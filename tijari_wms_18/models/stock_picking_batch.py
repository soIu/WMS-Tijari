from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

class StockPickingBatch(models.Model):
    _inherit = 'stock.picking.batch'

    # Batch responsibility selections are stored on the batch and written
    # through to its pickings when changed.
    pick_responsible_ids = fields.Many2many(
        comodel_name='res.users',
        relation='stock_picking_batch_pick_resp_rel',
        column1='batch_id',
        column2='user_id',
        string='Assigned Picker',
        domain=[('share', '=', False)],
    )
    pack_responsible_ids = fields.Many2many(
        comodel_name='res.users',
        relation='stock_picking_batch_pack_resp_rel',
        column1='batch_id',
        column2='user_id',
        string='Assigned Packer',
        domain=[('share', '=', False)],
    )
    do_responsible_ids = fields.Many2many(
        comodel_name='res.users',
        relation='stock_picking_batch_do_resp_rel',
        column1='batch_id',
        column2='user_id',
        string='Assigned Loader',
        domain=[('share', '=', False)],
    )
    putaway_responsible_ids = fields.Many2many(
        comodel_name='res.users',
        relation='stock_picking_batch_putaway_resp_rel',
        column1='batch_id',
        column2='user_id',
        string='Assigned Putaway',
        domain=[('share', '=', False)],
    )
    batch_responsible_user_ids = fields.Many2many(
        comodel_name='res.users',
        relation='stock_picking_batch_user_resp_rel',
        column1='batch_id',
        column2='user_id',
        string='Responsible Users',
    )

    _RESPONSIBLE_BATCH_TO_PICKING_FIELDS = {
        'pick_responsible_ids': 'pick_responsible_ids',
        'pack_responsible_ids': 'pack_responsible_ids',
        'do_responsible_ids': 'do_responsible_ids',
        'putaway_responsible_ids': 'putaway_responsible_ids',
    }

    @api.model_create_multi
    def create(self, vals_list):
        batches = super().create(vals_list)
        for batch in batches.filtered('picking_ids'):
            responsible_vals = {
                field_name: [(6, 0, batch.picking_ids.mapped(picking_field).ids)]
                for field_name, picking_field
                in batch._RESPONSIBLE_BATCH_TO_PICKING_FIELDS.items()
            }
            batch.with_context(skip_batch_responsible_sync=True).write(
                responsible_vals
            )
        return batches

    def write(self, vals):
        result = super().write(vals)
        responsible_fields = self._RESPONSIBLE_BATCH_TO_PICKING_FIELDS
        changed_responsible_fields = responsible_fields.keys() & vals.keys()
        skip_picking_sync = self.env.context.get('skip_batch_responsible_sync')

        for batch in self:
            if changed_responsible_fields and not skip_picking_sync:
                picking_vals = {
                    responsible_fields[field_name]: [(6, 0, batch[field_name].ids)]
                    for field_name in changed_responsible_fields
                }
                if batch.picking_ids:
                    batch.picking_ids.with_context(
                        tijari_responsible_sync=True,
                    ).write(picking_vals)

            if changed_responsible_fields or 'picking_ids' in vals:
                if 'picking_ids' in vals and not changed_responsible_fields:
                    batch_responsible_vals = {
                        field_name: [(6, 0, batch.picking_ids.mapped(picking_field).ids)]
                        for field_name, picking_field
                        in responsible_fields.items()
                    }
                    responsible_users = batch.picking_ids.mapped(
                        'pick_responsible_ids'
                    ) | batch.picking_ids.mapped('pack_responsible_ids')
                    responsible_users |= batch.picking_ids.mapped('do_responsible_ids')
                    responsible_users |= batch.picking_ids.mapped('putaway_responsible_ids')
                else:
                    batch_responsible_vals = {}
                    responsible_users = (
                        batch.pick_responsible_ids
                        | batch.pack_responsible_ids
                        | batch.do_responsible_ids
                        | batch.putaway_responsible_ids
                    )
                batch_responsible_vals['batch_responsible_user_ids'] = [
                    (6, 0, responsible_users.ids)
                ]
                super(StockPickingBatch, batch).write({
                    **batch_responsible_vals,
                })
        return result

    allowed_putaway_product_ids = fields.Many2many(
        'product.product',
        compute='_compute_allowed_putaway_product_ids',
    )
    allowed_putaway_move_line_ids = fields.Many2many(
        'stock.move.line',
        compute='_compute_allowed_putaway_move_line_ids',
    )
    scan_product_button = fields.Boolean(string='Scan Product')
    scan_product_by_lot_button = fields.Boolean(
        string='Scan Product By Lot',
        compute='_compute_scan_product_by_lot_button',
    )
    putaway_product_id = fields.Many2one(
        'product.product',
        string='Product Name',
    )
    putaway_scan_code = fields.Char(
        string='Scan Handheld',
        help='Bisa diisi dari scanner handheld atau diketik manual.',
    )
    allowed_putaway_lot_ids = fields.Many2many(
        'stock.lot',
        compute='_compute_allowed_putaway_lot_ids',
    )
    putaway_lot_id = fields.Many2one(
        comodel_name='stock.lot',
        string='LOT Number',
        readonly=False,
        domain="[('id', 'in', allowed_putaway_lot_ids)]",
    )
    putaway_qty_finish = fields.Float(
        string='Quantity Dipenuhi',
        compute='_compute_putaway_qty_finish',
    )
    putaway_move_line_ids = fields.Many2many(
        comodel_name='stock.move.line',
        string='Stock Move Lines',
        compute='_compute_putaway_values',
        readonly=False,
    )
    putaway_move_ids = fields.Many2many(
        comodel_name='stock.move',
        string='Putaway Stock Moves',
        compute='_compute_putaway_move_ids',
    )
    picker_scan_code = fields.Char(
        string='Scan Handheld',
        help='Bisa diisi dari scanner handheld atau diketik manual.',
    )
    scan_picker_product_by_lot_button = fields.Boolean(
        compute='_compute_scan_picker_product_by_lot_button',
    )
    scan_picker_product_button = fields.Boolean(string='Scan Product')
    allowed_picker_product_ids = fields.Many2many(
        'product.product',
        compute='_compute_allowed_picker_product_ids',
    )
    picker_product_id = fields.Many2one(
        'product.product',
        string='Product Name',
    )
    allowed_picker_lot_ids = fields.Many2many(
        'stock.lot',
        compute='_compute_allowed_picker_lot_ids',
    )
    picker_lot_id = fields.Many2one(
        'stock.lot',
        string='LOT Number',
        domain="[('id', 'in', allowed_picker_lot_ids)]",
    )
    picker_move_ids = fields.Many2many(
        'stock.move',
        string='Picker Stock Moves',
        compute='_compute_picker_values',
    )
    picker_move_line_ids = fields.Many2many(
        'stock.move.line',
        string='Picker Stock Move Lines',
        compute='_compute_picker_move_line_values',
        readonly=False,
    )
    picker_qty_finish = fields.Float(
        string='Quantity Dipenuhi',
        compute='_compute_picker_qty_finish',
    )
    picker_sort_by_location = fields.Boolean(string='Location')
    picker_sort_by_product = fields.Boolean(string='Product')
    picking_type_name = fields.Char(
        related='picking_type_id.name',
        store=False,
    )
    show_picker_page = fields.Boolean(
        compute='_compute_show_picker_page',
    )
    show_packer_page = fields.Boolean(
        compute='_compute_show_packer_page',
    )
    show_putaway_page = fields.Boolean(
        compute='_compute_show_putaway_page',
    )
    show_delivery_page = fields.Boolean(compute='_compute_show_delivery_page')
    allowed_unpacked_product_ids = fields.Many2many(
        'product.product', compute='_compute_allowed_unpacked_product_ids',
    )
    allowed_unpacked_lot_ids = fields.Many2many(
        'stock.lot', compute='_compute_allowed_unpacked_lot_ids',
    )
    unpacked_product_id = fields.Many2one('product.product', string='Product Name')
    unpacked_lot_id = fields.Many2one(
        'stock.lot', string='LOT Number',
        domain="[('id', 'in', allowed_unpacked_lot_ids)]",
    )
    unpacked_lot_code = fields.Char(string='Filter Lot Code')
    unpacked_scan_code = fields.Char(string='Scan Handheld')
    filtered_unpacked_move_ids = fields.Many2many(
        'stock.move', compute='_compute_filtered_unpacked_move_ids',
        string='Filtered Unpacked Moves',
    )
    available_package_ids = fields.Many2many(
        'stock.quant.package', compute='_compute_batch_available_package_ids',
        string='Available Packages',
    )
    scanned_package_id = fields.Many2one('stock.quant.package', string='Package Number')
    package_scan_code = fields.Char(string='Scan Handheld')
    filtered_package_level_id = fields.Many2one(
        'stock.package_level', string='Filtered Package Level', readonly=True,
    )
    filtered_package_level_ids = fields.Many2many(
        'stock.package_level', compute='_compute_batch_package_level_ids',
        string='Filtered Package Levels',
    )
    scan_package_button = fields.Boolean(string='Scan Package')

    @api.depends('picking_ids.do_responsible_ids')
    def _compute_show_delivery_page(self):
        user = self.env.user
        is_warehouse_operator = (
            user.has_group('tijari_wms_18.group_operator')
            and not user.has_group('base.group_system')
        )
        for batch in self:
            batch.show_delivery_page = (
                not is_warehouse_operator
                or user in batch.picking_ids.mapped('do_responsible_ids')
            )

    def _get_unpacked_base_moves(self):
        self.ensure_one()
        return self.move_ids.filtered(
            lambda move: move.state != 'cancel' and bool(move.product_id)
        )

    @api.depends('move_ids.product_id', 'move_ids.state')
    def _compute_allowed_unpacked_product_ids(self):
        for batch in self:
            batch.allowed_unpacked_product_ids = batch._get_unpacked_base_moves().product_id

    @api.depends('move_ids.move_line_ids.lot_id', 'move_ids.move_line_ids.lot_name')
    def _compute_allowed_unpacked_lot_ids(self):
        for batch in self:
            lines = batch._get_unpacked_base_moves().mapped('move_line_ids')
            lots = lines.lot_id
            lot_names = lines.filtered(lambda line: line.lot_name and not line.lot_id).mapped('lot_name')
            if lot_names:
                domain = [('name', 'in', lot_names)]
                products = lines.product_id
                if products:
                    domain.append(('product_id', 'in', products.ids))
                lots |= self.env['stock.lot'].search(domain)
            batch.allowed_unpacked_lot_ids = lots

    @api.depends(
        'unpacked_product_id', 'unpacked_lot_id', 'unpacked_lot_code',
        'move_ids.product_id', 'move_ids.state', 'move_ids.move_line_ids.lot_id',
        'move_ids.move_line_ids.lot_name',
    )
    def _compute_filtered_unpacked_move_ids(self):
        for batch in self:
            moves = batch._get_unpacked_base_moves()
            if batch.unpacked_product_id:
                moves = moves.filtered(lambda move: move.product_id == batch.unpacked_product_id)
            if batch.unpacked_lot_id or batch.unpacked_lot_code:
                lot_code = batch.unpacked_lot_code or batch.unpacked_lot_id.name
                moves = moves.filtered(lambda move: move.move_line_ids.filtered(
                    lambda line: (
                        (batch.unpacked_lot_id and line.lot_id == batch.unpacked_lot_id)
                        or (lot_code and (line.lot_name == lot_code or line.lot_id.name == lot_code))
                    )
                ))
            batch.filtered_unpacked_move_ids = moves

    @api.depends('picking_ids.package_level_ids.package_id')
    def _compute_batch_available_package_ids(self):
        for batch in self:
            batch.available_package_ids = batch.picking_ids.mapped('package_level_ids.package_id')

    @api.depends('picking_ids.package_level_ids', 'scanned_package_id')
    def _compute_batch_package_level_ids(self):
        for batch in self:
            levels = batch.picking_ids.mapped('package_level_ids')
            if batch.scanned_package_id:
                levels = levels.filtered(lambda level: level.package_id == batch.scanned_package_id)
            batch.filtered_package_level_ids = levels

    @api.onchange('unpacked_product_id')
    def _onchange_batch_unpacked_product_id(self):
        for batch in self:
            if batch.unpacked_lot_id and batch.unpacked_lot_id.product_id != batch.unpacked_product_id:
                batch.unpacked_lot_id = False
                batch.unpacked_lot_code = False

    @api.onchange('unpacked_lot_id')
    def _onchange_batch_unpacked_lot_id(self):
        for batch in self:
            batch.unpacked_lot_code = batch.unpacked_lot_id.name or False
            if batch.unpacked_lot_id:
                moves = batch._get_unpacked_base_moves().filtered(
                    lambda move: batch.unpacked_lot_id in move.move_line_ids.lot_id
                    or batch.unpacked_lot_id.name in move.move_line_ids.mapped('lot_name')
                )
                if moves:
                    batch.unpacked_product_id = moves.product_id[:1]

    @api.onchange('unpacked_scan_code')
    def _onchange_batch_unpacked_scan_code(self):
        for batch in self:
            code = (batch.unpacked_scan_code or '').strip()
            if not code:
                continue
            product, _matched_by = batch._find_product_scan_match(code)
            if product:
                if product not in batch.allowed_unpacked_product_ids:
                    raise ValidationError(_('Product hasil scan tidak ada di dokumen ini.'))
                lines = batch._get_unpacked_base_moves().filtered(
                    lambda move: move.product_id == product
                ).mapped('move_line_ids')
                lot = lines.lot_id[:1]
                batch.update({
                    'unpacked_product_id': product.id,
                    'unpacked_lot_id': lot.id or False,
                    'unpacked_lot_code': lot.name or (lines.mapped('lot_name')[:1] or False),
                    'unpacked_scan_code': False,
                })
                continue
            lines = batch._get_unpacked_base_moves().mapped('move_line_ids').filtered(
                lambda line: (line.lot_id and line.lot_id.name == code) or line.lot_name == code
            )
            if not lines:
                raise ValidationError(_('Kode %s tidak ditemukan sebagai Barcode, SKU, atau Lot.') % code)
            products = lines.product_id
            if len(products) > 1:
                raise ValidationError(_('Lot hasil scan mengarah ke lebih dari satu product.'))
            batch.update({
                'unpacked_product_id': products[:1].id,
                'unpacked_lot_id': lines.lot_id.filtered(lambda lot: lot.name == code)[:1].id or False,
                'unpacked_lot_code': code,
                'unpacked_scan_code': False,
            })

    @api.onchange('scanned_package_id')
    def _onchange_batch_scanned_package_id(self):
        for batch in self:
            batch.filtered_package_level_id = batch.picking_ids.mapped('package_level_ids').filtered(
                lambda level: level.package_id == batch.scanned_package_id
            )[:1]

    @api.onchange('package_scan_code')
    def _onchange_batch_package_scan_code(self):
        for batch in self:
            code = (batch.package_scan_code or '').strip()
            if code:
                package = batch.available_package_ids.filtered(lambda item: item.name == code)[:1]
                if not package:
                    raise ValidationError(_('Package tidak ditemukan untuk kode scan: %s') % code)
                batch.scanned_package_id = package
                batch.package_scan_code = False

    def action_apply_scanned_package(self, scanned_code):
        self.ensure_one()
        package = self.available_package_ids.filtered(lambda item: item.name == (scanned_code or '').strip())[:1]
        if not package:
            raise ValidationError(_('Package tidak ditemukan untuk kode scan: %s') % scanned_code)
        level = self.picking_ids.mapped('package_level_ids').filtered(lambda item: item.package_id == package)[:1]
        wizard = self.env['package.level.detail.wizard'].create({
            'package_level_id': level.id,
            'status': 'finished' if level.status == 'finished' else 'unfinished',
            'package_id': package.id,
            'move_line_ids': [(6, 0, level.move_line_ids.ids)],
        }) if level else False
        self.write({'scanned_package_id': package.id, 'filtered_package_level_id': level.id or False})
        return {
            'package_id': package.id,
            'package_level_id': level.id or False,
            'package_detail_wizard_id': wizard.id if wizard else False,
            'display_name': package.display_name,
        }

    @api.depends('picking_ids.pick_responsible_ids')
    def _compute_show_picker_page(self):
        user = self.env.user
        is_warehouse_operator = (
            user.has_group('tijari_wms_18.group_operator')
            and not user.has_group('base.group_system')
        )
        for batch in self:
            assigned_users = batch.picking_ids.mapped('pick_responsible_ids')
            batch.show_picker_page = (
                not is_warehouse_operator
                or user in assigned_users
            )

    @api.depends('picking_ids.pack_responsible_ids')
    def _compute_show_packer_page(self):
        user = self.env.user
        is_warehouse_operator = (
            user.has_group('tijari_wms_18.group_operator')
            and not user.has_group('base.group_system')
        )
        for batch in self:
            assigned_users = batch.picking_ids.mapped('pack_responsible_ids')
            batch.show_packer_page = (
                not is_warehouse_operator
                or user in assigned_users
            )

    @api.depends('picking_ids.putaway_responsible_ids')
    def _compute_show_putaway_page(self):
        user = self.env.user
        is_warehouse_operator = (
            user.has_group('tijari_wms_18.group_operator')
            and not user.has_group('base.group_system')
        )
        for batch in self:
            assigned_users = batch.picking_ids.mapped('putaway_responsible_ids')
            batch.show_putaway_page = (
                not is_warehouse_operator
                or user in assigned_users
            )

    def _sync_responsible_users(self):
        for picking in self:
            missing_do = (
                picking.pick_responsible_ids
                - picking.do_responsible_ids
            )

            if missing_do:
                picking.do_responsible_ids = (
                    picking.do_responsible_ids | missing_do
                )

            responsible_users = (
                picking.pick_responsible_ids
                | picking.pack_responsible_ids
                | picking.do_responsible_ids
            )

            picking.user_ids = [(6, 0, responsible_users.ids)]

    def _compute_scan_product_by_lot_button(self):
        for batch in self:
            batch.scan_product_by_lot_button = True

    def _compute_scan_picker_product_by_lot_button(self):
        for batch in self:
            batch.scan_picker_product_by_lot_button = True

    def _get_picker_base_moves(self):
        self.ensure_one()
        return self.move_ids.filtered(
            lambda move: move.state != 'cancel' and move.product_id
        )

    def _get_picker_moves(self):
        self.ensure_one()
        moves = self._get_picker_base_moves()
        if self.picker_product_id:
            moves = moves.filtered(
                lambda move: move.product_id == self.picker_product_id
            )
        if self.picker_lot_id:
            moves = moves.filtered(
                lambda move: (
                    self.picker_lot_id in move.lot_ids
                    or self.picker_lot_id in move.move_line_ids.lot_id
                    or self.picker_lot_id.name in move.move_line_ids.mapped('lot_name')
                )
            )
        if self.picking_type_id.name == 'Pick' and self.picker_sort_by_location:
            return moves.sorted(key=lambda move: (
                move.pick_status_sequence,
                (move.location_id.name or '').casefold(),
                move.sequence,
                move._origin.id or 0,
            ))
        if self.picking_type_id.name == 'Pick' and self.picker_sort_by_product:
            return moves.sorted(key=lambda move: (
                move.pick_status_sequence,
                move.product_id.display_name or '',
                move.sequence,
                move._origin.id or 0,
            ))
        return moves.sorted(
            key=lambda move: (move.pick_status_sequence, move._origin.id or 0)
        )

    def _get_picker_base_move_lines(self):
        self.ensure_one()
        return self._get_picker_base_moves().mapped('move_line_ids').filtered(
            lambda line: line.state != 'cancel' and line.product_id
        )

    def _get_picker_move_lines(self):
        self.ensure_one()
        lines = self._get_picker_base_move_lines()
        if self.picker_product_id:
            lines = lines.filtered(lambda line: line.product_id == self.picker_product_id)
        if self.picker_lot_id:
            lines = lines.filtered(lambda line: (
                line.lot_id == self.picker_lot_id
                or line.lot_name == self.picker_lot_id.name
            ))
        if self.picker_sort_by_location:
            return lines.sorted(key=lambda line: (
                line.pick_status == 'finished',
                (line.location_id.name or '').casefold(),
                line.move_id.sequence,
                line._origin.id or 0,
            ))
        if self.picker_sort_by_product:
            return lines.sorted(key=lambda line: (
                line.pick_status == 'finished',
                line.product_id.display_name or '',
                line.move_id.sequence,
                line._origin.id or 0,
            ))
        return lines.sorted(key=lambda line: (
            line.pick_status == 'finished',
            line.move_id.sequence,
            line._origin.id or 0,
        ))

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

    def _get_putaway_base_move_lines(self):
        self.ensure_one()

        return self._get_batch_move_lines().filtered(
            lambda line: (
                (line.lot_id or line.lot_name)
                and line.quantity > 0
                and (
                    line.picker_status == 'finished'
                    or line.confirmation_quantity > 0
                )
            )
        )

    def _get_putaway_move_lines(self):
        self.ensure_one()

        move_lines = self._get_putaway_base_move_lines()
        if self.putaway_product_id:
            move_lines = move_lines.filtered(
                lambda line: line.product_id == self.putaway_product_id
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

    def _get_putaway_moves(self):
        self.ensure_one()

        moves = self.move_ids.filtered(
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

    def _get_putaway_move_lines_for_product(self, product):
        self.ensure_one()

        move_lines = self._get_putaway_base_move_lines()
        if product:
            move_lines = move_lines.filtered(
                lambda line: line.product_id == product
            )
        return move_lines

    def _get_putaway_lots(self):
        self.ensure_one()

        move_lines = self._get_putaway_base_move_lines()
        if self.putaway_product_id:
            move_lines = move_lines.filtered(
                lambda line: line.product_id == self.putaway_product_id
            )

        lots = move_lines.lot_id
        lot_names = move_lines.filtered(
            lambda line: line.lot_name and not line.lot_id
        ).mapped('lot_name')
        if lot_names:
            domain = [('name', 'in', lot_names)]
            products = move_lines.product_id
            if products:
                domain.append(('product_id', 'in', products.ids))
            lots |= self.env['stock.lot'].search(domain)
        return lots

    def _get_batch_move_lines(self):
        self.ensure_one()
        return self.move_ids.move_line_ids

    @api.depends(
        'move_ids',
        'move_ids.product_id',
        'move_ids.move_line_ids',
        'move_ids.move_line_ids.product_id',
        'move_ids.move_line_ids.lot_id',
        'move_ids.move_line_ids.quantity',
        'move_ids.move_line_ids.confirmation_quantity',
        'move_ids.move_line_ids.picker_status',
    )
    def _compute_allowed_putaway_product_ids(self):
        for batch in self:
            batch.allowed_putaway_product_ids = batch._get_putaway_base_move_lines().product_id

    @api.depends(
        'putaway_product_id',
        'putaway_lot_id',
        'move_ids',
        'move_ids.move_line_ids',
        'move_ids.move_line_ids.product_id',
        'move_ids.move_line_ids.lot_id',
        'move_ids.move_line_ids.lot_name',
        'move_ids.move_line_ids.quantity',
        'move_ids.move_line_ids.confirmation_quantity',
        'move_ids.move_line_ids.picker_status',
    )
    def _compute_allowed_putaway_lot_ids(self):
        for batch in self:
            batch.allowed_putaway_lot_ids = batch._get_putaway_lots()

    @api.depends(
        'putaway_product_id',
        'putaway_lot_id',
        'move_ids',
        'move_ids.move_line_ids',
        'move_ids.move_line_ids.product_id',
        'move_ids.move_line_ids.lot_id',
        'move_ids.move_line_ids.quantity',
        'move_ids.move_line_ids.confirmation_quantity',
        'move_ids.move_line_ids.picker_status',
    )
    def _compute_allowed_putaway_move_line_ids(self):
        for batch in self:
            batch.allowed_putaway_move_line_ids = batch._get_putaway_move_lines()

    @api.depends(
        'putaway_product_id',
        'putaway_lot_id',
        'move_ids',
        'move_ids.move_line_ids',
        'move_ids.move_line_ids.product_id',
        'move_ids.move_line_ids.lot_id',
        'move_ids.move_line_ids.quantity',
        'move_ids.move_line_ids.confirmation_quantity',
        'move_ids.move_line_ids.picker_status',
    )
    def _compute_putaway_values(self):
        for batch in self:
            move_lines = batch._get_putaway_move_lines()
            batch.putaway_move_line_ids = move_lines

    @api.depends(
        'putaway_product_id',
        'putaway_lot_id',
        'move_ids',
        'move_ids.product_id',
        'move_ids.state',
        'move_ids.putaway_sequence',
        'move_ids.move_line_ids',
        'move_ids.move_line_ids.lot_id',
        'move_ids.move_line_ids.lot_name',
        'move_ids.move_line_ids.quantity',
        'move_ids.move_line_ids.confirmation_quantity',
        'move_ids.move_line_ids.picker_status',
        'move_ids.move_line_ids.putaway_status',
    )
    def _compute_putaway_move_ids(self):
        for batch in self:
            batch.putaway_move_ids = batch._get_putaway_moves()

    @api.depends(
        'putaway_product_id',
        'putaway_lot_id',
        'move_ids',
        'move_ids.move_line_ids',
        'move_ids.move_line_ids.product_id',
        'move_ids.move_line_ids.lot_id',
        'move_ids.move_line_ids.lot_name',
        'move_ids.move_line_ids.quantity',
        'move_ids.move_line_ids.confirmation_quantity',
        'move_ids.move_line_ids.picker_status',
    )
    def _compute_putaway_qty_finish(self):
        for batch in self:
            batch.putaway_qty_finish = sum(
                batch._get_putaway_move_lines().mapped('confirmation_quantity')
            )

    @api.depends(
        'move_ids',
        'move_ids.product_id',
        'move_ids.state',
    )
    def _compute_allowed_picker_product_ids(self):
        for batch in self:
            batch.allowed_picker_product_ids = batch._get_picker_base_moves().product_id

    @api.depends(
        'picker_product_id',
        'move_ids',
        'move_ids.product_id',
        'move_ids.state',
        'move_ids.lot_ids',
        'move_ids.move_line_ids.lot_id',
        'move_ids.move_line_ids.lot_name',
    )
    def _compute_allowed_picker_lot_ids(self):
        for batch in self:
            batch.allowed_picker_lot_ids = batch._get_picker_lots()

    @api.depends(
        'picker_product_id',
        'picker_lot_id',
        'picker_sort_by_location',
        'picker_sort_by_product',
        'move_ids',
        'move_ids.product_id',
        'move_ids.location_id',
        'move_ids.state',
        'move_ids.pick_status_sequence',
    )
    def _compute_picker_values(self):
        for batch in self:
            batch.picker_move_ids = batch._get_picker_moves()

    @api.depends(
        'picker_product_id', 'picker_lot_id', 'picker_sort_by_location',
        'picker_sort_by_product', 'move_ids.move_line_ids',
        'move_ids.move_line_ids.product_id', 'move_ids.move_line_ids.lot_id',
        'move_ids.move_line_ids.lot_name', 'move_ids.move_line_ids.pick_status',
        'move_ids.move_line_ids.location_id', 'move_ids.move_line_ids.quantity',
    )
    def _compute_picker_move_line_values(self):
        for batch in self:
            batch.picker_move_line_ids = batch._get_picker_move_lines()

    @api.depends(
        'picker_product_id',
        'picker_lot_id',
        'move_ids',
        'move_ids.product_id',
        'move_ids.quantity',
        'move_ids.state',
    )
    def _compute_picker_qty_finish(self):
        for batch in self:
            batch.picker_qty_finish = sum(
                batch._get_picker_moves().mapped('quantity')
            )

    @api.onchange('picker_product_id')
    def _onchange_picker_product_id(self):
        for batch in self:
            batch.picker_move_ids = batch._get_picker_moves()
            batch.picker_move_line_ids = batch._get_picker_move_lines()
            batch.picker_lot_id = (
                batch._get_picker_lots()[:1]
                if batch.picker_product_id
                else False
            )

    @api.onchange('picker_sort_by_location')
    def _onchange_picker_sort_by_location(self):
        for batch in self:
            if batch.picker_sort_by_location:
                batch.picker_sort_by_product = False
            batch.picker_move_ids = batch._get_picker_moves()
            batch.picker_move_line_ids = batch._get_picker_move_lines()

    @api.onchange('picker_sort_by_product')
    def _onchange_picker_sort_by_product(self):
        for batch in self:
            if batch.picker_sort_by_product:
                batch.picker_sort_by_location = False
            batch.picker_move_ids = batch._get_picker_moves()
            batch.picker_move_line_ids = batch._get_picker_move_lines()

    @api.onchange('picker_lot_id')
    def _onchange_picker_lot_id(self):
        for batch in self:
            if not batch.picker_lot_id:
                batch.picker_product_id = False
                batch.picker_move_ids = batch._get_picker_moves()
                batch.picker_move_line_ids = batch._get_picker_move_lines()
                continue
            moves = batch._get_picker_base_moves().filtered(
                lambda move: (
                    batch.picker_lot_id in move.lot_ids
                    or batch.picker_lot_id in move.move_line_ids.lot_id
                    or batch.picker_lot_id.name in move.move_line_ids.mapped('lot_name')
                )
            )
            products = moves.product_id
            if products:
                batch.picker_product_id = products[:1]
            batch.picker_move_ids = batch._get_picker_moves()
            batch.picker_move_line_ids = batch._get_picker_move_lines()

    def _find_product_scan_match(self, scan_code):
        scan_code = (scan_code or '').strip()
        product = self.env['product.product'].search(
            [('barcode', '=', scan_code)], limit=1
        )
        if product:
            return product, _('Barcode')
        product = self.env['product.product'].search(
            [('default_code', '=', scan_code)], limit=1
        )
        if product:
            return product, _('SKU')
        return self.env['product.product'].browse(), False

    def _get_picker_scan_values(self, scanned_code):
        self.ensure_one()
        scanned_code = (scanned_code or '').strip()
        if not scanned_code:
            raise ValidationError(_('Kode scan tidak boleh kosong.'))

        moves = self._get_picker_base_moves()
        product, matched_by = self._find_product_scan_match(scanned_code)
        if product:
            matching_moves = moves.filtered(lambda move: move.product_id == product)
            if not matching_moves:
                raise ValidationError(_(
                    '%s %s dikenali sebagai Product %s, tetapi product tersebut '
                    'tidak ada di batch ini.'
                ) % (matched_by, scanned_code, product.display_name))
            lot = matching_moves.lot_ids[:1] or matching_moves.move_line_ids.lot_id[:1]
        else:
            lots = self.env['stock.lot'].search([('name', '=', scanned_code)])
            matching_moves = moves.filtered(
                lambda move: (
                    bool(move.lot_ids & lots)
                    or bool(move.move_line_ids.filtered(
                        lambda line: (
                            line.lot_id in lots
                            or line.lot_name == scanned_code
                        )
                    ))
                )
            )
            if not matching_moves:
                raise ValidationError(_(
                    'Kode %s tidak ditemukan sebagai Barcode, SKU, atau Lot di batch ini.'
                ) % scanned_code)
            products = matching_moves.product_id
            if len(products) > 1:
                raise ValidationError(_('Lot hasil scan mengarah ke lebih dari satu product.'))
            product = products[:1]
            lot = lots[:1]

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
            },
        }

    def action_apply_scanned_picker_code(self, scanned_code):
        self.ensure_one()
        values = self._get_picker_scan_values(scanned_code)
        self.write(values['values'])
        return values['result']

    def action_apply_scanned_picker_product_by_lot(self, scanned_code):
        return self.action_apply_scanned_picker_code(scanned_code)

    @api.onchange('picker_scan_code')
    def _onchange_picker_scan_code(self):
        for batch in self:
            if not batch.picker_scan_code:
                continue
            values = batch._get_picker_scan_values(batch.picker_scan_code)
            batch.update(values['values'])
            batch.picker_scan_code = False

    @api.onchange('putaway_product_id')
    def _onchange_putaway_product_id(self):
        for batch in self:
            move_lines = batch._get_putaway_move_lines_for_product(batch.putaway_product_id)
            batch.putaway_move_line_ids = move_lines
            batch.putaway_lot_id = (
                move_lines.mapped('lot_id')[:1]
                if batch.putaway_product_id
                else False
            )

    @api.onchange('putaway_lot_id')
    def _onchange_putaway_lot_id(self):
        for batch in self:
            if not batch.putaway_lot_id:
                batch.putaway_product_id = False
                batch.putaway_move_line_ids = batch._get_putaway_move_lines()
                continue
            move_lines = batch._get_putaway_base_move_lines().filtered(
                lambda line: (
                    line.lot_id == batch.putaway_lot_id
                    or line.lot_name == batch.putaway_lot_id.name
                )
            )
            products = move_lines.product_id
            if products:
                batch.putaway_product_id = products[:1]
            batch.putaway_move_line_ids = batch._get_putaway_move_lines()

    def action_prepare_scan_product(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'adt_scan_qr_to_picking_product',
            'params': {
                'res_model': self._name,
                'res_id': self.id,
                'title': 'Scan Product QR Code',
            },
        }

    def action_detailed_operations(self):
        self.ensure_one()
        view_id = self.env.ref('stock.view_stock_move_line_detailed_operation_tree').id
        first_picking = self.picking_ids[:1]
        return {
            'name': _('Detailed Operations'),
            'view_mode': 'list',
            'type': 'ir.actions.act_window',
            'res_model': 'stock.move.line',
            'views': [(view_id, 'list')],
            'domain': [('id', 'in', self._get_batch_move_lines().ids)],
            'context': {
                'sml_specific_default': True,
                'default_picking_id': first_picking.id,
                'default_location_id': first_picking.location_id.id,
                'default_location_dest_id': first_picking.location_dest_id.id,
                'default_company_id': self.company_id.id,
                'show_lots_text': False,
                'picking_code': self.picking_type_code,
                'create': self.state not in ('done', 'cancel'),
            },
        }

    def action_generate_lot(self):
        for batch in self:
            batch._get_batch_move_lines().generate_lot()


    def action_validate_lot(self):
        for batch in self:
            batch.picking_ids.move_ids.action_validate_lot()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Done'),
                'message': _('Lot validated for batch operations.'),
                'type': 'success',
                'sticky': False,
            },
        }

    def _check_all_move_lines_matched(self):
        for batch in self:
            active_pickings = batch.picking_ids.filtered(
                lambda picking: picking.state not in ('done', 'cancel')
            )
            for picking in active_pickings:
                # Keep batch validation aligned with the operation-specific
                # checks used when validating an individual picking.
                picking._check_validate_requirements()

    def action_done(self):
        self._check_all_move_lines_matched()
        return super().action_done()

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
                    _('Lot %s ditemukan, tetapi tidak ada di dokumen ini.')
                    % scan_code
                )
            raise ValidationError(
                _('Kode %s tidak ditemukan sebagai Barcode, SKU, atau Lot.')
                % scan_code
            )

        products = move_lines.product_id
        if len(products) > 1:
            raise ValidationError(_('Lot hasil scan mengarah ke lebih dari satu product.'))

        return products[:1], move_lines, lots[:1]

    def action_apply_scanned_product(self, scanned_code):
        self.ensure_one()
        scanned_code = (scanned_code or '').strip()
        product = self._find_product_by_scan_code(scanned_code)

        if not product:
            raise ValidationError(
                'Product tidak ditemukan untuk kode scan: %s' % scanned_code
            )

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
            raise ValidationError(_('Kode scan tidak boleh kosong.'))

        move_lines = self._get_putaway_base_move_lines()
        product, matched_by = self._find_product_scan_match(scanned_code)
        if product:
            matching_lines = move_lines.filtered(
                lambda line: line.product_id == product
            )
            if matching_lines:
                lot = matching_lines.mapped('lot_id')[:1]
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
                        'matched_by': 'product',
                    },
                }
            raise ValidationError(
                _(
                    '%s %s dikenali sebagai Product %s, tetapi product tersebut '
                    'tidak ada di dokumen ini.'
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
                'matched_by': 'lot',
            },
        }

    def action_apply_scanned_putaway_code(self, scanned_code):
        self.ensure_one()
        values = self._get_putaway_scan_values(scanned_code)
        self.write(values['values'])
        return values['result']

    @api.onchange('putaway_scan_code')
    def _onchange_putaway_scan_code(self):
        for batch in self:
            if not batch.putaway_scan_code:
                continue
            values = batch._get_putaway_scan_values(batch.putaway_scan_code)
            batch.update(values['values'])
            batch.putaway_scan_code = False

class StockPickingToBatchInherit(models.TransientModel):
    _inherit = 'stock.picking.to.batch'

    operation_type_name = fields.Char(
            string='Operation Type',
            readonly=True,
        )

    pick_responsible_ids = fields.Many2many(
        comodel_name="res.users",
        relation="stock_picking_to_batch_pick_responsible_rel",
        column1="wizard_id",
        column2="user_id",
        string="Assigned Picker",
        domain=[("share", "=", False)],
    )

    pack_responsible_ids = fields.Many2many(
        comodel_name="res.users",
        relation="stock_picking_to_batch_pack_responsible_rel",
        column1="wizard_id",
        column2="user_id",
        string="Assigned Packer",
        domain=[("share", "=", False)],
    )

    do_responsible_ids = fields.Many2many(
        comodel_name="res.users",
        relation="stock_picking_to_batch_do_responsible_rel",
        column1="wizard_id",
        column2="user_id",
        string="Assigned Loader",
        domain=[("share", "=", False)],
    )

    putaway_responsible_ids = fields.Many2many(
        comodel_name="res.users",
        relation="stock_picking_to_batch_putaway_responsible_rel",
        column1="wizard_id",
        column2="user_id",
        string="Assigned Putaway",
        domain=[("share", "=", False)],
    )

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)

        active_ids = self.env.context.get('active_ids', [])
        pickings = self.env['stock.picking'].browse(active_ids).exists()

        if not pickings:
            return vals

        operation_types = pickings.mapped('picking_type_id')

        if len(operation_types) == 1:
            vals['operation_type_name'] = operation_types.name
        else:
            vals['operation_type_name'] = False

        return vals

    def attach_pickings(self):
        self.ensure_one()

        pickings = self.env['stock.picking'].browse(
            self.env.context.get('active_ids', [])
        ).exists()

        if not pickings:
            raise UserError(
                _("Tidak ada transfer yang dipilih untuk dimasukkan ke batch.")
            )

        # Operation Type harus sama
        operation_types = pickings.mapped('picking_type_id')

        if len(operation_types) > 1:
            operation_type_names = ", ".join(
                operation_types.mapped('name')
            )

            raise UserError(
                _(
                    "Transfer yang dipilih memiliki Operation Type yang berbeda.\n\n"
                    "Operation Type yang ditemukan:\n%s\n\n"
                    "Silakan pilih transfer dengan Operation Type yang sama "
                    "untuk membuat batch."
                )
                % operation_type_names
            )

        operation_type = operation_types[:1]
        self.operation_type_name = operation_type.name

        if self.mode == 'new':
            company = pickings.company_id

            if len(company) > 1:
                raise UserError(
                    _("The selected pickings should belong to an unique company.")
                )

            batch = self.env['stock.picking.batch'].create({
                'user_id': self.user_id.id,
                'company_id': company.id,
                'picking_type_id': operation_type.id,
                'description': self.description,
            })
        else:
            batch = self.batch_id

        # Assign responsible users exactly as selected in the wizard.
        pickings.with_context(
            tijari_responsible_sync=True
        ).write({
            'batch_id': batch.id,

            'pick_responsible_ids': [
                (6, 0, self.pick_responsible_ids.ids)
            ],

            'pack_responsible_ids': [
                (6, 0, self.pack_responsible_ids.ids)
            ],

            'do_responsible_ids': [
                (6, 0, self.do_responsible_ids.ids)
            ],

            'putaway_responsible_ids': [
                (6, 0, self.putaway_responsible_ids.ids)
            ],
        })

        # Keep the batch form initialized from the real picking assignments.
        # Subsequent edits on the batch are propagated back to its pickings.
        batch.with_context(skip_batch_responsible_sync=True).write({
            field_name: [(6, 0, batch.picking_ids.mapped(picking_field).ids)]
            for field_name, picking_field
            in batch._RESPONSIBLE_BATCH_TO_PICKING_FIELDS.items()
        })

        if self.mode == 'new' and not self.is_create_draft:
            batch.action_confirm()

        return {
            'name': _('Batch Transfer'),
            'view_mode': 'form',
            'res_model': 'stock.picking.batch',
            'type': 'ir.actions.act_window',
            'res_id': batch.id,
        }


class StockAddToWaveInherit(models.TransientModel):
    _inherit = 'stock.add.to.wave'

    operation_type_name = fields.Char(string='Operation Type', readonly=True)
    pick_responsible_ids = fields.Many2many(
        'res.users', 'stock_add_to_wave_pick_resp_rel', 'wizard_id', 'user_id',
        string='Assigned Picker', domain=[('share', '=', False)],
    )
    pack_responsible_ids = fields.Many2many(
        'res.users', 'stock_add_to_wave_pack_resp_rel', 'wizard_id', 'user_id',
        string='Assigned Packer', domain=[('share', '=', False)],
    )
    do_responsible_ids = fields.Many2many(
        'res.users', 'stock_add_to_wave_do_resp_rel', 'wizard_id', 'user_id',
        string='Assigned Loader', domain=[('share', '=', False)],
    )
    putaway_responsible_ids = fields.Many2many(
        'res.users', 'stock_add_to_wave_putaway_resp_rel', 'wizard_id', 'user_id',
        string='Assigned Putaway', domain=[('share', '=', False)],
    )

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        active_ids = self.env.context.get('active_ids', [])
        if self.env.context.get('active_model') == 'stock.move.line':
            operation_types = self.env['stock.move.line'].browse(active_ids).mapped('picking_type_id')
        else:
            operation_types = self.env['stock.picking'].browse(active_ids).mapped('picking_type_id')
        vals['operation_type_name'] = operation_types.name if len(operation_types) == 1 else False
        return vals

    def attach_pickings(self):
        self.ensure_one()
        responsible_values = {
            'pick_responsible_ids': self.pick_responsible_ids.ids,
            'pack_responsible_ids': self.pack_responsible_ids.ids,
            'do_responsible_ids': self.do_responsible_ids.ids,
            'putaway_responsible_ids': self.putaway_responsible_ids.ids,
        }
        operation_responsible_ids = (
            self.putaway_responsible_ids
            if self.operation_type_name in ('Receipts', 'Receipt Internal', 'Storage', 'Store Finished Product')
            else self.pick_responsible_ids
        )
        return super(StockAddToWaveInherit, self.with_context(
            tijari_wave_responsible_values=responsible_values,
            active_owner_id=operation_responsible_ids[:1].id or self.user_id.id,
        )).attach_pickings()


class StockMoveLineWaveResponsible(models.Model):
    _inherit = 'stock.move.line'

    def _add_to_wave(self, wave=False, description=False):
        result = super()._add_to_wave(wave=wave, description=description)
        responsible_values = self.env.context.get('tijari_wave_responsible_values')
        if responsible_values:
            transferred_pickings = self.mapped('picking_id').filtered(
                lambda picking: picking.batch_id and picking.batch_id.is_wave
            )
            if transferred_pickings:
                transferred_pickings.with_context(tijari_responsible_sync=True).write({
                    field_name: [(6, 0, user_ids)]
                    for field_name, user_ids in responsible_values.items()
                })
        return result
