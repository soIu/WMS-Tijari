# -*- coding: utf-8 -*-
from odoo import http, _
from odoo.http import request
from odoo.exceptions import UserError
import logging
_logger = logging.getLogger(__name__)
import json
from odoo import http, models, fields, api, tools, service, modules, _
from odoo.http import request, Response, content_disposition, serialize_exception
import json
from datetime import datetime, time, timedelta
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.service.db import list_dbs
from num2words import num2words
from collections import defaultdict
from PIL import Image
from lxml import etree
from requests.exceptions import ConnectTimeout, ReadTimeout, ConnectionError, SSLError
import os
import psycopg2
import qrcode
import io
import base64

def create_blank_image():
    blank_image = Image.new(mode="RGB", size=(200, 200), color=(255, 255, 255))
    img_bytes = io.BytesIO()
    blank_image.save(img_bytes, format='PNG')
    qr = base64.b64encode(img_bytes.getvalue())
    return qr.decode('utf-8')

def format_tanggal_indo(dt):
    if not dt:
        return ''

    hari = {
        0: 'Senin',
        1: 'Selasa',
        2: 'Rabu',
        3: 'Kamis',
        4: 'Jumat',
        5: 'Sabtu',
        6: 'Minggu',
    }

    bulan = {
        1: 'Januari',
        2: 'Februari',
        3: 'Maret',
        4: 'April',
        5: 'Mei',
        6: 'Juni',
        7: 'Juli',
        8: 'Agustus',
        9: 'September',
        10: 'Oktober',
        11: 'November',
        12: 'Desember',
    }

    return (
        f"{hari[dt.weekday()]}, "
        f"{dt.day} "
        f"{bulan[dt.month]} "
        f"{dt.year}"
    )


def generate_stok_opname_pdf(stock):
    """Generate the same Berita Acara PDF used by the stock opname print route."""
    import requests

    stock = stock.sudo()
    blank_image = create_blank_image()

    domain = [
        ('location_id', 'in', stock.location_ids.ids),
        ('inventory_diff_quantity', '>', 0),
    ]

    if 'inventory_id' in stock.env['stock.quant']._fields:
        domain.append(('inventory_id', '=', stock.id))
    elif 'stock_inventory_id' in stock.env['stock.quant']._fields:
        domain.append(('stock_inventory_id', '=', stock.id))

    lines = stock.env['stock.quant'].sudo().search(domain)

    if not lines and hasattr(stock, 'quant_ids') and stock.quant_ids:
        lines = stock.quant_ids.filtered(lambda q: q.inventory_diff_quantity > 0)


    warehouse = stock.env['stock.warehouse'].sudo().search([
        ('lot_stock_id', 'in', stock.location_ids.ids)
    ], limit=1)

    data = {
        'warehouse_id': warehouse.name or '',
        'date': format_tanggal_indo(stock.date) if hasattr(stock, 'date') and stock.date else '',
        'lok': ", ".join(stock.location_ids.mapped('complete_name')) if stock.location_ids else '',
        'note': stock.notes or '',
        'created_by': stock.responsible_id.name or '',
        'ttd_created_by': (
            stock.responsible_id.partner_id.digital_signature.decode('utf-8')
            if stock.responsible_id.partner_id.digital_signature else blank_image
        ),
        'status_ba': dict(stock._fields['status_ba'].selection).get(stock.status_ba, ''),
        'detail': [],
    }
    for line in lines:
        diff = float(line.inventory_diff_quantity or 0)
        system_qty = float(line.quantity or 0)
        qty2 = float(line.inventory_quantity or 0)
        obj = {
            'name': line.product_id.display_name or '',
            'uom': line.product_uom_id.name or '',
            'system_qty': system_qty,
            'qty2': qty2,
            'diff': diff,
            'ket': ''
        }
        data['detail'].append(obj)

    data = json.dumps(data)
    usrnm = 'jasperadmin'
    psswd = 't1j4r1s0lut10n'
    execution = requests.post(
        'http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions',
        auth=('jasperadmin', 't1j4r1s0lut10n'),
        json={
            'reportUnitUri': '/Odoo/ADITYA/BERITA_ACARA_STOCK_OPNAME',
            'outputFormat': 'pdf',
            'parameters': {
                'reportParameter': [{'name': 'data', 'value': [data]}],
            },
        },
    )
    xml = etree.XML(execution.content)
    request_id = xml.find('.//requestId').text
    export_id = xml.find('.//export//id').text
    result = requests.get(
        'http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions/'
        f'{request_id}/exports/{export_id}/outputResource',
        cookies=execution.cookies,
        verify=False,
    )
    result.raise_for_status()
    return result.content

