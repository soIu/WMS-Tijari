import base64
import hashlib

from odoo import api, fields, models
from odoo.exceptions import ValidationError

import logging

_logger = logging.getLogger(__name__)

class StockLocation(models.Model):
    _inherit = 'stock.location'

    sequence = fields.Boolean(string='Sequence', default=False, help='Indicates if the location uses sequence for operations')
    unique_code = fields.Char(string='Unique Code', help='A unique code for the location')
    is_location_mapping = fields.Boolean(string="Parent Mapping Location")
    location_group_id = fields.Many2one(
        'stock.location',
        string='Location Group',
        index=True,
        ondelete='set null',
        domain="[('is_location_mapping', '=', True), ('id', '!=', id)]",
        help='Pilih lokasi grup agar lokasi ini dapat dipakai dalam mapping grup selain struktur parent-child.',
    )
    is_consignment = fields.Boolean(string="Consignment Location")
    is_document_only = fields.Boolean(string="Document Only Location")
    location_owner = fields.Char(string="Location Owner")

    @api.depends(
        'name',
        'complete_name',
        'warehouse_id',
        'warehouse_id.code',
    )
    @api.depends_context('show_location_short_name')
    def _compute_display_name(self):
        if not self.env.context.get('show_location_short_name'):
            return super()._compute_display_name()

        for location in self:
            location.display_name = (
                f"{location.warehouse_id.code}/{location.name}"
                if location.warehouse_id
                else location.complete_name
            )

    @api.model
    @api.readonly
    def name_search(self, name='', args=None, operator='ilike', limit=100):
        if not self.env.context.get('show_location_short_name'):
            return super().name_search(name=name, args=args, operator=operator, limit=limit)

        return super(StockLocation, self.with_context(show_location_short_name=False)).name_search(
            name=name,
            args=args,
            operator=operator,
            limit=limit,
        )

    def generate_unique_code(self):
        for location in self:
            if not location.unique_code:
                raw = f"{location.id}-{location.create_date}".encode("utf-8")
                hash_str = base64.b32encode(hashlib.sha256(raw).digest())[:8].decode()
                location.unique_code = f"LOC-{hash_str}"


class StockLocationAcumatica(models.Model):
    _name = 'stock.location.acumatica'
    _description = 'Location mapping untuk grouping location dari Acumatica ke location di Odoo (untuk kebutuhan import data dari Acumatica). stock.location.acumatica -> stock.location'

    @api.depends('name', 'code')
    @api.depends_context('show_location_code')
    def _compute_display_name(self):
        if not self.env.context.get('show_location_code'):
            return super()._compute_display_name()

        for record in self:
            record.display_name = f"{record.name or ''} [{record.code or ''}]"

    name = fields.Char(string='Location Name (Acumatica)', required=True)
    code = fields.Char(string='Location Code (Acumatica)', required=True)
    building = fields.Char(string='Building ID (Acumatica)')
    odoo_location_id = fields.Many2one('stock.location', string='Location Grouping')
    odoo_building_id = fields.Many2one('stock.warehouse', string='Building Grouping')
    odoo_building_location_id = fields.Many2one(
        'stock.location',
        string='Building Virtual Location',
        ondelete='set null',
    )
    active = fields.Boolean(string='Aktif', default=True)
    is_active = fields.Boolean(string='Is Active')
    is_error = fields.Boolean(string='Is Error')
    error_message = fields.Char(string='Error Message', readonly=True)
    is_ungropping = fields.Boolean(string='Tidak Digrouping', default=False)

    def action_generate_stock_location(self):
        """Create an internal child location using the Acumatica location code."""
        Location = self.env['stock.location']
        created_locations = Location

        for mapping in self:
            code = (mapping.code or '').strip()
            if not code:
                raise ValidationError('Location Code harus diisi sebelum membuat lokasi.')

            warehouse = mapping.odoo_building_id
            if not warehouse and mapping.building:
                warehouse = self.env['stock.warehouse'].search(
                    [('code', '=', mapping.building)], limit=1
                )
            if not warehouse or not warehouse.lot_stock_id:
                warehouse_name = mapping.building or (
                    mapping.odoo_building_id.display_name
                    if mapping.odoo_building_id else ''
                )
                raise ValidationError(
                    'Gudang %s tidak ditemukan atau belum memiliki lokasi stok.'
                    % warehouse_name
                )

            location = Location.search([
                ('name', '=', code),
                ('location_id', '=', warehouse.lot_stock_id.id),
            ], limit=1)
            if not location:
                location = Location.create({
                    'name': code,
                    'location_id': warehouse.lot_stock_id.id,
                    'usage': 'internal',
                    'is_location_mapping': True,
                })
            elif not location.is_location_mapping:
                location.is_location_mapping = True
            if mapping.odoo_location_id != location:
                mapping.odoo_location_id = location
            created_locations |= location

        if len(created_locations) == 1:
            return {
                'type': 'ir.actions.act_window',
                'name': 'Stock Location',
                'res_model': 'stock.location',
                'view_mode': 'form',
                'res_id': created_locations.id,
                'target': 'current',
            }
        return {
            'type': 'ir.actions.act_window',
            'name': 'Stock Locations',
            'res_model': 'stock.location',
            'view_mode': 'list,form',
            'domain': [('id', 'in', created_locations.ids)],
            'target': 'current',
        }

    @api.onchange('odoo_location_id')
    def _onchange_odoo_location_id(self):
        if self.is_error:
            raise ValidationError('Lokasi ini memiliki error, silakan perbaiki terlebih dahulu sebelum mengubah grouping.')
        if self.odoo_location_id:
            warehouse_id = self.env['stock.warehouse'].search([('code', '=', self.building)], limit=1)
            if warehouse_id:
                if warehouse_id != self.env.user.warehouse_id and warehouse_id not in self.env.user.warehouse_ids:
                    # Jika warehouse hasil pencarian tidak termasuk dalam warehouse yang diizinkan untuk user, maka tidak diset
                    raise ValidationError(f'Lokasi ini berada di building {self.building} yang tidak termasuk dalam warehouse yang diizinkan untuk user. Warehouse yang diizinkan: {", ".join(self.env.user.warehouse_ids.mapped("name"))}')
                else:
                    self.odoo_building_id = warehouse_id
            else:
                raise ValidationError(f'Lokasi ini berada di building {self.building} yang tidak ditemukan di Odoo. Pastikan building sudah terdaftar di Odoo sebelum mengatur grouping.')
