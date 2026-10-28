/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { onMounted } from "@odoo/owl";
import { BooleanField } from "@web/views/fields/boolean/boolean_field";

class PickerSortBooleanField extends BooleanField {
    setup() {
        super.setup();
        // A saved sort option is already checked when the form opens, so
        // onChange will not run. Only one of the two responsive checkbox
        // widgets should initialize the shared x2many sort.
        onMounted(() => {
            if (this.props.name === "picker_sort_by_location") {
                this.sortPickerLines();
            }
        });
    }

    async onChange(newValue) {
        this.state.value = newValue;
        await this.props.record.update({ [this.props.name]: newValue });
        await this.sortPickerLines();
    }

    async sortPickerLines() {
        const data = this.props.record.data;
        const sortField = data.picker_sort_by_location
            ? "picker_location_sort_key"
            : data.picker_sort_by_product
              ? "picker_product_sort_key"
              : "picker_default_sort_key";
        const moveLines = data.picker_move_line_ids;
        if (moveLines?.sortBy) {
            const currentOrder = moveLines.orderBy?.[0];
            if (currentOrder?.name === sortField && currentOrder.asc) {
                return;
            }
            await moveLines.sortBy(sortField);
        }
    }
}

registry.category("fields").add("adt_picker_sort_boolean", {
    component: PickerSortBooleanField,
    displayName: _t("Picker Sort Checkbox"),
    supportedTypes: ["boolean"],
    isEmpty: () => false,
});