class WmsApiController(http.Controller):

    # ---------- Helper: API Key Auth ----------
    def _check_api_key(self):
        api_key = request.httprequest.headers.get('X-API-Key')
        if not api_key:
            return False, _("Missing X-API-Key header")

        expected = request.env['ir.config_parameter'].sudo().get_param('wms.api_key')
        if not expected:
            return False, _("WMS API Key is not configured in System Parameters")

        if api_key != expected:
            return False, _("Invalid API Key")

        return True, None

    def _json_error(self, message, status=400, details=None):
        _logger.warning("WMS API error: %s", message)
        body = {'status': 'error', 'message': message}
        if details:
            body['details'] = details
        return request.make_json_response(body, status=status)

    def _document_only_value(self, value):
        if isinstance(value, bool):
            return value
        if value in (False, None, ''):
            return False
        return str(value).strip().lower() in ('true', '1', 'yes', 'y', 'document only')

    def _get_payload_value(self, payload, *keys):
        for key in keys:
            if payload.get(key) not in (False, None, ''):
                return payload.get(key)
        return False

    def _resolve_picking_type_from_payload(self, PickingType, payload, operation_category):
        document_only = self._document_only_value(
            self._get_payload_value(payload, 'document_only', 'adt_is_document_only', 'adt_DocumentOnly')
        )
        warehouse_code = self._get_payload_value(
            payload, 'warehouse_code', 'adt_warehouse_code'
        )

        # Override eksplisit lewat sequence_code (misal 'RECEIPT', 'DDO', dst.)
        picking_type_code = payload.get('picking_type_code')
        if picking_type_code:
            code_domain = [('sequence_code', '=', picking_type_code), ('active', '=', True)]
            warehouse = PickingType._adt_find_warehouse(warehouse_code)
            if warehouse:
                code_domain.append(('warehouse_id', '=', warehouse.id))
            candidates = PickingType.search(code_domain)
            if len(candidates) > 1:
                raise UserError(_(
                    "Kode picking type %s ambigu untuk warehouse %s: %s."
                ) % (
                    picking_type_code,
                    warehouse.display_name if warehouse else '-',
                    ', '.join(pt.display_name for pt in candidates),
                ))
            if candidates:
                return candidates

        return PickingType.resolve_picking_type(
            target_picking_type_id=self._get_payload_value(payload, 'target_picking_type_id', 'picking_type_id'),
            target_operation_type=self._get_payload_value(payload, 'target_operation_type', 'target_operation_type_code'),
            api_acumatica=self._get_payload_value(payload, 'api_acumatica', 'api_type'),
            transaction_key=operation_category,
            document_only=document_only,
            warehouse_code=warehouse_code,
            operation_category=operation_category,
            required=True,
        )

    def _prepare_acumatica_header_vals(self, payload, transaction_type):
        vals = {
            'transaction_type': transaction_type,
        }
        field_mapping = {
            'adt_type': 'adt_type',
            'adt_external_ref': 'external_ref',
            'adt_warehouse_code': 'warehouse_code',
            'adt_external_id': 'external_id',
            'adt_description': 'description',
        }
        for field_name, payload_key in field_mapping.items():
            if payload.get(payload_key):
                vals[field_name] = payload.get(payload_key)

        document_only = self._document_only_value(
            self._get_payload_value(payload, 'document_only', 'adt_is_document_only', 'adt_DocumentOnly')
        )
        if (
            document_only
            or self._get_payload_value(payload, 'target_picking_type_id', 'picking_type_id')
            or self._get_payload_value(payload, 'target_operation_type', 'target_operation_type_code')
        ):
            vals['adt_is_document_only'] = True
        return vals

    # =========================================================
    #  POST /wms/api/receiving  -> create inbound / receiving
    # =========================================================
    @http.route('/wms/api/receiving', type='json', auth='public', methods=['POST'], csrf=False)
    def wms_receiving(self, **payload):
        ok, err = self._check_api_key()
        if not ok:
            return self._json_error(err, status=401)

        _logger.info("WMS Receiving payload: %s", payload)

        try:
            Picking = request.env['stock.picking'].sudo()
            Partner = request.env['res.partner'].sudo()
            Product = request.env['product.product'].sudo()
            Location = request.env['stock.location'].sudo()
            Uom = request.env['uom.uom'].sudo()
            Lot = request.env['stock.lot'].sudo()
            PickingType = request.env['stock.picking.type'].sudo()

            external_ref = payload.get('external_ref')
            vendor_code = payload.get('vendor_code')
            vendor_name = payload.get('vendor_name')
            warehouse_code = payload.get('warehouse_code')
            picking_type_code = payload.get('picking_type_code')  # optional
            scheduled_date = payload.get('scheduled_date')
            auto_validate = payload.get('auto_validate', False)
            lines = payload.get('lines')

            if not lines:
                raise UserError(_(f"No lines provided in payload {lines}"))

            # 1) Cari partner
            partner = False
            if vendor_code:
                partner = Partner.search([('ref', '=', vendor_code)], limit=1)
            if not partner and vendor_name:
                partner = Partner.search([('name', '=', vendor_name)], limit=1)
            if not partner:
                raise UserError(_("Vendor not found (vendor_code or vendor_name required)"))

            # 2) Cari picking type dari target staging/payload, fallback ke kategori receive.
            picking_type = self._resolve_picking_type_from_payload(
                PickingType,
                payload,
                'receive',
            )

            location_src_id = (
                picking_type.default_location_src_id
                or Location.search([('complete_name', '=', 'Partners/Vendors')], limit=1)
            )
            # 3) Create picking (draft)
            picking_vals = {
                'picking_type_id': picking_type.id,
                'partner_id': partner.id,
                'scheduled_date': scheduled_date,
                'origin': external_ref or payload.get('origin'),
                'location_id': location_src_id.id,
                'location_dest_id': picking_type.default_location_dest_id.id,
            }
            picking_vals.update(self._prepare_acumatica_header_vals(payload, 'inbound'))

            picking = Picking.create(picking_vals)

            # 4) Create moves
            for line in lines:
                product_code = line.get('product_code')   # default_code / internal reference
                product_barcode = line.get('product_barcode')
                qty = line.get('qty') or line.get('quantity')
                uom_name = line.get('uom')
                lot_name = line.get('lot')
                dest_location_code = line.get('location_dest_code')  # unique_code dari location

                if not product_code and not product_barcode:
                    raise UserError(_("Each line must have product_code or product_barcode"))

                # Cari product
                domain_prod = []
                if product_code:
                    domain_prod = [('default_code', '=', product_code)]
                elif product_barcode:
                    domain_prod = [('barcode', '=', product_barcode)]
                product = Product.search(domain_prod, limit=1)
                if not product:
                    raise UserError(_("Product not found for code/barcode: %s") % (product_code or product_barcode))

                # UoM
                uom = product.uom_id
                if uom_name:
                    uom = Uom.search([('name', '=', uom_name)], limit=1) or product.uom_id

                # Location dest pakai unique_code
                dest_location = picking.location_dest_id
                if dest_location_code:
                    dest_location = Location.search([('complete_name', '=', dest_location_code)], limit=1)
                    if not dest_location:
                        raise UserError(_("Destination location not found for unique_code: %s") % dest_location_code)

                move_vals = {
                    'name': product.display_name,
                    'product_id': product.id,
                    'product_uom_qty': qty,
                    'product_uom': uom.id,
                    'picking_id': picking.id,
                    'location_id': picking.location_id.id,
                    'location_dest_id': dest_location.id,
                }
                move = request.env['stock.move'].sudo().create(move_vals)

                # Lot + immediate done (optional)
                if lot_name:
                    lot = Lot.search([
                        ('name', '=', lot_name),
                        ('product_id', '=', product.id),
                    ], limit=1)
                    if not lot:
                        lot = Lot.create({
                            'name': lot_name,
                            'product_id': product.id,
                        })

                    move_line_vals = {
                        'move_id': move.id,
                        'picking_id': picking.id,
                        'product_id': product.id,
                        'product_uom_id': uom.id,
                        'qty_done': qty if auto_validate else 0.0,
                        'location_id': picking.location_id.id,
                        'location_dest_id': dest_location.id,
                        'lot_id': lot.id,
                    }
                    request.env['stock.move.line'].sudo().create(move_line_vals)

            # 5) Auto-validate jika diminta (WMS langsung terima)
            if auto_validate:
                picking.action_confirm()
                picking.action_assign()
                picking.button_validate()

            return {
                'status': 'success',
                'picking_id': picking.id,
                'picking_name': picking.name,
                'operation_category': 'receive',
            }

        except UserError as e:
            return self._json_error(str(e), status=400)
        except Exception as e:
            _logger.exception("Unexpected error in /wms/api/receiving")
            return self._json_error(_("Unexpected server error: %s") % str(e), status=500)

    # =========================================================
    #  POST /wms/api/delivery  -> create outbound / DO
    # =========================================================
    @http.route('/wms/api/delivery', type='json', auth='public', methods=['POST'], csrf=False)
    def wms_delivery(self, **kwargs):
        ok, err = self._check_api_key()
        if not ok:
            return self._json_error(err, status=401)

        payload = kwargs
        _logger.info("WMS Delivery payload: %s", payload)

        try:
            Picking = request.env['stock.picking'].sudo()
            Partner = request.env['res.partner'].sudo()
            Product = request.env['product.product'].sudo()
            Location = request.env['stock.location'].sudo()
            Uom = request.env['uom.uom'].sudo()
            Lot = request.env['stock.lot'].sudo()
            PickingType = request.env['stock.picking.type'].sudo()

            external_ref = payload.get('external_ref')
            customer_code = payload.get('customer_code')
            customer_name = payload.get('customer_name')
            warehouse_code = payload.get('warehouse_code')
            picking_type_code = payload.get('picking_type_code')  # optional
            scheduled_date = payload.get('scheduled_date')
            auto_validate = payload.get('auto_validate', False)
            lines = payload.get('lines') or []

            if not lines:
                raise UserError(_("No lines provided in payload"))

            # 1) Partner
            partner = False
            if customer_code:
                partner = Partner.search([('ref', '=', customer_code)], limit=1)
            if not partner and customer_name:
                partner = Partner.search([('name', '=', customer_name)], limit=1)
            if not partner:
                raise UserError(_("Customer not found (customer_code or customer_name required)"))

            # 2) Cari picking type dari target staging/payload, fallback ke kategori delivery.
            picking_type = self._resolve_picking_type_from_payload(
                PickingType,
                payload,
                'delivery',
            )

            location_dest_id = (
                picking_type.default_location_dest_id
                or Location.search([('complete_name', '=', 'Partners/Customers')], limit=1)
            )
            # 3) Create picking
            picking_vals = {
                'picking_type_id': picking_type.id,
                'partner_id': partner.id,
                'scheduled_date': scheduled_date,
                'origin': external_ref or payload.get('origin'),
                'location_id': picking_type.default_location_src_id.id,
                'location_dest_id': location_dest_id.id
            }
            picking_vals.update(self._prepare_acumatica_header_vals(payload, 'outbound'))

            picking = Picking.create(picking_vals)

            # 4) Create moves
            for line in lines:
                product_code = line.get('product_code')
                product_barcode = line.get('product_barcode')
                qty = line.get('qty') or line.get('quantity')
                uom_name = line.get('uom')
                lot_name = line.get('lot')
                src_location_code = line.get('location_src_code')  # unique_code

                if not product_code and not product_barcode:
                    raise UserError(_("Each line must have product_code or product_barcode"))

                # Product
                domain_prod = []
                if product_code:
                    domain_prod = [('default_code', '=', product_code)]
                elif product_barcode:
                    domain_prod = [('barcode', '=', product_barcode)]
                product = Product.search(domain_prod, limit=1)
                if not product:
                    raise UserError(_("Product not found for code/barcode: %s") % (product_code or product_barcode))

                # UoM
                uom = product.uom_id
                if uom_name:
                    uom = Uom.search([('name', '=', uom_name)], limit=1) or product.uom_id

                # Source location pakai unique_code
                src_location = picking.location_id
                if src_location_code:
                    src_location = Location.search([('unique_code', '=', src_location_code)], limit=1)
                    if not src_location:
                        raise UserError(_("Source location not found for unique_code: %s") % src_location_code)

                move_vals = {
                    'name': product.display_name,
                    'product_id': product.id,
                    'product_uom_qty': qty,
                    'product_uom': uom.id,
                    'picking_id': picking.id,
                    'location_id': src_location.id,
                    'location_dest_id': picking.location_dest_id.id,
                }
                move = request.env['stock.move'].sudo().create(move_vals)

                # Lot + immediate done (optional)
                if lot_name:
                    lot = Lot.search([
                        ('name', '=', lot_name),
                        ('product_id', '=', product.id),
                    ], limit=1)
                    if not lot:
                        lot = Lot.create({
                            'name': lot_name,
                            'product_id': product.id,
                        })

                    move_line_vals = {
                        'move_id': move.id,
                        'picking_id': picking.id,
                        'product_id': product.id,
                        'product_uom_id': uom.id,
                        'qty_done': qty if auto_validate else 0.0,
                        'location_id': src_location.id,
                        'location_dest_id': picking.location_dest_id.id,
                        'lot_id': lot.id,
                    }
                    request.env['stock.move.line'].sudo().create(move_line_vals)

            # 5) Auto validate (ship langsung)
            if auto_validate:
                picking.action_confirm()
                picking.action_assign()
                picking.button_validate()

            return {
                'status': 'success',
                'picking_id': picking.id,
                'picking_name': picking.name,
                'operation_category': 'delivery',
            }

        except UserError as e:
            return self._json_error(str(e), status=400)
        except Exception as e:
            _logger.exception("Unexpected error in /wms/api/delivery")
            return self._json_error(_("Unexpected server error: %s") % str(e), status=500)


    @http.route('/print/warehouse_receipt', type='http', auth='user')
    def PrintWarehouseReceipt(self, id, **kw):
        blank_image = create_blank_image()
        receipt = request.env['stock.picking'].sudo().browse(int(id))

        data = {
            'type': receipt.adt_type or '',
            'tgl': format_tanggal_indo(receipt.scheduled_date),
            'date': format_tanggal_indo(receipt.create_date.date()) if receipt.create_date else '',
            'no': receipt.origin or '',
            'vendor': receipt.partner_id.name or '',
            'warehouse': receipt.picking_type_id.name or '',
            'warehouse_id': receipt.picking_type_id.warehouse_id.name or '',
            'receipt_date': receipt.scheduled_date.strftime('%Y-%m-%d %H:%M:%S') if receipt.scheduled_date else '',
            'penerima': receipt.location_id.complete_name or '',
            'note': receipt.note_ba or '',
            'user': receipt.user_id.name or '',
            'ttd_user':  (receipt.user_id.digital_signature.decode('utf-8') if receipt.user_id.digital_signature else blank_image),
            'created_by': receipt.created_by_id.name or '',
            'ttd_created_by': (receipt.created_by_id.partner_id.digital_signature.decode('utf-8') if receipt.created_by_id.partner_id.digital_signature else blank_image),
            'approved_by': receipt.approved_by_id.name or '',
            'ttd_approved_by': (receipt.approved_by_id.partner_id.digital_signature.decode('utf-8') if receipt.approved_by_id.partner_id.digital_signature else blank_image),
            'status_ba': dict(receipt._fields['status_ba'].selection).get(receipt.status_ba, ''),
            'status_ba_color': 'green' if receipt.status_ba == 'conform' else 'red',
            'detail': []
        }
        for line in receipt.move_ids_without_package:
            difference_qty = float(line.difference_quantity or 0)
            if difference_qty.is_integer():
                difference_text = str(int(difference_qty))
            else:
                difference_text = str(difference_qty)

            obj = {
                'name': line.product_id.display_name or '',
                'uom': line.product_uom.name or '',
                'difference_quantity': (
                    "<style forecolor='red'>%s</style>" % difference_text
                    if difference_qty > 0
                    else difference_text
                ),
                'demand': float(line.product_uom_qty or 0),
                'demand1': float(line.confirmation_quantity or 0),
                'qty': float(line.confirmation_quantity or 0),
                'qty2': float(line.product_uom_qty or 0),
                'ket': ''
            }
            data['detail'].append(obj)

        data = json.dumps(data)

        usrnm = 'jasperadmin'
        psswd = 't1j4r1s0lut10n'
        import requests
        execution = requests.post('http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions',
            auth=(usrnm, psswd),
            json={
                'reportUnitUri': '/Odoo/ADITYA/Penerimaan_Barang',
                'outputFormat': 'pdf',
                'parameters': {
                    'reportParameter': [{
                        'name': 'data',
                        'value': [data]
                    }]
                }
            })
        from lxml import etree
        xml = etree.XML(execution.content)

        try:
            request_id = xml.find('.//requestId').text
            export_id = xml.find('.//export//id').text
            result = requests.get('http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions/' + request_id + '/exports/' + export_id + '/outputResource', cookies=execution.cookies, verify=False)
            filename = "warehouse_receipt.pdf"
            return request.make_response(result.content, [('Content-Type', result.headers['content-type']), ('Content-Disposition', 'inline; filename=%s;' % filename)])
        except:
            import traceback
            msg = traceback.format_exc()
            request_content = execution.content
            resp = {'msg': msg, 'request_content': str(request_content)}
            resp = json.dumps(resp)
            return request.make_response(resp, [('Content-Type', 'application/json')])

    @http.route('/print/lot_number', type='http', auth='user')
    def PrintLotNumber(self, ids=None, id=None, **kw):
        blank_image = create_blank_image()
        raw = ids or id or ''
        lot_ids = [int(x) for x in str(raw).split(',') if x]
        lots = request.env['stock.lot'].sudo().browse(lot_ids).exists()

        data = {'detail': []}

        for move_detail in lots:
            obj = {
                'lot_name': move_detail.name or '',
                'product': move_detail.product_id.name or '',
                'product_code': move_detail.product_id.default_code or '',
                'ref': move_detail.ref or '',
                'date': format_tanggal_indo(move_detail.create_date) if move_detail.create_date else '',
                'qrcode': (
                    move_detail.qr_code_image.decode('utf-8')
                    if move_detail.qr_code_image else blank_image
                ),
            }
            data['detail'].append(obj)

        data = json.dumps(data)
        usrnm = 'jasperadmin'
        psswd = 't1j4r1s0lut10n'
        import requests
        execution = requests.post('http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions',
            auth=(usrnm, psswd),
            json={
                'reportUnitUri': '/Odoo/ADITYA/Lot_line_list',
                'outputFormat': 'pdf',
                'parameters': {
                    'reportParameter': [{
                        'name': 'data',
                        'value': [data]
                    }]
                }
            })
        from lxml import etree
        xml = etree.XML(execution.content)

        try:
            request_id = xml.find('.//requestId').text
            export_id = xml.find('.//export//id').text
            result = requests.get('http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions/' + request_id + '/exports/' + export_id + '/outputResource', cookies=execution.cookies, verify=False)
            filename = "warehouse_receipt.pdf"
            return request.make_response(result.content, [('Content-Type', result.headers['content-type']), ('Content-Disposition', 'inline; filename=%s;' % filename)])
        except:
            import traceback
            msg = traceback.format_exc()
            request_content = execution.content
            resp = {'msg': msg, 'request_content': str(request_content)}
            resp = json.dumps(resp)
            return request.make_response(resp, [('Content-Type', 'application/json')])

    @http.route('/print/lot_number_move_line', type='http', auth='user')
    def PrintLotNumberLine(self, ids=None, **kw):
        blank_image = create_blank_image()
        if not ids:
            return request.make_response(
                json.dumps({'error': 'ids tidak ditemukan'}),
                [('Content-Type', 'application/json')]
            )

        ids = [int(x) for x in ids.split(',') if x]

        # lots = request.env['stock.picking'].sudo().browse(ids)
        move = request.env['stock.move'].sudo().browse(ids)

        data = {
            'lot_name': move.lot_ids.name or '',
            'product': move.product_id.name or '',
            'product_code': move.product_id.default_code or '',
            'ref': "",
            'docs': move.picking_id.origin or '',
            'date': format_tanggal_indo(move.create_date) if move.create_date else '',
            'qrcode': (move.qr_code_image.decode('utf-8') if move.qr_code_image else blank_image),
            # 'detail': []
        }

        # for move in lots.move_ids_without_package:
        #     obj = {
        #         'lot_name': move.lot_ids.name or '',
        #         'product': move.product_id.name or '',
        #         'product_code': move.product_id.default_code or '',
        #         'ref': "",
        #         'docs': lots.origin or '',
        #         'date': format_tanggal_indo(lots.scheduled_date),
        #         'qrcode': (
        #             lots.qr_code_image.decode('utf-8')
        #             if lots.qr_code_image else blank_image
        #         ),
        #     }
        #     data['detail'].append(obj)

        data = json.dumps(data)
        usrnm = 'jasperadmin'
        psswd = 't1j4r1s0lut10n'
        import requests
        execution = requests.post('http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions',
            auth=(usrnm, psswd),
            json={
                'reportUnitUri': '/Odoo/ADITYA/Lot_line',
                'outputFormat': 'pdf',
                'parameters': {
                    'reportParameter': [{
                        'name': 'data',
                        'value': [data]
                    }]
                }
            })
        from lxml import etree
        xml = etree.XML(execution.content)

        try:
            request_id = xml.find('.//requestId').text
            export_id = xml.find('.//export//id').text
            result = requests.get('http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions/' + request_id + '/exports/' + export_id + '/outputResource', cookies=execution.cookies, verify=False)
            filename = "warehouse_receipt.pdf"
            return request.make_response(result.content, [('Content-Type', result.headers['content-type']), ('Content-Disposition', 'inline; filename=%s;' % filename)])
        except:
            import traceback
            msg = traceback.format_exc()
            request_content = execution.content
            resp = {'msg': msg, 'request_content': str(request_content)}
            resp = json.dumps(resp)
            return request.make_response(resp, [('Content-Type', 'application/json')])

    @http.route('/print/packing_list', type='http', auth='user')
    def PrintPackingList(self, id=None, **kw):
        if not id:
            return request.make_response(
                json.dumps({'error': 'id tidak ditemukan'}),
                [('Content-Type', 'application/json')]
            )
        blank_image = create_blank_image()
        stok = request.env['stock.quant.package'].sudo().browse(int(id))
        package_level = request.env['stock.package_level'].sudo().search([
            ('package_id', '=', stok.id)
        ], limit=1)
        picking = package_level.picking_id

        origin = ""
        if picking and picking.origin:
            origin = picking.origin.split(",")[0].strip()

        data = {
            'partner_id': picking.partner_id.name if picking else "",
            'warehouse': stok.location_id.name or "",
            'name': stok.name or "",
            'date': format_tanggal_indo(stok.pack_date),
            'origin': origin,
            'note': stok.note or "",
            'ket': stok.warehouse_note or "",
            'warehouse_name': picking.picking_type_id.name or '',
            'warehouse_id': picking.picking_type_id.warehouse_id.name or '',
            'scheduled_date': format_tanggal_indo(picking.scheduled_date) if picking else "",
            'qrcode': (stok.qr_code_image.decode('utf-8') if stok.qr_code_image else blank_image),
            'detail': [],
        }

        for quant in stok.quant_ids:
            data['detail'].append({
                'product': quant.product_id.name or "",
                'product_code': quant.product_id.default_code or "",
                'qty': quant.quantity,
                'uom': quant.product_id.uom_id.name or "",
            })

        data = json.dumps(data)
        usrnm = 'jasperadmin'
        psswd = 't1j4r1s0lut10n'
        import requests
        execution = requests.post('http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions',
            auth=(usrnm, psswd),
            json={
                'reportUnitUri': '/Odoo/ADITYA/PACKING_LIST',
                'outputFormat': 'pdf',
                'parameters': {
                    'reportParameter': [{
                        'name': 'data',
                        'value': [data]
                    }]
                }
            })
        from lxml import etree
        xml = etree.XML(execution.content)

        try:
            request_id = xml.find('.//requestId').text
            export_id = xml.find('.//export//id').text
            result = requests.get('http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions/' + request_id + '/exports/' + export_id + '/outputResource', cookies=execution.cookies, verify=False)
            filename = "PACKING_LIST.pdf"
            return request.make_response(result.content, [('Content-Type', result.headers['content-type']), ('Content-Disposition', 'inline; filename=%s;' % filename)])
        except:
            import traceback
            msg = traceback.format_exc()
            request_content = execution.content
            resp = {'msg': msg, 'request_content': str(request_content)}
            resp = json.dumps(resp)
            return request.make_response(resp, [('Content-Type', 'application/json')])

    @http.route('/print/packing_package', type='http', auth='user')
    def PrintPackingPackage(self, id=None, **kw):
        if not id:
            return request.make_response(
                json.dumps({'error': 'id tidak ditemukan'}),
                [('Content-Type', 'application/json')]
            )
        blank_image = create_blank_image()
        stok = request.env['stock.quant.package'].sudo().browse(int(id))
        

        data = {
            'name': stok.name or "",
            'package_type': stok.package_cat or "",
            'date': format_tanggal_indo(stok.pack_date),
            'qrcode': (stok.qr_code_image.decode('utf-8') if stok.qr_code_image else blank_image),
        }

    

        data = json.dumps(data)
        usrnm = 'jasperadmin'
        psswd = 't1j4r1s0lut10n'
        import requests
        execution = requests.post('http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions',
            auth=(usrnm, psswd),
            json={
                'reportUnitUri': '/Odoo/ADITYA/Lot_package',
                'outputFormat': 'pdf',
                'parameters': {
                    'reportParameter': [{
                        'name': 'data',
                        'value': [data]
                    }]
                }
            })
        from lxml import etree
        xml = etree.XML(execution.content)

        try:
            request_id = xml.find('.//requestId').text
            export_id = xml.find('.//export//id').text
            result = requests.get('http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions/' + request_id + '/exports/' + export_id + '/outputResource', cookies=execution.cookies, verify=False)
            filename = "PACKING_LIST.pdf"
            return request.make_response(result.content, [('Content-Type', result.headers['content-type']), ('Content-Disposition', 'inline; filename=%s;' % filename)])
        except:
            import traceback
            msg = traceback.format_exc()
            request_content = execution.content
            resp = {'msg': msg, 'request_content': str(request_content)}
            resp = json.dumps(resp)
            return request.make_response(resp, [('Content-Type', 'application/json')])

    @http.route('/print/stok_opname', type='http', auth='user')
    def PrintStokOpname(self, id, **kw):
        blank_image = create_blank_image()
        stock = request.env['stock.inventory'].sudo().browse(int(id))
        lines = request.env['stock.quant'].sudo().search([
            ('id', 'in', stock.stock_quant_ids.ids),
            ('current_inventory_id', '=', stock.id),
        ])
        
        warehouse = request.env['stock.warehouse'].sudo().search([
            ('lot_stock_id', 'in', stock.location_ids.ids)
        ], limit=1)

        data = {
            'warehouse_id': ", ".join(stock.warehouse_ids.mapped('name')) if stock.warehouse_ids else '',
            'date': format_tanggal_indo(stock.date),
            'lok': ", ".join(stock.location_ids.mapped('complete_name')) if stock.location_ids else '',
            'note': stock.notes or '',
            'created_by': stock.responsible_id.name or '',
            'ttd_created_by': (stock.responsible_id.partner_id.digital_signature.decode('utf-8') if stock.responsible_id.partner_id.digital_signature else blank_image),
            'status_ba': dict(stock._fields['status_ba'].selection).get(stock.status_ba, ''),

            'detail': []
        }

        grouped_lines = {}
        for line in lines:
            product = line.product_id
            if not product:
                continue
                
            if product.id not in grouped_lines:
                grouped_lines[product.id] = {
                    'name': product.display_name or '',
                    'uom': line.product_uom_id.name or '',
                    'system_qty': 0.0,
                    'qty2': 0.0,
                    'diff': 0.0,
                    'ket': ''
                }
            
            grouped_lines[product.id]['system_qty'] += float(line.quantity or 0)
            grouped_lines[product.id]['qty2'] += float(line.inventory_quantity or 0)
            grouped_lines[product.id]['diff'] += float(line.inventory_diff_quantity or 0)

        for prod_id, obj in grouped_lines.items():
            diff = obj['diff']
            if diff < 0:
                obj['diff_color'] = "#FF0000"
            elif diff > 0:
                obj['diff_color'] = "#008000" 
            else:
                obj['diff_color'] = "#000000" 
                
            data['detail'].append(obj)
        data = json.dumps(data)

        usrnm = 'jasperadmin'
        psswd = 't1j4r1s0lut10n'
        import requests
        execution = requests.post('http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions',
            auth=(usrnm, psswd),
            json={
                'reportUnitUri': '/Odoo/ADITYA/BERITA_ACARA_STOCK_OPNAME',
                'outputFormat': 'pdf',
                'parameters': {
                    'reportParameter': [{
                        'name': 'data',
                        'value': [data]
                    }]
                }
            })
        from lxml import etree
        xml = etree.XML(execution.content)

        try:
            request_id = xml.find('.//requestId').text
            export_id = xml.find('.//export//id').text
            result = requests.get('http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions/' + request_id + '/exports/' + export_id + '/outputResource', cookies=execution.cookies, verify=False)
            filename = "Stok Opname.pdf"
            return request.make_response(result.content, [('Content-Type', result.headers['content-type']), ('Content-Disposition', 'inline; filename=%s;' % filename)])
        except:
            import traceback
            msg = traceback.format_exc()
            request_content = execution.content
            resp = {'msg': msg, 'request_content': str(request_content)}
            resp = json.dumps(resp)
            return request.make_response(resp, [('Content-Type', 'application/json')])

    @http.route('/print/stok_scrab', type='http', auth='user')
    def PrintStokScrab(self, id, **kw):
        blank_image = create_blank_image()
        stock = request.env['stock.scrap'].sudo().browse(int(id))
        
        if stock and stock.location_id:
            location_id = stock.location_id.id
            warehouse = request.env['stock.warehouse'].sudo().search([
                ('lot_stock_id', '=', location_id)
            ], limit=1)
        else:
            warehouse = request.env['stock.warehouse'].sudo() 

        data = {
            'name': stock.name or '', 
            'warehouse_id': warehouse.name or '',
            'source': ( stock.location_id.complete_name or ''),
            'scrap': ( stock.scrap_location_id.complete_name or ''),
            'reason': ( stock.scrap_reason_tag_ids.name or ''),
            'date': format_tanggal_indo(datetime.now()),
            'note': stock.note or '',
            'created_by': request.env.user.name or '',
            'ttd_created_by': (request.env.user.partner_id.digital_signature.decode('utf-8') if request.env.user.partner_id.digital_signature else blank_image),
            'detail': [],
        
        }
        for line in stock.scrap_line_ids:
            obj = {
                'name': line.product_id.display_name or '',
                'uom': line.product_uom_id.name or '',
                'qty2': line.scrapped_qty or '',
                'diff': '',
            }
            data['detail'].append(obj)
        data = json.dumps(data)

        usrnm = 'jasperadmin'
        psswd = 't1j4r1s0lut10n'
        import requests
        execution = requests.post('http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions',
            auth=(usrnm, psswd),
            json={
                'reportUnitUri': '/Odoo/ADITYA/BERITA_ACARA_SCRAP_ORDER',
                'outputFormat': 'pdf',
                'parameters': {
                    'reportParameter': [{
                        'name': 'data',
                        'value': [data]
                    }]
                }
            })
        from lxml import etree
        xml = etree.XML(execution.content)

        try:
            request_id = xml.find('.//requestId').text
            export_id = xml.find('.//export//id').text
            result = requests.get('http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions/' + request_id + '/exports/' + export_id + '/outputResource', cookies=execution.cookies, verify=False)
            filename = "stok_scrab.pdf"
            return request.make_response(result.content, [('Content-Type', result.headers['content-type']), ('Content-Disposition', 'inline; filename=%s;' % filename)])
        except:
            import traceback
            msg = traceback.format_exc()
            request_content = execution.content
            resp = {'msg': msg, 'request_content': str(request_content)}
            resp = json.dumps(resp)
            return request.make_response(resp, [('Content-Type', 'application/json')])

    @http.route('/print/hasil_manufacturing', type='http', auth='user')
    def PrintHasilManufacturing(self, id, **kw):
        blank_image = create_blank_image()
        mrp = request.env['mrp.production'].sudo().browse(int(id))
        
        data = {
            'reference': mrp.origin or '', 
            'date': format_tanggal_indo(mrp.date_start) if mrp.date_start else '',
            'warehouse_id': mrp.warehouse_id.name or '', 
            # 'lok':  '',
            # 'lok':  mrp.warehouse_id.source_location or'',
            'lok':  mrp.location_src_id.name or'',
            # 'lok':  mrp.location_dest_id.name or'',
            'user': mrp.user_id.name or '',
            'note': mrp.catatan or '',
            'name': mrp.product_id.name or '',
            'default_code': mrp.product_id.default_code or '',
            'lot': mrp.lot_producing_id.name or '',
            'demand': mrp.qty_producing or 0.0,
            'qty': mrp.product_qty or 0.0,
            'uom': mrp.product_uom_id.name or 0.0,
            'created_by': mrp.user_id.name or '',
            'ttd_created_by': (mrp.user_id.partner_id.digital_signature.decode('utf-8') if mrp.user_id.partner_id.digital_signature else blank_image),
        }

        list_dikonsumsi = []
        list_tidak_digunakan = []
        detail_diff = []

        for line in mrp.move_raw_ids:
            lot_names = ", ".join(line.lot_ids.mapped('name')) if line.lot_ids else ''
            # diff_qty = getattr(line, 'difference_quantity', 0.0) or 0.0

            list_tidak_digunakan.append({
                'name': line.product_id.name or '',
                'lot': lot_names,
                'qty': line.difference_quantity,
                'qty2': line.quantity,
                'uom': line.product_uom.name or '',
                'lok': line.location_id.name or '',
            })

            list_dikonsumsi.append({
                'name': line.product_id.name or '',
                'lot': lot_names,
                'demand': line.product_uom_qty or 0.0,
                'qty': line.mo_confirmation_quantity or 0.0,
                'qty2': line.quantity or 0.0,
                'uom': line.product_uom.name or '',
            })

        data['detail_konsumsi'] = list_dikonsumsi
        data['detail_sisa'] = list_tidak_digunakan

        for diff in mrp.move_raw_ids:
            if diff.difference_note:
                obj = {
                    'name': diff.product_id.default_code or '',
                    'uom': diff.difference_note,
                    'ket': ''
                }
                detail_diff.append(obj)
        data['detail_diff'] = detail_diff

        data = json.dumps(data)

        usrnm = 'jasperadmin'
        psswd = 't1j4r1s0lut10n'
        import requests
        execution = requests.post('http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions',
            auth=(usrnm, psswd),
            json={
                'reportUnitUri': '/Odoo/ADITYA/BERITA_ACARA_HASIL_MANUFACTURING',
                'outputFormat': 'pdf',
                'parameters': {
                    'reportParameter': [{
                        'name': 'data',
                        'value': [data]
                    }]
                }
            })
        from lxml import etree
        xml = etree.XML(execution.content)

        try:
            request_id = xml.find('.//requestId').text
            export_id = xml.find('.//export//id').text
            result = requests.get('http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions/' + request_id + '/exports/' + export_id + '/outputResource', cookies=execution.cookies, verify=False)
            filename = "berita_acara_hasil_manufacturing.pdf"
            return request.make_response(result.content, [('Content-Type', result.headers['content-type']), ('Content-Disposition', 'inline; filename=%s;' % filename)])
        except Exception:
            import traceback
            msg = traceback.format_exc()
            request_content = execution.content
            resp = {'msg': msg, 'request_content': str(request_content)}
            resp = json.dumps(resp)
            return request.make_response(resp, [('Content-Type', 'application/json')])

    @http.route('/print/warehouse_pick', type='http', auth='user')
    def PrintWarehousePick(self, id, **kw):
        blank_image = create_blank_image()
        receipt = request.env['stock.picking'].sudo().browse(int(id))

        data = {
            'type': receipt.adt_type or '',
            'tgl': format_tanggal_indo(receipt.scheduled_date),
            'date': format_tanggal_indo(receipt.create_date.date()) if receipt.create_date else '',
            'no': receipt.origin or '',
            'vendor': receipt.partner_id.name or '',
            'warehouse': receipt.picking_type_id.name or '',
            'warehouse_id': receipt.picking_type_id.warehouse_id.name or '',
            'receipt_date': receipt.scheduled_date.strftime('%Y-%m-%d %H:%M:%S') if receipt.scheduled_date else '',
            'penerima': receipt.location_id.complete_name or '',
            'note': receipt.note_ba or '',
            'user': receipt.user_id.name or '',
            'ttd_user':  (receipt.user_id.digital_signature.decode('utf-8') if receipt.user_id.digital_signature else blank_image),
            'created_by': receipt.created_by_id.name or '',
            'ttd_created_by': (receipt.created_by_id.partner_id.digital_signature.decode('utf-8') if receipt.created_by_id.partner_id.digital_signature else blank_image),
            'approved_by': receipt.approved_by_id.name or '',
            'ttd_approved_by': (receipt.approved_by_id.partner_id.digital_signature.decode('utf-8') if receipt.approved_by_id.partner_id.digital_signature else blank_image),
            'status_ba': dict(receipt._fields['status_ba'].selection).get(receipt.status_ba, ''),
            'status_ba_color': 'green' if receipt.status_ba == 'conform' else 'red',
            'detail': []
        }
        for line in receipt.move_ids_without_package:
            difference_qty = float(line.difference_quantity or 0)
            if difference_qty.is_integer():
                difference_text = str(int(difference_qty))
            else:
                difference_text = str(difference_qty)

            obj = {
                'name': line.product_id.display_name or '',
                'uom': line.product_uom.name or '',
                'difference_quantity': (
                    "<style forecolor='red'>%s</style>" % difference_text
                    if difference_qty > 0
                    else difference_text
                ),
                'demand': float(line.product_uom_qty or 0),
                'demand1': float(line.confirmation_quantity or 0),
                'qty': float(line.confirmation_quantity or 0),
                'qty2': float(line.product_uom_qty or 0),
                'ket': ''
            }
            data['detail'].append(obj)

        data = json.dumps(data)

        usrnm = 'jasperadmin'
        psswd = 't1j4r1s0lut10n'
        import requests
        execution = requests.post('http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions',
            auth=(usrnm, psswd),
            json={
                'reportUnitUri': '/Odoo/ADITYA/warehouse_pick',
                'outputFormat': 'pdf',
                'parameters': {
                    'reportParameter': [{
                        'name': 'data',
                        'value': [data]
                    }]
                }
            })
        from lxml import etree
        xml = etree.XML(execution.content)

        try:
            request_id = xml.find('.//requestId').text
            export_id = xml.find('.//export//id').text
            result = requests.get('http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions/' + request_id + '/exports/' + export_id + '/outputResource', cookies=execution.cookies, verify=False)
            filename = "warehouse_receipt.pdf"
            return request.make_response(result.content, [('Content-Type', result.headers['content-type']), ('Content-Disposition', 'inline; filename=%s;' % filename)])
        except:
            import traceback
            msg = traceback.format_exc()
            request_content = execution.content
            resp = {'msg': msg, 'request_content': str(request_content)}
            resp = json.dumps(resp)
            return request.make_response(resp, [('Content-Type', 'application/json')])

    @http.route('/print/warehouse_pack', type='http', auth='user')
    def PrintWarehousePack(self, id, **kw):
        blank_image = create_blank_image()
        receipt = request.env['stock.picking'].sudo().browse(int(id))

        data = {
            'type': receipt.adt_type or '',
            'tgl': format_tanggal_indo(receipt.scheduled_date),
            'date': format_tanggal_indo(receipt.create_date.date()) if receipt.create_date else '',
            'no': receipt.origin or '',
            'vendor': receipt.partner_id.name or '',
            'warehouse': receipt.picking_type_id.name or '',
            'warehouse_id': receipt.picking_type_id.warehouse_id.name or '',
            'receipt_date': receipt.scheduled_date.strftime('%Y-%m-%d %H:%M:%S') if receipt.scheduled_date else '',
            'penerima': receipt.location_id.complete_name or '',
            'note': receipt.note_ba or '',
            'user': receipt.user_id.name or '',
            'ttd_user':  (receipt.user_id.digital_signature.decode('utf-8') if receipt.user_id.digital_signature else blank_image),
            'created_by': receipt.created_by_id.name or '',
            'ttd_created_by': (receipt.created_by_id.partner_id.digital_signature.decode('utf-8') if receipt.created_by_id.partner_id.digital_signature else blank_image),
            'approved_by': receipt.approved_by_id.name or '',
            'ttd_approved_by': (receipt.approved_by_id.partner_id.digital_signature.decode('utf-8') if receipt.approved_by_id.partner_id.digital_signature else blank_image),
            'status_ba': dict(receipt._fields['status_ba'].selection).get(receipt.status_ba, ''),
            'status_ba_color': 'green' if receipt.status_ba == 'conform' else 'red',
            'detail': []
        }
        for line in receipt.move_ids_without_package:
            difference_qty = float(line.difference_quantity or 0)
            if difference_qty.is_integer():
                difference_text = str(int(difference_qty))
            else:
                difference_text = str(difference_qty)

            obj = {
                'name': line.product_id.display_name or '',
                'uom': line.product_uom.name or '',
                'difference_quantity': (
                    "<style forecolor='red'>%s</style>" % difference_text
                    if difference_qty > 0
                    else difference_text
                ),
                'demand': float(line.product_uom_qty or 0),
                'demand1': float(line.confirmation_quantity or 0),
                'qty': float(line.confirmation_quantity or 0),
                'qty2': float(line.product_uom_qty or 0),
                'ket': ''
            }
            data['detail'].append(obj)

        data = json.dumps(data)

        usrnm = 'jasperadmin'
        psswd = 't1j4r1s0lut10n'
        import requests
        execution = requests.post('http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions',
            auth=(usrnm, psswd),
            json={
                'reportUnitUri': '/Odoo/ADITYA/warehouse_pack',
                'outputFormat': 'pdf',
                'parameters': {
                    'reportParameter': [{
                        'name': 'data',
                        'value': [data]
                    }]
                }
            })
        from lxml import etree
        xml = etree.XML(execution.content)

        try:
            request_id = xml.find('.//requestId').text
            export_id = xml.find('.//export//id').text
            result = requests.get('http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions/' + request_id + '/exports/' + export_id + '/outputResource', cookies=execution.cookies, verify=False)
            filename = "warehouse_pack.pdf"
            return request.make_response(result.content, [('Content-Type', result.headers['content-type']), ('Content-Disposition', 'inline; filename=%s;' % filename)])
        except:
            import traceback
            msg = traceback.format_exc()
            request_content = execution.content
            resp = {'msg': msg, 'request_content': str(request_content)}
            resp = json.dumps(resp)
            return request.make_response(resp, [('Content-Type', 'application/json')])

    @http.route('/print/warehouse_transfer', type='http', auth='user')
    def PrintWarehouseTransfer(self, id, **kw):
        blank_image = create_blank_image()
        receipt = request.env['stock.picking'].sudo().browse(int(id))

        data = {
            'type': receipt.adt_type or '',
            'tgl': format_tanggal_indo(receipt.scheduled_date),
            'date': format_tanggal_indo(receipt.create_date.date()) if receipt.create_date else '',
            'no': receipt.origin or '',
            'vendor': receipt.partner_id.name or '',
            'warehouse': receipt.picking_type_id.name or '',
            'warehouse_id': receipt.picking_type_id.warehouse_id.name or '',
            'receipt_date': receipt.scheduled_date.strftime('%Y-%m-%d %H:%M:%S') if receipt.scheduled_date else '',
            'penerima': receipt.location_id.complete_name or '',
            'note': receipt.note_ba or '',
            'user': receipt.user_id.name or '',
            'ttd_user':  (receipt.user_id.digital_signature.decode('utf-8') if receipt.user_id.digital_signature else blank_image),
            'created_by': receipt.created_by_id.name or '',
            'ttd_created_by': (receipt.created_by_id.partner_id.digital_signature.decode('utf-8') if receipt.created_by_id.partner_id.digital_signature else blank_image),
            'approved_by': receipt.approved_by_id.name or '',
            'ttd_approved_by': (receipt.approved_by_id.partner_id.digital_signature.decode('utf-8') if receipt.approved_by_id.partner_id.digital_signature else blank_image),
            'status_ba': dict(receipt._fields['status_ba'].selection).get(receipt.status_ba, ''),
            'status_ba_color': 'green' if receipt.status_ba == 'conform' else 'red',
            'detail': []
        }
        for line in receipt.move_ids_without_package:
            difference_qty = float(line.difference_quantity or 0)
            if difference_qty.is_integer():
                difference_text = str(int(difference_qty))
            else:
                difference_text = str(difference_qty)

            obj = {
                'name': line.product_id.display_name or '',
                'uom': line.product_uom.name or '',
                'difference_quantity': (
                    "<style forecolor='red'>%s</style>" % difference_text
                    if difference_qty > 0
                    else difference_text
                ),
                'demand': float(line.product_uom_qty or 0),
                'demand1': float(line.confirmation_quantity or 0),
                'qty': float(line.confirmation_quantity or 0),
                'qty2': float(line.product_uom_qty or 0),
                'ket': ''
            }
            data['detail'].append(obj)

        data = json.dumps(data)

        usrnm = 'jasperadmin'
        psswd = 't1j4r1s0lut10n'
        import requests
        execution = requests.post('http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions',
            auth=(usrnm, psswd),
            json={
                'reportUnitUri': '/Odoo/ADITYA/warehouse_pack',
                'outputFormat': 'pdf',
                'parameters': {
                    'reportParameter': [{
                        'name': 'data',
                        'value': [data]
                    }]
                }
            })
        from lxml import etree
        xml = etree.XML(execution.content)

        try:
            request_id = xml.find('.//requestId').text
            export_id = xml.find('.//export//id').text
            result = requests.get('http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions/' + request_id + '/exports/' + export_id + '/outputResource', cookies=execution.cookies, verify=False)
            filename = "warehouse_transfer.pdf"
            return request.make_response(result.content, [('Content-Type', result.headers['content-type']), ('Content-Disposition', 'inline; filename=%s;' % filename)])
        except:
            import traceback
            msg = traceback.format_exc()
            request_content = execution.content
            resp = {'msg': msg, 'request_content': str(request_content)}
            resp = json.dumps(resp)
            return request.make_response(resp, [('Content-Type', 'application/json')])

    @http.route('/print/delivery_slip_pack_', type='http', auth='user')
    def PrintDeliverySlipPack(self, id, **kw):
        blank_image = create_blank_image()
        receipt = request.env['stock.picking'].sudo().browse(int(id))

        data = {
            'type': receipt.adt_type or '',
            'tgl': format_tanggal_indo(receipt.scheduled_date),
            'date': format_tanggal_indo(receipt.create_date.date()) if receipt.create_date else '',
            'no': receipt.origin or '',
            'name': receipt.name or '',
            'vendor': receipt.partner_id.name or '',
            'warehouse': receipt.picking_type_id.name or '',
            'warehouse_id': receipt.picking_type_id.warehouse_id.name or '',
            'scheduled_date': receipt.scheduled_date.strftime('%Y-%m-%d %H:%M:%S') if receipt.scheduled_date else '',
            'date_done': format_tanggal_indo(receipt.date_done.date()) if receipt.date_done else '',
            'penerima': receipt.location_id.complete_name or '',
            'note': receipt.note_ba or '',
            'user': receipt.user_id.name or '',
            'ttd_user':  (receipt.user_id.digital_signature.decode('utf-8') if receipt.user_id.digital_signature else blank_image),
            'created_by': receipt.created_by_id.name or '',
            'ttd_created_by': (receipt.created_by_id.partner_id.digital_signature.decode('utf-8') if receipt.created_by_id.partner_id.digital_signature else blank_image),
            'approved_by': receipt.approved_by_id.name or '',
            'ttd_approved_by': (receipt.approved_by_id.partner_id.digital_signature.decode('utf-8') if receipt.approved_by_id.partner_id.digital_signature else blank_image),
            'status_ba': dict(receipt._fields['status_ba'].selection).get(receipt.status_ba, ''),
            'status_ba_color': 'green' if receipt.status_ba == 'conform' else 'red',
            'detail': []
        }
        package_groups = {}

        for move in receipt.move_ids_without_package:
            move_lines = receipt.move_line_ids.filtered( lambda ml: ml.move_id.id == move.id)

            if not move_lines:
                move_lines = [False]

            for line in move_lines:
                package = line.result_package_id if line else False
                package_key = package.id if package else False

                if package_key not in package_groups:
                    package_groups[package_key] = {
                        'group': (
                            package.name if package else 'Products With No Package Assigned'
                        ),
                        'detail_dua': []
                    }

                obj = {
                    'code': move.product_id.default_code or '',
                    'product': move.product_id.name or '',
                    'uom': move.product_uom.name or '',
                    'qty2': float(move.product_uom_qty or 0),
                    'qty': float(line.quantity or 0) if line else 0.0,
                }

                package_groups[package_key]['detail_dua'].append(obj)

        data['detail'] = list(package_groups.values())
        lines = receipt.move_line_ids
        data['total'] = len(lines.mapped('result_package_id'))             # package unik
        data['undpack'] = len(lines.filtered(lambda l: not l.result_package_id))

        data = json.dumps(data)
        usrnm = 'jasperadmin'
        psswd = 't1j4r1s0lut10n'
        import requests
        execution = requests.post('http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions',
            auth=(usrnm, psswd),
            json={
                'reportUnitUri': '/Odoo/ADITYA/Delivery_Slip_Pack_',
                'outputFormat': 'pdf',
                'parameters': {
                    'reportParameter': [{
                        'name': 'data',
                        'value': [data]
                    }]
                }
            })
        from lxml import etree
        xml = etree.XML(execution.content)

        try:
            request_id = xml.find('.//requestId').text
            export_id = xml.find('.//export//id').text
            result = requests.get('http://35.194.200.100:8080/jasperserver/rest_v2/reportExecutions/' + request_id + '/exports/' + export_id + '/outputResource', cookies=execution.cookies, verify=False)
            filename = "Delivery_Slip_Pack.pdf"
            return request.make_response(result.content, [('Content-Type', result.headers['content-type']), ('Content-Disposition', 'inline; filename=%s;' % filename)])
        except:
            import traceback
            msg = traceback.format_exc()
            request_content = execution.content
            resp = {'msg': msg, 'request_content': str(request_content)}
            resp = json.dumps(resp)
            return request.make_response(resp, [('Content-Type', 'application/json')])