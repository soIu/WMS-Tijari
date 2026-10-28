import logging
import time

from odoo import api, fields, models
from odoo.addons.base.models.res_partner import _tz_get
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class ApiSchedulerJob(models.Model):
    _name = 'api.scheduler.job'
    _description = 'API Scheduler'
    _order = 'name'

    name = fields.Char(string='Job Name', required=True)
    code = fields.Char(string='Code / Technical Name', required=True)
    description = fields.Text()
    active = fields.Boolean(default=True)
    acumatica_import = fields.Boolean(string='Acumatica Import')
    acumatica_data_type = fields.Selection(
        [
            ('master', 'Data Master'),
            ('transaction', 'Data Transaksi'),
        ],
        string='Acumatica Data',
    )
    process_phase = fields.Selection(
        [
            ('combined', 'Fetch and Import'),
            ('fetch', 'Fetch Only'),
            ('import', 'Import Only'),
        ],
        string='Process Phase',
        default='combined',
        required=True,
        help='Controls whether this scheduler fetches staging data, imports it, or runs the legacy combined flow.',
    )

    execution_interval = fields.Selection(
        [
            ('recurring', 'Recurring'),
            ('manual', 'Manual'),
        ],
        string='Execution Interval',
        default='recurring',
        required=True,
    )
    interval_value = fields.Integer(string='Interval Value', default=1)
    interval_unit = fields.Selection(
        [
            ('minutes', 'Minutes'),
            ('hours', 'Hours'),
            ('days', 'Days'),
            ('weeks', 'Weeks'),
            ('months', 'Months'),
        ],
        string='Interval Unit',
        default='days',
        required=True,
    )
    next_execution = fields.Datetime(string='Next Execution', default=fields.Datetime.now)
    timezone = fields.Selection(_tz_get, string='Timezone', default=lambda self: self.env.user.tz or 'UTC')
    do_not_overlap = fields.Boolean(string='Do Not Overlap', default=True)
    is_running = fields.Boolean(readonly=True, copy=False)

    ir_cron_id = fields.Many2one('ir.cron', string='Odoo Cron', readonly=True, copy=False)
    log_ids = fields.One2many('api.scheduler.log', 'scheduler_job_id', string='Log Activity', readonly=True)

    _sql_constraints = [
        ('code_unique', 'unique(code)', 'Code / Technical Name must be unique.'),
        ('interval_value_positive', 'CHECK(interval_value > 0)', 'Interval Value must be greater than zero.'),
    ]

    @api.constrains('acumatica_import', 'acumatica_data_type')
    def _check_acumatica_data_type(self):
        for job in self:
            if job.acumatica_import and not job.acumatica_data_type:
                raise ValidationError('Acumatica Data is required when Acumatica Import is enabled.')

    def action_run_now(self):
        for job in self:
            job.run_from_cron()
        return True

    def action_open_ir_cron(self):
        self.ensure_one()
        if not self.ir_cron_id:
            raise UserError('No Odoo Cron is linked to this API Scheduler.')
        return {
            'type': 'ir.actions.act_window',
            'name': 'Odoo Cron',
            'res_model': 'ir.cron',
            'view_mode': 'form',
            'res_id': self.ir_cron_id.id,
            'target': 'current',
        }

    def run_from_cron(self):
        for job in self:
            job._run_single_job()
        return True

    def _run_single_job(self):
        self.ensure_one()
        if not self.active:
            return False
        if self.do_not_overlap and self.is_running:
            self._create_log(
                status='failed',
                execution_time=fields.Datetime.now(),
                duration=0.0,
                details=[],
                message='Skipped because the previous execution is still running.',
            )
            return False

        execution_time = fields.Datetime.now()
        started = time.monotonic()
        before = self._snapshot_stats()
        status = 'success'
        message = False
        process_result = {}
        activity_log = False

        try:
            self.write({'is_running': True})
            activity_log = self._create_log(
                status='running',
                execution_time=execution_time,
                duration=0.0,
                details=[],
                message='Job sedang berjalan.',
            )
            self.env.cr.commit()
            process_result = self.with_context(
                api_scheduler_log_id=activity_log.id,
            )._run_configured_process() or {}
            message = self._build_process_message(process_result)
            if not self._is_process_result_success(process_result):
                status = 'failed'
        except Exception as exc:
            status = 'failed'
            message = str(exc)
            _logger.exception("API Scheduler %s failed.", self.code)
        finally:
            after = self._snapshot_stats()
            details = self._build_detail_values(before, after, process_result)
            duration = time.monotonic() - started
            if activity_log:
                activity_log.write({
                    'status': status,
                    'duration': duration,
                    'message': message,
                })
                self._replace_log_details(activity_log, details)
            else:
                activity_log = self._create_log(
                    status=status,
                    execution_time=execution_time,
                    duration=duration,
                    details=details,
                    message=message,
                )
            self.write({'is_running': False})
            if (
                self.process_phase == 'fetch'
                and self.acumatica_data_type in ('master', 'transaction')
                and self.code in ('acumatica_master_fetch', 'acumatica_transaction_fetch')
            ):
                successful_types = [
                    key for key, result in (process_result or {}).items()
                    if isinstance(result, dict)
                    and result.get('success', False)
                    and result.get('data_retrieved', 0) > 0
                ]
                if successful_types:
                    self._trigger_import(successful_types)
        return status == 'success'

    def _replace_log_details(self, log, details):
        """Replace detail rows and refresh aggregate counters on a live log."""
        log.detail_ids.unlink()
        log.write({
            'data_retrieved': sum(item.get('data_retrieved', 0) for item in details),
            'staging_created': sum(item.get('staging_created', 0) for item in details),
            'records_created': sum(item.get('records_created', 0) for item in details),
            'records_errors': sum(item.get('records_errors', 0) for item in details),
            'records_skipped': sum(item.get('records_skipped', 0) for item in details),
            'detail_ids': [(0, 0, item) for item in details],
        })

    def _trigger_import(self, successful_types):
        """Start import for data types whose fetch phase completed successfully."""
        import_code = (
            'acumatica_master_import'
            if self.acumatica_data_type == 'master'
            else 'acumatica_transaction_import'
        )
        import_job = self.search([
            ('code', '=', import_code),
            ('active', '=', True),
        ], limit=1)
        if not import_job:
            _logger.warning(
                'Fetch succeeded, but no active %s job was found.', import_code,
            )
            return False

        # Persist the successful fetch log and staging rows before import starts.
        # This keeps an import failure from rolling back the completed fetch phase.
        self.env.cr.commit()
        _logger.info('Master fetch succeeded; triggering Master import job.')
        context_key = (
            'acumatica_successful_master_types'
            if self.acumatica_data_type == 'master'
            else 'acumatica_successful_transaction_types'
        )
        return import_job.with_context(**{context_key: successful_types}).run_from_cron()

    def _run_configured_process(self):
        self.ensure_one()
        if self.code == 'acumatica_webhook_process':
            return self.env['acumatica.webhook.log'].cron_process_pending_webhook_logs()
        if not self.acumatica_import:
            raise UserError('No process is configured for this scheduler.')
        if self.acumatica_data_type == 'master':
            return self.env['acumatica.config'].with_context(
                acumatica_process_phase=self.process_phase,
                acumatica_scheduler_code=self.code,
                api_scheduler_log_id=self.env.context.get('api_scheduler_log_id'),
            )._cron_import_master_data()
        if self.acumatica_data_type == 'transaction':
            return self.env['acumatica.config'].with_context(
                acumatica_process_phase=self.process_phase,
                acumatica_scheduler_code=self.code,
                api_scheduler_log_id=self.env.context.get('api_scheduler_log_id'),
            )._cron_import_transaction_data()
        raise UserError('Acumatica Data is not configured.')

    def _is_process_result_success(self, process_result):
        if not isinstance(process_result, dict):
            return bool(process_result)
        for result in process_result.values():
            if isinstance(result, dict):
                if not result.get('success', True):
                    return False
            elif not result:
                return False
        return True

    def _build_process_message(self, process_result):
        if not isinstance(process_result, dict):
            return False
        messages = []
        for acumatica_data, result in process_result.items():
            if isinstance(result, dict) and result.get('message') and (
                result.get('records_errors') or not result.get('success', True)
            ):
                prefix = 'Skipped with error' if result.get('success', True) else 'Failed'
                messages.append('%s: %s - %s' % (acumatica_data, prefix, result.get('message')))
            elif result is False:
                messages.append('%s: Failed' % acumatica_data)
        return '\n'.join(messages) if messages else False

    def _get_acumatica_data_definitions(self):
        master_defs = [
            ('customer', 'Customer', 'acumatica.customer.line'),
            ('vendor', 'Vendor', 'acumatica.vendor.line'),
            ('uom', 'Unit of Measure', 'acumatica.uom.line'),
            ('warehouse', 'Warehouse', 'acumatica.warehouse.line'),
            ('stock_item', 'Stock Item', 'acumatica.stock.item.line'),
            ('non_stock_item', 'Non Stock Item', 'acumatica.non.stock.item.line'),
        ]
        if self.acumatica_data_type == 'master':
            return master_defs
        if self.acumatica_data_type == 'transaction':
            return [
                ('po_receipt', 'PO Receipt', 'acumatica.receipt.line'),
                ('so_transfer', 'Transfer (In/SOTR)', 'acumatica.transfer.line'),
                ('inventory_received', 'Receipt : Inventory Receipt', 'acumatica.inventory.recieved.line'),
                ('shipment_so', 'SO Return', 'acumatica.shipment.line'),
                ('so_shipment', 'SO Shipment', 'acumatica.shipment.gi.staging'),
                ('transfer_shipment', 'Transfer (Out/Shipment)', 'acumatica.transfer.shipment.line'),
                ('inventory_issue', 'Issue : Inventory Issue', 'acumatica.inventory.issue.line'),
                ('issue_po_return', 'PO Return', 'acumatica.issue.po.return.line'),
                ('transfer_one_step', 'Transfer 1 Step', 'acumatica.transfer.one.step.line'),
                ('transfer_two_step', 'Transfer 2 Step', 'acumatica.transfer.two.step.line'),
                ('shipment_completed', 'Update SO completed', 'acumatica.shipment.completed.staging'),
                ('kit_assembly', 'Manufacturing Order', 'acumatica.kit.assembly'),
                ('invoice_consignment', 'Invoice Consignment', 'acumatica.invoice.consignment.staging'),
                ('credit_memo_consignment', 'Credit Memo Consignment', 'acumatica.credit.memo.consignment.staging'),
            ]
        return []

    def _snapshot_stats(self):
        stats = {}
        for acumatica_data, label, model_name in self._get_acumatica_data_definitions():
            Model = self.env[model_name].sudo()
            stats[acumatica_data] = {
                'label': label,
                'total': Model.search_count([]),
                'created': Model.search_count([('import_state', '=', 'created')]),
                'updated': Model.search_count([('import_state', '=', 'updated')]),
                'error': Model.search_count([('import_state', '=', 'error')]),
            }
        return stats

    def _get_process_detail(self, process_result, acumatica_data):
        if isinstance(process_result, dict):
            result = process_result.get(acumatica_data)
            if isinstance(result, dict):
                return result
        return {}

    def _build_detail_values(self, before, after, process_result=False):
        details = []
        for acumatica_data, values in after.items():
            previous = before.get(acumatica_data, {})
            result = self._get_process_detail(process_result, acumatica_data)
            records_created = (
                values.get('created', 0)
                + values.get('updated', 0)
                - previous.get('created', 0)
                - previous.get('updated', 0)
            )
            staging_created = values.get('total', 0) - previous.get('total', 0)
            data_retrieved = result.get('data_retrieved', staging_created)
            records_created = result.get('records_created', records_created)
            records_errors = result.get('records_errors', values.get('error', 0) - previous.get('error', 0))
            if result and not result.get('success', True) and not records_errors:
                records_errors = 1
            records_skipped = result.get(
                'records_skipped',
                data_retrieved - records_created - records_errors,
            )
            details.append({
                'data_type': self.acumatica_data_type,
                'acumatica_data': acumatica_data,
                'data_retrieved': max(data_retrieved, 0),
                'staging_created': max(staging_created, 0),
                'records_created': max(records_created, 0),
                'records_errors': max(records_errors, 0),
                'records_skipped': max(records_skipped, 0),
            })
        return details

    def _create_log(self, status, execution_time, duration, details, message=False):
        log_values = {
            'scheduler_job_id': self.id,
            'execution_time': execution_time,
            'duration': duration,
            'data_retrieved': sum(item.get('data_retrieved', 0) for item in details),
            'staging_created': sum(item.get('staging_created', 0) for item in details),
            'records_created': sum(item.get('records_created', 0) for item in details),
            'records_errors': sum(item.get('records_errors', 0) for item in details),
            'records_skipped': sum(item.get('records_skipped', 0) for item in details),
            'status': status,
            'message': message,
            'detail_ids': [(0, 0, item) for item in details],
        }
        return self.env['api.scheduler.log'].sudo().create(log_values)


