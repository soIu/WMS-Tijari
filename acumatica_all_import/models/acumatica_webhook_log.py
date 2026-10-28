import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AcumaticaWebhookLog(models.Model):
    _name = 'acumatica.webhook.log'
    _description = 'Acumatica Webhook Log'
    _order = 'create_date desc, id desc'

    name = fields.Char(string='Name', compute='_compute_name')
    webhook_type = fields.Selection([
        ('master', 'Master'),
        ('transaction', 'Transaksi'),
    ], string='Type', required=True, readonly=True)
    webhook_name = fields.Char(string='Nama Webhook', required=True, readonly=True, index=True)
    company_id_text = fields.Char(string='Company ID', readonly=True)
    acumatica_event_id = fields.Char(string='Acumatica Event ID', readonly=True)
    acumatica_timestamp = fields.Char(string='Acumatica Timestamp', readonly=True)
    payload = fields.Text(string='Payload', required=True, readonly=True)

    state = fields.Selection([
        ('pending', 'Pending'),
        ('processed', 'Processed'),
        ('error', 'Error'),
    ], string='Status', default='pending', readonly=True, index=True)
    created_count = fields.Integer(string='Staging Records', readonly=True)
    updated_count = fields.Integer(string='Updated', readonly=True)
    imported_count = fields.Integer(string='Generated to WMS', readonly=True)
    error_count = fields.Integer(string='WMS Errors', readonly=True)
    line_count = fields.Integer(string='Lines', readonly=True)
    result_message = fields.Text(string='Result Message', readonly=True)
    processed_date = fields.Datetime(string='Processed Date', readonly=True)

    def _compute_name(self):
        for rec in self:
            rec.name = '%s - %s - %s' % (
                dict(self._fields['webhook_type'].selection).get(rec.webhook_type, rec.webhook_type or '-'),
                rec.webhook_name or '-',
                rec.create_date or fields.Datetime.now(),
            )

    @staticmethod
    def _loads_payload(payload_text):
        try:
            return json.loads(payload_text or '{}')
        except json.JSONDecodeError as exc:
            raise UserError(_('Payload JSON tidak valid: %s') % exc)

    @staticmethod
    def _processing_payload(payload):
        return payload

    def _result_records(self, result):
        model_name = result.get('model')
        record_ids = result.get('line_ids') or result.get('header_ids') or []
        if not model_name or not record_ids:
            return self.env['ir.model'].browse()
        try:
            return self.env[model_name].sudo().browse(record_ids).exists()
        except KeyError:
            _logger.exception('Webhook result refers to unknown model %s', model_name)
            return self.env['ir.model'].browse()

    @staticmethod
    def _result_identity(record):
        for field_name in (
            'inventory_id', 'reference_nbr', 'shipment_nbr', 'receipt_nbr',
            'transfer_order_number', 'transfer_nbr', 'customer_id', 'vendor_id',
            'unit_id', 'warehouse_id', 'line_external_id', 'external_id',
        ):
            if field_name in record._fields and record[field_name]:
                value = record[field_name]
                return value.display_name if hasattr(value, 'display_name') else str(value)
        for field_name in ('description', 'customer_name', 'vendor_name', 'name'):
            if field_name in record._fields and record[field_name]:
                return str(record[field_name])
        return 'Record #%s' % record.id

    def _format_result_message(self, result, counts):
        records = self._result_records(result)
        is_shipment_completed = (
            result.get('transaction_type') == 'shipment_completed'
            or result.get('model') == 'acumatica.shipment.completed.staging'
        )
        if records and is_shipment_completed:
            refresh_fields = [
                field_name
                for field_name in ('is_used', 'import_message')
                if field_name in records._fields
            ]
            records.invalidate_recordset(refresh_fields)
        elif records and 'import_state' in records._fields:
            refresh_fields = [
                field_name
                for field_name in ('import_state', 'import_message', 'odoo_product_id')
                if field_name in records._fields
            ]
            records.invalidate_recordset(refresh_fields)
        success_states = {'created', 'updated', 'imported', 'skipped'}
        failed_states = {'error', 'rejected'}
        if records and is_shipment_completed and 'is_used' in records._fields:
            success_records = records.filtered(lambda row: row.is_used)
            failed_records = records - success_records
            pending_records = records.browse()
        elif records and 'import_state' in records._fields:
            success_records = records.filtered(lambda row: row.import_state in success_states)
            failed_records = records.filtered(lambda row: row.import_state in failed_states)
            pending_records = records - success_records - failed_records
        else:
            success_records = records
            failed_records = pending_records = records.browse()

        # The importer can update staging status with SQL. Use its post-import
        # database count as the counter, and read refreshed records for details.
        if is_shipment_completed and 'is_used' in records._fields:
            imported_count = len(success_records)
            effective_error_count = len(failed_records)
        else:
            imported_count = counts['imported']
            effective_error_count = max(
                counts['errors'],
                len(failed_records) + len(pending_records),
            )
        status = 'GAGAL' if effective_error_count else 'BERHASIL'
        label = result.get('label') or result.get('transaction_type') or self.webhook_name
        lines = [
            'Hasil webhook %s: %s' % (label, status),
            'Record staging: %s baru, %s diperbarui' % (counts['created'], counts['updated']),
            'Berhasil digenerate ke WMS: %s' % imported_count,
            'Gagal atau belum selesai: %s' % effective_error_count,
        ]

        detail_lines = []
        for record in records:
            state = record.import_state if 'import_state' in record._fields else False
            if is_shipment_completed and 'is_used' in record._fields:
                state_label = 'BERHASIL' if record.is_used else 'GAGAL'
            elif state in success_states:
                state_label = 'BERHASIL' if state != 'skipped' else 'DILEWATI'
            elif state in failed_states:
                state_label = 'GAGAL' if state == 'error' else 'DITOLAK'
            else:
                state_label = 'BELUM SELESAI'
            message = record.import_message if 'import_message' in record._fields else False
            if not message:
                if is_shipment_completed and 'is_used' in record._fields:
                    message = (
                        'Shipment Completed berhasil diterapkan ke WMS.'
                        if record.is_used
                        else 'Dokumen %s tidak ada di WMS atau belum berhasil diproses.' % self._result_identity(record)
                    )
                else:
                    message = {
                        'staged': 'Record masih berstatus Staged; belum berhasil digenerate ke WMS.',
                        'error': 'Import gagal; tidak ada pesan error pada record staging.',
                        'rejected': 'Record ditolak saat proses import.',
                        'created': 'Data berhasil dibuat.',
                        'updated': 'Data berhasil diperbarui.',
                    }.get(state, 'Tidak ada pesan proses pada record ini.')
            detail_lines.append('- [%s] %s: %s' % (
                state_label,
                self._result_identity(record),
                str(message).replace('\n', ' ').strip(),
            ))

        if not detail_lines:
            for item in result.get('logs', []):
                detail_lines.append('- [%s] %s: %s' % (
                    (item.get('status') or 'info').upper(),
                    item.get('reference_nbr') or item.get('document') or 'Record',
                    item.get('message') or 'Tidak ada pesan proses.',
                ))

        max_details = 50
        if detail_lines:
            lines.extend(['', 'Rincian per record:'])
            lines.extend(detail_lines[:max_details])
            if len(detail_lines) > max_details:
                lines.append('... %s record lainnya tidak ditampilkan.' % (len(detail_lines) - max_details))
        elif effective_error_count:
            lines.extend(['', 'Pesan: Proses melaporkan error, tetapi tidak menyediakan rincian record.'])

        return '\n'.join(lines), imported_count, effective_error_count

    def action_process_to_staging(self):
        for rec in self:
            payload = rec._loads_payload(rec.payload)
            processing_payload = rec._processing_payload(payload)
            try:
                if rec.webhook_type == 'master':
                    from ..controllers.master_webhook import AcumaticaMasterWebhookController
                    controller = AcumaticaMasterWebhookController()
                    controller.env = self.env
                    result = controller._process_master_payload(rec.webhook_name, processing_payload)
                elif rec.webhook_type == 'transaction':
                    from ..controllers.transaction_webhook import AcumaticaTransactionWebhookController
                    controller = AcumaticaTransactionWebhookController()
                    controller.env = self.env
                    result = controller._process_transaction_payload(rec.webhook_name, processing_payload)
                else:
                    raise UserError(_('Webhook type tidak valid: %s') % rec.webhook_type)

                counts = {
                    'created': result.get('created', 0),
                    'updated': result.get('updated', 0),
                    'imported': result.get('imported', 0),
                    'errors': result.get('errors', 0),
                }
                message, imported_count, error_count = rec._format_result_message(result, counts)
                rec.write({
                    'state': 'error' if error_count else 'processed',
                    'created_count': counts['created'] + counts['updated'],
                    'updated_count': counts['updated'],
                    'imported_count': imported_count,
                    'error_count': error_count,
                    'line_count': result.get('product_lines') or result.get('component_lines') or len(result.get('line_ids', [])),
                    'result_message': message,
                    'processed_date': fields.Datetime.now(),
                })
            except Exception as exc:
                _logger.exception('Error processing Acumatica webhook log %s', rec.id)
                rec.write({
                    'state': 'error',
                    'error_count': rec.error_count + 1,
                    'result_message': 'Hasil webhook %s: GAGAL\nPesan error: %s' % (rec.webhook_name, str(exc)),
                    'processed_date': fields.Datetime.now(),
                })
                raise

        return True

    @api.model
    def cron_process_pending_webhook_logs(self, limit=100):
        """Process pending webhook logs in small, isolated batches."""
        self.env.cr.execute("""
            SELECT id
              FROM acumatica_webhook_log
             WHERE state = 'pending'
             ORDER BY create_date, id
             LIMIT %s
             FOR UPDATE SKIP LOCKED
        """, [limit])
        records = self.sudo().browse([row[0] for row in self.env.cr.fetchall()])
        error_count = 0

        for record in records:
            try:
                with self.env.cr.savepoint():
                    record.action_process_to_staging()
            except Exception as exc:
                error_count += 1
                record.invalidate_recordset()
                _logger.exception('Scheduled processing failed for Acumatica webhook log %s', record.id)
                record.sudo().write({
                    'state': 'error',
                    'error_count': record.error_count + 1,
                    'result_message': 'Hasil webhook %s: GAGAL\nPesan error: %s' % (
                        record.webhook_name,
                        str(exc),
                    ),
                    'processed_date': fields.Datetime.now(),
                })

        return {
            'webhook_logs': {
                'success': not error_count,
                'data_retrieved': len(records),
                'records_errors': error_count,
                'message': '%s webhook diproses; %s gagal.' % (len(records), error_count),
            },
        }
