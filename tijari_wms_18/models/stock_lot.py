# -*- coding: utf-8 -*-
import base64
import io
import os

from PIL import Image
import qrcode

from odoo import fields, models


class StockLot(models.Model):
    _inherit = 'stock.lot'

    qr_code_image = fields.Binary(string='Sign')

    def action_print_lot_number(self):
        for lot in self:
            lot.create_qrcode_name()
        ids = ','.join(str(i) for i in self.ids)
        return {
            'type': 'ir.actions.act_url',
            'url': '/print/lot_number?ids=%s' % ids,
            'target': 'new',
        }

    def create_qrcode_name(self):
        for record in self:
            if record.name:
                qr = record.create_qrcode(record.name)
                record.write({
                    'qr_code_image': qr
                })

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
