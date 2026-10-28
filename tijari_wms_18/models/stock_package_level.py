from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

class WMSStockPackageLevel(models.Model):
    _inherit = 'stock.package_level'
    _description = 'WMSStockPackageLevel'

    status = fields.Selection([
        ('finished', 'Finished'),
        ('unfinished', 'Unfinished'),
    ], string='Status', default='unfinished')

    package_cat = fields.Selection([
            ('box', 'Box'),
            ('palet', 'Palet'),
        ], string='Package Category', related='package_id.package_cat')
    batch_adt_type = fields.Char(
        related='picking_id.adt_type', string='Tipe Transaksi', readonly=True,
    )
    batch_origin = fields.Char(
        related='picking_id.origin', string='No. Dokumen', readonly=True,
    )

    def action_mark_finished(self):
        for line in self.move_line_ids:
            line.so_confirmation_quantity = line.quantity

        self.write({'status': 'finished'})
        if self.env.context.get('wms_refresh_packed_sequence'):
            return {'type': 'ir.actions.client', 'tag': 'soft_reload'}
        return True

    def action_mark_unfinished(self):
        for line in self.move_line_ids:
            line.so_confirmation_quantity = 0.0
            
        self.write({'status': 'unfinished'})
        if self.env.context.get('wms_refresh_packed_sequence'):
            return {'type': 'ir.actions.client', 'tag': 'soft_reload'}
        return True

    # def action_show_package_details(self):
    #     self.ensure_one()
    #     view = self.env.ref('tijari_wms_18.tijari_stock_package_level_view_form_inherit')

    #     return {
    #         'name': _('Package Content'),
    #         'type': 'ir.actions.act_window',
    #         'view_mode': 'form',
    #         'res_model': 'stock.package_level',
    #         'views': [(view.id, 'form')],
    #         'target': 'new',
    #         'res_id': self.id,
    #     }

    def action_show_package_details(self):
        self.ensure_one()

        wizard = self.env["package.level.detail.wizard"].create({
            "package_level_id": self.id,
            "status": (
                "finished"
                if self.status == "finished"
                else "unfinished"
            ),
            "package_id": self.package_id.id,
            "picking_id": self.picking_id.id,
            "move_line_ids": [(6, 0, self.move_line_ids.ids)],
        })

        view = self.env.ref(
            "tijari_wms_18.tijari_package_level_detail_wizard_view_form"
        )

        return {
            "name": _("Package Content"),
            "type": "ir.actions.act_window",
            "res_model": "package.level.detail.wizard",
            "view_mode": "form",
            "views": [(view.id, "form")],
            "target": "new",
            "res_id": wizard.id,
        }