class ApiSchedulerLog(models.Model):
    _name = 'api.scheduler.log'
    _description = 'API Scheduler Log'
    _order = 'execution_time desc, id desc'

    scheduler_job_id = fields.Many2one('api.scheduler.job', string='Scheduler Job', required=True, ondelete='cascade')
    execution_time = fields.Datetime(string='Execution Time', readonly=True)
    duration = fields.Float(readonly=True)
    data_retrieved = fields.Integer(string='Data Retrieved', readonly=True)
    staging_created = fields.Integer(string='Staging Created', readonly=True)
    records_created = fields.Integer(string='Records Created', readonly=True)
    records_errors = fields.Integer(string='Records Errors', readonly=True)
    records_skipped = fields.Integer(string='Records Skipped', readonly=True)
    status = fields.Selection(
        [
            ('running', 'Running'),
            ('success', 'Success'),
            ('failed', 'Failed'),
        ],
        readonly=True,
    )
    message = fields.Text(readonly=True)
    detail_ids = fields.One2many('api.scheduler.log.detail', 'scheduler_log_id', string='Details', readonly=True)

    def action_view_details(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Scheduler Log Details',
            'res_model': 'api.scheduler.log.detail',
            'view_mode': 'list',
            'domain': [('scheduler_log_id', '=', self.id)],
            'context': {
                'create': False,
                'edit': False,
                'delete': False,
            },
        }


class ApiSchedulerLogDetail(models.Model):
    _name = 'api.scheduler.log.detail'
    _description = 'API Scheduler Log Detail'
    _order = 'scheduler_log_id desc, acumatica_data'

    scheduler_log_id = fields.Many2one('api.scheduler.log', string='Scheduler Log', required=True, ondelete='cascade')
    scheduler_job_id = fields.Many2one(related='scheduler_log_id.scheduler_job_id', store=True, string='Scheduler Job')
    data_type = fields.Selection(
        [
            ('master', 'Master'),
            ('transaction', 'Transaksi'),
        ],
        readonly=True,
    )
    acumatica_data = fields.Selection(
        [
            ('customer', 'Customer'),
            ('vendor', 'Vendor'),
            ('uom', 'Unit of Measure'),
            ('warehouse', 'Warehouse'),
            ('stock_item', 'Stock Item'),
            ('non_stock_item', 'Non Stock Item'),
            ('po_receipt', 'PO Receipt'),
            ('so_transfer', 'Transfer (In/SOTR)'),
            ('inventory_received', 'Receipt : Inventory Receipt'),
            ('shipment_so', 'SO Return'),
                ('so_shipment', 'SO Shipment'),
                ('transfer_shipment', 'Transfer (Out/Shipment)'),
                ('inventory_issue', 'Issue : Inventory Issue'),
                ('issue_po_return', 'PO Return'),
                ('transfer_one_step', 'Transfer 1 Step'),
                ('transfer_two_step', 'Transfer 2 Step'),
                ('shipment_completed', 'Update SO completed'),
                ('kit_assembly', 'Manufacturing Order'),
                ('invoice_consignment', 'Invoice Consignment'),
                ('credit_memo_consignment', 'Credit Memo Consignment'),
            ],
        string='Acumatica Data',
        readonly=True,
    )
    data_retrieved = fields.Integer(string='Data Retrieved', readonly=True)
    staging_created = fields.Integer(string='Staging Created', readonly=True)
    records_created = fields.Integer(string='Records Created', readonly=True)
    records_errors = fields.Integer(string='Records Errors', readonly=True)
    records_skipped = fields.Integer(string='Records Skipped', readonly=True)
